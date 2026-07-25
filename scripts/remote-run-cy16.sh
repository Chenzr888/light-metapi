#!/usr/bin/env bash
set -euo pipefail
umask 077

RELEASE_SHA=${RELEASE_SHA:?RELEASE_SHA is required}
RELEASE_DIR=${RELEASE_DIR:?RELEASE_DIR is required}
ATTEMPT_ID=${ATTEMPT_ID:?ATTEMPT_ID is required}
IMPORT_FILE=${IMPORT_FILE:-}
IMAGE_ARCHIVE=${IMAGE_ARCHIVE:-}
[[ "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]] || exit 64
[[ "$ATTEMPT_ID" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{32}$ ]] || exit 64
[ "$RELEASE_DIR" = "/home/ubuntu/upstream-balance/releases/$RELEASE_SHA/$ATTEMPT_ID" ] || exit 64

# shellcheck disable=SC2329 # reached from the EXIT/signal cleanup trap
secure_delete() {
  local target=${1:-}
  [ -n "$target" ] && [ -f "$target" ] || return 0
  python3 - "$target" <<'PY'
import os, sys
path=sys.argv[1]
size=os.path.getsize(path)
with open(path, "r+b", buffering=0) as handle:
    handle.write(b"\0" * size)
    handle.flush()
    os.fsync(handle.fileno())
os.unlink(path)
PY
}

# shellcheck disable=SC2329 # reached from the EXIT/signal cleanup trap
cleanup_staging() {
  if [ -n "$IMPORT_FILE" ] && [ "$IMPORT_FILE" = "$RELEASE_DIR/opencode-import.json" ]; then
    secure_delete "$IMPORT_FILE" || true
  fi
  if [ -n "$IMAGE_ARCHIVE" ] && [ "$IMAGE_ARCHIVE" = "$RELEASE_DIR/light-metapi-image.tar.gz" ]; then
    find "$IMAGE_ARCHIVE" -maxdepth 0 -type f -delete 2>/dev/null || true
  fi
}
trap cleanup_staging EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

STATUS_FILE="$RELEASE_DIR/deploy-$ATTEMPT_ID.status"
STATUS_TEMP="$STATUS_FILE.tmp"
LOG_FILE="$RELEASE_DIR/deploy-$ATTEMPT_ID.log"
[ ! -e "$STATUS_FILE" ] && [ ! -e "$STATUS_TEMP" ] && [ ! -e "$LOG_FILE" ] || exit 65

set +e
bash "$RELEASE_DIR/scripts/remote-deploy-cy16.sh" > "$LOG_FILE" 2>&1
DEPLOY_STATUS=$?
set -e
chmod 600 "$LOG_FILE"
{
  printf 'exit_code=%s\n' "$DEPLOY_STATUS"
  printf 'finished_at=%s\n' "$(date -u +%FT%TZ)"
  printf 'log_file=%s\n' "$LOG_FILE"
  if [ "$DEPLOY_STATUS" -eq 0 ]; then
    sed -n 's/.*DEPLOY_OK .*backup=\([^ ]*\).*/backup_path=\1/p' "$LOG_FILE" | tail -n 1
  fi
} > "$STATUS_TEMP"
chmod 600 "$STATUS_TEMP"
mv "$STATUS_TEMP" "$STATUS_FILE"
exit "$DEPLOY_STATUS"
