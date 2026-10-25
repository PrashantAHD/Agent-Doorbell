#!/usr/bin/env bash
# Removes Agent Doorbell's autostart, hooks and helper extension on macOS/Linux, and stops it.
# config.json (your bot token) is kept; delete the folder to remove everything.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-$(command -v python3 || command -v python || true)}"

PLIST="$HOME/Library/LaunchAgents/com.agentdoorbell.supervisor.plist"
UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/agent-doorbell.service"
AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/agent-doorbell.desktop"

if [ -f "$PLIST" ]; then
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed launch agent"
fi
if [ -f "$UNIT" ]; then
  systemctl --user disable --now agent-doorbell.service >/dev/null 2>&1 || true
  rm -f "$UNIT"
  systemctl --user daemon-reload 2>/dev/null || true
  echo "Removed systemd user service"
fi
rm -f "$AUTOSTART"
pkill -f "$HERE/bridge.py supervise" 2>/dev/null || true
pkill -f "$HERE/bridge.py" 2>/dev/null || true

if [ -n "$PY" ]; then
  (cd "$HERE" && "$PY" bridge.py uninstall-hooks) || true
fi
rm -f "${COPILOT_HOME:-$HOME/.copilot}/hooks/agent-doorbell.json"

if command -v code >/dev/null 2>&1; then
  code --uninstall-extension local.agent-doorbell-helper >/dev/null 2>&1 || true
  code --uninstall-extension local.copilot-phone-bridge >/dev/null 2>&1 || true
fi
echo "Agent Doorbell uninstalled. config.json is kept; delete the folder to remove everything."
