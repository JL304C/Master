#!/usr/bin/env bash
# uninstall_linux.sh — strip the Insider block from crontab.
set -euo pipefail
MARK_START="# >>> insider-routines >>>"
MARK_END="# <<< insider-routines <<<"
current="$(crontab -l 2>/dev/null || true)"
stripped="$(printf '%s\n' "$current" | awk -v s="$MARK_START" -v e="$MARK_END" '
  $0==s {skip=1; next}
  $0==e {skip=0; next}
  !skip {print}
')"
printf '%s\n' "$stripped" | crontab -
echo "Insider block removed from crontab. Your scripts + state are untouched."
