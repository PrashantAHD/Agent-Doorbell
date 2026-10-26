"""Spend-Guard sessionStart hook (managed by spend-guard): inexpensive deterministic project context."""

import json
import os
import re
import sys
from pathlib import Path

MAX_CONTEXT = 6000
MAX_NOTES_LINES = 60
MAX_MESSAGE = 700


def _cwd(data):
    value = data.get("cwd") or data.get("workspace") or data.get("workspaceFolder")
    if isinstance(value, dict):
        value = value.get("cwd") or value.get("path")
    return os.path.normcase(os.path.abspath(str(value))) if value else ""


def _workspace(path):
    try:
        for line in (path / "workspace.yaml").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("cwd:"):
                return os.path.normcase(os.path.abspath(line[4:].strip().strip('"')))
    except OSError:
        pass
    return ""


def _message(record, wanted):
    found = []
    def walk(value, key=""):
        if isinstance(value, dict):
            for name, child in value.items():
                low = str(name).lower()
                if isinstance(child, str) and (low in wanted or low.endswith("content")):
                    found.append(child)
                else:
                    walk(child, low)
        elif isinstance(value, list):
            for child in value:
                walk(child, key)
    walk(record)
    text = "\n".join(found).strip()
    safe = [line for line in text.splitlines()
            if not re.search(r"(^|[\\/])(?:config\.json|\.env(?:\b|[.]))", line, re.I)]
    return "\n".join(safe).strip()


def _previous(home, current, cwd):
    candidates = []
    for path in home.glob("session-state/*"):
        if not path.is_dir() or path.name == current or _workspace(path) != cwd:
            continue
        log = path / "events.jsonl"
        if not log.exists():
            continue
        try:
            head = log.read_bytes()[:200000].lower()
        except OSError:
            continue
        if b'"initiator":"sub-agent"' in head or b'"automation"' in head:
            continue
        candidates.append((log.stat().st_mtime, path.name, log))
    if not candidates:
        return ""
    _, sid, log = max(candidates)
    users, final_assistant = [], ""
    try:
        for raw in log.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            kind = str(record.get("type", "")).lower()
            if "user.message" in kind:
                text = _message(record, {"content", "text", "message", "prompt"})
                if text:
                    users.append(text)
            elif "assistant.message" in kind:
                text = _message(record, {"content", "text", "message", "response"})
                if text:
                    final_assistant = text
    except OSError:
        return ""
    parts = [f"Previous chat: {sid}"]
    parts.extend(f"User: {text[-MAX_MESSAGE:]}" for text in users[-8:])
    if final_assistant:
        parts.append(f"Final assistant: {final_assistant[-MAX_MESSAGE:]}")
    return "\n".join(parts)


def main():
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
        current = data.get("sessionId") or data.get("session_id") or ""
        cwd = _cwd(data)
        if not current or not cwd:
            return "{}"
        home = Path(os.environ.get("COPILOT_HOME", Path.home() / ".copilot"))
        notes_path = Path(cwd) / ".github" / "notes.md"
        notes = ""
        if notes_path.exists():
            notes = "\n".join(notes_path.read_text(encoding="utf-8", errors="replace")
                               .splitlines()[-MAX_NOTES_LINES:])
        graph = ""
        graph_path = Path(cwd) / ".github" / "spend-guard-graph" / "GRAPH_REPORT.md"
        if graph_path.exists():
            graph = "\n".join(graph_path.read_text(encoding="utf-8", errors="replace")
                              .splitlines()[:40])
        previous = _previous(home, current, cwd)
        context = "Spend-Guard startup context\n"
        if notes:
            context += "\nProject notes:\n" + notes
        if graph:
            context += "\n\nLocal code graph summary:\n" + graph
        if previous:
            context += "\n\n" + previous
        return json.dumps({"additionalContext": context[-MAX_CONTEXT:]}) if context.strip() else "{}"
    except Exception:
        return "{}"


if __name__ == "__main__":
    sys.stdout.write(main())
