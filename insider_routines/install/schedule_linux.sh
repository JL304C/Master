#!/usr/bin/env bash
# schedule_linux.sh — register the 7 Insider agents with crontab.
#
# Writes 7 cron lines inside a managed block. Idempotent: re-running
# strips the block and rewrites it.
# Uses the python3 on your PATH (override: PYTHON=/path/to/python3).
#
# Logs land in insider_routines/.state/logs/.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENTS="$ROOT/agents"
LOGS="$ROOT/.state/logs"
PY="${PYTHON:-$(command -v python3 || true)}"

if [[ -z "$PY" ]]; then
  echo "python3 not found on PATH. Install Python 3.10+ first." >&2
  exit 1
fi
if ! "$PY" -c 'import anthropic, dotenv' 2>/dev/null; then
  echo "$PY is missing dependencies. Run: $PY -m pip install -r $ROOT/requirements.txt" >&2
  exit 1
fi

mkdir -p "$LOGS"

MARK_START="# >>> insider-routines >>>"
MARK_END="# <<< insider-routines <<<"

current="$(crontab -l 2>/dev/null || true)"
stripped="$(printf '%s\n' "$current" | awk -v s="$MARK_START" -v e="$MARK_END" '
  $0==s {skip=1; next}
  $0==e {skip=0; next}
  !skip {print}
')"

run() {  # one cron line: schedule python script >> log 2>&1
  echo "$1 \"$PY\" \"$AGENTS/$2.py\" >> \"$LOGS/$2.cron.log\" 2>&1"
}

block="$(cat <<BLOCK
${MARK_START}
$(run "0 6 * * *"    eddie)
$(run "0 19 * * 0"   maggie)
$(run "0 8 * * 1"    frank)
$(run "0 */6 * * *"  maya)
$(run "0 17 * * *"   janet)
$(run "*/30 * * * *" sophie)
$(run "*/30 * * * *" ross)
${MARK_END}
BLOCK
)"

printf '%s\n\n%s\n' "$stripped" "$block" | crontab -

echo "All 7 agents registered with crontab (python: $PY). Logs → $LOGS"
echo "Inspect: crontab -l"
echo "Uninstall: bash $ROOT/install/uninstall_linux.sh"
