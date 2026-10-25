"""Copilot hook entry point. Usage: python hook.py <eventName>

Forwards the hook payload (stdin) to the local bridge and prints its decision.
Fails open: if the bridge is not running or anything goes wrong, prints {}
so VS Code continues with its normal behaviour.
"""

import json
import os
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> str:
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    raw = sys.stdin.buffer.read()
    cfg_path = Path(os.environ.get("AGENT_DOORBELL_CONFIG", HERE / "config.json"))
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    payload = json.loads(raw.decode("utf-8") or "{}")
    if os.environ.get("AGENT_DOORBELL_DEBUG"):
        with open(HERE / "hook-payloads.log", "a", encoding="utf-8") as f:
            f.write(json.dumps({"event": event, "payload": payload})[:4000] + "\n")
    req = urllib.request.Request(
        f"http://127.0.0.1:{cfg.get('port', 47615)}/hook/{event}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Bridge-Token": cfg.get("token", "")},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=4 * 3600) as r:
        out = json.loads(r.read().decode() or "{}")
    return json.dumps(out if isinstance(out, dict) else {})


if __name__ == "__main__":
    try:
        result = main()
    except Exception:
        result = "{}"
    sys.stdout.write(result)
    sys.stdout.flush()
    sys.exit(0)
