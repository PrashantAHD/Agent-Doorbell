"""Hook entry point for Copilot, Claude and Codex. Usage: python hook.py <eventName>
(Claude/Codex events are passed as claude/<Event> or codex/<Event>).

Forwards the hook payload (stdin) to the local bridge and prints its decision.
Fails open: if the bridge is not running or anything goes wrong, prints {}
so the agent continues with its normal behaviour.

The bridge token is never sent. The hook first checks that whatever listens on the port can prove
it knows the token, signs its request with it, and only uses an answer that is signed back. So a
program that takes over the port while the bridge is stopped gets neither the token nor the
request, and can't approve anything.
"""

import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _mac(token: str, *parts: str) -> str:
    return hmac.new(token.encode(), "\n".join(parts).encode(), hashlib.sha256).hexdigest()


def _digest(data: bytes) -> str:
    return hashlib.sha256(data or b"").hexdigest()


def _proven(token: str, nonce: str, body: bytes, proof: str) -> bool:
    return bool(proof) and hmac.compare_digest(_mac(token, "resp", nonce, _digest(body)), proof)


def main() -> str:
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    raw = sys.stdin.buffer.read()
    cfg_path = Path(os.environ.get("AGENT_DOORBELL_CONFIG", HERE / "config.json"))
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    token, port = str(cfg.get("token", "")), cfg.get("port", 47615)
    if not token:
        return "{}"
    payload = json.loads(raw.decode("utf-8") or "{}")
    if os.environ.get("AGENT_DOORBELL_DEBUG"):
        with open(HERE / "hook-payloads.log", "a", encoding="utf-8") as f:
            f.write(json.dumps({"event": event, "payload": payload})[:4000] + "\n")
    base = f"http://127.0.0.1:{port}"

    # 1. Is the real bridge listening? (nothing about the request is sent yet)
    nonce = secrets.token_hex(16)
    with urllib.request.urlopen(f"{base}/health?nonce={nonce}", timeout=5) as r:
        if not _proven(token, nonce, r.read(), r.headers.get("X-Doorbell-Proof", "")):
            return "{}"

    # 2. Signed request; the answer counts only if it's signed by the bridge.
    path = f"/hook/{event}"
    body = json.dumps(payload).encode()
    nonce, ts = secrets.token_hex(16), str(int(time.time()))
    req = urllib.request.Request(base + path, data=body, method="POST", headers={
        "Content-Type": "application/json", "X-Doorbell-Nonce": nonce, "X-Doorbell-Time": ts,
        "X-Doorbell-Auth": _mac(token, "req", "POST", path, ts, nonce, _digest(body))})
    with urllib.request.urlopen(req, timeout=7 * 24 * 3600) as r:
        data = r.read()
        if not _proven(token, nonce, data, r.headers.get("X-Doorbell-Proof", "")):
            return "{}"
    out = json.loads(data.decode() or "{}")
    return json.dumps(out if isinstance(out, dict) else {})


if __name__ == "__main__":
    try:
        result = main()
    except Exception:
        result = "{}"
    sys.stdout.write(result)
    sys.stdout.flush()
    sys.exit(0)
