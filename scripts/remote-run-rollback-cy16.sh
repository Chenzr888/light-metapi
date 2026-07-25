#!/usr/bin/env bash
set -euo pipefail
umask 077

BACKUP_ID=${BACKUP_ID:?BACKUP_ID is required}
TASK_DIR=${TASK_DIR:?TASK_DIR is required}
ATTEMPT_ID=${ATTEMPT_ID:?ATTEMPT_ID is required}
[[ "$BACKUP_ID" =~ ^[0-9]{8}T[0-9]{6}Z-([0-9a-f]{40})$ ]] || exit 64
[[ "$ATTEMPT_ID" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{32}$ ]] || exit 64
[ "$TASK_DIR" = "/home/ubuntu/upstream-balance/backups/$BACKUP_ID" ] || exit 64

STATUS_FILE="$TASK_DIR/rollback-$ATTEMPT_ID.status"
STATUS_TEMP="$STATUS_FILE.tmp"
LOG_FILE="$TASK_DIR/rollback-$ATTEMPT_ID.log"
[ ! -e "$STATUS_FILE" ] && [ ! -e "$STATUS_TEMP" ] && [ ! -e "$LOG_FILE" ] || exit 65

set +e
bash "$TASK_DIR/remote-rollback-cy16.sh" > "$LOG_FILE" 2>&1
ROLLBACK_STATUS=$?
set -e
chmod 600 "$LOG_FILE"
{
  printf 'exit_code=%s\n' "$ROLLBACK_STATUS"
  printf 'finished_at=%s\n' "$(date -u +%FT%TZ)"
  printf 'log_file=%s\n' "$LOG_FILE"
} > "$STATUS_TEMP"
chmod 600 "$STATUS_TEMP"
mv "$STATUS_TEMP" "$STATUS_FILE"
exit "$ROLLBACK_STATUS"
