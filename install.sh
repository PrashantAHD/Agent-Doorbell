#!/usr/bin/env bash
# Installs Agent Doorbell for this macOS or Linux user.
#  - writes the Copilot hook (~/.copilot/hooks/agent-doorbell.json), plus Claude (~/.claude/settings.json)
#    and Codex (~/.codex/hooks.json) hooks when those agents are installed
#  - installs the helper VS Code extension
#  - starts the supervisor at login and keeps it running (launchd on macOS, a systemd user service on
#    Linux, or an autostart entry where systemd isn't available); the supervisor restarts a crashed
#    or hung bridge
#  - (re)starts everything
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-$(command -v python3 || command -v python || true)}"
if [ -z "$PY" ]; then echo "Python 3.10+ is required (python3 not found)." >&2; exit 1; fi
PY="$(cd "$(dirname "$PY")" && pwd)/$(basename "$PY")"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || { echo "Python 3.10+ is required." >&2; exit 1; }
cd "$HERE"

# truststore is optional (uses the system certificate store); skip quietly if pip can't install it.
"$PY" -m pip install --quiet --disable-pip-version-check --user -r requirements.txt 2>/dev/null \
  || "$PY" -m pip install --quiet --disable-pip-version-check -r requirements.txt 2>/dev/null \
  || echo "note: couldn't install truststore; using Python's default certificates."
"$PY" -c "import bridge; bridge.load_config()"
chmod 600 config.json

LABEL="com.agentdoorbell.supervisor"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/agent-doorbell.service"
AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/agent-doorbell.desktop"

# Stop anything already running (supervisor first, so it can't restart the bridge).
if [ "$(uname)" = "Darwin" ]; then
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
elif command -v systemctl >/dev/null 2>&1; then
  systemctl --user stop agent-doorbell.service 2>/dev/null || true
fi
pkill -f "$HERE/bridge.py supervise" 2>/dev/null || true
pkill -f "$HERE/bridge.py" 2>/dev/null || true

# Hooks: Copilot always; Claude and Codex too when they're installed for this user.
"$PY" bridge.py install-hooks

# Helper VS Code extension: applies phone answers to prompts that are already showing in VS Code.
VSIX="$("$PY" build_vsix.py)"
if command -v code >/dev/null 2>&1; then
  code --uninstall-extension local.copilot-phone-bridge >/dev/null 2>&1 || true  # name used by older versions
  code --install-extension "$VSIX" --force >/dev/null 2>&1
  echo "Helper extension installed: $VSIX"
else
  echo "warning: 'code' command not found. Install $VSIX manually (Extensions view > ... > Install from VSIX)," >&2
  echo "         and add 'code' to PATH (VS Code: Shell Command: Install 'code' command in PATH)." >&2
fi

# Autostart with crash recovery.
if [ "$(uname)" = "Darwin" ]; then
  mkdir -p "$(dirname "$PLIST")"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$PY</string><string>$HERE/bridge.py</string><string>supervise</string></array>
  <key>WorkingDirectory</key><string>$HERE</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$PATH</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>/dev/null</string>
  <key>StandardErrorPath</key><string>/dev/null</string>
</dict>
</plist>
EOF
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "Launch agent registered: $PLIST (starts at login, restarted if it stops)."
elif command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT" <<EOF
[Unit]
Description=Agent Doorbell: phone approvals for coding agents
After=graphical-session.target

[Service]
ExecStart="$PY" "$HERE/bridge.py" supervise
WorkingDirectory=$HERE
Environment="PATH=$PATH"
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF
  # Idle detection (xprintidle / GNOME) and opening VS Code need the desktop session's environment.
  systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS 2>/dev/null || true
  systemctl --user daemon-reload
  systemctl --user enable --now agent-doorbell.service >/dev/null
  echo "systemd user service enabled: $UNIT (starts at login, restarted if it stops)."
else
  mkdir -p "$(dirname "$AUTOSTART")"
  cat > "$AUTOSTART" <<EOF
[Desktop Entry]
Type=Application
Name=Agent Doorbell
Exec="$PY" "$HERE/bridge.py" supervise
Path=$HERE
X-GNOME-Autostart-enabled=true
NoDisplay=true
EOF
  nohup "$PY" "$HERE/bridge.py" supervise >/dev/null 2>&1 &
  echo "Autostart entry: $AUTOSTART (no systemd user session, so there's no restart if the supervisor itself is killed)."
fi

# Wait for the real bridge (it must prove it knows the token).
H=""
for _ in $(seq 1 30); do
  sleep 0.7
  if H="$("$PY" bridge.py health 2>/dev/null)"; then break; fi
  H=""
done
if [ -z "$H" ]; then
  echo "The bridge didn't start (or another program is using its port). See supervisor.log and bridge.log in $HERE" >&2
  exit 1
fi
echo "Bridge running: $H"
case "$H" in *'"channel": "dry-run"'*)
  echo "warning: no phone channel configured yet (dry-run). Run: $PY bridge.py setup-telegram   then re-run this script." >&2;;
esac
if [ "$(uname)" != "Darwin" ] && ! command -v xprintidle >/dev/null 2>&1 && ! command -v gdbus >/dev/null 2>&1; then
  echo "note: install 'xprintidle' so Claude/Codex requests can reach the phone while you're away." >&2
fi
