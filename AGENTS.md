# Agent Doorbell: agent guide

Forwards VS Code coding-agent approvals and questions to Telegram and applies phone
answers back. Telegram only (WhatsApp/Twilio support was removed). Windows primary;
macOS/Linux via `install.sh`.

## Map
- `bridge.py`: the local service (127.0.0.1): routing, Telegram transport, question
  forms and buttons, VS Code session-log scanning, signed local API, supervisor,
  `install-hooks`. Large file: search it, then read only the needed range.
- `hook.py`: hook entry for Copilot, Claude (`claude/<Event>`) and Codex (`codex/<Event>`); fails open.
- `vscode-extension/extension.js`: helper extension that presses Accept/Skip; `build_vsix.py` packages it.
- `tests/test_bridge.py`: unittest suite. `install.ps1` / `install.sh` and uninstallers.

## Commands
- Tests: `python -m unittest discover -s tests` (about 30 s; run twice if a timing test flakes).
- Apply changes to the running bridge: `powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1`, then `python bridge.py health`.

## Rules
- Never read or print `config.json` (bot token, signing key) or paste secrets anywhere. `bridge.log` contains command text; quote only what's needed.
- `EXT_VERSION` in `bridge.py` must equal `EXT_VERSION` in `extension.js`; bump `vscode-extension/package.json` version with it.
- Write non-ASCII files (tests, README) with the edit tool or Python UTF-8, not PowerShell `Set-Content` (adds a BOM / mangles `·‹›✓`).
- Commit with `git commit -F <file>` (quotes in `-m` break in PowerShell); include the Co-authored-by trailer. Update the README tests badge when the count changes.
- Every behaviour change gets a test; security-relevant changes must keep the README Security section accurate.

## Model routing (keeps costs down)
New chats here start on a low-cost model (`.vscode/settings.json`). Whichever agent
you are, route every request yourself; the user does not pick agents:
- Code changes beyond a one-line fix: delegate to the `Doorbell Developer` subagent.
- After a change to authentication/signing, Telegram buttons, approvals or session
  grants, the press guard, `hook.py` or `extension.js`: delegate a review to the
  `Doorbell Security Reviewer` subagent and send its findings back to the Developer.
- Tests, reinstall, health checks, logs, docs, git: do it yourself.
Give a subagent everything in one message (goal, files, expected behaviour); it does
not see the conversation.

## Long chats
`.github/hooks/cost_guard.py` adds a "[Cost guard …]" note to your message once this
chat passes ~100k tokens of context. Follow it: continue follow-ups here, but move a
new task to a fresh chat with a short handoff. New chats read `.github/notes.md` first;
after each task, append 1-3 lines with a decision, current state, half-done work or a
gotcha. Condense older entries around 60 lines. Never put secrets or client data there.
For optional code context, run `python "%USERPROFILE%\.copilot\skills\spend-guard\spendguard.py" graph .`.
It is local-only and excludes secret-looking files, symlinks, dependencies and large files; never
point it at client data or generated logs.
