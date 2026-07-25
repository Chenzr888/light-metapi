#!/usr/bin/env bash
set -euo pipefail
umask 077

HOST=cy16
BACKUP_ID=""
CONFIRM=""
NOTIFY="$HOME/ai/api/scripts/notify-lark-deploy.sh"

usage() {
  cat <<'EOF'
Usage:
  scripts/rollback-cy16.sh --backup <YYYYMMDDTHHMMSSZ-release-sha> --confirm cy16:rollback:<YYYYMMDDTHHMMSSZ>

The backup ID is printed by a successful deployment and stored under
/home/ubuntu/upstream-balance/backups on CY16.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --backup) BACKUP_ID=${2:?missing value for --backup}; shift ;;
    --confirm) CONFIRM=${2:?missing value for --confirm}; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "FATAL: unknown argument: $1" >&2; usage; exit 1 ;;
  esac
  shift
done

[[ "$BACKUP_ID" =~ ^([0-9]{8}T[0-9]{6}Z)-([0-9a-f]{40})$ ]] || {
  echo "FATAL: invalid backup ID" >&2
  exit 1
}
TIMESTAMP=${BASH_REMATCH[1]}
RELEASE_SHA=${BASH_REMATCH[2]}
[ "$CONFIRM" = "cy16:rollback:$TIMESTAMP" ] || {
  echo "FATAL: confirmation must be --confirm cy16:rollback:$TIMESTAMP" >&2
  exit 1
}
command -v python3 >/dev/null 2>&1 || { echo "FATAL: missing required command: python3" >&2; exit 1; }

# shellcheck source=/dev/null
source "$HOME/.claude/skills/autonomous-task/scripts/ssh-safe.sh"
TASK_DIR="/home/ubuntu/upstream-balance/backups/$BACKUP_ID"

notify() {
  local status=$1 title=$2 detail=$3
  [ -x "$NOTIFY" ] || return 0
  "$NOTIFY" "$status" "$title" "$detail" >/dev/null 2>&1 || true
}

notify start "开始回滚 — upstream-balance" "host: cy16\nbackup: \`$BACKUP_ID\`"
ATTEMPT_ID="$(date -u +%Y%m%dT%H%M%SZ)-$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
TASK_NAME="upstream-balance-rollback-${RELEASE_SHA:0:12}-$ATTEMPT_ID"
STATUS_FILE="$TASK_DIR/rollback-$ATTEMPT_ID.status"
LOG_FILE="$TASK_DIR/rollback-$ATTEMPT_ID.log"
PID_FILE="$TASK_DIR/rollback-$ATTEMPT_ID.pid"
LAUNCH_LOG="$TASK_DIR/rollback-$ATTEMPT_ID.launch.log"
echo "REMOTE_ROLLBACK_TASK name=$TASK_NAME attempt=$ATTEMPT_ID status=$STATUS_FILE"
ssafe "$HOST" "command -v nohup >/dev/null && command -v setsid >/dev/null"
ssafe "$HOST" "test -f '$TASK_DIR/remote-run-rollback-cy16.sh' && test -f '$TASK_DIR/remote-rollback-cy16.sh'"
ssafe "$HOST" \
  "nohup setsid env BACKUP_ID='$BACKUP_ID' TASK_DIR='$TASK_DIR' ATTEMPT_ID='$ATTEMPT_ID' \
    /bin/bash '$TASK_DIR/remote-run-rollback-cy16.sh' \
    > '$LAUNCH_LOG' 2>&1 < /dev/null & printf '%s\n' \$! > '$PID_FILE'"

ROLLBACK_STATUS=3
for _ in $(seq 1 180); do
  set +e
  REMOTE_STATE=$(ssafe "$HOST" \
    "if [ -f '$STATUS_FILE' ]; then cat '$STATUS_FILE'; elif [ -s '$PID_FILE' ] && kill -0 \$(cat '$PID_FILE') 2>/dev/null; then echo RUNNING; else echo MISSING; fi" 2>/dev/null)
  POLL_STATUS=$?
  set -e
  if [ "$POLL_STATUS" -ne 0 ]; then
    sleep 5
    continue
  fi
  if grep -q '^exit_code=' <<< "$REMOTE_STATE"; then
    ROLLBACK_STATUS=$(awk -F= '/^exit_code=/{print $2}' <<< "$REMOTE_STATE")
    ssafe "$HOST" "tail -n 200 '$LOG_FILE'" || true
    break
  fi
  if [ "$REMOTE_STATE" = "MISSING" ]; then
    ROLLBACK_STATUS=4
    break
  fi
  sleep 5
done
if [ "$ROLLBACK_STATUS" -ne 0 ]; then
  notify fail "回滚失败 — upstream-balance" "host: cy16\nbackup: \`$BACKUP_ID\`\nexit: $ROLLBACK_STATUS"
  exit "$ROLLBACK_STATUS"
fi
notify success "回滚成功 — upstream-balance" "host: cy16\nbackup: \`$BACKUP_ID\`"
echo "ROLLBACK_OK backup=$BACKUP_ID url=https://ai.sandboxai.top/upstream-balance/"
