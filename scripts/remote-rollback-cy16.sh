#!/usr/bin/env bash
set -euo pipefail
umask 077

APP_ROOT=/home/ubuntu/upstream-balance
DATA_DIR="$APP_ROOT/data"
ENV_FILE="$APP_ROOT/.env"
COMPOSE_FILE="$APP_ROOT/docker-compose.cy16.yml"
RELEASE_ENV="$APP_ROOT/.release.env"
CONTAINER=upstream-balance
PUBLIC_URL=https://ai.sandboxai.top/upstream-balance/
BACKUP_ID=${BACKUP_ID:?BACKUP_ID is required}
LOCK_FILE="$APP_ROOT/.deploy.lock"
AUDIT_LOG="$APP_ROOT/deployments.log"
NOW=$(date -u +%Y%m%dT%H%M%SZ)
EMERGENCY_DIR="$APP_ROOT/backups/manual-rollback-$NOW"
EMERGENCY_IMAGE="light-metapi:forward-recovery-$NOW"
CURL_COMMON=(--connect-timeout 5 --max-time 20 --fail --silent --show-error)
EMERGENCY_READY=0
ROLLBACK_CUTOVER=0
ROLLBACK_SUCCESS=0
FORWARD_RECOVERED=0

log() {
  printf '[%s] %s\n' "$(date -u +%FT%TZ)" "$*"
}

audit() {
  printf '%s operator=%s backup=%s status=%s\n' \
    "$(date -u +%FT%TZ)" "$(id -un)" "$BACKUP_ID" "$1" >> "$AUDIT_LOG" || return 1
  chmod 600 "$AUDIT_LOG" || return 1
}

normalize_ownership() {
  local target=$1 image=$2
  docker run --rm --user 0:0 --entrypoint python \
    -v "$target:/target" "$image" -c '
import os
for root, dirs, files in os.walk("/target"):
    os.chown(root, 1000, 1000)
    os.chmod(root, 0o700)
    for name in dirs:
        path=os.path.join(root, name)
        os.chown(path, 1000, 1000)
        os.chmod(path, 0o700)
    for name in files:
        path=os.path.join(root, name)
        os.chown(path, 1000, 1000)
        os.chmod(path, 0o600)
' || return 1
}

restore_compose_state() {
  local source_dir=$1 state
  [ -f "$source_dir/compose-state.txt" ] || return 1
  state=$(< "$source_dir/compose-state.txt")
  case "$state" in
    present)
      [ -f "$source_dir/docker-compose.cy16.yml" ] || return 1
      cp -a "$source_dir/docker-compose.cy16.yml" "$COMPOSE_FILE" || return 1
      chmod 600 "$COMPOSE_FILE" || return 1
      ;;
    absent)
      if [ -e "$COMPOSE_FILE" ]; then
        [ -f "$COMPOSE_FILE" ] || return 1
        find "$COMPOSE_FILE" -maxdepth 0 -type f -delete || return 1
      fi
      ;;
    *) return 1 ;;
  esac
}

write_release_env() {
  local image=$1
  {
    printf 'UPSTREAM_BALANCE_IMAGE=%s\n' "$image"
    printf 'UPSTREAM_BALANCE_DATA_DIR=%s\n' "$DATA_DIR"
    printf 'UPSTREAM_BALANCE_ENV_FILE=%s\n' "$ENV_FILE"
    printf 'UPSTREAM_BALANCE_CONTAINER_NAME=%s\n' "$CONTAINER"
    printf 'UPSTREAM_BALANCE_BIND=%s\n' '127.0.0.1:8756'
  } > "$RELEASE_ENV" || return 1
  chmod 600 "$RELEASE_ENV" || return 1
}

restore_files() {
  local source_dir=$1 image=$2
  ROLLBACK_CUTOVER=1
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  docker run --rm --user 0:0 --entrypoint python \
    -v "$DATA_DIR:/data" -v "$source_dir:/backup:ro" "$image" -c '
import os, shutil
for name in ("upstreams.sqlite3-wal", "upstreams.sqlite3-shm"):
    path="/data/"+name
    if os.path.exists(path):
        os.unlink(path)
for name in ("upstreams.sqlite3", "secret.key", "session.secret"):
    shutil.copyfile("/backup/"+name, "/data/"+name)
os.chown("/data", 1000, 1000)
os.chmod("/data", 0o700)
for name in ("upstreams.sqlite3", "secret.key", "session.secret"):
    path="/data/"+name
    os.chown(path, 1000, 1000)
    os.chmod(path, 0o600)
' || return 1
  write_release_env "$image" || return 1
  restore_compose_state "$source_dir" || return 1
  UPSTREAM_BALANCE_IMAGE="$image" \
  UPSTREAM_BALANCE_DATA_DIR="$DATA_DIR" \
  UPSTREAM_BALANCE_ENV_FILE="$ENV_FILE" \
  UPSTREAM_BALANCE_CONTAINER_NAME="$CONTAINER" \
  UPSTREAM_BALANCE_BIND=127.0.0.1:8756 \
    docker compose --project-name upstream-balance \
      -f "$source_dir/rollback-compose.yml" \
      up -d --no-build --wait --wait-timeout 90 --no-deps upstream-balance || return 1
}

verify_public() {
  local expected_counts=$1
  curl "${CURL_COMMON[@]}" http://127.0.0.1:8756/api/health >/dev/null || return 1
  local bootstrap counts page
  counts=$(docker exec "$CONTAINER" python -c '
import sqlite3
db=sqlite3.connect("/app/data/upstreams.sqlite3")
result=db.execute("pragma quick_check").fetchone()[0]
tables={row[0] for row in db.execute("select name from sqlite_master where type=\"table\"")}
def count(name):
    return db.execute("select count(*) from "+name).fetchone()[0] if name in tables else 0
print("{}|{}|{}".format(result, count("users"), count("channels")))
db.close()') || return 1
  [ "$counts" = "ok|$expected_counts" ] || {
    log "rollback verification mismatch: expected=ok|$expected_counts actual=$counts"
    return 1
  }
  page=$(curl "${CURL_COMMON[@]}" "$PUBLIC_URL") || return 1
  [ -n "$page" ] || return 1
  bootstrap=$(curl "${CURL_COMMON[@]}" "${PUBLIC_URL}_ub_api/auth/bootstrap") || return 1
  python3 -c 'import json,sys; p=json.loads(sys.stdin.read()); assert p["ok"] is True; assert p["data"]["needs_setup"] is False' <<< "$bootstrap" || return 1
}

validate_rollback_compose() {
  local source_dir=$1 image=$2 policy_dir resolved
  policy_dir=$(mktemp -d /tmp/upstream-balance-rollback-policy.XXXXXX) || return 1
  resolved="$policy_dir/resolved.json"
  : > "$policy_dir/runtime.env" || {
    find "$policy_dir" -depth -delete 2>/dev/null || true
    return 1
  }
  if ! UPSTREAM_BALANCE_IMAGE="$image" \
    UPSTREAM_BALANCE_DATA_DIR="$DATA_DIR" \
    UPSTREAM_BALANCE_ENV_FILE="$policy_dir/runtime.env" \
    UPSTREAM_BALANCE_CONTAINER_NAME="$CONTAINER" \
    UPSTREAM_BALANCE_BIND=127.0.0.1:8756 \
      docker compose -f "$source_dir/rollback-compose.yml" config --format json > "$resolved"; then
    find "$policy_dir" -depth -delete 2>/dev/null || true
    return 1
  fi
  if ! python3 "$source_dir/validate-compose-policy.py" "$resolved" "$image" "$DATA_DIR"; then
    find "$policy_dir" -depth -delete 2>/dev/null || true
    return 1
  fi
  find "$policy_dir" -depth -delete || return 1
}

recover_forward() {
  log "restoring the state captured immediately before manual rollback" || true
  restore_files "$EMERGENCY_DIR" "$EMERGENCY_IMAGE" || return 1
  verify_public "$CURRENT_COUNTS" || return 1
  FORWARD_RECOVERED=1
  audit forward_restore_ok || true
}

# shellcheck disable=SC2329 # invoked by trap
on_exit() {
  local status=$?
  trap - EXIT HUP INT TERM
  if [ "$status" -ne 0 ] && [ "$ROLLBACK_CUTOVER" = "1" ] && \
     [ "$ROLLBACK_SUCCESS" = "0" ] && [ "$EMERGENCY_READY" = "1" ] && \
     [ "$FORWARD_RECOVERED" = "0" ]; then
    if recover_forward; then
      audit interrupted_manual_rollback_forward_restore_ok || true
    else
      audit interrupted_forward_restore_failed || true
    fi
  fi
  exit "$status"
}

trap on_exit EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

[ "$(hostname)" = "ecsPIIB" ] || { log "FATAL: unexpected host $(hostname)"; exit 1; }
[ "$(id -u):$(id -g)" = "1000:1000" ] || { log "FATAL: unexpected operator UID:GID"; exit 1; }
[[ "$BACKUP_ID" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{40}$ ]] || { log "FATAL: invalid backup ID"; exit 1; }
BACKUP_DIR="$APP_ROOT/backups/$BACKUP_ID"
[ -d "$BACKUP_DIR" ] || { log "FATAL: backup does not exist"; exit 1; }
for required in upstreams.sqlite3 secret.key session.secret rollback-image.txt manifest.sha256 \
  rollback-compose.yml compose-state.txt remote-rollback-cy16.sh \
  remote-run-rollback-cy16.sh validate-compose-policy.py; do
  [ -f "$BACKUP_DIR/$required" ] || { log "FATAL: incomplete backup: $required"; exit 1; }
done
(cd "$BACKUP_DIR" && sha256sum --check manifest.sha256)
case "$(< "$BACKUP_DIR/compose-state.txt")" in
  present) [ -f "$BACKUP_DIR/docker-compose.cy16.yml" ] || { log "FATAL: previous Compose is missing"; exit 1; } ;;
  absent) ;;
  *) log "FATAL: invalid Compose state"; exit 1 ;;
esac

ROLLBACK_IMAGE=$(< "$BACKUP_DIR/rollback-image.txt")
[[ "$ROLLBACK_IMAGE" =~ ^light-metapi:rollback-[0-9]{8}T[0-9]{6}Z$ ]] || { log "FATAL: invalid rollback image"; exit 1; }
[ -f "$BACKUP_DIR/rollback-image.tar.gz" ] || { log "FATAL: rollback image is unavailable"; exit 1; }
# Always reload the manifest-verified archive so a mutable local tag cannot
# substitute a different image for the audited backup.
docker load --input "$BACKUP_DIR/rollback-image.tar.gz" >/dev/null
docker image inspect "$ROLLBACK_IMAGE" >/dev/null
[ -f "$ENV_FILE" ] && [ -f "$COMPOSE_FILE" ] && [ -d "$DATA_DIR" ] || { log "FATAL: production files are incomplete"; exit 1; }
validate_rollback_compose "$BACKUP_DIR" "$ROLLBACK_IMAGE" || { log "FATAL: rollback Compose policy failed"; exit 1; }

docker run --rm --user 1000:1000 --entrypoint python -v "$BACKUP_DIR:/backup:ro" "$ROLLBACK_IMAGE" -c '
import sqlite3
db=sqlite3.connect("file:/backup/upstreams.sqlite3?mode=ro", uri=True)
result=db.execute("pragma integrity_check").fetchone()[0]
db.close()
raise SystemExit(0 if result == "ok" else "backup integrity check failed: " + str(result))'
TARGET_COUNTS=$(docker run --rm --user 1000:1000 --entrypoint python -v "$BACKUP_DIR:/backup:ro" "$ROLLBACK_IMAGE" -c '
import sqlite3
db=sqlite3.connect("file:/backup/upstreams.sqlite3?mode=ro", uri=True)
tables={row[0] for row in db.execute("select name from sqlite_master where type=\"table\"")}
def count(name):
    return db.execute("select count(*) from "+name).fetchone()[0] if name in tables else 0
print("{}|{}".format(count("users"), count("channels")))
db.close()')

exec 9>"$LOCK_FILE"
flock -n 9 || { log "FATAL: another deployment is running"; exit 1; }
audit rollback_started

CURRENT_IMAGE_ID=$(docker inspect "$CONTAINER" --format '{{.Image}}')
docker tag "$CURRENT_IMAGE_ID" "$EMERGENCY_IMAGE"
CURRENT_COUNTS=$(docker exec "$CONTAINER" python -c '
import sqlite3
db=sqlite3.connect("/app/data/upstreams.sqlite3")
tables={row[0] for row in db.execute("select name from sqlite_master where type=\"table\"")}
def count(name):
    return db.execute("select count(*) from "+name).fetchone()[0] if name in tables else 0
print("{}|{}".format(count("users"), count("channels")))
db.close()')
mkdir -p "$EMERGENCY_DIR"
chmod 700 "$EMERGENCY_DIR"
TEMP_DB="/app/data/.manual-rollback-$NOW.sqlite3"
docker exec -i "$CONTAINER" python - "$TEMP_DB" <<'PY'
import os, sqlite3, sys
source=sqlite3.connect("/app/data/upstreams.sqlite3")
target=sqlite3.connect(sys.argv[1])
source.backup(target)
result=target.execute("pragma integrity_check").fetchone()[0]
target.execute("pragma wal_checkpoint(truncate)")
mode=target.execute("pragma journal_mode=delete").fetchone()[0]
target.close()
source.close()
if result != "ok":
    raise SystemExit("emergency backup integrity check failed: " + str(result))
if str(mode).lower() != "delete":
    raise SystemExit("emergency backup journal mode was not normalized: " + str(mode))
for suffix in ("-wal", "-shm"):
    candidate=sys.argv[1]+suffix
    if os.path.exists(candidate):
        os.unlink(candidate)
os.chmod(sys.argv[1], 0o600)
PY
docker cp "$CONTAINER:$TEMP_DB" "$EMERGENCY_DIR/upstreams.sqlite3"
docker cp "$CONTAINER:/app/data/secret.key" "$EMERGENCY_DIR/secret.key"
docker cp "$CONTAINER:/app/data/session.secret" "$EMERGENCY_DIR/session.secret"
docker exec -i "$CONTAINER" python - "$TEMP_DB" <<'PY'
import os, sys
for suffix in ("", "-wal", "-shm"):
    candidate=sys.argv[1]+suffix
    if os.path.exists(candidate):
        os.unlink(candidate)
PY
cp -a "$COMPOSE_FILE" "$EMERGENCY_DIR/rollback-compose.yml"
cp -a "$BACKUP_DIR/validate-compose-policy.py" "$EMERGENCY_DIR/validate-compose-policy.py"
cp -a "$COMPOSE_FILE" "$EMERGENCY_DIR/docker-compose.cy16.yml"
printf 'present\n' > "$EMERGENCY_DIR/compose-state.txt"
normalize_ownership "$EMERGENCY_DIR" "$EMERGENCY_IMAGE"
validate_rollback_compose "$EMERGENCY_DIR" "$EMERGENCY_IMAGE"
(
  cd "$EMERGENCY_DIR"
  sha256sum upstreams.sqlite3 secret.key session.secret rollback-compose.yml \
    validate-compose-policy.py compose-state.txt > manifest.sha256
  if [ -f docker-compose.cy16.yml ]; then
    sha256sum docker-compose.cy16.yml >> manifest.sha256
  fi
  chmod 600 manifest.sha256
)
EMERGENCY_READY=1

log "restoring backup $BACKUP_ID"
if restore_files "$BACKUP_DIR" "$ROLLBACK_IMAGE" && verify_public "$TARGET_COUNTS"; then
  ROLLBACK_SUCCESS=1
  audit rollback_success || true
  log "ROLLBACK_OK backup=$BACKUP_ID forward_recovery=$EMERGENCY_DIR" || true
  exit 0
fi

audit rollback_failed_forward_restore_started || true
if recover_forward; then
  exit 1
fi
audit forward_restore_failed || true
log "FATAL: rollback and forward recovery both failed"
exit 2
