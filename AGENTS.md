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
The `Doorbell` agent (`.github/agents/`) is the entry point and runs on a low-cost model.
If you are any other agent, route the same way: code changes beyond a one-line fix go to
the `Doorbell Developer` subagent; after a change to authentication, signing, buttons,
approvals/grants, the guard, `hook.py` or `extension.js`, get a review from the
`Doorbell Security Reviewer` subagent. Do routine work (tests, install, health, logs, git, docs) yourself.
