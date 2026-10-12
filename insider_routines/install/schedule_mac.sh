#!/usr/bin/env bash
# schedule_mac.sh — register the 7 Insider agents with macOS launchd.
#
# Writes 7 .plist files to ~/Library/LaunchAgents/ and loads them.
# Idempotent: re-running unloads + reloads cleanly.
# Uses the python3 on your PATH (override: PYTHON=/path/to/python3).
#
# Logs land in insider_routines/.state/logs/.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENTS="$ROOT/agents"
LOGS="$ROOT/.state/logs"
LA_DIR="$HOME/Library/LaunchAgents"
PY="${PYTHON:-$(command -v python3 || true)}"

if [[ -z "$PY" ]]; then
  echo "python3 not found on PATH. Install Python 3.10+ first." >&2
  exit 1
fi
if ! "$PY" -c 'import anthropic, dotenv' 2>/dev/null; then
  echo "$PY is missing dependencies. Run: $PY -m pip install -r $ROOT/requirements.txt" >&2
  exit 1
fi

mkdir -p "$LA_DIR" "$LOGS"

write_plist() {
  local name="$1"
  local schedule_xml="$2"
  local label="com.insider-routines.${name}"
  local plist="$LA_DIR/${label}.plist"

  cat >"$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PY}</string>
    <string>${AGENTS}/${name}.py</string>
  </array>
  <key>WorkingDirectory</key><string>${ROOT}</string>
  <key>StandardOutPath</key><string>${LOGS}/${name}.out.log</string>
  <key>StandardErrorPath</key><string>${LOGS}/${name}.err.log</string>
  ${schedule_xml}
</dict>
</plist>
PLIST

  launchctl unload "$plist" 2>/dev/null || true
  launchctl load "$plist"
  echo "  ✓ ${label}"
}

at_hm() {  # daily at HH:MM
  echo "<key>StartCalendarInterval</key><dict><key>Hour</key><integer>$1</integer><key>Minute</key><integer>$2</integer></dict>"
}

weekly_at() {  # weekday (0=Sun, 1=Mon, ...) at HH:MM
  echo "<key>StartCalendarInterval</key><dict><key>Weekday</key><integer>$1</integer><key>Hour</key><integer>$2</integer><key>Minute</key><integer>$3</integer></dict>"
}

every_seconds() {
  echo "<key>StartInterval</key><integer>$1</integer>"
}

echo "Registering Insider agents with launchd (python: $PY)…"
write_plist eddie  "$(at_hm 6 0)"
write_plist maggie "$(weekly_at 0 19 0)"    # Sunday 19:00
write_plist frank  "$(weekly_at 1 8 0)"     # Monday 08:00
write_plist maya   "$(every_seconds 21600)" # every 6h
write_plist janet  "$(at_hm 17 0)"
write_plist sophie "$(every_seconds 1800)"  # every 30m
write_plist ross   "$(every_seconds 1800)"  # every 30m

echo
echo "All 7 agents registered. Logs → $LOGS"
echo "Unregister anytime with: bash $ROOT/install/uninstall_mac.sh"
