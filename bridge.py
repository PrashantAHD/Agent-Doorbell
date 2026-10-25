"""Agent Doorbell: answer your VS Code Copilot agents from your phone (Telegram or WhatsApp).

Runs locally. Copilot hooks (hook.py) POST events here. VS Code always shows its own prompt;
anything still unanswered after a minute is also sent to your phone, and whichever side
answers first wins.

Usage:
    python bridge.py                 run the bridge
    python bridge.py supervise       run the bridge and restart it if it crashes or hangs
    python bridge.py setup-telegram  pair a Telegram bot (recommended, free)
    python bridge.py setup           Twilio WhatsApp setup
    python bridge.py test-send       send a test message to your phone
"""

from __future__ import annotations

import base64
import ctypes
import fnmatch
import getpass
import html
import itertools
import json
import logging
import logging.handlers
import os
import re
import secrets
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("HITL_BRIDGE_CONFIG", HERE / "config.json"))
LOG_PATH = HERE / "bridge.log"
COPILOT_HOME = Path(os.environ.get("COPILOT_HOME", Path.home() / ".copilot"))
SESSION_STATE = COPILOT_HOME / "session-state"
WA_MAX = 1550  # Twilio WhatsApp body limit is 1600 chars
EXT_VERSION = "9"  # must match X-Ext-Version sent by vscode-extension/extension.js

DEFAULT_CONFIG: dict[str, Any] = {
    # "auto" uses Telegram when paired, otherwise Twilio WhatsApp when configured.
    "provider": "auto",
    "telegram": {
        "bot_token": "",
        "chat_id": "",
    },
    "twilio": {
        "account_sid": "",
        "auth_token": "",
        "from": "whatsapp:+14155238886",
        "to": "",
    },
    "port": 47615,  # unusual on purpose: 8765/8000/8080 are common dev-server ports
    "token": "",
    "approval_wait_minutes": 60,
    "question_wait_minutes": 60,
    "followup_wait_minutes": 10,
    # VS Code always shows its own prompts. Anything still unanswered after this many seconds is
    # also sent to the phone, and stays open in both places until you answer in either one.
    "push_after_seconds": 60,
    # Legacy (hold_in_hook / laptop_popup modes only): seconds without keyboard/mouse input = "away".
    "away_after_seconds": 60,
    # Phone requests stay open this long at most (default one week).
    "max_wait_hours": 168,
    # Stop Windows from sleeping because of inactivity while the bridge runs (the screen can still
    # turn off and lock). Closing the lid or choosing Sleep still sleeps the laptop.
    "keep_awake": True,
    # Close the helper VS Code window the bridge opened after this many minutes without work
    # (it's reopened automatically when a phone answer needs it).
    "helper_idle_minutes": 10,
    # Hold a finished turn open (via the agentStop hook) for up to this many minutes so a phone
    # reply can continue it reliably. 0 = don't hold; phone replies are delivered best-effort.
    "turn_hold_minutes": 0,
    # Turn agent questions into normal chat messages, so answers can come back reliably through
    # the held turn (from VS Code chat or the phone). False = VS Code's question form.
    "questions_via_chat": False,
    # Legacy: hold requests in the hook (VS Code shows nothing until the phone answers or you return).
    "hold_in_hook": False,
    # Opt-in alternative: always show a laptop popup first and escalate it to
    # the phone after escalate_after_seconds (instead of idle detection).
    "laptop_popup": False,
    "escalate_after_seconds": 60,
    "notify_turn_end": True,
    "poll_seconds_active": 2,
    "poll_seconds_idle": 15,
    # Tools VS Code approves silently; never routed to the phone.
    "passthrough_tools": [
        "ask_user",
        "rename_chat",
        "add_artifact_or_reference",
        "remove_artifact_or_reference",
        "list_artifacts_and_references",
        "get_current_session",
        "report_intent",
    ],
    # File writes inside the session folder are auto-approved by VS Code
    # except for these (mirrors chat.tools.edits.autoApprove defaults).
    "sensitive_write_globs": [
        "**/.*",
        "**/.vscode/**",
        "**/.git/**",
        "**/.github/**",
        "**/package.json",
        "**/package-lock.json",
        "**/*.code-workspace",
        "**/*.csproj",
        "**/web.config",
    ],
}

log = logging.getLogger("hitl")


def _ssl_context() -> ssl.SSLContext:
    # truststore verifies via the Windows certificate store, so corporate
    # TLS-inspection roots trusted by Windows are trusted here too.
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        return ssl.create_default_context()


SSL_CTX = _ssl_context()


class _LastInputInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def idle_seconds() -> float:
    """Seconds since the last keyboard/mouse input on this Windows session."""
    if os.name != "nt":
        return 0.0
    info = _LastInputInfo()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    ctypes.windll.kernel32.GetTickCount.restype = ctypes.c_uint
    return ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load_config(path: Path = CONFIG_PATH) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if path.exists():
        cfg = _merge(DEFAULT_CONFIG, json.loads(path.read_text(encoding="utf-8")))
    tw = cfg["twilio"]
    tw["account_sid"] = os.environ.get("TWILIO_ACCOUNT_SID", tw["account_sid"])
    tw["auth_token"] = os.environ.get("TWILIO_AUTH_TOKEN", tw["auth_token"])
    cfg["telegram"]["bot_token"] = os.environ.get("TELEGRAM_BOT_TOKEN", cfg["telegram"]["bot_token"])
    if not cfg.get("token"):
        cfg["token"] = secrets.token_urlsafe(24)
        save_config(cfg, path)
    return cfg


def save_config(cfg: dict, path: Path = CONFIG_PATH) -> None:
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _wa(number: str) -> str:
    number = number.strip().replace(" ", "")
    return number if number.startswith("whatsapp:") else f"whatsapp:{number}"


# ----------------------------------------------------------------------------
# Phone transports. All are polled - no public webhook needed.
# ----------------------------------------------------------------------------

Buttons = list[list[tuple[str, str]]]  # rows of (label, reply text)


class Transport:
    """Base / dry-run transport: records outbound messages, inbound injected."""

    name = "dry-run"
    supports_buttons = False
    long_poll = False

    def __init__(self) -> None:
        self.outbox: list[str] = []
        self.finals: list[tuple[Any, str]] = []
        self.last_error: str = ""

    @property
    def configured(self) -> bool:
        return False

    def send(self, body: str, buttons: Buttons | None = None) -> Any:
        """Send a message; returns a truthy message reference on success."""
        body = to_plain(body)
        self.outbox.append(body)
        log.info("[dry-run] %s", body.replace("\n", " | ")[:300])
        return len(self.outbox)

    def finalize(self, ref: Any, label: str) -> None:
        """Mark a request message as resolved (e.g. replace its buttons)."""
        self.finals.append((ref, label))

    def poll(self) -> list[str]:
        return []


# Layout markers used in message bodies (private-use characters, stripped from all untrusted text):
# SUB = small italic subtitle line, MORE = collapsed "tap to expand" section.
SUB_OPEN, SUB_CLOSE, MORE_OPEN, MORE_CLOSE = "\ue001", "\ue002", "\ue003", "\ue004"
_MARKS = re.compile("[\ue001-\ue004]")


def _unmark(s: str) -> str:
    return _MARKS.sub("", s or "")


def to_plain(s: str) -> str:
    """Message body for channels without rich formatting (WhatsApp, logs): subtitle in _italics_,
    collapsed sections as > quotes."""
    s = re.sub(f"{SUB_OPEN}(.*?){SUB_CLOSE}", lambda m: f"_{m.group(1)}_", s, flags=re.S)
    s = re.sub(f"{MORE_OPEN}(.*?){MORE_CLOSE}",
               lambda m: "\n".join("> " + line for line in m.group(1).split("\n")), s, flags=re.S)
    return _unmark(s)


def to_telegram_html(s: str) -> str:
    """Convert the message markup (*bold*, `code`, ```pre```, subtitle, collapsed section) to Telegram HTML."""
    out = []
    for part in re.split(r"(```.*?```|`[^`\n]+`)", s, flags=re.S):
        if len(part) >= 6 and part.startswith("```") and part.endswith("```"):
            out.append("<pre>" + html.escape(_unmark(part[3:-3]).strip("\n"), quote=False) + "</pre>")
        elif len(part) >= 2 and part.startswith("`") and part.endswith("`"):
            out.append("<code>" + html.escape(_unmark(part[1:-1]), quote=False) + "</code>")
        else:
            t = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<b>\1</b>", html.escape(part, quote=False))
            out.append(t.replace(SUB_OPEN, "<i>").replace(SUB_CLOSE, "</i>")
                        .replace(MORE_OPEN, "<blockquote expandable>").replace(MORE_CLOSE, "</blockquote>"))
    return "".join(out)


class TelegramTransport(Transport):
    name = "telegram"
    supports_buttons = True
    long_poll = True
    API = "https://api.telegram.org/bot{token}/{method}"

    def __init__(self, token: str, chat_id: str | int) -> None:
        super().__init__()
        self.token, self.chat_id = token, str(chat_id)
        self.offset: int | None = None

    @property
    def configured(self) -> bool:
        return True

    def call(self, method: str, params: dict | None = None, timeout: float = 20) -> Any:
        req = urllib.request.Request(self.API.format(token=self.token, method=method),
                                     data=json.dumps(params or {}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
                res = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            res = json.loads(e.read().decode(errors="replace") or "{}")
        if not res.get("ok"):
            raise RuntimeError(f"Telegram {method}: {res.get('description', 'request failed')}")
        return res["result"]

    def send(self, body: str, buttons: Buttons | None = None) -> Any:
        if len(body) > 3800:
            body = body[:3799] + "…"
        params: dict[str, Any] = {"chat_id": self.chat_id, "disable_web_page_preview": True}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": [
                [{"text": label, "callback_data": data[:64]} for label, data in row] for row in buttons]}
        try:
            try:
                msg = self.call("sendMessage", {**params, "text": to_telegram_html(body), "parse_mode": "HTML"})
            except RuntimeError as e:
                if "parse" not in str(e).lower():
                    raise
                msg = self.call("sendMessage", {**params, "text": to_plain(body)})  # markup problem: send plain
            self.last_error = ""
            return msg["message_id"]
        except Exception as e:
            self.last_error = str(e)[:300]
            log.error("Telegram send failed: %s", self.last_error)
            return None

    def finalize(self, ref: Any, label: str) -> None:
        if not ref:
            return
        try:
            self.call("editMessageReplyMarkup", {"chat_id": self.chat_id, "message_id": ref, "reply_markup": {
                "inline_keyboard": [[{"text": label, "callback_data": "noop"}]]}})
        except Exception as e:
            log.debug("finalize failed: %s", e)

    def poll(self) -> list[Any]:
        """New messages (str) and button taps (dict with data + message_id) from the paired chat."""
        try:
            if self.offset is None:  # skip anything sent while the bridge was down
                last = self.call("getUpdates", {"offset": -1, "timeout": 0})
                self.offset = last[-1]["update_id"] + 1 if last else 0
                return []
            updates = self.call("getUpdates", {"offset": self.offset, "timeout": 25,
                                               "allowed_updates": ["message", "callback_query"]}, timeout=40)
        except Exception as e:
            log.warning("Telegram poll failed: %s", e)
            time.sleep(5)
            return []
        out: list[Any] = []
        for u in updates:
            self.offset = u["update_id"] + 1
            if "message" in u:
                m = u["message"]
                if str(m.get("chat", {}).get("id")) == self.chat_id and m.get("text"):
                    out.append(m["text"])
            elif "callback_query" in u:
                cq = u["callback_query"]
                msg = cq.get("message") or {}
                if str((msg.get("chat") or {}).get("id")) != self.chat_id:
                    continue
                try:
                    self.call("answerCallbackQuery", {"callback_query_id": cq["id"]})
                except Exception:
                    pass
                if cq.get("data") and cq["data"] != "noop":
                    out.append({"data": cq["data"], "message_id": msg.get("message_id")})
        return out


class TwilioTransport(Transport):
    name = "whatsapp"

    API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"

    def __init__(self, sid: str, token: str, from_: str, to: str) -> None:
        super().__init__()
        self.sid, self.from_, self.to = sid, _wa(from_), _wa(to)
        self._auth = "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode()
        self._seen: set[str] = set()
        self._primed = False

    @property
    def configured(self) -> bool:
        return True

    def _req(self, method: str, url: str, data: dict | None = None) -> dict:
        body = urllib.parse.urlencode(data).encode() if data else None
        req = urllib.request.Request(url, data=body, method=method, headers={"Authorization": self._auth})
        with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as r:
            return json.loads(r.read().decode())

    def send(self, body: str, buttons: Buttons | None = None) -> Any:
        body = to_plain(body)
        if len(body) > WA_MAX:
            body = body[: WA_MAX - 1] + "…"
        try:
            self._req("POST", self.API.format(sid=self.sid), {"From": self.from_, "To": self.to, "Body": body})
            self.last_error = ""
            return True
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            self.last_error = f"Twilio {e.code}: {detail[:300]}"
        except Exception as e:  # network errors
            self.last_error = f"Twilio send failed: {e}"
        log.error(self.last_error)
        return False

    def poll(self) -> list[str]:
        q = urllib.parse.urlencode({"From": self.to, "To": self.from_, "PageSize": 20})
        try:
            data = self._req("GET", self.API.format(sid=self.sid) + "?" + q)
        except Exception as e:
            log.warning("Twilio poll failed: %s", e)
            return []
        msgs = [m for m in data.get("messages", []) if str(m.get("direction", "")).startswith("inbound")]
        new = [m for m in msgs if m["sid"] not in self._seen]
        self._seen.update(m["sid"] for m in msgs)
        if not self._primed:  # ignore history from before the bridge started
            self._primed = True
            return []
        new.sort(key=lambda m: m.get("date_created") or "")
        return [m.get("body") or "" for m in new]


# ----------------------------------------------------------------------------
# Session helpers
# ----------------------------------------------------------------------------

def _read_tail(path: Path, max_bytes: int = 512_000) -> list[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = data.splitlines()
    return lines[1:] if size > max_bytes else lines


def _events(session_id: str, transcript: str | None = None) -> list[dict]:
    paths = [SESSION_STATE / session_id / "events.jsonl"] if session_id else []
    if transcript:
        paths.append(Path(transcript))
    for p in paths:
        if p.exists():
            out = []
            for line in _read_tail(p):
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
            return out
    return []


def session_title(session_id: str, cwd: str) -> str:
    """'folder · title' using the session title VS Code shows (falls back to the first prompt)."""
    folder = Path(cwd).name if cwd else "?"
    name = ""
    try:
        titled = [s for s in (vscode_sessions_for_cwd(cwd) if cwd else []) if s["title"] != "(untitled)"]
        if titled:
            name = titled[0]["title"]  # the most recently active session in that folder
    except Exception:
        pass
    ws = SESSION_STATE / session_id / "workspace.yaml" if session_id else None
    if not name and ws and ws.exists():
        for line in ws.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("name:"):
                name = line[5:].strip().strip("'\"")
                break
    name = re.sub(r"\s+", " ", name)
    if len(name) > 60:
        name = name[:57] + "…"
    return folder + (f" · {name}" if name else "")


def _message_events(session_id: str, max_bytes: int) -> list[dict]:
    """user.message / assistant.message events from the last max_bytes of a session log. Other
    lines (tool output, screenshots) are skipped without parsing, so large tails stay cheap."""
    path = SESSION_STATE / session_id / "events.jsonl"
    out = []
    for line in _read_tail(path, max_bytes):
        head = line[:60]
        if '"user.message"' in head or '"assistant.message"' in head:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def request_context(session_id: str, tool_call_id: str = "") -> tuple[str, str]:
    """(your prompt for this turn, what the agent said just before this request), shortened for the
    phone. With tool_call_id, the agent text comes from the message that made that tool call."""

    def scan(events: list[dict], call_id: str) -> tuple[str, str, bool]:
        say, found = "", not call_id
        for e in reversed(events):
            t, d = e.get("type"), e.get("data") or {}
            if t == "assistant.message":
                if not found:
                    if any((r or {}).get("toolCallId") == call_id for r in d.get("toolRequests") or []):
                        found = True
                    else:
                        continue
                if not say and (d.get("content") or "").strip():
                    say = d["content"]
            elif t == "user.message" and found:
                return d.get("content") or "", say, True
        return "", say, found

    task = say = ""
    for size in (512_000, 4_000_000, 32_000_000):  # big tool outputs can push the prompt far back
        events = _message_events(session_id, size)
        task, say, found = scan(events, tool_call_id)
        if tool_call_id and not found:
            task, say, _ = scan(events, "")
        if task:
            break

    def tidy(s: str, n: int) -> str:
        return _inline(re.sub(r"<[^>]{1,40}>", " ", s).replace("*", ""), n)

    return tidy(task, 200), tidy(say, 350)


def context_block(task: str, say: str) -> str:
    """Background for a phone message, as a collapsed section (tap to expand)."""
    lines = ([f"You asked: {task}"] if task else []) + ([f"Agent: {say}"] if say else [])
    return f"\n{MORE_OPEN}" + "\n".join(lines) + MORE_CLOSE if lines else ""


def _ts(e: dict) -> float:
    try:
        return datetime.fromisoformat(str(e.get("timestamp", "")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


_PROMPT_TYPES = ('"permission.requested"', '"permission.completed"', '"tool.execution_start"',
                 '"tool.execution_complete"', '"abort"', '"assistant.turn_end"')
_prompt_cache: dict[str, dict] = {}
_prompt_lock = threading.Lock()


def _apply_prompt_event(st: dict, e: dict) -> None:
    t, d = e.get("type"), e.get("data") or {}
    perms, questions = st["perms"], st["questions"]
    if t == "permission.requested":
        perms[d.get("requestId", "")] = (d.get("permissionRequest") or {}, _ts(e))
    elif t == "permission.completed":
        perms.pop(d.get("requestId", ""), None)
    elif t == "tool.execution_start" and d.get("toolName") == "ask_user":
        args = d.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {"question": args}
        questions[d.get("toolCallId", "")] = (args, _ts(e))
    elif t == "tool.execution_complete":
        questions.pop(d.get("toolCallId", ""), None)
    elif t == "abort":  # turn cancelled: whatever was waiting is gone
        perms.clear()
        questions.clear()
    elif t == "assistant.turn_end":
        questions.clear()


def pending_prompts(session_id: str) -> tuple[dict[str, tuple[dict, float]], dict[str, tuple[dict, float]]]:
    """Unanswered VS Code prompts in a session: (permissions by requestId, ask_user questions by toolCallId).

    Reads the session log incrementally (logs can be tens of MB), so a prompt is
    never missed just because a lot was logged after it.
    """
    path = SESSION_STATE / session_id / "events.jsonl"
    with _prompt_lock:
        st = _prompt_cache.get(str(path))
        try:
            size = path.stat().st_size
            with open(path, "rb") as f:
                head = f.read(64)
        except OSError:
            return {}, {}
        if st is None or size < st["offset"] or head != st["head"]:
            st = {"offset": 0, "rest": b"", "perms": {}, "questions": {}, "head": head}
            _prompt_cache[str(path)] = st
        if size > st["offset"]:
            try:
                with open(path, "rb") as f:
                    f.seek(st["offset"])
                    chunk = f.read(size - st["offset"])
            except OSError:
                chunk = b""
            st["offset"] += len(chunk)
            lines = (st["rest"] + chunk).split(b"\n")
            st["rest"] = lines.pop()  # possibly incomplete last line
            if st["rest"].strip():
                try:  # a complete last line without a trailing newline: process it now
                    json.loads(st["rest"])
                    lines.append(st["rest"])
                    st["rest"] = b""
                except ValueError:
                    pass
            for raw in lines:
                head = raw[:80].decode("utf-8", errors="replace")
                if not any(k in head for k in _PROMPT_TYPES):
                    continue
                try:
                    _apply_prompt_event(st, json.loads(raw))
                except ValueError:
                    pass
        return dict(st["perms"]), dict(st["questions"])


def session_busy(session_id: str) -> bool:
    """True while the agent is working on a turn (a new message would be queued, not dropped)."""
    for e in reversed(_events(session_id)):
        t, d = e.get("type"), e.get("data") or {}
        if t in ("abort", "session.end") or (t in ("hook.start", "hook.end") and d.get("hookType") in ("agentStop", "sessionEnd")):
            return False
        if t in ("user.message", "assistant.turn_start", "tool.execution_start"):
            return True
    return False


def event_after(session_id: str, since: float, types: tuple[str, ...]) -> bool:
    """True if an event of one of `types` was logged in this session after `since` (1 s tolerance)."""
    for e in reversed(_events(session_id)):
        if _ts(e) < since - 1:
            return False
        if e.get("type") in types:
            return True
    return False


def user_message_after(session_id: str, since: float) -> bool:
    """True if the user sent a chat message in this session after `since` (epoch seconds)."""
    for e in reversed(_events(session_id)):
        if _ts(e) < since - 1:  # 1 s tolerance for clock/rounding differences
            return False
        if e.get("type") == "user.message":
            return True
    return False


def user_message_since(session_id: str, text: str, since: float) -> bool:
    for e in reversed(_events(session_id)):
        if _ts(e) < since - 2:
            return False
        if e.get("type") == "user.message" and ((e.get("data") or {}).get("content") or "").strip() == text.strip():
            return True
    return False


def session_cwd(session_id: str) -> str:
    ws = SESSION_STATE / session_id / "workspace.yaml"
    try:
        for line in ws.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("cwd:"):
                return line[4:].strip().strip("'\"")
    except OSError:
        pass
    return ""


AGENT_HOST_DB = Path(os.environ.get("APPDATA", "")) / "Code" / "User" / "globalStorage" / "agent-host.db"


def list_vscode_sessions() -> list[dict]:
    """All open (non-archived) VS Code agent-host sessions, most recently active first."""
    out = []
    try:
        con = sqlite3.connect(f"file:{AGENT_HOST_DB}?mode=ro", uri=True, timeout=2)
        rows = con.execute("select session_uri, payload from sessions_v2").fetchall()
        con.close()
    except sqlite3.Error:
        return out
    for uri, payload in rows:
        try:
            data = (json.loads(payload or "{}").get("data")) or {}
        except ValueError:
            continue
        if data.get("isArchived") or data.get("isChatBacking"):
            continue
        dirs = [urllib.parse.unquote(urllib.parse.urlparse(u).path).lstrip("/") for u in data.get("workingDirectories") or []]
        out.append({"id": uri.split(":/", 1)[-1], "modified": data.get("modifiedTime") or 0,
                    "title": (data.get("summary") or "").strip() or "(untitled)", "dirs": dirs,
                    "chats": [c.get("uri") for c in data.get("chats") or [] if c.get("uri")]})
    out.sort(key=lambda s: s["modified"], reverse=True)
    return out


def vscode_sessions_for_cwd(cwd: str) -> list[dict]:
    """VS Code agent-host sessions whose working folder is cwd, most recently active first."""
    want = os.path.normcase(os.path.abspath(cwd)) if cwd else ""
    return [s for s in list_vscode_sessions()
            if not want or any(os.path.normcase(os.path.abspath(x)) == want for x in s["dirs"])]


def ago(ms: float) -> str:
    s = max(0, time.time() - (ms or 0) / 1000)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{int(s // n)}{unit} ago"
    return "just now"


def session_resources(vs_sessions: list[dict], runtime_id: str) -> list[str]:
    """Every URI form VS Code may use for these sessions' chat widgets. Accept/Skip on a
    session without a waiting prompt is a no-op, so trying all forms is safe."""
    res = []
    for s in vs_sessions:
        res += [f"ahp-session:/{s['id']}", f"agent-host-copilotcli:/{s['id']}", f"copilotcli:/{s['id']}"] + s["chats"]
    res += [f"agent-host-copilotcli:/{runtime_id}", f"copilotcli:/{runtime_id}"]
    return list(dict.fromkeys(res))


def last_assistant_message(session_id: str, transcript: str | None) -> str:
    for e in reversed(_events(session_id, transcript)):
        if e.get("type") == "assistant.message":
            content = (e.get("data") or {}).get("content") or ""
            if content.strip():
                return _md(content).strip()
    return ""


def permission_key(kind: str, tool: str, server: str = "") -> str:
    """Coarse key (kind, or tool for MCP/custom tools). Only used to recognise what VS Code itself
    is already auto-approving; it never grants anything."""
    if kind in ("custom-tool", "mcp"):
        return f"{kind}:{tool}"
    return kind


def grant_key(p: dict) -> str:
    """What "Allow for session" covers: the exact command, the exact file, one website, or one
    MCP/custom tool. Never a whole kind such as "every shell command"."""
    kind, a = p["kind"], p["args"]
    if kind == "shell":
        return "shell:" + " ".join(_first(a, "fullCommandText", "command", "cmd", "value").split())
    if kind == "write":
        f = _first(a, "fileName", "path", "file_path", "filePath")
        return "write:" + (os.path.normcase(os.path.abspath(f)) if f else "")
    if kind == "url":
        u = urllib.parse.urlparse(_first(a, "url", "value"))
        return f"url:{u.scheme}://{u.netloc.lower()}"
    return f"{kind}:{p['tool']}"


def session_grant_ok(p: dict, cwd: str, sensitive: list[str]) -> bool:
    """Whether "Allow for session" may be offered. Never for writes to sensitive files or outside
    the workspace (they can lead to running code), and never when the target is unknown."""
    key = grant_key(p)
    if key.split(":", 1)[1] in ("", "://"):
        return False
    if p["kind"] == "write":
        return not write_needs_approval(_first(p["args"], "fileName", "path", "file_path", "filePath"), cwd, sensitive)
    return True


def host_policy_keys(session_id: str) -> set[str]:
    """Permission keys VS Code is currently auto-approving in this session.

    VS Code asks once per kind (or tool), then approves the rest of the session
    with decisionSource=host_policy. A key counts as auto-approved when its most
    recent decision in the session log came from host_policy.
    """
    reqs: dict[str, str] = {}
    last: dict[str, str] = {}
    for e in _events(session_id):
        t, d = e.get("type"), e.get("data") or {}
        if t == "permission.requested":
            pr = d.get("permissionRequest") or {}
            reqs[d.get("requestId", "")] = permission_key(pr.get("kind", ""), pr.get("toolName", ""), pr.get("serverName", ""))
        elif t == "permission.completed":
            key = reqs.get(d.get("requestId", ""))
            if key:
                last[key] = str(d.get("decisionSource"))
    return {k for k, src in last.items() if src == "host_policy"}


SHELL_TOOLS = {"powershell", "bash", "shell", "run_in_terminal", "write_powershell", "write_bash"}
WRITE_TOOLS = {"edit", "create", "apply_patch", "str_replace_editor", "write", "multi_edit"}
URL_TOOLS = {"web_fetch", "fetch", "fetch_webpage"}
READ_TOOLS = {"view", "grep", "glob", "rg", "read", "read_file"}


def normalize_permission(payload: dict) -> dict:
    """Pull kind/tool/args out of whatever shape the permissionRequest hook sends."""
    tool = payload.get("toolName") or payload.get("tool_name") or ""
    args = payload.get("toolInput") or payload.get("toolArgs") or payload.get("tool_input") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {"value": args}
    pr = payload.get("permissionRequest") or payload.get("permission_request") or {}
    kind = pr.get("kind") or payload.get("kind") or ""
    if not kind:
        t = tool.lower()
        if t in SHELL_TOOLS:
            kind = "shell"
        elif t in WRITE_TOOLS:
            kind = "write"
        elif t in URL_TOOLS:
            kind = "url"
        elif t in READ_TOOLS:
            kind = "read"
        elif "-" in tool or payload.get("serverName"):
            kind = "mcp"
        else:
            kind = "custom-tool"
    merged = {**(args if isinstance(args, dict) else {"value": args}), **pr}
    return {"kind": kind, "tool": pr.get("toolName") or tool, "args": merged, "server": pr.get("serverName", "")}


def _first(d: dict, *keys: str) -> str:
    for k in keys:
        v = d.get(k)
        if v:
            return v if isinstance(v, str) else json.dumps(v)
    return ""


def _clip(s: str, limit: int) -> tuple[str, bool]:
    """Fit text for the phone without hiding anything silently: keeps the start and the end
    with a visible warning in between. Neutralises ``` so the text can't end its code block."""
    s = _unmark(s).replace("```", "`\u200b`\u200b`")
    if len(s) <= limit:
        return s, False
    head, tail = s[: limit * 2 // 3], s[-(limit // 3):]
    hidden = len(s) - len(head) - len(tail)
    return f"{head}\n… ⚠️ {hidden} characters NOT shown here …\n{tail}", True


def _inline(s: str, limit: int = 200) -> str:
    s = re.sub(r"\s+", " ", _unmark(s).replace("`", "'")).strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _md(s: str) -> str:
    """Agent markdown for the phone: **bold** → *bold* (the bridge's bold markup)."""
    return _unmark(s).replace("**", "*")


def permission_heading(p: dict) -> str:
    """Card title for an approval, worded like VS Code's prompt."""
    kind, tool, a = p["kind"], _inline(p["tool"], 40).replace("*", ""), p["args"]
    if kind == "shell":
        return f"Run {tool or 'shell'} command?"
    if kind == "write":
        return f"{_inline(_first(a, 'intention'), 40).replace('*', '') or 'Edit file'}?"
    if kind == "url":
        return "Fetch web page?"
    if kind == "mcp":
        return f"Run MCP tool {tool}?"
    return f"Use {tool or 'tool'}?"


def describe_permission(p: dict, limit: int = 2500) -> tuple[str, bool]:
    """Phone text for an approval request (under its heading), and whether part of it couldn't be shown."""
    kind, tool, a = p["kind"], p["tool"], p["args"]
    if kind == "shell":
        cmd, cut = _clip(_first(a, "fullCommandText", "command", "cmd", "value"), limit)
        why = _inline(_first(a, "intention", "description"))
        return (f"{why}\n" if why else "") + f"```{cmd}```", cut
    if kind == "write":
        f = _inline(_first(a, "fileName", "path", "file_path", "filePath"), 400)
        diff = _first(a, "diff")
        out = f"`{f}`"
        cut = False
        if diff:
            shown, cut = _clip(diff.strip(), limit)
            out += f"\n```{shown}```"
        return out, cut
    if kind == "url":
        return _inline(_first(a, "url", "value"), 500), False
    if kind == "mcp":
        args = a.get("args", {k: v for k, v in a.items() if k not in ('kind', 'toolCallId', 'toolName', 'serverName', 'toolTitle', 'readOnly')})
        shown, cut = _clip(json.dumps(args), limit)
        server = _inline(p.get("server") or a.get("serverName", ""), 80)
        return f"`{_inline(tool, 80)}`" + (f" · {server}" if server else "") + f"\n```{shown}```", cut
    args = a.get("args", {k: v for k, v in a.items() if k not in ('kind', 'toolCallId', 'toolName', 'toolDescription')})
    shown, cut = _clip(json.dumps(args), limit)
    return f"`{_inline(tool, 80)}`\n```{shown}```", cut


def _under(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(root))]) == os.path.normcase(os.path.abspath(root))
    except ValueError:
        return False


def write_needs_approval(fname: str, cwd: str, sensitive: list[str]) -> bool:
    if not fname or not cwd or not _under(fname, cwd):
        return True
    rel = os.path.relpath(fname, cwd).replace("\\", "/")
    return any(fnmatch.fnmatch("/" + rel, g.replace("**/", "*/")) or fnmatch.fnmatch(rel, g.replace("**/", "")) for g in sensitive)


def format_question(args: dict) -> tuple[str, list[str]]:
    """Render ask_user arguments (simple or form style). Returns text + option list."""
    q = _first(args, "question", "message", "prompt")
    options: list[str] = []
    for c in args.get("choices") or args.get("options") or []:
        options.append(c if isinstance(c, str) else _first(c, "label", "title", "value"))
    props = ((args.get("requestedSchema") or {}).get("properties") or {})
    lines = [q] if q else []
    for name, f in props.items():
        title = f.get("title") or name
        vals = f.get("enum") or [o.get("const") for o in f.get("oneOf", [])] or (f.get("items") or {}).get("enum") or []
        labels = f.get("enumNames") or [o.get("title") for o in f.get("oneOf", [])] or vals
        line = f"• *{title}*"
        if f.get("description"):
            line += f" — {f['description'][:200]}"
        if labels:
            line += "\n   " + " / ".join(str(x) for x in labels)
            if len(props) == 1:
                options = [str(x) for x in labels]
        lines.append(line)
    return _unmark("\n".join(lines).strip()), options


def question_card(args: dict) -> tuple[str, str, list[str]]:
    """(heading, body, options) for an agent question, laid out like VS Code's question card:
    the question as the title, the explanation underneath, options as buttons (listed once)."""
    q = _md(_first(args, "question", "message", "prompt")).strip()
    options = [str(c if isinstance(c, str) else _first(c, "label", "title", "value"))
               for c in args.get("choices") or args.get("options") or []]
    props = (args.get("requestedSchema") or {}).get("properties") or {}
    body: list[str] = []
    if len(props) == 1:
        name, f = next(iter(props.items()))
        vals = f.get("enum") or [o.get("const") for o in f.get("oneOf", [])] or (f.get("items") or {}).get("enum") or []
        labels = f.get("enumNames") or [o.get("title") for o in f.get("oneOf", [])] or vals
        options = [str(x) for x in labels] or options
        heading = _md(f.get("title") or "").strip() or q or name
        if q and q != heading:
            body.append(q)
        if f.get("description"):
            body.append(_md(f["description"]))
    elif props:
        heading = q or "A few questions"
        for name, f in props.items():
            vals = f.get("enum") or [o.get("const") for o in f.get("oneOf", [])] or (f.get("items") or {}).get("enum") or []
            labels = f.get("enumNames") or [o.get("title") for o in f.get("oneOf", [])] or vals
            line = f"• *{_inline(f.get('title') or name, 80).replace('*', '')}*"
            if f.get("description"):
                line += f" — {_inline(f['description'], 200)}"
            if labels:
                line += f" ({' / '.join(_inline(str(x), 40) for x in labels)})"
            body.append(line)
        options = []
    else:
        heading = q or "Question"
    if len(heading) > 120:  # long questions go in the body, with a short title
        body.insert(0, heading)
        heading = "Question"
    text = _clip("\n\n".join(body), 1500)[0] if body else ""
    return _inline(heading, 120).replace("*", ""), text, options


# ----------------------------------------------------------------------------
# Pending request registry
# ----------------------------------------------------------------------------

@dataclass
class Pending:
    id: int
    kind: str  # approval | question | followup
    session_id: str
    title: str
    text: str
    options: list[str] = field(default_factory=list)
    result: dict | None = None
    answered_by: str = ""
    event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    popup: subprocess.Popen | None = None
    meta: dict = field(default_factory=dict)

    def resolve(self, result: dict, by: str, label: str = "") -> bool:
        with self.lock:
            if self.event.is_set():
                return False
            self.result, self.answered_by = result, by
            self.meta["label"] = label
            self.event.set()
            return True


def outcome_label(kind: str, result: dict, by: str) -> str:
    if result.get("timeout"):
        return "No reply: turn ended" if kind == "followup" else "Timed out: answer in VS Code"
    if kind == "approval":
        d = result.get("decision", "")
        label = {"allow": "✓ Allowed", "session": "✓ Allowed in this session", "deny": "✕ Skipped",
                 "stop": "✕ Skipped & stopped", "vscode": "Sent to VS Code"}.get(d, d)
    elif kind == "question":
        label = "Answering in VS Code" if result.get("vscode") else f"✓ {result.get('text', '')}"[:45]
    else:
        label = "✓ Follow-up sent" if result.get("prompt") else "✓ Turn ended"
    return label + (" · laptop" if by == "laptop" else "")


def make_transport(cfg: dict) -> Transport:
    prov = str(cfg.get("provider", "auto")).lower()
    tg, tw = cfg.get("telegram") or {}, cfg.get("twilio") or {}
    if prov in ("auto", "telegram") and tg.get("bot_token") and tg.get("chat_id"):
        return TelegramTransport(tg["bot_token"], tg["chat_id"])
    if prov in ("auto", "twilio", "whatsapp") and tw.get("account_sid") and tw.get("auth_token") and tw.get("to"):
        return TwilioTransport(tw["account_sid"], tw["auth_token"], tw["from"], tw["to"])
    return Transport()


APPROVE_ONCE = {"1", "y", "yes", "ok", "allow", "approve", "approved", "👍", "✅"}
APPROVE_SESSION = {"2", "a", "all", "session", "allow session", "allow all", "always"}
DENY = {"3", "n", "no", "deny", "reject", "❌", "👎"}
END_TURN = {"ok", "okay", "done", "end", "stop", "no", "n", "👍", "✅", "thanks", "thank you", "0"}


def parse_approval(text: str) -> tuple[str, str]:
    """Return (decision, reason) with decision in allow|session|deny|stop."""
    t = text.strip()
    low = t.lower().rstrip(".!")
    if low in APPROVE_ONCE:
        return "allow", ""
    if low in APPROVE_SESSION:
        return "session", ""
    if low in DENY:
        return "deny", ""
    if low in ("stop", "abort", "cancel", "4"):
        return "stop", ""
    m = re.match(r"^(3|n|no|deny)[\s,:\-]+(.+)$", t, re.I | re.S)
    if m:
        return "deny", m.group(2).strip()
    return "deny", t  # anything else = deny with feedback


class Bridge:
    def __init__(self, cfg: dict, transport: Transport | None = None,
                 popup_launcher: Callable[[Pending], subprocess.Popen | None] | None = None) -> None:
        self.cfg = cfg
        self.transport = transport if transport is not None else make_transport(cfg)
        self.popup_launcher = popup_launcher if popup_launcher is not None else self._launch_popup
        self.pending: dict[int, Pending] = {}
        self.plock = threading.Lock()
        self.ids = itertools.count(1)
        self.session_allow: dict[str, set[str]] = {}
        self.cb_tokens: dict[str, dict] = {}  # button code -> {"ref": message id, "data": allowed actions}
        self.consecutive_blocks: dict[str, int] = {}
        self.turn_seq: dict[str, int] = {}
        self.question_for: dict[str, dict] = {}  # session -> ask_user args converted to a chat question
        self.recent_local: dict[str, float] = {}
        self.paused = False
        self.stop_event = threading.Event()
        self.idle = idle_seconds
        self.vs_lookup = vscode_sessions_for_cwd
        self.vs_list = list_vscode_sessions
        self.phone_prompts: dict[str, int] = {}  # folder -> phone prompts awaiting their turn end
        self._last_list: list[dict] = []
        self.state_path = Path(cfg["state_file"]) if cfg.get("state_file") else HERE / "state.json"
        self.active: dict | None = self._load_state().get("active_session")
        self.native_seen: set[str] = set()
        self.native_grace = float(cfg.get("push_after_seconds", 60))
        self.scan_interval, self.press_timeout = 5.0, 5.0
        # Jobs for the companion VS Code extension (it long-polls /vscode/jobs).
        self.jobs: list[dict] = []
        self.jobs_cond = threading.Condition()
        self.job_ids = itertools.count(int(time.time() * 1000))  # keeps ids increasing across bridge restarts
        self.ext_seen = 0.0
        self.ext_instances: dict[str, dict] = {}  # helper extension instance -> {"first", "seen", "empty"}
        self.helper_id: str | None = None  # the window the bridge opened itself (auto-closed when idle)
        self.last_job_at = 0.0

    def _at_laptop(self) -> bool:
        """True when the hook should let VS Code show its own prompt right away."""
        if self.cfg.get("laptop_popup"):
            return False  # popup mode handles the laptop side itself
        if not self.transport.configured:
            return True
        if not self.cfg.get("hold_in_hook", False):
            # Default: VS Code always shows its prompt; scan_loop adds the phone once you're
            # away, and the request stays open in both places until either one answers it.
            return True
        return self.idle() < float(self.cfg.get("away_after_seconds", 60))

    def _max_wait_minutes(self) -> float:
        return float(self.cfg.get("max_wait_hours", 168)) * 60

    def _phone_limit(self) -> int:
        """Characters of a command/diff shown on the phone (Telegram allows much longer messages)."""
        return 2500 if self.transport.supports_buttons else 800

    def _start_turn_watch(self, payload: dict) -> None:
        """Agent finished a turn: once you're away, offer a follow-up on the phone. It stays open
        until you reply there or continue the chat in VS Code."""
        sid = payload.get("sessionId") or payload.get("session_id") or ""
        if not sid:
            return
        cwd = payload.get("cwd") or session_cwd(sid)
        transcript = payload.get("transcriptPath") or payload.get("transcript_path")
        with self.plock:
            seq = self.turn_seq[sid] = self.turn_seq.get(sid, 0) + 1
        stop_at = time.time()
        phone_driven = self._consume_phone_prompt(cwd)

        def current() -> bool:
            return self.turn_seq.get(sid) == seq and not user_message_after(sid, stop_at)

        def run() -> None:
            # No new message from you within push_after_seconds: also offer the follow-up on the phone.
            # If the turn was started from the phone, send the reply right away.
            if self.stop_event.wait(0 if phone_driven else self.native_grace) or not current():
                return
            if self.paused:
                return
            last = last_assistant_message(sid, transcript)
            if len(last) > 900:
                last = last[:900] + "…"
            task = request_context(sid)[0]
            req = self.ask("followup", sid, session_title(sid, cwd), last + context_block(task, ""),
                           self.followup_footer(), self._max_wait_minutes(), heading="Agent finished",
                           still_pending=current, release_on_return=False)
            r = req.result or {}
            log.info("#%s (turn end in %s) resolved: %s by %s", req.id, sid[:8], r, req.answered_by)
            if r.get("prompt"):
                problem = self.submit_vscode(sid, "", cwd, r["prompt"])
                log.info("#%s follow-up sent to VS Code: %s", req.id, problem or "ok")
                if problem:
                    self.send(f"⚠️ #{req.id}: couldn't send your follow-up to VS Code ({problem}).")

        threading.Thread(target=run, daemon=True).start()

    # -- VS Code prompts left unanswered while you're away ------------------------
    def post_job(self, job: dict) -> int:
        with self.jobs_cond:
            job = {**job, "id": next(self.job_ids), "created": time.time()}
            self.jobs = [j for j in self.jobs if time.time() - j["created"] < 30] + [job]
            self.last_job_at = time.time()
            self.jobs_cond.notify_all()
            return job["id"]

    def _helper_alive(self) -> bool:
        if not self.helper_id:
            return False
        inst = self.ext_instances.get(self.helper_id)
        return bool(inst) and time.time() - inst["seen"] < 40

    def _helper_idle(self) -> bool:
        idle_for = time.time() - max(self.last_job_at, getattr(self, "_companion_launched", 0.0))
        with self.plock:
            busy = bool(self.pending)  # a phone answer may need the window soon
        return not busy and idle_for > float(self.cfg.get("helper_idle_minutes", 10)) * 60

    def wait_jobs(self, after: int, timeout: float, instance: str = "", empty: bool = False) -> dict:
        """Long-poll from a helper extension instance (one per VS Code window). Each job goes to one
        window only, preferably the helper window the bridge opened itself."""
        now = time.time()
        with self.jobs_cond:
            self.ext_seen = now
            inst = self.ext_instances.setdefault(instance, {"first": now})
            inst.update(seen=now, empty=empty)
            launched = getattr(self, "_companion_launched", 0.0)
            if (instance and empty and self.helper_id is None and now - launched < 180
                    and inst["first"] >= launched - 1 and getattr(self, "_helper_for_launch", None) != launched):
                self.helper_id = instance  # the window that connected right after we opened one
                self._helper_for_launch = launched  # only one helper per window we opened
                log.info("helper window connected (%s)", instance[:8])
            is_helper = bool(instance) and instance == self.helper_id
            last = self.jobs[-1]["id"] if self.jobs else 0
            if after < 0:
                return {"jobs": [], "last": last, "helper": is_helper}
            if is_helper and self._helper_idle():
                self.helper_id = None
                self.ext_instances.pop(instance, None)
                log.info("closing the idle helper window (%s)", instance[:8])
                return {"jobs": [], "last": last, "helper": True, "close": True}

            def claimable(j: dict) -> bool:
                age = time.time() - j["created"]
                if j["id"] <= after or j.get("claimed") or age >= 30:
                    return False
                return is_helper or not self._helper_alive() or age >= 3  # let the helper take it first

            end = now + timeout
            while not any(claimable(j) for j in self.jobs):
                rem = end - time.time()
                if rem <= 0:
                    break
                self.jobs_cond.wait(min(rem, 1.0))
            fresh = [j for j in self.jobs if claimable(j)]
            for j in fresh:
                j["claimed"] = instance or "?"
            if instance in self.ext_instances:
                self.ext_instances[instance]["seen"] = time.time()
            return {"jobs": fresh, "last": self.jobs[-1]["id"] if self.jobs else 0, "helper": is_helper}

    @property
    def extension_connected(self) -> bool:
        cutoff = time.time() - 40
        return any(i["seen"] > cutoff for i in self.ext_instances.values())

    def _waiting_runtime_sessions(self, cwd: str) -> list[str]:
        want = os.path.normcase(os.path.abspath(cwd))
        out = []
        try:
            dirs = [d for d in SESSION_STATE.iterdir() if d.is_dir()]
        except OSError:
            return out
        for d in dirs:
            try:
                if time.time() - (d / "events.jsonl").stat().st_mtime > 7200:
                    continue
            except OSError:
                continue
            c = session_cwd(d.name)
            if c and os.path.normcase(os.path.abspath(c)) == want and pending_prompts(d.name)[0]:
                out.append(d.name)
        return out

    def ensure_companion(self, wait: float = 45.0) -> bool:
        """Make sure a VS Code window running the companion extension is open (it can't run in the Agents window)."""
        if self.extension_connected:
            return True
        if time.time() - getattr(self, "_companion_launched", 0) > 120:
            self._companion_launched = time.time()
            self.helper_id = None
            log.info("companion extension not connected: opening a minimized VS Code window")
            try:
                subprocess.Popen(["powershell", "-NoProfile", "-Command",
                                  "Start-Process -FilePath (Get-Command code).Source -ArgumentList '-n' -WindowStyle Minimized"],
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except OSError as e:
                log.error("could not start VS Code: %s", e)
                return False
        end = time.time() + wait
        while time.time() < end:
            if self.extension_connected:
                return True
            time.sleep(1)
        return False

    def press_vscode(self, sid: str, rid: str, cwd: str, command: str) -> str:
        """Press Accept/Skip on the VS Code prompt `rid`. Returns '' on success, else a reason."""
        if not self.ensure_companion():
            return "the companion VS Code window couldn't be started"
        if len(self._waiting_runtime_sessions(cwd)) > 1:
            return "several sessions in this folder are waiting, so it isn't clear which prompt to press"
        vs = self.vs_lookup(cwd)
        resources = session_resources(vs, sid)

        def done() -> bool:
            return rid not in pending_prompts(sid)[0]

        def wait_done(seconds: float) -> bool:
            end = time.time() + seconds
            while time.time() < end:
                if done():
                    return True
                time.sleep(0.3)
            return done()

        # The companion window must have the session open before Accept/Skip can reach it.
        for s in vs[:3] or [None]:
            job = {"command": command, "resources": resources}
            if s:
                job["open"] = f"agent-host-session://ahp-session/{s['id']}"
            self.post_job(job)
            if wait_done(self.press_timeout + 15):  # a freshly opened window can take a while
                return ""
        return "" if wait_done(5) else "VS Code didn't accept the button press"

    def submit_vscode(self, sid: str, cid: str, cwd: str, text: str) -> str:
        """Answer an open question form by sending `text` to the session. '' on success."""
        if not self.ensure_companion():
            return "the companion VS Code window couldn't be started"
        if sum(1 for s in self._sessions_in(cwd) if pending_prompts(s)[1]) > 1:
            return "several sessions in this folder are waiting, so it isn't clear which one to answer"
        vs = self.vs_lookup(cwd)
        start = time.time()

        def done() -> bool:
            return user_message_since(sid, text, start)

        for s in vs[:3]:
            self.post_job({"command": "workbench.action.chat.submit", "text": text,
                           "open": f"agent-host-session://ahp-session/{s['id']}"})
            end = time.time() + self.press_timeout + 15
            while time.time() < end:
                if done():
                    return ""
                time.sleep(0.5)
        return "" if done() else "VS Code didn't take the answer"

    def _sessions_in(self, cwd: str) -> list[str]:
        want = os.path.normcase(os.path.abspath(cwd))
        out = []
        try:
            dirs = [d for d in SESSION_STATE.iterdir() if d.is_dir()]
        except OSError:
            return out
        for d in dirs:
            try:
                if time.time() - (d / "events.jsonl").stat().st_mtime > 7200:
                    continue
            except OSError:
                continue
            c = session_cwd(d.name)
            if c and os.path.normcase(os.path.abspath(c)) == want:
                out.append(d.name)
        return out

    def handle_native_question(self, sid: str, cid: str, args: dict) -> None:
        cwd = session_cwd(sid)
        heading, text, options = question_card(args)
        task, _ = request_context(sid, cid)
        text = self._numbered(text, options) + context_block(task, "")
        req = self.ask("question", sid, session_title(sid, cwd), text.strip("\n"), self.question_footer(options),
                       self._max_wait_minutes(), options, heading=heading,
                       still_pending=lambda: cid in pending_prompts(sid)[1], release_on_return=False)
        r = req.result or {}
        log.info("#%s (VS Code question %s in %s) resolved: %s by %s", req.id, cid[:8], sid[:8], r, req.answered_by)
        if not r.get("text"):
            return
        problem = self.submit_vscode(sid, cid, cwd, r["text"])
        log.info("#%s answer sent to VS Code: %s", req.id, problem or "ok")
        if problem:
            self.send(f"⚠️ #{req.id}: couldn't answer it in VS Code ({problem}). Please answer it on the laptop.")

    def handle_native_permission(self, sid: str, rid: str, pr: dict) -> None:
        cwd = session_cwd(sid)
        p = normalize_permission({"toolName": pr.get("toolName", ""), "permissionRequest": pr})
        gkey = grant_key(p)
        grant_ok = session_grant_ok(p, cwd, self.cfg["sensitive_write_globs"])
        detail, cut = describe_permission(p, self._phone_limit())
        detail += context_block(*request_context(sid, str(pr.get("toolCallId") or "")))
        req = self.ask("approval", sid, session_title(sid, cwd), detail, self.approval_footer(cut, not grant_ok),
                       self._max_wait_minutes(), ["Allow once", "Allow for session", "Deny"],
                       meta={"no_allow": cut, "no_session": not grant_ok}, heading=permission_heading(p),
                       still_pending=lambda: rid in pending_prompts(sid)[0], release_on_return=False)
        r = req.result or {}
        d = r.get("decision")
        log.info("#%s (VS Code prompt %s in %s) resolved: %s by %s", req.id, rid[:8], sid[:8], r, req.answered_by)
        if d not in ("allow", "session", "deny", "stop"):
            return
        problem = self.press_vscode(sid, rid, cwd, "workbench.action.chat.acceptTool" if d in ("allow", "session")
                                    else "workbench.action.chat.skipTool")
        log.info("#%s press in VS Code: %s", req.id, problem or "ok")
        if problem:
            self.send(f"⚠️ #{req.id}: couldn't answer it in VS Code ({problem}). Please answer it on the laptop.")
            return
        if d == "session" and grant_ok:
            self.session_allow.setdefault(sid, set()).add(gkey)
        if d in ("deny", "stop") and r.get("reason"):
            self.send(f"ℹ️ #{req.id} was skipped. VS Code prompts can't carry feedback text to the agent.")

    def scan_once(self) -> None:
        if self.paused or not self.transport.configured:
            return
        try:
            dirs = [d for d in SESSION_STATE.iterdir() if d.is_dir()]
        except OSError:
            return
        now = time.time()
        with self.plock:
            held = {(x.session_id, x.kind) for x in self.pending.values()}
        for d in dirs:
            try:
                if now - (d / "events.jsonl").stat().st_mtime > 7200:
                    continue
            except OSError:
                continue
            perms, questions = pending_prompts(d.name)
            waiting = [(rid, "prompt", pr, ts) for rid, (pr, ts) in perms.items()] + \
                      [(cid, "question", a, ts) for cid, (a, ts) in questions.items()
                       if (d.name, "question") not in held]
            for key, kind, data, ts in waiting:
                # Unanswered in VS Code for push_after_seconds: also send it to the phone.
                if key in self.native_seen or now - ts < self.native_grace:
                    continue
                self.native_seen.add(key)
                log.info("VS Code %s %s in %s unanswered for %ds: sending to phone too", kind, key[:8], d.name[:8], now - ts)
                target = self.handle_native_permission if kind == "prompt" else self.handle_native_question
                threading.Thread(target=target, args=(d.name, key, data), daemon=True).start()

    def scan_loop(self) -> None:
        while not self.stop_event.wait(self.scan_interval):
            try:
                self.scan_once()
            except Exception:
                log.exception("scan failed")

    # -- messaging -----------------------------------------------------------
    def _seal(self, buttons: Buttons) -> tuple[str, Buttons]:
        """Prefix every button's data with a random code that only becomes valid once it's tied
        to the message the bridge sent (see _register_buttons / handle_callback)."""
        n = secrets.token_hex(6)
        sealed = [[(label, f"{n}|{data}") for label, data in row] for row in buttons]
        with self.plock:
            self.cb_tokens[n] = {"ref": None, "data": {data for row in buttons for _, data in row}}
        return n, sealed

    def _register_buttons(self, n: str, ref: Any) -> None:
        with self.plock:
            entry = self.cb_tokens.get(n)
            if entry is None:
                return
            if not ref:
                self.cb_tokens.pop(n, None)
                return
            entry["ref"] = ref
            while len(self.cb_tokens) > 500:  # forget the oldest messages' buttons
                self.cb_tokens.pop(next(iter(self.cb_tokens)))

    def handle_callback(self, data: str, message_id: Any) -> None:
        """A button tap. Accepted only if it carries the code of a message the bridge sent, comes
        from that same message, and is one of the actions that message offered. Button data is
        never treated as typed text, so a forged button can't send a prompt."""
        n, sep, action = (data or "").partition("|")
        with self.plock:
            entry = self.cb_tokens.get(n) if sep else None
        if (not entry or entry["ref"] is None or str(entry["ref"]) != str(message_id)
                or action not in entry["data"]):
            log.warning("ignored a button tap that doesn't match a message the bridge sent: %r", (data or "")[:80])
            return
        if re.match(r"^#\d+\s", action) or action.startswith("/use "):
            self.handle_inbound(action)
        else:
            log.warning("ignored unexpected button action %r", action[:80])

    def send(self, body: str, buttons: Buttons | None = None) -> None:
        if buttons is not None and not self.transport.supports_buttons:
            buttons = None

        def run() -> None:
            if buttons:
                n, sealed = self._seal(buttons)
                self._register_buttons(n, self.transport.send(body, sealed))
            else:
                self.transport.send(body, None)

        threading.Thread(target=run, daemon=True).start()

    # -- prompting a chosen VS Code session from the phone ------------------------
    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        try:
            self.state_path.write_text(json.dumps({"active_session": self.active}, indent=2), encoding="utf-8")
        except OSError as e:
            log.warning("could not save state: %s", e)

    @staticmethod
    def _folder(s: dict) -> str:
        return Path(s["dirs"][0]).name if s.get("dirs") else "?"

    def cmd_sessions(self, show_all: bool = False) -> None:
        ss = [s for s in self.vs_list() if show_all or s["title"] != "(untitled)"][:10]
        self._last_list = ss
        if not ss:
            self.send("No VS Code sessions found." + ("" if show_all else " Try /sessions all."))
            return
        lines = []
        for i, s in enumerate(ss, 1):
            mark = " ✅" if self.active and self.active.get("id") == s["id"] else ""
            lines.append(f"*{i}.* {s['title'][:60]}\n     📁 {self._folder(s)} · {ago(s['modified'])}{mark}")
        tail = ("Tap one to send prompts to it." if self.transport.supports_buttons
                else "Reply */use N* to pick one.")
        if not show_all:
            tail += " Untitled sessions are hidden: /sessions all"
        self.send("🗂 *Your VS Code sessions*\n\n" + "\n".join(lines) + "\n\n" + tail,
                  [[(f"{i}. {s['title'][:45]}", f"/use {s['id']}")] for i, s in enumerate(ss, 1)])

    def cmd_use(self, arg: str) -> None:
        arg = arg.strip()
        target = None
        if arg.isdigit():
            pool = self._last_list or self.vs_list()[:10]
            if 1 <= int(arg) <= len(pool):
                target = pool[int(arg) - 1]
        elif arg:
            target = next((s for s in self.vs_list() if s["id"].startswith(arg)), None)
        if not target:
            self.send("Couldn't find that session. Send /sessions to see the list.")
            return
        self.active = {"id": target["id"], "title": target["title"], "dir": target["dirs"][0] if target["dirs"] else ""}
        self._save_state()
        log.info("phone prompts now go to session %s (%s)", target["id"][:8], target["title"])
        self.send(f"🎯 Now prompting *{target['title']}* (📁 {self._folder(target)}).\n"
                  "Send any message to run it in that session. /sessions to switch · /session to check.")

    def cmd_session(self) -> None:
        if not self.active:
            self.send("No session selected. Send /sessions to pick one.")
        else:
            self.send(f"🎯 Prompts go to *{self.active['title']}* (📁 {Path(self.active['dir']).name or '?'}).")

    def _consume_phone_prompt(self, cwd: str) -> bool:
        key = os.path.normcase(os.path.abspath(cwd)) if cwd else ""
        with self.plock:
            if self.phone_prompts.get(key, 0) > 0:
                self.phone_prompts[key] -= 1
                return True
        return False

    def send_prompt(self, text: str) -> None:
        a = self.active
        if not a:
            self.send("No session selected. Send /sessions to pick one first.")
            return

        def run() -> None:
            cwd = a.get("dir", "")
            key = os.path.normcase(os.path.abspath(cwd)) if cwd else ""
            if not self.ensure_companion():
                self.send("⚠️ Couldn't reach VS Code (the companion window couldn't be started).")
                return
            with self.plock:
                self.phone_prompts[key] = self.phone_prompts.get(key, 0) + 1
            start = time.time()
            # A session that hasn't been used for a while takes longer to load in VS Code, and text
            # sent before it's ready is dropped. Retry with a longer load wait, but only once the
            # previous attempt has clearly not arrived, so a prompt is never sent twice.
            for attempt, (open_wait, confirm) in enumerate(((2500, 15), (10000, 30), (20000, 45)), 1):
                self.post_job({"command": "workbench.action.chat.submit", "text": text, "cancel": False,
                               "open": f"agent-host-session://ahp-session/{a['id']}", "openWait": open_wait})
                end = time.time() + confirm * (self.press_timeout / 5.0)
                while time.time() < end:
                    if any(user_message_since(s, text, start) for s in self._sessions_in(cwd)):
                        log.info("phone prompt delivered to %s (attempt %d)", a["id"][:8], attempt)
                        self.send(f"📨 Sent to *{a['title']}*. You'll get the agent's reply here when it finishes.")
                        return
                    time.sleep(0.5)
                log.info("phone prompt to %s not seen after attempt %d", a["id"][:8], attempt)
                if any(session_busy(s) for s in self._sessions_in(cwd)):
                    # Busy session: VS Code queues the message and logs it when the current turn ends.
                    # Retrying would send it twice.
                    log.info("session busy: phone prompt to %s assumed queued", a["id"][:8])
                    self.send(f"⏳ *{a['title']}* is busy. Your prompt is queued in VS Code and runs when the current step finishes.")
                    return
            with self.plock:
                self.phone_prompts[key] = max(0, self.phone_prompts.get(key, 0) - 1)
            log.info("phone prompt to %s not confirmed", a["id"][:8])
            self.send(f"⚠️ Couldn't confirm your prompt reached *{a['title']}*. Check VS Code, or try again.")

        threading.Thread(target=run, daemon=True).start()

    def _launch_popup(self, p: Pending) -> subprocess.Popen | None:
        if not self.cfg.get("laptop_popup", False):
            return None
        spec = {"id": p.id, "kind": p.kind, "title": p.title, "text": to_plain(p.text), "options": p.options,
                "escalate_seconds": int(float(self.cfg.get("escalate_after_seconds", 60))) if self.transport.configured else 0,
                "warning": self.transport.last_error and "Phone delivery problem: " + self.transport.last_error}
        try:
            proc = subprocess.Popen(
                [sys.executable.replace("pythonw.exe", "python.exe"), str(HERE / "popup.py")],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            proc.stdin.write(json.dumps(spec).encode())
            proc.stdin.close()
        except Exception as e:
            log.error("popup failed: %s", e)
            return None

        def watch() -> None:
            out = proc.stdout.read().decode(errors="replace").strip() if proc.stdout else ""
            proc.wait()
            if not out:
                if proc.returncode == 0 and p.meta.get("escalate"):
                    p.meta["escalate"]()  # closed without answering: hand over to the phone now
                return
            try:
                ans = json.loads(out)
            except ValueError:
                return
            self.recent_local[p.session_id] = time.time()
            self.answer(p, ans.get("answer", ""), by="laptop", raw=ans)

        threading.Thread(target=watch, daemon=True).start()
        return proc

    # -- core wait -------------------------------------------------------------
    @staticmethod
    def _buttons(p: Pending) -> Buttons | None:
        i = p.id
        if p.kind == "approval":
            deny = [("Skip", f"#{i} 3"), ("Skip & stop agent", f"#{i} stop")]
            if p.meta.get("no_allow"):
                return [deny]
            allow = [("Allow", f"#{i} 1")]
            if not p.meta.get("no_session"):
                allow.append(("Allow in this session", f"#{i} 2"))
            return [allow, deny]
        if p.kind == "question":
            return [[(o[:60], f"#{i} {n}")] for n, o in enumerate(p.options[:10], 1)] or None
        return [[("End turn", f"#{i} ok")]]

    def approval_footer(self, no_allow: bool, no_session: bool) -> str:
        if no_allow:
            return "⚠️ Part of this isn't shown here, so it can only be skipped from the phone. Check it in VS Code to allow it."
        if self.transport.supports_buttons:
            return ""  # the buttons say it all; typing a reply denies with feedback (see /help)
        return ("Reply *1* allow" + ("" if no_session else " · *2* allow in this session")
                + " · *3* skip · *stop* skip & stop agent, or type feedback to skip.")

    def question_footer(self, options: list[str]) -> str:
        if self.transport.supports_buttons:
            return "" if options else "Type your answer below."
        return "Reply with a number or your answer." if options else "Reply with your answer."

    def followup_footer(self) -> str:
        return "Reply here to keep going." if self.transport.supports_buttons else "Reply to keep going, or *ok* to end."

    def _numbered(self, body: str, options: list[str]) -> str:
        """Without buttons (WhatsApp), options are listed in the text instead."""
        if not options or self.transport.supports_buttons:
            return body
        return (body + "\n\n" if body else "") + "\n".join(f"*{i}.* {_inline(o, 80)}" for i, o in enumerate(options, 1))

    def ask(self, kind: str, session_id: str, title: str, text: str, footer: str,
            wait_minutes: float, options: list[str] | None = None, meta: dict | None = None,
            still_pending: Callable[[], bool] | None = None, release_on_return: bool = True,
            heading: str = "") -> Pending:
        p = Pending(next(self.ids), kind, session_id, title, text, options or [], meta=meta or {})
        with self.plock:
            self.pending[p.id] = p
        deadline = time.time() + max(1.0, wait_minutes * 60)
        buttons = self._buttons(p) if self.transport.supports_buttons else None

        def deliver(escalated: bool) -> None:
            head = f"*{heading}*\n" if heading else ""
            body = f"{head}{SUB_OPEN}{title} · #{p.id}{SUB_CLOSE}" + (f"\n\n{text.strip()}" if text.strip() else "") \
                + (f"\n\n{footer}" if footer else "")
            n, sealed = self._seal(buttons) if buttons else ("", None)
            ref = self.transport.send(body, sealed)
            if n:
                self._register_buttons(n, ref)
            with p.lock:
                p.meta["ref"] = ref
                done = p.event.is_set()
            if done and ref:
                self.transport.finalize(ref, p.meta.get("label") or "Closed")

        def to_phone(escalated: bool) -> None:
            with p.lock:
                if p.meta.get("phone_sent") or p.event.is_set():
                    return
                p.meta["phone_sent"] = True
            threading.Thread(target=deliver, args=(escalated,), daemon=True).start()

        # Popup mode: laptop popup first, phone if unanswered in time (or popup closed).
        # Default mode: you are away, so straight to the phone; released back to
        # VS Code's own prompt as soon as you touch the laptop again.
        popup_mode = bool(self.cfg.get("laptop_popup"))
        p.meta["escalate"] = lambda: to_phone(True)
        if popup_mode:
            p.popup = self.popup_launcher(p)
            escalate = float(self.cfg.get("escalate_after_seconds", 60))
            if p.popup is None or escalate <= 0:
                to_phone(False)
            elif not p.event.wait(timeout=min(escalate, max(0.0, deadline - time.time()))):
                to_phone(True)
        else:
            to_phone(False)
        last_check = time.time()
        while not p.event.is_set():
            remaining = deadline - time.time()
            if remaining <= 0 or p.event.wait(timeout=min(1.0, remaining)):
                break
            if still_pending and time.time() - last_check >= 2:
                last_check = time.time()
                if not still_pending():
                    p.resolve({"answered_in_vscode": True}, "laptop", "Answered in VS Code")
                    break
            if release_on_return and not popup_mode and self.idle() < 3:
                back = {"approval": {"decision": "vscode"}, "question": {"vscode": True}}.get(kind, {"end": True})
                if p.resolve(back, "laptop", "Moved to VS Code"):
                    log.info("#%s released to VS Code (laptop activity)", p.id)
        if p.resolve({"timeout": True}, "timeout", outcome_label(kind, {"timeout": True}, "timeout")) \
                and not self.transport.supports_buttons and p.meta.get("phone_sent"):
            self.send(f"⌛ #{p.id} timed out — falling back to the normal VS Code prompt.")
        with self.plock:
            self.pending.pop(p.id, None)
        with p.lock:
            ref = p.meta.get("ref")
        if ref:
            threading.Thread(target=self.transport.finalize, args=(ref, p.meta.get("label") or "Closed"),
                             daemon=True).start()
        if p.popup and p.popup.poll() is None:
            try:
                p.popup.kill()
            except OSError:
                pass
        return p

    def answer(self, p: Pending, text: str, by: str, raw: dict | None = None) -> None:
        raw = raw or {}
        if p.kind == "approval":
            decision, reason = (raw.get("decision"), raw.get("reason", "")) if raw.get("decision") else parse_approval(text)
            if decision in ("allow", "session") and p.meta.get("no_allow") and by != "laptop":
                self.send(f"⚠️ #{p.id} can't be allowed from the phone because part of it wasn't shown. "
                          "Check it in VS Code to allow it, or deny it here.")
                return
            if decision == "session" and p.meta.get("no_session"):
                decision = "allow"  # session approval isn't offered for this one; allow just once
            result = {"decision": decision, "reason": reason}
        elif p.kind == "question":
            if raw.get("vscode"):
                result = {"vscode": True}
            else:
                t = text.strip()
                if t.isdigit() and p.options and 1 <= int(t) <= len(p.options):
                    t = p.options[int(t) - 1]
                result = {"text": t}
        else:  # followup
            t = text.strip()
            result = {"end": True} if (raw.get("end") or t.lower().rstrip(".!") in END_TURN or not t) else {"prompt": t}
        if p.resolve(result, by, outcome_label(p.kind, result, by)):
            log.info("#%s answered by %s: %s", p.id, by, result)
            if self.transport.supports_buttons:
                return  # the message's buttons are replaced with the outcome instead
            if by == "laptop" and p.meta.get("phone_sent"):
                self.send(f"💻 #{p.id} was answered on the laptop.")
            elif by == self.transport.name:
                self.send(f"👌 #{p.id} received.")

    # -- inbound phone messages -------------------------------------------------
    def handle_inbound(self, body: str) -> None:
        text = (body or "").strip()
        if not text:
            return
        log.info("Phone in: %s", text[:200])
        low = text.lower()
        if low.startswith("/"):  # Telegram-style commands: /status, /help@MyBot
            low = low[1:].split("@")[0].split(" ")[0]
        with self.plock:
            pend = sorted(self.pending.values(), key=lambda x: x.id)
        m = re.match(r"^#(\d+)\s*(.*)$", text, re.S)
        if m:
            target = next((x for x in pend if x.id == int(m.group(1))), None)
            if not target:
                self.send(f"#{m.group(1)} is no longer pending. Send *status* to list open requests.")
                return
            self.answer(target, m.group(2), by=self.transport.name)
            return
        if low in ("help", "?", "commands", "start"):
            self.send(HELP_TEXT)
            return
        if low == "sessions":
            self.cmd_sessions(show_all=text.lower().split()[-1] == "all")
            return
        if low == "session" and text.startswith("/"):
            self.cmd_session()
            return
        if low == "use" and text.startswith("/"):
            self.cmd_use(text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else "")
            return
        if low == "prompt" and text.startswith("/"):
            rest = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
            if rest:
                self.send_prompt(rest)
            else:
                self.send("Usage: /prompt <what the agent should do>")
            return
        if low in ("status", "list", "pending"):
            if not pend:
                self.send(f"Nothing pending. Remote mode: {'⏸️ paused' if self.paused else '▶️ on'}.")
            else:
                self.send("Pending:\n" + "\n".join(f"#{x.id} {x.kind} — {x.title}" for x in pend))
            return
        if low == "pause":
            self.paused = True
            self.send("⏸️ Paused. New requests stay on the laptop only. Send *resume* to re-enable.")
            return
        if low == "resume":
            self.paused = False
            self.send("▶️ Resumed. Requests will come to your phone and the laptop.")
            return
        if not pend:
            if self.active:
                self.send_prompt(text)
            else:
                self.send("Nothing is waiting for you right now. Send /sessions to pick a VS Code session "
                          "and prompt it from here, or *help* for commands.")
            return
        if len(pend) > 1:
            self.send("Several requests are open: " + ", ".join(f"#{x.id}" for x in pend)
                      + f"\nPrefix your reply with the id, e.g. *#{pend[-1].id} 1*")
            return
        self.answer(pend[0], text, by=self.transport.name)

    def poll_loop(self) -> None:
        if not self.transport.configured:
            return
        while not self.stop_event.is_set():
            for item in self.transport.poll():
                try:
                    if isinstance(item, dict):
                        self.handle_callback(item.get("data", ""), item.get("message_id"))
                    else:
                        self.handle_inbound(item)
                except Exception:
                    log.exception("inbound handling failed")
            if self.transport.long_poll:
                continue  # poll() itself waits for new messages
            with self.plock:
                busy = bool(self.pending)
            self.stop_event.wait(self.cfg["poll_seconds_active"] if busy else self.cfg["poll_seconds_idle"])

    # -- hook handlers ---------------------------------------------------------
    def handle_hook(self, event: str, payload: dict) -> dict:
        if self.cfg.get("debug_payloads"):
            with open(HERE / "hook-payloads.log", "a", encoding="utf-8") as f:
                f.write(json.dumps({"event": event, "payload": payload})[:4000] + "\n")
        if self.paused:
            return {}
        handler = {
            "permissionRequest": self.on_permission,
            "preToolUse": self.on_pre_tool,
            "agentStop": self.on_agent_stop,
            "notification": self.on_notification,
        }.get(event)
        return handler(payload) if handler else {}

    def on_permission(self, payload: dict) -> dict:
        sid = payload.get("sessionId") or payload.get("session_id") or ""
        cwd = payload.get("cwd") or ""
        p = normalize_permission(payload)
        key = permission_key(p["kind"], p["tool"], p["server"])
        gkey = grant_key(p)
        sensitive = self.cfg["sensitive_write_globs"]
        grant_ok = session_grant_ok(p, cwd, sensitive)
        if p["kind"] in ("read", "hook", "custom-tool") or p["tool"] in self.cfg["passthrough_tools"]:
            # custom-tool: VS Code approves most of these silently. If it does show a prompt,
            # scan_loop forwards it to the phone (with working buttons) once you're away.
            return {}
        if p["args"].get("requestSandboxBypass"):
            return {}  # must be confirmed interactively in VS Code
        if grant_ok and gkey in self.session_allow.get(sid, set()):
            return {"behavior": "allow"}
        if p["kind"] == "write":
            fname = _first(p["args"], "fileName", "path", "file_path", "filePath")
            if not write_needs_approval(fname, cwd, sensitive):
                return {}
        if key in host_policy_keys(sid):
            return {}  # VS Code is already auto-approving this in the session
        if self._at_laptop():
            return {}  # you're using the laptop: VS Code shows its normal prompt (scanner covers walk-aways)
        detail, cut = describe_permission(p, self._phone_limit())
        detail += context_block(*request_context(sid))
        title = session_title(sid, cwd)
        req = self.ask("approval", sid, title, detail, self.approval_footer(cut, not grant_ok),
                       self.cfg["approval_wait_minutes"], ["Allow once", "Allow for session", "Deny"],
                       meta={"no_allow": cut, "no_session": not grant_ok}, heading=permission_heading(p))
        r = req.result or {}
        if r.get("timeout") or r.get("decision") == "vscode":
            return {}
        d = r.get("decision")
        if d == "session" and grant_ok:
            self.session_allow.setdefault(sid, set()).add(gkey)
            return {"behavior": "allow"}
        if d in ("allow", "session"):
            return {"behavior": "allow"}
        msg = "Denied by the user" + (f" via {req.answered_by}" if req.answered_by else "")
        if r.get("reason"):
            msg += f". The user said: {r['reason']}"
        out = {"behavior": "deny", "message": msg}
        if d == "stop":
            out["interrupt"] = True
        return out

    def on_pre_tool(self, payload: dict) -> dict:
        tool = payload.get("toolName") or payload.get("tool_name") or ""
        if tool not in ("ask_user", "AskUserQuestion", "vscode_askQuestions"):
            return {}
        sid = payload.get("sessionId") or payload.get("session_id") or ""
        args = payload.get("toolArgs") or payload.get("tool_input") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {"question": args}
        cwd = payload.get("cwd", "")
        if (self.cfg.get("questions_via_chat") and float(self.cfg.get("turn_hold_minutes", 0)) > 0
                and not self.cfg.get("hold_in_hook") and not self.cfg.get("laptop_popup") and not self.paused):
            with self.plock:
                self.question_for[sid] = args
            return {
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "Don't use the question form here. Ask this question as a normal chat message instead "
                    "(list any options as a numbered list) and then end your turn. The user will reply in the "
                    "chat or from their phone; treat that reply as their answer."),
            }
        if self._at_laptop():
            return {}  # you're using the laptop: VS Code shows its normal question form
        heading, text, options = question_card(args)
        text = (self._numbered(text, options) + context_block(request_context(sid)[0], "")).strip("\n")
        req = self.ask("question", sid, session_title(sid, cwd), text, self.question_footer(options),
                       self.cfg["question_wait_minutes"], options, heading=heading)
        r = req.result or {}
        if r.get("timeout") or r.get("vscode"):
            return {}
        return {
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"The user answered this question remotely ({req.answered_by}) instead of through the form. "
                f"Treat the following as their answer and continue: {r.get('text', '')}"),
        }

    def on_agent_stop(self, payload: dict) -> dict:
        if not self.cfg.get("notify_turn_end", True) or not self.transport.configured:
            return {}  # holding a turn only makes sense when the phone can answer
        if not self.cfg.get("laptop_popup") and not self.cfg.get("hold_in_hook", False):
            if float(self.cfg.get("turn_hold_minutes", 0)) > 0:
                return self._hold_turn(payload)
            self._start_turn_watch(payload)
            return {}
        if self._at_laptop():
            return {}  # you're at the laptop and can see the result yourself
        sid = payload.get("sessionId") or payload.get("session_id") or ""
        if not payload.get("stop_hook_active"):
            self.consecutive_blocks[sid] = 0
        title = session_title(sid, payload.get("cwd", ""))
        last = last_assistant_message(sid, payload.get("transcriptPath") or payload.get("transcript_path"))
        if len(last) > 900:
            last = last[:900] + "…"
        can_continue = self.consecutive_blocks.get(sid, 0) < 7
        if not can_continue or self.cfg.get("followup_wait_minutes", 0) <= 0:
            self.send(f"✅ {title} finished.\n\n{last}")
            return {}
        req = self.ask("followup", sid, title, last, self.followup_footer(),
                       self.cfg["followup_wait_minutes"], heading="Agent finished")
        r = req.result or {}
        if r.get("prompt"):
            self.consecutive_blocks[sid] = self.consecutive_blocks.get(sid, 0) + 1
            return {"decision": "block", "reason": f"Follow-up instruction from the user (sent via {req.answered_by}): {r['prompt']}"}
        return {}

    def _hold_turn(self, payload: dict) -> dict:
        """Reliable path: keep the finished turn open in the agentStop hook. A reply typed in VS Code
        wins if it comes first (the hook lets the turn end so the message runs). Otherwise, after
        push_after_seconds, the phone gets it, and a phone reply continues the turn via the hook."""
        sid = payload.get("sessionId") or payload.get("session_id") or ""
        if not sid:
            return {}
        cwd = payload.get("cwd") or session_cwd(sid)
        if not payload.get("stop_hook_active"):
            self.consecutive_blocks[sid] = 0
        if self.consecutive_blocks.get(sid, 0) >= 7:  # runtime allows 8 forced continuations in a row
            return {}
        stop_at = time.time()
        with self.plock:
            q = self.question_for.pop(sid, None)
        phone_driven = self._consume_phone_prompt(cwd)
        hold_end = stop_at + float(self.cfg.get("turn_hold_minutes", 0)) * 60

        def replied_in_vscode() -> bool:
            return event_after(sid, stop_at, ("user.message", "abort"))

        end = stop_at + (0 if phone_driven else self.native_grace)
        while time.time() < end:
            if replied_in_vscode():
                log.info("turn end in %s: answered in VS Code within the delay", sid[:8])
                return {}
            if self.stop_event.wait(1):
                return {}
        if self.paused or not self.transport.configured:
            return {}
        title = session_title(sid, cwd)
        last = last_assistant_message(sid, payload.get("transcriptPath") or payload.get("transcript_path"))
        if len(last) > 900:
            last = last[:900] + "…"
        if q:
            heading, body, options = question_card(q)
            body = self._numbered(body, options)
            footer = self.question_footer(options)
            kind = "question"
        else:
            options, heading, body, footer = [], "Agent finished", last, self.followup_footer()
            kind = "followup"
        req = self.ask(kind, sid, title, body, footer, max(0.1, (hold_end - time.time()) / 60), options,
                       heading=heading, still_pending=lambda: not replied_in_vscode(), release_on_return=False)
        r = req.result or {}
        reply = r.get("prompt") or r.get("text")
        log.info("#%s (held turn in %s) resolved: %s by %s", req.id, sid[:8], r, req.answered_by)
        if reply:
            self.consecutive_blocks[sid] = self.consecutive_blocks.get(sid, 0) + 1
            return {"decision": "block", "reason": f"The user replied from their phone: {reply}"}
        return {}

    def on_notification(self, payload: dict) -> dict:
        return {}  # unanswered VS Code prompts are picked up by scan_loop from the session logs


HELP_TEXT = (
    "🔔 *Agent Doorbell*\n"
    "Approvals: tap a button, or reply *1* allow once · *2* allow for session · *3* deny · *stop* deny & stop agent. "
    "Any other text denies and is sent to the agent as feedback.\n"
    "Questions: tap an option, or reply with a number or free text.\n"
    "Turn finished: type a follow-up prompt, or *ok* to end.\n"
    "Several open? Prefix typed replies with the id: *#4 1*\n\n"
    "Prompt a session from your phone: /sessions, pick one, then just send messages. "
    "/prompt <text> sends a prompt even while requests are open. /session shows the selected one.\n"
    "Commands: /sessions · /session · /prompt · /status · /pause · /resume · /help"
)

BOT_COMMANDS = [
    ("sessions", "List VS Code sessions and pick one to prompt"),
    ("session", "Show which session your messages go to"),
    ("prompt", "Send a prompt to the selected session"),
    ("status", "Show open requests"),
    ("pause", "Stop sending requests to the phone"),
    ("resume", "Start sending requests to the phone again"),
    ("help", "How to use this bot"),
]


# ----------------------------------------------------------------------------
# HTTP server (127.0.0.1 only)
# ----------------------------------------------------------------------------

def make_handler(bridge: Bridge) -> type[BaseHTTPRequestHandler]:
    token = bridge.cfg["token"]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug(fmt, *args)

        def _json(self, code: int, obj: Any) -> None:
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            return secrets.compare_digest(self.headers.get("X-Bridge-Token", ""), token)

        def do_GET(self) -> None:
            if self.path == "/health":
                self._json(200, {"ok": True, "channel": bridge.transport.name, "paused": bridge.paused,
                                 "pending": len(bridge.pending), "vscode_extension": bridge.extension_connected})
            elif self.path.startswith("/vscode/jobs") and self._authorized():
                ext_v = self.headers.get("X-Ext-Version") or ""
                if ext_v != EXT_VERSION:
                    if ext_v.isdigit() and int(ext_v) >= 3:
                        self._json(200, {"jobs": [], "last": 0, "close": True})  # outdated copy closes its window
                        return
                    time.sleep(25)  # very old copy: keep it idle and give it nothing to do
                    self._json(200, {"jobs": [], "last": 0})
                    return
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                try:
                    after = int((q.get("after") or ["-1"])[0])
                except ValueError:
                    after = -1
                instance = re.sub(r"[^\w-]", "", (q.get("instance") or [""])[0])[:40]
                self._json(200, bridge.wait_jobs(after, timeout=25, instance=instance,
                                                 empty=(q.get("empty") or ["0"])[0] == "1"))
            elif self.path == "/debug/outbox" and self._authorized():
                self._json(200, bridge.transport.outbox)
            else:
                self._json(404, {})

        def do_POST(self) -> None:
            if not self._authorized():
                self._json(403, {"error": "bad token"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                payload = {}
            if self.path.startswith("/hook/"):
                event = self.path[len("/hook/"):]
                try:
                    out = bridge.handle_hook(event, payload)
                except Exception:
                    log.exception("hook %s failed", event)
                    out = {}
                self._json(200, out)
            elif self.path == "/debug/inbound":
                bridge.handle_inbound(payload.get("body", ""))
                self._json(200, {"ok": True})
            elif self.path == "/vscode/result":
                log.info("extension job %s: errors=%s info=%s", payload.get("id"),
                         str(payload.get("errors") or [])[:400], json.dumps(payload.get("info") or {})[:600])
                self._json(200, {"ok": True})
            elif self.path == "/debug/job":
                self._json(200, {"id": bridge.post_job(payload)})
            else:
                self._json(404, {})

    return Handler


def keep_awake() -> bool:
    """Ask Windows not to sleep because of inactivity while this thread runs. Released
    automatically when the process exits. The screen can still turn off and lock."""
    if os.name != "nt":
        return False
    es_continuous, es_system_required = 0x80000000, 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(es_continuous | es_system_required))
    return True


_MUTEXES: list[Any] = []


def single_instance(name: str) -> bool:
    """False if another process already holds the named Windows mutex."""
    if os.name != "nt":
        return True
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    handle = k32.CreateMutexW(None, False, name)
    if not handle:
        return True
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        k32.CloseHandle(ctypes.c_void_p(handle))
        return False
    _MUTEXES.append(handle)  # keep it for the life of the process
    return True


class Supervisor:
    """Keeps one bridge running: starts it, restarts it if it exits, and replaces it if it stops
    answering /health. Leaves a bridge that was started some other way alone while it's healthy."""

    def __init__(self, cfg: dict, start: Callable[[bool], Any] | None = None,
                 healthy: Callable[[], bool] | None = None) -> None:
        self.port = int(cfg["port"])
        self.start = start or self._start_child
        self.healthy = healthy or self._healthy
        self.child: Any = None
        self.started_at = 0.0
        self.unhealthy = 0
        self.restarts = 0
        self.backoff = 1.0
        self.seen_running = False

    def _healthy(self) -> bool:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=5) as r:
                return json.loads(r.read().decode() or "{}").get("ok") is True
        except Exception:
            return False

    @staticmethod
    def _start_child(restarted: bool) -> subprocess.Popen:
        env = dict(os.environ)
        env.pop("AGENT_DOORBELL_RESTARTED", None)
        if restarted:
            env["AGENT_DOORBELL_RESTARTED"] = "1"
        return subprocess.Popen([sys.executable, str(HERE / "bridge.py"), "run"], cwd=str(HERE), env=env,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def step(self) -> float:
        """One check. Returns how many seconds to wait before the next one."""
        if self.child is not None:
            code = self.child.poll()
            if code is None:
                if time.time() - self.started_at < 30 or self.healthy():
                    self.unhealthy = 0
                    return 5
                self.unhealthy += 1
                if self.unhealthy < 6:
                    return 5
                log.warning("bridge (PID %s) stopped answering for about 30 s; restarting it", self.child.pid)
                try:
                    self.child.kill()
                    self.child.wait(timeout=10)
                except Exception:
                    pass
                code = "hung"
            uptime = time.time() - self.started_at
            log.warning("bridge exited (%s) after %d s", code, uptime)
            self.child, self.unhealthy = None, 0
            self.restarts += 1
            self.backoff = 2.0 if uptime > 600 else min(self.backoff * 2, 300.0)
            return self.backoff
        if self.healthy():
            self.seen_running = True
            return 30  # a bridge started some other way is running fine
        restarted = self.restarts > 0 or self.seen_running
        self.child = self.start(restarted)
        self.started_at = time.time()
        log.info("started bridge (PID %s)%s", getattr(self.child, "pid", "?"), " after a stop" if restarted else "")
        return 5


def supervise(cfg: dict) -> None:
    if not single_instance("Local\\AgentDoorbellSupervisor"):
        log.info("another supervisor is already running; exiting")
        return
    sup = Supervisor(cfg)
    log.info("supervisor started (PID %s)", os.getpid())
    while True:
        try:
            wait = sup.step()
        except Exception:
            log.exception("supervisor check failed")
            wait = 30.0
        time.sleep(wait)


def serve(cfg: dict) -> None:
    bridge = Bridge(cfg)
    server = ThreadingHTTPServer(("127.0.0.1", int(cfg["port"])), make_handler(bridge))
    server.daemon_threads = True
    threading.Thread(target=bridge.poll_loop, daemon=True).start()
    threading.Thread(target=bridge.scan_loop, daemon=True).start()
    mode = bridge.transport.name if bridge.transport.configured else "DRY-RUN (no phone channel configured; messages only logged)"
    log.info("Agent Doorbell listening on 127.0.0.1:%s — %s", cfg["port"], mode)
    if cfg.get("keep_awake", True) and keep_awake():
        log.info("keeping the laptop awake while the bridge runs")
    if bridge.transport.configured:
        if os.environ.get("AGENT_DOORBELL_RESTARTED"):
            bridge.send("🔁 Agent Doorbell stopped unexpectedly and was restarted. It's online again.")
        else:
            bridge.send("🔔 Agent Doorbell is online. Send *help* for commands.")
    if isinstance(bridge.transport, TelegramTransport):
        try:  # shows the commands in Telegram's "/" menu
            bridge.transport.call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in BOT_COMMANDS]})
        except Exception as e:
            log.warning("setMyCommands failed: %s", e)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stop_event.set()
        server.server_close()


def setup() -> None:
    cfg = load_config()
    tw = cfg["twilio"]
    print("Twilio WhatsApp setup (values are stored only in", CONFIG_PATH, ")")
    tw["account_sid"] = input(f"Account SID [{tw['account_sid'] or 'AC...'}]: ").strip() or tw["account_sid"]
    tok = getpass.getpass("Auth Token (hidden, Enter to keep current): ").strip()
    tw["auth_token"] = tok or tw["auth_token"]
    tw["from"] = _wa(input(f"Twilio WhatsApp number [{tw['from']}]: ").strip() or tw["from"])
    tw["to"] = _wa(input(f"Your WhatsApp number incl. country code, e.g. +9198xxxxxxx [{tw['to']}]: ").strip() or tw["to"])
    save_config(cfg)
    print("Saved. Run:  python bridge.py test-send")


def setup_telegram() -> None:
    cfg = load_config()
    tg = cfg["telegram"]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{cfg['port']}/health", timeout=2) as r:
            if json.loads(r.read().decode()).get("channel") == "telegram":
                sys.exit("The bridge is running and polling Telegram. Stop it first: "
                         "powershell -ExecutionPolicy Bypass -File .\\uninstall.ps1")
    except OSError:
        pass
    print("Telegram setup (stored only in", CONFIG_PATH, ")")
    print(" 1. In Telegram, open @BotFather, send /newbot, choose a name and a username ending in 'bot'.")
    print(" 2. Copy the HTTP API token it gives you.")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        token = getpass.getpass("Bot token (hidden input, Enter to keep current): ").strip() or tg["bot_token"]
    if not token:
        sys.exit("No token entered.")
    t = TelegramTransport(token, "")
    try:
        me = t.call("getMe")
        last = t.call("getUpdates", {"offset": -1, "timeout": 0})
    except Exception as e:
        sys.exit(f"Telegram rejected the token: {e}")
    offset = last[-1]["update_id"] + 1 if last else 0
    code = secrets.token_hex(4)
    print(f"\n 3. On your phone open  https://t.me/{me['username']}?start={code}  and tap START")
    print(f"    (or send the bot this message:  /start {code} )\nWaiting up to 5 minutes...")
    deadline = time.time() + 300
    while time.time() < deadline:
        for u in t.call("getUpdates", {"offset": offset, "timeout": 25, "allowed_updates": ["message"]}, timeout=40):
            offset = u["update_id"] + 1
            m = u.get("message") or {}
            if (m.get("text") or "").strip() == f"/start {code}" and (m.get("chat") or {}).get("type") == "private":
                t.call("getUpdates", {"offset": offset, "timeout": 0})  # acknowledge
                tg["bot_token"], tg["chat_id"] = token, str(m["chat"]["id"])
                save_config(cfg)
                TelegramTransport(token, tg["chat_id"]).send(
                    "✅ Paired with Agent Doorbell. Approvals, questions and finished turns will show up here.")
                print(f"Paired with {(m.get('from') or {}).get('first_name', 'you')} (chat {tg['chat_id']}).")
                print("Now (re)start the bridge:  powershell -ExecutionPolicy Bypass -File .\\install.ps1")
                return
    sys.exit("Timed out waiting for the /start message. Run setup-telegram again.")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    # The supervisor logs to its own file so two processes never rotate the same log.
    log_path = HERE / "supervisor.log" if cmd == "supervise" else LOG_PATH
    handlers: list[logging.Handler] = [logging.handlers.RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")]
    if sys.stdout is not None and sys.stdout.isatty():
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers, format="%(asctime)s %(levelname)s %(message)s")
    if cmd == "setup":
        setup()
    elif cmd == "setup-telegram":
        setup_telegram()
    elif cmd == "supervise":
        supervise(load_config())
    elif cmd == "test-send":
        cfg = load_config()
        b = Bridge(cfg)
        if not b.transport.configured:
            sys.exit("No phone channel configured. Run: python bridge.py setup-telegram")
        ok = b.transport.send("✅ Test from Agent Doorbell. Send *help* any time.")
        print(f"Sent via {b.transport.name}!" if ok else f"Failed: {b.transport.last_error}")
    else:
        serve(load_config())


if __name__ == "__main__":
    main()
