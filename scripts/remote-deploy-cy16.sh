#!/usr/bin/env bash
set -euo pipefail
umask 077

APP_ROOT=/home/ubuntu/upstream-balance
DATA_DIR="$APP_ROOT/data"
ENV_FILE="$APP_ROOT/.env"
CONTAINER=upstream-balance
PUBLIC_URL=https://ai.sandboxai.top/upstream-balance/
RELEASE_SHA=${RELEASE_SHA:?RELEASE_SHA is required}
ATTEMPT_ID=${ATTEMPT_ID:?ATTEMPT_ID is required}
IMAGE_REF=${IMAGE_REF:?IMAGE_REF is required}
IMAGE_ARCHIVE=${IMAGE_ARCHIVE:?IMAGE_ARCHIVE is required}
IMAGE_ARCHIVE_SHA256=${IMAGE_ARCHIVE_SHA256:?IMAGE_ARCHIVE_SHA256 is required}
RELEASE_DIR=${RELEASE_DIR:?RELEASE_DIR is required}
IMPORT_FILE=${IMPORT_FILE:-}
EXPECTED_OPENCODE=${EXPECTED_OPENCODE:-preserve}
COMPOSE_SOURCE="$RELEASE_DIR/deploy/docker-compose.cy16.yml"
ROLLBACK_COMPOSE_SOURCE="$RELEASE_DIR/deploy/docker-compose.rollback.yml"
COMPOSE_POLICY="$RELEASE_DIR/scripts/validate-compose-policy.py"
COMPOSE_FILE="$APP_ROOT/docker-compose.cy16.yml"
RELEASE_ENV="$APP_ROOT/.release.env"
AUDIT_LOG="$APP_ROOT/deployments.log"
LOCK_FILE="$APP_ROOT/.deploy.lock"
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_DIR="$APP_ROOT/backups/$TIMESTAMP-$RELEASE_SHA"
ROLLBACK_TAG="light-metapi:rollback-$TIMESTAMP"
RAW_ROLLBACK_TAG="light-metapi:rollback-raw-$TIMESTAMP"
CANARY_CONTAINER="upstream-balance-canary-$ATTEMPT_ID"
CANARY_DATA="$RELEASE_DIR/canary-data"
CANARY_BACKUP_DIR="$BACKUP_DIR/canary-source"
CUTOVER_STARTED=0
FINAL_BACKUP_READY=0
DEPLOY_SUCCEEDED=0
ROLLBACK_COMPLETED=0
IMAGE_ARCHIVE_PATH_SAFE=0
IMPORT_PATH_SAFE=0
CURL_COMMON=(--connect-timeout 5 --max-time 20 --fail --silent --show-error)

# Establish cleanup authority from canonical paths before any fallible preflight
# or audit write. Content validation remains a separate deployment gate.
if [[ "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]] && \
   [[ "$ATTEMPT_ID" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{32}$ ]] && \
   [ "$RELEASE_DIR" = "$APP_ROOT/releases/$RELEASE_SHA/$ATTEMPT_ID" ]; then
  [ "$IMAGE_ARCHIVE" = "$RELEASE_DIR/light-metapi-image.tar.gz" ] && IMAGE_ARCHIVE_PATH_SAFE=1
  if [ -n "$IMPORT_FILE" ] && [ "$IMPORT_FILE" = "$RELEASE_DIR/opencode-import.json" ]; then
    IMPORT_PATH_SAFE=1
  fi
fi

log() {
  printf '[%s] %s\n' "$(date -u +%FT%TZ)" "$*"
}

audit() {
  printf '%s operator=%s sha=%s image=%s status=%s\n' \
    "$(date -u +%FT%TZ)" "$(id -un)" "$RELEASE_SHA" "$IMAGE_REF" "$1" >> "$AUDIT_LOG" || return 1
  chmod 600 "$AUDIT_LOG" || return 1
}

secure_delete() {
  local target=${1:-}
  [ -n "$target" ] || return 0
  [ -f "$target" ] || return 0
  python3 - "$target" <<'PY'
import os, sys
path = sys.argv[1]
size = os.path.getsize(path)
with open(path, "r+b", buffering=0) as handle:
    handle.write(b"\0" * size)
    handle.flush()
    os.fsync(handle.fileno())
os.unlink(path)
PY
}

secure_remove_tree() {
  local target=${1:-}
  [ -n "$target" ] || return 0
  [ -d "$target" ] || return 0
  [ "$target" = "$CANARY_DATA" ] || {
    log "refusing to remove unexpected directory: $target"
    return 1
  }
  python3 - "$target" <<'PY'
import os, pathlib, sys
root = pathlib.Path(sys.argv[1])
for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
    if path.is_symlink():
        path.unlink()
    elif path.is_file():
        try:
            size = path.stat().st_size
            with path.open("r+b", buffering=0) as handle:
                handle.write(b"\0" * size)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            path.unlink(missing_ok=True)
    elif path.is_dir():
        path.rmdir()
root.rmdir()
PY
}

cleanup_sensitive_files() {
  docker rm -f "$CANARY_CONTAINER" >/dev/null 2>&1 || true
  secure_remove_tree "$CANARY_DATA" || true
  if [ "$IMPORT_PATH_SAFE" = "1" ]; then
    secure_delete "$IMPORT_FILE" || true
  fi
  if [ "$IMAGE_ARCHIVE_PATH_SAFE" = "1" ]; then
    find "$IMAGE_ARCHIVE" -maxdepth 0 -type f -delete 2>/dev/null || true
  fi
}

on_exit() {
  local status=$?
  trap - EXIT HUP INT TERM
  if [ "$status" -ne 0 ] && [ "$CUTOVER_STARTED" = "1" ] && \
     [ "$DEPLOY_SUCCEEDED" = "0" ] && [ "$ROLLBACK_COMPLETED" = "0" ]; then
    if [ "$FINAL_BACKUP_READY" = "1" ]; then
      if restore_backup; then
        audit interrupted_after_cutover_rollback_ok || true
      else
        audit interrupt_rollback_failed || true
      fi
    elif [ "$FINAL_BACKUP_READY" = "0" ]; then
      if docker start "$CONTAINER" >/dev/null 2>&1; then
        audit interrupted_before_backup_old_container_restarted || true
      else
        audit old_container_restart_failed || true
      fi
    fi
  fi
  cleanup_sensitive_files || true
  exit "$status"
}

trap on_exit EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

validate_compose_contract() {
  local policy_env="$RELEASE_DIR/compose-policy.env"
  local candidate_json="$RELEASE_DIR/candidate-compose.json"
  local rollback_json="$RELEASE_DIR/rollback-compose.json"
  : > "$policy_env" || return 1
  chmod 600 "$policy_env" || return 1
  if ! UPSTREAM_BALANCE_IMAGE="$IMAGE_REF" \
    UPSTREAM_BALANCE_DATA_DIR="$DATA_DIR" \
    UPSTREAM_BALANCE_ENV_FILE="$policy_env" \
    UPSTREAM_BALANCE_CONTAINER_NAME="$CONTAINER" \
    UPSTREAM_BALANCE_BIND=127.0.0.1:8756 \
      docker compose -f "$COMPOSE_SOURCE" config --format json > "$candidate_json"; then
    find "$policy_env" "$candidate_json" "$rollback_json" -maxdepth 0 -type f -delete 2>/dev/null || true
    return 1
  fi
  if ! python3 "$COMPOSE_POLICY" "$candidate_json" "$IMAGE_REF" "$DATA_DIR"; then
    find "$policy_env" "$candidate_json" "$rollback_json" -maxdepth 0 -type f -delete 2>/dev/null || true
    return 1
  fi
  if ! UPSTREAM_BALANCE_IMAGE="$IMAGE_REF" \
    UPSTREAM_BALANCE_DATA_DIR="$DATA_DIR" \
    UPSTREAM_BALANCE_ENV_FILE="$policy_env" \
    UPSTREAM_BALANCE_CONTAINER_NAME="$CONTAINER" \
    UPSTREAM_BALANCE_BIND=127.0.0.1:8756 \
      docker compose -f "$ROLLBACK_COMPOSE_SOURCE" config --format json > "$rollback_json"; then
    find "$policy_env" "$candidate_json" "$rollback_json" -maxdepth 0 -type f -delete 2>/dev/null || true
    return 1
  fi
  if ! python3 "$COMPOSE_POLICY" "$rollback_json" "$IMAGE_REF" "$DATA_DIR"; then
    find "$policy_env" "$candidate_json" "$rollback_json" -maxdepth 0 -type f -delete 2>/dev/null || true
    return 1
  fi
  find "$policy_env" "$candidate_json" "$rollback_json" -maxdepth 0 -type f -delete || return 1
}

validate_inputs() {
  [ "$(hostname)" = "ecsPIIB" ] || { log "FATAL: unexpected host $(hostname)"; return 1; }
  [ "$(id -u)" = "1000" ] && [ "$(id -g)" = "1000" ] || {
    log "FATAL: release operator must be UID:GID 1000:1000"
    return 1
  }
  [[ "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]] || { log "FATAL: invalid release SHA"; return 1; }
  [ "$IMAGE_REF" = "ghcr.io/chenzr888/light-metapi:sha-$RELEASE_SHA" ] || {
    log "FATAL: image must be derived from the exact release SHA"
    return 1
  }
  [[ "$ATTEMPT_ID" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{32}$ ]] || { log "FATAL: invalid attempt ID"; return 1; }
  [ "$RELEASE_DIR" = "$APP_ROOT/releases/$RELEASE_SHA/$ATTEMPT_ID" ] || {
    log "FATAL: release directory is outside the audited path"
    return 1
  }
  [ "$IMAGE_ARCHIVE" = "$RELEASE_DIR/light-metapi-image.tar.gz" ] || {
    log "FATAL: image archive is outside the release directory"
    return 1
  }
  [ -f "$IMAGE_ARCHIVE" ] || { log "FATAL: tested image archive is missing"; return 1; }
  [ "$(stat -c '%a' "$IMAGE_ARCHIVE")" = "600" ] || { log "FATAL: image archive must have mode 600"; return 1; }
  [[ "$IMAGE_ARCHIVE_SHA256" =~ ^[0-9a-f]{64}$ ]] || { log "FATAL: invalid image archive checksum"; return 1; }
  [ -f "$COMPOSE_SOURCE" ] || { log "FATAL: missing release Compose file"; return 1; }
  [ -f "$ROLLBACK_COMPOSE_SOURCE" ] || { log "FATAL: missing rollback Compose file"; return 1; }
  [ -f "$COMPOSE_POLICY" ] || { log "FATAL: missing Compose policy validator"; return 1; }
  validate_compose_contract || return 1
  [ -f "$ENV_FILE" ] || { log "FATAL: missing production .env"; return 1; }
  [ -d "$DATA_DIR" ] || { log "FATAL: missing production data directory"; return 1; }
  docker inspect "$CONTAINER" >/dev/null 2>&1 || { log "FATAL: current container is missing"; return 1; }
  [ "$(df -Pk "$APP_ROOT" | awk 'NR==2 {print $4}')" -ge 2097152 ] || {
    log "FATAL: less than 2 GiB free disk"
    return 1
  }
  if [ "$EXPECTED_OPENCODE" != "preserve" ] && ! [[ "$EXPECTED_OPENCODE" =~ ^[0-9]+$ ]]; then
    log "FATAL: invalid expected account count"
    return 1
  fi
  if [ -n "$IMPORT_FILE" ]; then
    [ "$IMPORT_FILE" = "$RELEASE_DIR/opencode-import.json" ] || {
      log "FATAL: import file is outside the release directory"
      return 1
    }
    [ -f "$IMPORT_FILE" ] || { log "FATAL: import file is missing"; return 1; }
    [ "$(stat -c '%a' "$IMPORT_FILE")" = "600" ] || {
      log "FATAL: import file must have mode 600"
      return 1
    }
    local import_count
    import_count=$(python3 - "$IMPORT_FILE" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
accounts = payload.get("accounts") if isinstance(payload, dict) else None
if not isinstance(accounts, list):
    raise SystemExit("invalid accounts payload")
print(len(accounts))
PY
)
    [ "$import_count" = "$EXPECTED_OPENCODE" ] || {
      log "FATAL: expected $EXPECTED_OPENCODE import accounts, found $import_count"
      return 1
    }
  fi
}

database_counts() {
  local target=${1:-$CONTAINER}
  docker exec "$target" python -c '
import sqlite3
db=sqlite3.connect("/app/data/upstreams.sqlite3")
tables={row[0] for row in db.execute("select name from sqlite_master where type=\"table\"")}
def count(name):
    return db.execute("select count(*) from "+name).fetchone()[0] if name in tables else 0
print(f"{count(chr(117)+chr(115)+chr(101)+chr(114)+chr(115))}|{count(chr(99)+chr(104)+chr(97)+chr(110)+chr(110)+chr(101)+chr(108)+chr(115))}|{count(chr(111)+chr(112)+chr(101)+chr(110)+chr(99)+chr(111)+chr(100)+chr(101)+chr(95)+chr(97)+chr(99)+chr(99)+chr(111)+chr(117)+chr(110)+chr(116)+chr(115))}")'
}

prepare_rollback_image() {
  local old_image_id=$1
  docker tag "$old_image_id" "$RAW_ROLLBACK_TAG" || return 1
  docker build --quiet --build-arg "BASE_IMAGE=$RAW_ROLLBACK_TAG" \
    --tag "$ROLLBACK_TAG" - <<'DOCKERFILE' >/dev/null || return 1
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER 0:0
RUN chmod 755 /app \
    && find /app -maxdepth 1 -type f -exec chmod 644 {} + \
    && if [ -d /app/static ]; then find /app/static -type d -exec chmod 755 {} +; fi \
    && if [ -d /app/static ]; then find /app/static -type f -exec chmod 644 {} +; fi
ENV HOME=/tmp
USER 1000:1000
DOCKERFILE
  docker image rm "$RAW_ROLLBACK_TAG" >/dev/null || return 1
}

create_backup() {
  mkdir -p "$CANARY_BACKUP_DIR" || return 1
  chmod 700 "$APP_ROOT/backups" "$BACKUP_DIR" "$CANARY_BACKUP_DIR" || return 1
  local temp_db="/app/data/.deploy-backup-$TIMESTAMP.sqlite3"
  log "creating online SQLite backup for the isolated canary"
  docker exec -i "$CONTAINER" python - "$temp_db" <<'PY' || return 1
import os, sqlite3, sys
source = sqlite3.connect("/app/data/upstreams.sqlite3")
target = sqlite3.connect(sys.argv[1])
source.backup(target)
result = target.execute("pragma integrity_check").fetchone()[0]
target.execute("pragma wal_checkpoint(truncate)")
mode = target.execute("pragma journal_mode=delete").fetchone()[0]
target.close()
source.close()
if result != "ok":
    raise SystemExit("backup integrity check failed: " + result)
if str(mode).lower() != "delete":
    raise SystemExit("backup journal mode was not normalized: " + str(mode))
for suffix in ("-wal", "-shm"):
    candidate = sys.argv[1] + suffix
    if os.path.exists(candidate):
        os.unlink(candidate)
os.chmod(sys.argv[1], 0o600)
PY
  docker cp "$CONTAINER:$temp_db" "$CANARY_BACKUP_DIR/upstreams.sqlite3" || return 1
  docker cp "$CONTAINER:/app/data/secret.key" "$CANARY_BACKUP_DIR/secret.key" || return 1
  docker cp "$CONTAINER:/app/data/session.secret" "$CANARY_BACKUP_DIR/session.secret" || return 1
  docker exec -i "$CONTAINER" python - "$temp_db" <<'PY' || return 1
import os, sys
for suffix in ("", "-wal", "-shm"):
    candidate = sys.argv[1] + suffix
    if os.path.exists(candidate):
        os.unlink(candidate)
PY
  normalize_ownership "$BACKUP_DIR" || return 1
  chmod 600 "$CANARY_BACKUP_DIR"/* || return 1
  printf '%s\n' "$ROLLBACK_TAG" > "$BACKUP_DIR/rollback-image.txt" || return 1
  chmod 600 "$BACKUP_DIR/rollback-image.txt" || return 1
  cp -a "$ROLLBACK_COMPOSE_SOURCE" "$BACKUP_DIR/rollback-compose.yml" || return 1
  cp -a "$COMPOSE_POLICY" "$BACKUP_DIR/validate-compose-policy.py" || return 1
  cp -a "$RELEASE_DIR/scripts/remote-rollback-cy16.sh" "$BACKUP_DIR/remote-rollback-cy16.sh" || return 1
  cp -a "$RELEASE_DIR/scripts/remote-run-rollback-cy16.sh" "$BACKUP_DIR/remote-run-rollback-cy16.sh" || return 1
  if [ -f "$COMPOSE_FILE" ]; then
    cp -a "$COMPOSE_FILE" "$BACKUP_DIR/docker-compose.cy16.yml" || return 1
    printf 'present\n' > "$BACKUP_DIR/compose-state.txt" || return 1
  else
    printf 'absent\n' > "$BACKUP_DIR/compose-state.txt" || return 1
  fi
  if [ -f "$RELEASE_ENV" ]; then
    cp -a "$RELEASE_ENV" "$BACKUP_DIR/release.env" || return 1
  fi
  cp -a "$ENV_FILE" "$BACKUP_DIR/runtime.env" || return 1
  chmod 600 "$BACKUP_DIR/runtime.env" "$BACKUP_DIR/rollback-compose.yml" \
    "$BACKUP_DIR/remote-rollback-cy16.sh" "$BACKUP_DIR/remote-run-rollback-cy16.sh" \
    "$BACKUP_DIR/validate-compose-policy.py" "$BACKUP_DIR/compose-state.txt" || return 1
  if [ -f "$APP_ROOT/docker-compose.yml" ]; then
    cp -a "$APP_ROOT/docker-compose.yml" "$BACKUP_DIR/legacy-docker-compose.yml" || return 1
    chmod 600 "$BACKUP_DIR/legacy-docker-compose.yml" || return 1
  fi
  log "archiving the rollback image"
  if ! docker save "$ROLLBACK_TAG" | gzip -1 > "$BACKUP_DIR/rollback-image.tar.gz"; then
    return 1
  fi
  chmod 600 "$BACKUP_DIR/rollback-image.tar.gz" || return 1
}

normalize_ownership() {
  local target=$1
  docker run --rm --user 0:0 --entrypoint python \
    -v "$target:/target" "$ROLLBACK_TAG" -c '
import os
for root, dirs, files in os.walk("/target"):
    os.chown(root, 1000, 1000)
    os.chmod(root, 0o700)
    for name in dirs:
        os.chown(os.path.join(root, name), 1000, 1000)
        os.chmod(os.path.join(root, name), 0o700)
    for name in files:
        path=os.path.join(root, name)
        os.chown(path, 1000, 1000)
        os.chmod(path, 0o600)
' || return 1
}

create_final_backup() {
  local final_dir="$BACKUP_DIR/finalizing"
  mkdir -p "$final_dir" || return 1
  chmod 700 "$final_dir" || return 1
  log "creating final offline backup while the old container is stopped"
  docker run --rm --user 0:0 --entrypoint python \
    -v "$DATA_DIR:/data" -v "$final_dir:/backup" "$ROLLBACK_TAG" -c '
import os, shutil, sqlite3
source=sqlite3.connect("/data/upstreams.sqlite3")
target=sqlite3.connect("/backup/upstreams.sqlite3")
source.backup(target)
result=target.execute("pragma integrity_check").fetchone()[0]
target.execute("pragma wal_checkpoint(truncate)")
mode=target.execute("pragma journal_mode=delete").fetchone()[0]
target.close()
source.close()
if result != "ok":
    raise SystemExit("final backup integrity check failed: " + str(result))
if str(mode).lower() != "delete":
    raise SystemExit("final backup journal mode was not normalized: " + str(mode))
for suffix in ("-wal", "-shm"):
    candidate="/backup/upstreams.sqlite3"+suffix
    if os.path.exists(candidate):
        os.unlink(candidate)
for name in ("secret.key", "session.secret"):
    shutil.copyfile("/data/"+name, "/backup/"+name)
    os.chmod("/backup/"+name, 0o600)
' || return 1
  normalize_ownership "$final_dir" || return 1
  docker run --rm --user 1000:1000 --entrypoint python \
    -v "$final_dir:/backup:ro" "$ROLLBACK_TAG" -c '
import sqlite3
db=sqlite3.connect("file:/backup/upstreams.sqlite3?mode=ro", uri=True)
result=db.execute("pragma integrity_check").fetchone()[0]
db.close()
raise SystemExit(0 if result == "ok" else "final backup integrity check failed: " + str(result))' || return 1
  (
    cd "$final_dir" || exit 1
    sha256sum upstreams.sqlite3 secret.key session.secret > manifest.sha256 || exit 1
    chmod 600 manifest.sha256 || exit 1
  ) || return 1
  mv "$final_dir/upstreams.sqlite3" "$BACKUP_DIR/upstreams.sqlite3" || return 1
  mv "$final_dir/secret.key" "$BACKUP_DIR/secret.key" || return 1
  mv "$final_dir/session.secret" "$BACKUP_DIR/session.secret" || return 1
  mv "$final_dir/manifest.sha256" "$BACKUP_DIR/manifest.sha256" || return 1
  rmdir "$final_dir" || return 1
  (
    cd "$BACKUP_DIR" || exit 1
    sha256sum rollback-image.tar.gz runtime.env rollback-image.txt \
      rollback-compose.yml compose-state.txt remote-rollback-cy16.sh \
      remote-run-rollback-cy16.sh validate-compose-policy.py >> manifest.sha256 || exit 1
    for optional in docker-compose.cy16.yml legacy-docker-compose.yml release.env; do
      if [ -f "$optional" ]; then
        sha256sum "$optional" >> manifest.sha256 || exit 1
      fi
    done
  ) || return 1
  FINAL_BACKUP_READY=1
}

normalize_production_data() {
  normalize_ownership "$DATA_DIR" || return 1
}

verify_database() {
  local target=$1
  docker exec "$target" python -c '
import sqlite3
db=sqlite3.connect("/app/data/upstreams.sqlite3")
result=db.execute("pragma quick_check").fetchone()[0]
db.close()
raise SystemExit(0 if result == "ok" else "database quick_check failed: " + str(result))' || return 1
}

verify_instance() {
  local target=$1 base_url=$2 before_users=$3 before_channels=$4
  curl "${CURL_COMMON[@]}" "$base_url/api/health" >/dev/null || return 1
  verify_database "$target" || return 1

  local counts users channels opencode bootstrap protected_code
  counts=$(database_counts "$target") || return 1
  IFS='|' read -r users channels opencode <<< "$counts"
  [ "$users" = "$before_users" ] || { log "users count changed: $before_users -> $users"; return 1; }
  [ "$channels" = "$before_channels" ] || { log "channels count changed: $before_channels -> $channels"; return 1; }
  [ "$opencode" = "$EXPECTED_OPENCODE" ] || {
    log "OpenCode account count mismatch: expected=$EXPECTED_OPENCODE actual=$opencode"
    return 1
  }

  bootstrap=$(curl "${CURL_COMMON[@]}" "$base_url/api/auth/bootstrap") || return 1
  python3 -c 'import json,sys; p=json.loads(sys.stdin.read()); assert p["ok"] is True; assert p["data"]["needs_setup"] is False' <<< "$bootstrap" || return 1
  protected_code=$(curl --connect-timeout 5 --max-time 20 --silent --output /dev/null --write-out '%{http_code}' \
    "$base_url/api/opencode/accounts") || return 1
  [ "$protected_code" = "401" ] || { log "protected endpoint returned $protected_code"; return 1; }
}

run_canary() {
  local before_users=$1 before_channels=$2
  log "starting isolated canary on 127.0.0.1:18756"
  mkdir -p "$CANARY_DATA" || return 1
  chmod 700 "$CANARY_DATA" || return 1
  cp "$CANARY_BACKUP_DIR/upstreams.sqlite3" "$CANARY_DATA/upstreams.sqlite3" || return 1
  cp "$CANARY_BACKUP_DIR/secret.key" "$CANARY_DATA/secret.key" || return 1
  cp "$CANARY_BACKUP_DIR/session.secret" "$CANARY_DATA/session.secret" || return 1
  chmod 600 "$CANARY_DATA"/* || return 1
  if [ -n "$IMPORT_FILE" ]; then
    cp "$IMPORT_FILE" "$CANARY_DATA/opencode-import.json" || return 1
    chmod 600 "$CANARY_DATA/opencode-import.json" || return 1
  fi

  docker run -d --name "$CANARY_CONTAINER" \
    --user 1000:1000 --init --read-only --cap-drop ALL --security-opt no-new-privileges:true \
    --cpus 1 --memory 512m --memory-reservation 192m --memory-swap 768m --pids-limit 128 \
    --tmpfs /tmp:rw,noexec,nosuid,size=64m \
    --env-file "$ENV_FILE" \
    -e HOST=0.0.0.0 -e PORT=8756 -e UPSTREAM_BALANCE_DATA_DIR=/app/data \
    -e REFRESH_INTERVAL_SECONDS=3600 -e NOTIFY_INTERVAL_SECONDS=3600 \
    -e OPENCODE_GO_ALERT_INTERVAL_SECONDS=3600 -e LOW_BALANCE_EMAIL_ENABLED=0 \
    -e SESSION_COOKIE_NAME=ub_admin_session -e SESSION_COOKIE_PATH=/upstream-balance \
    -p 127.0.0.1:18756:8756 -v "$CANARY_DATA:/app/data" "$IMAGE_REF" >/dev/null || return 1

  local ready=0
  for _ in $(seq 1 45); do
    if curl --connect-timeout 2 --max-time 3 --fail --silent http://127.0.0.1:18756/api/health >/dev/null 2>&1; then
      ready=1
      break
    fi
    sleep 1 || return 1
  done
  [ "$ready" = "1" ] || {
    docker logs --tail 100 "$CANARY_CONTAINER" >&2 || true
    log "candidate did not become ready"
    return 1
  }

  verify_instance "$CANARY_CONTAINER" "http://127.0.0.1:18756" "$before_users" "$before_channels" || return 1
  [ ! -e "$CANARY_DATA/opencode-import.json" ] || { log "canary did not remove plaintext import"; return 1; }
  [ "$(docker inspect "$CANARY_CONTAINER" --format '{{.Config.Image}}')" = "$IMAGE_REF" ] || return 1
  [ "$(docker inspect "$CANARY_CONTAINER" --format '{{.HostConfig.ReadonlyRootfs}}')" = "true" ] || return 1
  [ "$(docker inspect "$CANARY_CONTAINER" --format '{{.HostConfig.PidsLimit}}')" = "128" ] || return 1
  docker rm -f "$CANARY_CONTAINER" >/dev/null || return 1
  secure_remove_tree "$CANARY_DATA" || return 1
  log "isolated canary passed"
}

write_release_env() {
  local image=$1
  {
    printf 'UPSTREAM_BALANCE_IMAGE=%s\n' "$image"
    printf 'UPSTREAM_BALANCE_DATA_DIR=%s\n' "$DATA_DIR"
    printf 'UPSTREAM_BALANCE_ENV_FILE=%s\n' "$ENV_FILE"
    printf 'UPSTREAM_BALANCE_CONTAINER_NAME=%s\n' "$CONTAINER"
    printf 'UPSTREAM_BALANCE_BIND=%s\n' '127.0.0.1:8756'
    printf 'OPENCODE_GO_ALERT_INTERVAL_SECONDS=%s\n' '300'
  } > "$RELEASE_ENV" || return 1
  chmod 600 "$RELEASE_ENV" || return 1
}

compose_up() {
  local image=$1 compose_file=${2:-$COMPOSE_FILE}
  UPSTREAM_BALANCE_IMAGE="$image" \
  UPSTREAM_BALANCE_DATA_DIR="$DATA_DIR" \
  UPSTREAM_BALANCE_ENV_FILE="$ENV_FILE" \
  UPSTREAM_BALANCE_CONTAINER_NAME="$CONTAINER" \
  UPSTREAM_BALANCE_BIND="127.0.0.1:8756" \
  OPENCODE_GO_ALERT_INTERVAL_SECONDS=300 \
    docker compose --project-name upstream-balance \
      -f "$compose_file" up -d --no-build --wait --wait-timeout 90 \
      --no-deps upstream-balance
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
    *)
      return 1
      ;;
  esac
}

restore_backup() {
  log "rollback: restoring image and database backup" || true
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  docker run --rm --user 0:0 --entrypoint python \
    -v "$DATA_DIR:/data" \
    -v "$BACKUP_DIR:/backup:ro" \
    "$ROLLBACK_TAG" -c '
import os, shutil
for name in ("upstreams.sqlite3", "secret.key", "session.secret"):
    shutil.copyfile("/backup/"+name, "/data/"+name)
for name in ("upstreams.sqlite3-wal", "upstreams.sqlite3-shm"):
    path="/data/"+name
    if os.path.exists(path):
        os.unlink(path)
os.chmod("/data", 0o700)
os.chown("/data", 1000, 1000)
for name in ("upstreams.sqlite3", "secret.key", "session.secret"):
    os.chmod("/data/"+name, 0o600)
    os.chown("/data/"+name, 1000, 1000)
candidate="/data/opencode-import.json"
if os.path.exists(candidate):
    os.unlink(candidate)
' || return 1
  write_release_env "$ROLLBACK_TAG" || return 1
  restore_compose_state "$BACKUP_DIR" || return 1
  compose_up "$ROLLBACK_TAG" "$BACKUP_DIR/rollback-compose.yml" || return 1
  verify_rollback || return 1
  ROLLBACK_COMPLETED=1
  audit rollback_ok || true
  log "rollback completed" || true
}

verify_release() {
  local before_users=$1 before_channels=$2
  verify_instance "$CONTAINER" "http://127.0.0.1:8756" "$before_users" "$before_channels" || return 1
  [ ! -e "$DATA_DIR/opencode-import.json" ] || { log "plaintext import file was not removed"; return 1; }
  [ "$(stat -c '%a' "$DATA_DIR")" = "700" ] || { log "data directory mode is not 700"; return 1; }
  [ "$(stat -c '%a' "$DATA_DIR/upstreams.sqlite3")" = "600" ] || { log "database mode is not 600"; return 1; }
  [ "$(stat -c '%u:%g' "$DATA_DIR")" = "1000:1000" ] || { log "data directory owner is not 1000:1000"; return 1; }
  [ "$(stat -c '%u:%g' "$DATA_DIR/upstreams.sqlite3")" = "1000:1000" ] || { log "database owner is not 1000:1000"; return 1; }
  [ "$(docker inspect "$CONTAINER" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}')" = "healthy" ] || return 1
  [ "$(docker inspect "$CONTAINER" --format '{{.RestartCount}}')" = "0" ] || return 1

  local page bootstrap protected_code asset asset_body
  page=$(curl "${CURL_COMMON[@]}" "$PUBLIC_URL") || return 1
  bootstrap=$(curl "${CURL_COMMON[@]}" "${PUBLIC_URL}_ub_api/auth/bootstrap") || return 1
  python3 -c 'import json,sys; p=json.loads(sys.stdin.read()); assert p["ok"] is True; assert p["data"]["needs_setup"] is False' <<< "$bootstrap" || return 1
  protected_code=$(curl --connect-timeout 5 --max-time 20 --silent --output /dev/null --write-out '%{http_code}' \
    "${PUBLIC_URL}_ub_api/opencode/accounts") || return 1
  [ "$protected_code" = "401" ] || { log "protected endpoint returned $protected_code"; return 1; }
  asset=$(grep -oE 'assets/index-[A-Za-z0-9_-]+\.js' <<< "$page" | head -n 1) || return 1
  [ -n "$asset" ] || { log "frontend asset was not found"; return 1; }
  asset_body=$(curl "${CURL_COMMON[@]}" "${PUBLIC_URL}${asset}") || return 1
  grep -q 'OpenCode Go' <<< "$asset_body" || {
    log "deployed frontend does not contain OpenCode Go"
    return 1
  }
}

verify_rollback() {
  curl "${CURL_COMMON[@]}" http://127.0.0.1:8756/api/health >/dev/null || return 1
  verify_database "$CONTAINER" || return 1
  local counts bootstrap protected_code page
  counts=$(database_counts "$CONTAINER") || return 1
  [ "$counts" = "$USERS_BEFORE|$CHANNELS_BEFORE|$OPENCODE_BEFORE" ] || {
    log "rollback database counts mismatch: expected=$USERS_BEFORE|$CHANNELS_BEFORE|$OPENCODE_BEFORE actual=$counts"
    return 1
  }
  page=$(curl "${CURL_COMMON[@]}" "$PUBLIC_URL") || return 1
  [ -n "$page" ] || return 1
  bootstrap=$(curl "${CURL_COMMON[@]}" "${PUBLIC_URL}_ub_api/auth/bootstrap") || return 1
  python3 -c 'import json,sys; p=json.loads(sys.stdin.read()); assert p["ok"] is True; assert p["data"]["needs_setup"] is False' <<< "$bootstrap" || return 1
  protected_code=$(curl --connect-timeout 5 --max-time 20 --silent --output /dev/null --write-out '%{http_code}' \
    "${PUBLIC_URL}_ub_api/opencode/accounts") || return 1
  [ "$protected_code" = "401" ] || [ "$protected_code" = "404" ] || {
    log "rollback protected endpoint returned $protected_code"
    return 1
  }
}

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "FATAL: another deployment is running"
  exit 1
fi

audit started
validate_inputs || { audit preflight_failed; exit 1; }

log "loading the exact image artifact tested by GitHub CI"
if [ "$(sha256sum "$IMAGE_ARCHIVE" | awk '{print $1}')" != "$IMAGE_ARCHIVE_SHA256" ] || \
   ! docker load --input "$IMAGE_ARCHIVE" >/dev/null; then
  audit image_load_failed
  exit 1
fi
NEW_IMAGE_ID=$(docker image inspect "$IMAGE_REF" --format '{{.Id}}')
NEW_REVISION=$(docker image inspect "$IMAGE_REF" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')
[ "$NEW_REVISION" = "$RELEASE_SHA" ] || { log "FATAL: image revision label mismatch"; audit image_failed; exit 1; }

OLD_IMAGE_ID=$(docker inspect "$CONTAINER" --format '{{.Image}}')
prepare_rollback_image "$OLD_IMAGE_ID" || { audit rollback_image_prepare_failed; exit 1; }
COUNTS_BEFORE=$(database_counts)
IFS='|' read -r USERS_BEFORE CHANNELS_BEFORE OPENCODE_BEFORE <<< "$COUNTS_BEFORE"
if [ "$EXPECTED_OPENCODE" = "preserve" ]; then
  EXPECTED_OPENCODE=$OPENCODE_BEFORE
fi
if [ -n "$IMPORT_FILE" ] && [ "$OPENCODE_BEFORE" != "0" ]; then
  log "FATAL: refusing import into non-empty OpenCode table"
  audit import_refused
  secure_delete "$IMPORT_FILE"
  exit 1
fi

if ! create_backup; then
  audit backup_failed
  exit 1
fi
if ! run_canary "$USERS_BEFORE" "$CHANNELS_BEFORE"; then
  audit canary_failed
  exit 1
fi
cp "$COMPOSE_SOURCE" "$COMPOSE_FILE"
chmod 600 "$COMPOSE_FILE"

CUTOVER_STARTED=1
log "stopping the old container for a zero-write final backup"
docker stop --time 30 "$CONTAINER" >/dev/null
if ! create_final_backup; then
  audit final_backup_failed
  exit 1
fi
normalize_production_data

if [ -n "$IMPORT_FILE" ]; then
  log "staging encrypted-at-startup OpenCode import"
  cp "$IMPORT_FILE" "$DATA_DIR/opencode-import.json"
  chmod 600 "$DATA_DIR/opencode-import.json"
  secure_delete "$IMPORT_FILE"
fi

write_release_env "$IMAGE_REF"
if ! compose_up "$IMAGE_REF" || ! verify_release "$USERS_BEFORE" "$CHANNELS_BEFORE"; then
  audit deploy_failed
  if ! restore_backup; then
    audit rollback_failed
    log "FATAL: deployment and rollback both failed"
    exit 2
  fi
  exit 1
fi

RUNNING_IMAGE_ID=$(docker inspect "$CONTAINER" --format '{{.Image}}')
[ "$RUNNING_IMAGE_ID" = "$NEW_IMAGE_ID" ] || {
  audit image_mismatch
  restore_backup
  exit 1
}

audit success
DEPLOY_SUCCEEDED=1
log "DEPLOY_OK sha=$RELEASE_SHA backup=$BACKUP_DIR"
