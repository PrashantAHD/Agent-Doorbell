# Agent Doorbell project notes

Short handoff from previous chats. Append 1-3 lines when a task finishes: a decision,
current state, half-done work or a gotcha. Condense older entries around 60 lines.
Never put secrets or client data here.

- Signed local API and exact-prompt guard are in place.
- Long commands are passed as files; snooze/quiet hours and paged question buttons are supported.
- Claude/Codex and macOS/Linux support remain; Telegram-only transport is intentional.
- Question subtitles do not include session names.
- Model routing uses Luna/Sonnet with an Opus reviewer; the cost guard moves new tasks to fresh chats.
