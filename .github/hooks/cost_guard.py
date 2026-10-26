"""Cost guard (Copilot userPromptTransformed hook).

Every model call resends the whole chat, so a long chat makes every request expensive.
When this chat's context has grown past a threshold, append a note to the user's
message asking the agent to move a *new* task to a fresh chat (with a short handoff)
instead of carrying the whole history along. Follow-ups continue normally.

Fails open: on any problem it prints {} and the message is left unchanged.
"""

import json
import os
import sys
from pathlib import Path

SOFT_TOKENS = int(os.environ.get("COST_GUARD_TOKENS", "100000"))
HARD_TOKENS = SOFT_TOKENS * 2
DEFAULT_MODEL = "gpt-5.6-luna"


def context_tokens(session_id: str) -> int:
    """Input tokens of this chat's latest main-agent model call (0 if unknown)."""
    home = Path(os.environ.get("COPILOT_HOME", Path.home() / ".copilot"))
    log = home / "session-state" / session_id / "events.jsonl"
    with open(log, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - 4_000_000))
        lines = f.read().split(b"\n")
    for raw in reversed(lines):
        if not raw.startswith(b'{"type":"session.usage_record"'):
            continue
        try:
            usage = json.loads(raw)["data"]["usage"]
        except (ValueError, KeyError):
            continue
        if usage.get("initiator") == "sub-agent":
            continue
        return int(usage.get("inputTokens") or 0)
    return 0


def note(tokens: int) -> str:
    k = tokens // 1000
    urgency = "This chat is very long" if tokens >= HARD_TOKENS else "This chat is getting long"
    return (
        f"\n\n[Cost guard: {urgency} (~{k}k tokens of history are resent with every request). "
        "Decide whether the request above is a NEW task or a follow-up to the current work. "
        "Follow-up: ignore this note and continue. "
        "New task: do not start it here. If you have the create_session tool, call it with "
        f"model \"{DEFAULT_MODEL}\", this workspace, a short title, and a prompt containing a "
        "handoff (goal, key files, decisions and state that matter, max ~15 lines) followed by "
        "the user's request verbatim; then reply in one line with the link to the new chat. "
        "If you don't have that tool, tell the user in one line to start a new chat for it "
        "and give them a 3-line handoff to paste.]"
    )


def main() -> str:
    data = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    session_id = data.get("sessionId") or data.get("session_id") or ""
    content = data.get("transformedPrompt")
    if not session_id or not isinstance(content, str) or not content.strip():
        return "{}"
    tokens = context_tokens(session_id)
    if tokens < SOFT_TOKENS:
        return "{}"
    return json.dumps({"modifiedTransformedPrompt": content + note(tokens)})


if __name__ == "__main__":
    try:
        out = main()
    except Exception:
        out = "{}"
    sys.stdout.write(out)
    sys.stdout.flush()
