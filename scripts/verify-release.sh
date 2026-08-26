#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

if [ "${CI:-}" != "true" ] && [ -n "$(git status --porcelain)" ]; then
  echo "FATAL: release verification requires a clean worktree" >&2
  exit 1
fi

for command_name in git python3 npm docker curl; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "FATAL: missing required command: $command_name" >&2
    exit 1
  }
done
[ "$(id -u):$(id -g)" = "1000:1000" ] || {
  echo "FATAL: local verification requires UID:GID 1000:1000" >&2
  exit 1
}

VERIFY_DIR=$(mktemp -d /tmp/light-metapi-verify.XXXXXX)
VERIFY_CONTAINER="light-metapi-verify-$$"
VERIFY_IMAGE="light-metapi:verify-$(git rev-parse --short=12 HEAD)"

cleanup() {
  docker rm -f "$VERIFY_CONTAINER" >/dev/null 2>&1 || true
  if [ -d "$VERIFY_DIR" ]; then
    find "$VERIFY_DIR" -depth -delete 2>/dev/null || true
  fi
}
trap cleanup EXIT

mkdir -p "$VERIFY_DIR/data" "$VERIFY_DIR/tests"
touch "$VERIFY_DIR/runtime.env"
chmod 700 "$VERIFY_DIR" "$VERIFY_DIR/data" "$VERIFY_DIR/tests"
chmod 600 "$VERIFY_DIR/runtime.env"

echo "[1/7] install and audit frontend dependencies"
npm --prefix ui-preview ci
npm --prefix ui-preview audit --audit-level=high

echo "[2/7] build frontend and verify committed assets"
npm --prefix ui-preview run build
git diff --exit-code -- static/

echo "[3/7] run backend and deployment-contract tests in isolated data dir"
UPSTREAM_BALANCE_DATA_DIR="$VERIFY_DIR/tests" \
  python3 -m unittest discover -s tests -v

echo "[4/7] validate production Compose"
for compose_file in deploy/docker-compose.cy16.yml deploy/docker-compose.rollback.yml; do
  resolved="$VERIFY_DIR/$(basename "$compose_file").json"
  UPSTREAM_BALANCE_IMAGE="$VERIFY_IMAGE" \
  UPSTREAM_BALANCE_DATA_DIR="$VERIFY_DIR/data" \
  UPSTREAM_BALANCE_ENV_FILE="$VERIFY_DIR/runtime.env" \
    docker compose -f "$compose_file" config --format json > "$resolved"
  python3 scripts/validate-compose-policy.py "$resolved" "$VERIFY_IMAGE" "$VERIFY_DIR/data"
done

echo "[5/7] build immutable image"
docker build \
  --build-arg "VCS_REF=$(git rev-parse HEAD)" \
  --tag "$VERIFY_IMAGE" \
  .

echo "[6/7] smoke-test hardened container"
docker run -d --name "$VERIFY_CONTAINER" \
  --user 1000:1000 \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --memory 512m \
  --memory-swap 768m \
  --pids-limit 128 \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  -e UPSTREAM_BALANCE_DATA_DIR=/app/data \
  -e REFRESH_INTERVAL_SECONDS=3600 \
  -v "$VERIFY_DIR/data:/app/data" \
  -p 127.0.0.1:18756:8756 \
  "$VERIFY_IMAGE" >/dev/null

for _ in $(seq 1 30); do
  if curl --fail --silent http://127.0.0.1:18756/api/health >/dev/null; then
    break
  fi
  sleep 1
done
curl --fail --silent http://127.0.0.1:18756/api/health >/dev/null
curl --fail --silent http://127.0.0.1:18756/_ub_api/auth/bootstrap >/dev/null
test "$(curl --silent --output /dev/null --write-out '%{http_code}' \
  http://127.0.0.1:18756/_ub_api/channels)" = "401"

echo "[7/7] verify image revision and filesystem permissions"
test "$(docker image inspect "$VERIFY_IMAGE" \
  --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" = "$(git rev-parse HEAD)"
test "$(stat -c '%a' "$VERIFY_DIR/data")" = "700"
test "$(stat -c '%a' "$VERIFY_DIR/data/upstreams.sqlite3")" = "600"

echo "VERIFY_OK sha=$(git rev-parse HEAD) image=$VERIFY_IMAGE"
