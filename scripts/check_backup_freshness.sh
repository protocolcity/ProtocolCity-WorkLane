#!/usr/bin/env bash
set -euo pipefail

# Backup freshness monitor (wl-204).
#
# Checks WL_BACKUP_DIR for the most recently modified .db file. If no backup
# has landed in the past THRESHOLD_H hours, files a TP alert ticket
# (priority 2, label ops:backup-stale-alert) so silent backup death does not
# recur undetected. Deduplicates: skips creation when an open alert already
# exists in any active status.
#
# Usage:
#   scripts/check_backup_freshness.sh [--threshold-hours N] [--dry-run]
#
# Env overrides:
#   WL_BACKUP_DIR                    backup destination (default: <city root>/wl-backups)
#   WL_BACKUP_MONITOR_THRESHOLD_H    stale threshold in hours (default: 26)
#   WL_BOARD_URL                     board endpoint (default: http://localhost:8799)
#   WL_BACKUP_MONITOR_PRODUCT        product store for alert ticket (default: worklane)
#                                    (WL_* env name kept as intentional compatibility alias)

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${WL_BACKUP_DIR:-$(cd "$ROOT/.." && pwd)/wl-backups}"
THRESHOLD_H="${WL_BACKUP_MONITOR_THRESHOLD_H:-26}"
BOARD_URL="${WL_BOARD_URL:-http://localhost:8799}"
ALERT_PRODUCT="${WL_BACKUP_MONITOR_PRODUCT:-worklane}"
ALERT_LABEL="ops:backup-stale-alert"
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --threshold-hours)
      shift
      THRESHOLD_H="${1:?--threshold-hours requires a value}"
      ;;
    --threshold-hours=*)
      THRESHOLD_H="${1#--threshold-hours=}"
      ;;
    --dry-run)
      DRY_RUN=1
      ;;
    *)
      echo "Unknown option: $1" >&2
      echo "Usage: $0 [--threshold-hours N] [--dry-run]" >&2
      exit 2
      ;;
  esac
  shift
done

THRESHOLD_SECS=$(( THRESHOLD_H * 3600 ))

if [[ ! -d "$BACKUP_DIR" ]]; then
  echo "check_backup_freshness: backup dir not found: $BACKUP_DIR" >&2
  exit 1
fi

# Find the most recently modified .db backup file across all store subdirs.
NEWEST_MTIME=0
NEWEST_FILE=""
while IFS= read -r -d '' f; do
  mtime="$(stat -f %m "$f" 2>/dev/null || echo 0)"
  if [[ "$mtime" -gt "$NEWEST_MTIME" ]]; then
    NEWEST_MTIME="$mtime"
    NEWEST_FILE="$f"
  fi
done < <(find "$BACKUP_DIR" -maxdepth 2 -name "*.db" -type f -print0 2>/dev/null)

NOW="$(date +%s)"
AGE_SECS=$(( NOW - NEWEST_MTIME ))
AGE_H=$(( AGE_SECS / 3600 ))

if [[ "$NEWEST_MTIME" -eq 0 ]]; then
  STATUS="NO_BACKUPS_FOUND"
  LAST_BACKUP_TS="never"
else
  LAST_BACKUP_TS="$(date -r "$NEWEST_MTIME" '+%Y-%m-%dT%H:%M')"
  if [[ "$AGE_SECS" -lt "$THRESHOLD_SECS" ]]; then
    echo "check_backup_freshness: FRESH — last backup ${LAST_BACKUP_TS} (${AGE_H}h ago; threshold ${THRESHOLD_H}h)"
    exit 0
  fi
  STATUS="STALE"
fi

echo "check_backup_freshness: ${STATUS} — last backup ${LAST_BACKUP_TS} (${AGE_H}h ago; threshold ${THRESHOLD_H}h)"

if [[ "$DRY_RUN" == "1" ]]; then
  echo "check_backup_freshness: [dry-run] would file TP alert (product=${ALERT_PRODUCT} label=${ALERT_LABEL})"
  exit 1
fi

# Board health guard — if :8799 is down, log and exit without cascading noise.
board_code="$(curl -s -o /dev/null -w '%{http_code}' "${BOARD_URL}/admin/overview" 2>/dev/null || echo 000)"
if [[ "$board_code" != "200" ]]; then
  echo "check_backup_freshness: board not reachable (${board_code}); cannot file alert — check TP server" >&2
  exit 2
fi

# Dedup: skip if an open alert exists at any active status.
for check_status in backlog in_review in_progress; do
  existing_count="$(curl -s \
    "${BOARD_URL}/api/admin/tasks?project=${ALERT_PRODUCT}&status=${check_status}&label=${ALERT_LABEL}&limit=5" \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); tasks=d.get("tasks",[]) if isinstance(d,dict) else d if isinstance(d,list) else []; print(len(tasks))' \
    2>/dev/null || echo 0)"
  if [[ "$existing_count" -gt 0 ]]; then
    echo "check_backup_freshness: open alert ticket exists (status=${check_status} count=${existing_count}); skipping"
    exit 1
  fi
done

# File the alert ticket.
TITLE="BACKUP STALE: wl-backups last updated ${LAST_BACKUP_TS} (${AGE_H}h ago)"
DESCRIPTION=$(cat <<DESC_EOF
Automated freshness check (com.worklane.backup-monitor) detected stale backup.

Last backup: ${LAST_BACKUP_TS}
Age: ${AGE_H}h (threshold: ${THRESHOLD_H}h)
Newest file: ${NEWEST_FILE:-none found}
Backup dir: ${BACKUP_DIR}

Action needed: inspect com.worklane.backup status (launchctl print-disabled gui/$(id -u)), check backup.err.log, re-enable and run a catch-up backup if the daemon is disabled or the last run failed.
DESC_EOF
)

PAYLOAD="$(python3 - "$TITLE" "$DESCRIPTION" "$ALERT_PRODUCT" "$ALERT_LABEL" <<'PYEOF'
import json, sys
title, desc, product, label = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
print(json.dumps({
    "title": title,
    "description": desc,
    "project": product,
    "priority": 2,
    "labels": ["ops", "area:launchd", label],
    "author": "backup-monitor",
}))
PYEOF
)"

RESPONSE="$(curl -s -X POST "${BOARD_URL}/api/admin/tasks" \
  -H 'Content-Type: application/json' \
  -d "$PAYLOAD")"

echo "check_backup_freshness: alert filed — ${RESPONSE}"
exit 1
