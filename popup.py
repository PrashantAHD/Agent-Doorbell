"""Agent Doorbell: optional laptop dialog for a pending request.

Reads a JSON spec on stdin, prints the user's choice as JSON on stdout.
Closing the window prints nothing (the request stays open for WhatsApp).
"""

import json
import sys
import tkinter as tk
from tkinter import scrolledtext

spec = json.loads(sys.stdin.read() or "{}")
kind = spec.get("kind", "approval")
result: dict = {}

root = tk.Tk()
root.title(f"Agent Doorbell  [#{spec.get('id')}]")
root.attributes("-topmost", True)
root.minsize(520, 300)
root.configure(padx=12, pady=10)

header = {"approval": "🔐 Approval needed", "question": "❓ The agent has a question",
          "followup": "✅ Agent finished its turn"}.get(kind, "Copilot")
tk.Label(root, text=header, font=("Segoe UI", 13, "bold")).pack(anchor="w")
tk.Label(root, text=spec.get("title", ""), font=("Segoe UI", 9), fg="#555", wraplength=560, justify="left").pack(anchor="w")
if spec.get("warning"):
    tk.Label(root, text="⚠ " + spec["warning"][:300], fg="#b00", wraplength=560, justify="left").pack(anchor="w", pady=(4, 0))

body = scrolledtext.ScrolledText(root, height=12, wrap="word", font=("Consolas", 9))
body.insert("1.0", spec.get("text", "").replace("```", "").replace("*", ""))
body.configure(state="disabled")
body.pack(fill="both", expand=True, pady=6)

hint = {"approval": "Optional feedback (sent to the agent if you deny):",
        "question": "Your answer:", "followup": "Follow-up prompt (leave empty to end the turn):"}[kind]
tk.Label(root, text=hint).pack(anchor="w")
entry = tk.Text(root, height=3, wrap="word", font=("Segoe UI", 10))
entry.pack(fill="x")


def done(obj: dict) -> None:
    result.update(obj)
    root.destroy()


def typed() -> str:
    return entry.get("1.0", "end").strip()


buttons = tk.Frame(root)
buttons.pack(fill="x", pady=(8, 0))


def btn(label: str, cmd, **kw) -> None:
    tk.Button(buttons, text=label, command=cmd, padx=10, **kw).pack(side="left", padx=(0, 6))


if kind == "approval":
    btn("Allow once", lambda: done({"decision": "allow"}), bg="#2e7d32", fg="white")
    btn("Allow for session", lambda: done({"decision": "session"}))
    btn("Deny", lambda: done({"decision": "deny", "reason": typed()}), bg="#c62828", fg="white")
    btn("Deny & stop agent", lambda: done({"decision": "stop", "reason": typed()}))
    btn("Show VS Code prompt", lambda: done({"decision": "vscode"}))
elif kind == "question":
    for i, opt in enumerate(spec.get("options") or [], 1):
        btn(f"{i}. {opt}"[:40], lambda o=opt: done({"answer": o}))
    btn("Send answer", lambda: typed() and done({"answer": typed()}), bg="#1565c0", fg="white")
    btn("Answer in VS Code", lambda: done({"vscode": True}))
else:
    btn("Send follow-up", lambda: done({"answer": typed()} if typed() else {"end": True}), bg="#1565c0", fg="white")
    btn("End turn", lambda: done({"end": True}))

esc = int(spec.get("escalate_seconds") or 0)
footer_note = (f"Not answered within {esc} s? It is also sent to your phone, and whichever answers first wins. "
               "Closing this window sends it to the phone right away."
               if esc else "Closing this window leaves the decision to your phone.")
tk.Label(root, text=footer_note,
         fg="#777", font=("Segoe UI", 8)).pack(anchor="w", pady=(6, 0))

root.after(100, lambda: (root.lift(), root.focus_force(), root.bell()))
root.mainloop()

if result:
    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()
