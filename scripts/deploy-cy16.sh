#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
REPO=Chenzr888/light-metapi
HOST=cy16
IMPORT_FILE=""
CONFIRM=""
SHA_INPUT=""
PLAN_ONLY=0
NOTIFY="$HOME/ai/api/scripts/notify-lark-deploy.sh"
OFFSITE_DIR=${UPSTREAM_BALANCE_OFFSITE_DIR:-$HOME/ai/api/.backups/upstream-balance}

usage() {
  cat <<'EOF'
Usage:
  scripts/deploy-cy16.sh --sha <full-or-short-sha> --confirm cy16:<sha12> [--import-opencode <config.json>]
  scripts/deploy-cy16.sh --sha <full-or-short-sha> --plan [--import-opencode <config.json>]

Only a clean, locally verified origin/main commit with a successful GitHub CI run can deploy.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --sha) SHA_INPUT=${2:?missing value for --sha}; shift ;;
    --confirm) CONFIRM=${2:?missing value for --confirm}; shift ;;
    --import-opencode) IMPORT_FILE=${2:?missing value for --import-opencode}; shift ;;
    --plan) PLAN_ONLY=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "FATAL: unknown argument: $1" >&2; usage; exit 1 ;;
  esac
  shift
done

[ -n "$SHA_INPUT" ] || { usage; exit 1; }
cd "$ROOT"

for command_name in git gh docker curl python3; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "FATAL: missing required command: $command_name" >&2
    exit 1
  }
done

[ -z "$(git status --porcelain)" ] || { echo "FATAL: dirty worktree" >&2; exit 1; }
[ "$(git branch --show-current)" = "main" ] || { echo "FATAL: deployment is allowed only from main" >&2; exit 1; }

git fetch --quiet origin main
RELEASE_SHA=$(git rev-parse --verify "$SHA_INPUT^{commit}")
[[ "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "FATAL: invalid SHA" >&2; exit 1; }
[ "$(git rev-parse HEAD)" = "$RELEASE_SHA" ] || { echo "FATAL: HEAD is not the requested SHA" >&2; exit 1; }
[ "$(git rev-parse origin/main)" = "$RELEASE_SHA" ] || { echo "FATAL: requested SHA is not origin/main" >&2; exit 1; }

SHORT_SHA=${RELEASE_SHA:0:12}
IMAGE_REF="ghcr.io/chenzr888/light-metapi:sha-$RELEASE_SHA"
EXPECTED_OPENCODE=preserve
if [ -n "$IMPORT_FILE" ]; then
  IMPORT_FILE=$(readlink -f "$IMPORT_FILE")
  [ -f "$IMPORT_FILE" ] || { echo "FATAL: import file not found" >&2; exit 1; }
  [ "$(stat -c '%a' "$IMPORT_FILE")" = "600" ] || { echo "FATAL: import file must have mode 600" >&2; exit 1; }
  EXPECTED_OPENCODE=$(python3 - "$IMPORT_FILE" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
accounts = payload.get("accounts") if isinstance(payload, dict) else None
if not isinstance(accounts, list) or not accounts:
    raise SystemExit("invalid or empty accounts payload")
print(len(accounts))
PY
)
fi

RUN_JSON=$(gh run list --repo "$REPO" --workflow ci.yml --commit "$RELEASE_SHA" --limit 1 \
  --json databaseId,status,conclusion,headSha,url)
CI_META=$(RUN_JSON="$RUN_JSON" python3 - "$RELEASE_SHA" <<'PY'
import json, os, sys
sha = sys.argv[1]
runs = json.loads(os.environ["RUN_JSON"])
if len(runs) != 1:
    raise SystemExit("FATAL: no CI run found for release SHA")
run = runs[0]
if run.get("headSha") != sha or run.get("status") != "completed" or run.get("conclusion") != "success":
    raise SystemExit("FATAL: CI is not successful for release SHA")
print(str(run.get("databaseId")) + "|" + str(run.get("url")))
PY
)
IFS='|' read -r RUN_ID RUN_URL <<< "$CI_META"
echo "CI_OK run=$RUN_ID url=$RUN_URL"

echo "DEPLOY_PLAN"
echo "  host=$HOST"
echo "  sha=$RELEASE_SHA"
echo "  image=$IMAGE_REF"
echo "  expected_opencode=$EXPECTED_OPENCODE"
echo "  scope=upstream-balance container only; nginx/light-proxy/new-api unchanged"

if [ "$PLAN_ONLY" = "1" ]; then
  exit 0
fi

[ "$CONFIRM" = "cy16:$SHORT_SHA" ] || {
  echo "FATAL: production confirmation must be --confirm cy16:$SHORT_SHA" >&2
  exit 1
}

echo "running mandatory local release verification"
scripts/verify-release.sh

# shellcheck source=/dev/null
source "$HOME/.claude/skills/autonomous-task/scripts/ssh-safe.sh"
ATTEMPT_ID="$(date -u +%Y%m%dT%H%M%SZ)-$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
RELEASE_PARENT="/home/ubuntu/upstream-balance/releases/$RELEASE_SHA"
RELEASE_DIR="/home/ubuntu/upstream-balance/releases/$RELEASE_SHA/$ATTEMPT_ID"
ssafe "$HOST" "mkdir -p '$RELEASE_PARENT' && chmod 700 '$RELEASE_PARENT' && mkdir '$RELEASE_DIR' && mkdir '$RELEASE_DIR/deploy' '$RELEASE_DIR/scripts' && chmod 700 '$RELEASE_DIR' '$RELEASE_DIR/deploy' '$RELEASE_DIR/scripts'"

PACKAGE_DIR=$(mktemp -d /tmp/light-metapi-release.XXXXXX)
REMOTE_IMPORT=""
REMOTE_IMAGE_ARCHIVE=""
TASK_ACCEPTED=0
cleanup() {
  if [ "$TASK_ACCEPTED" = "0" ]; then
    if [ -n "$REMOTE_IMPORT" ]; then
      ssafe "$HOST" "find '$REMOTE_IMPORT' -maxdepth 0 -type f -delete 2>/dev/null || true" >/dev/null 2>&1 || true
    fi
    if [ -n "$REMOTE_IMAGE_ARCHIVE" ]; then
      ssafe "$HOST" "find '$REMOTE_IMAGE_ARCHIVE' -maxdepth 0 -type f -delete 2>/dev/null || true" >/dev/null 2>&1 || true
    fi
  fi
  if [ -d "$PACKAGE_DIR" ]; then
    find "$PACKAGE_DIR" -depth -delete 2>/dev/null || true
  fi
}
trap cleanup EXIT

git archive "$RELEASE_SHA" \
  deploy/docker-compose.cy16.yml deploy/docker-compose.rollback.yml \
  scripts/validate-compose-policy.py scripts/remote-deploy-cy16.sh \
  scripts/remote-rollback-cy16.sh scripts/remote-run-cy16.sh \
  scripts/remote-run-rollback-cy16.sh | tar -xf - -C "$PACKAGE_DIR"
mkdir -p "$PACKAGE_DIR/artifact"
gh run download "$RUN_ID" --repo "$REPO" \
  --name "light-metapi-image-$RELEASE_SHA" --dir "$PACKAGE_DIR/artifact"
IMAGE_ARCHIVE="$PACKAGE_DIR/artifact/light-metapi-image.tar.gz"
[ -f "$IMAGE_ARCHIVE" ] || { echo "FATAL: tested image artifact is missing" >&2; exit 1; }
IMAGE_ARCHIVE_SHA256=$(sha256sum "$IMAGE_ARCHIVE" | awk '{print $1}')
scpsafe "$PACKAGE_DIR/deploy/docker-compose.cy16.yml" "$HOST:$RELEASE_DIR/deploy/docker-compose.cy16.yml"
scpsafe "$PACKAGE_DIR/deploy/docker-compose.rollback.yml" "$HOST:$RELEASE_DIR/deploy/docker-compose.rollback.yml"
scpsafe "$PACKAGE_DIR/scripts/validate-compose-policy.py" "$HOST:$RELEASE_DIR/scripts/validate-compose-policy.py"
scpsafe "$PACKAGE_DIR/scripts/remote-deploy-cy16.sh" "$HOST:$RELEASE_DIR/scripts/remote-deploy-cy16.sh"
scpsafe "$PACKAGE_DIR/scripts/remote-rollback-cy16.sh" "$HOST:$RELEASE_DIR/scripts/remote-rollback-cy16.sh"
scpsafe "$PACKAGE_DIR/scripts/remote-run-cy16.sh" "$HOST:$RELEASE_DIR/scripts/remote-run-cy16.sh"
scpsafe "$PACKAGE_DIR/scripts/remote-run-rollback-cy16.sh" "$HOST:$RELEASE_DIR/scripts/remote-run-rollback-cy16.sh"
ssafe "$HOST" "chmod 600 '$RELEASE_DIR/deploy/docker-compose.cy16.yml' '$RELEASE_DIR/deploy/docker-compose.rollback.yml' && chmod 700 '$RELEASE_DIR/scripts/validate-compose-policy.py' '$RELEASE_DIR/scripts/remote-deploy-cy16.sh' '$RELEASE_DIR/scripts/remote-rollback-cy16.sh' '$RELEASE_DIR/scripts/remote-run-cy16.sh' '$RELEASE_DIR/scripts/remote-run-rollback-cy16.sh'"
REMOTE_IMAGE_ARCHIVE="$RELEASE_DIR/light-metapi-image.tar.gz"
scpsafe "$IMAGE_ARCHIVE" "$HOST:$REMOTE_IMAGE_ARCHIVE"
ssafe "$HOST" "chmod 600 '$REMOTE_IMAGE_ARCHIVE'"

if [ -n "$IMPORT_FILE" ]; then
  REMOTE_IMPORT="$RELEASE_DIR/opencode-import.json"
  scpsafe "$IMPORT_FILE" "$HOST:$REMOTE_IMPORT"
  ssafe "$HOST" "chmod 600 '$REMOTE_IMPORT'"
fi

notify() {
  local status=$1 title=$2 detail=$3
  [ -x "$NOTIFY" ] || return 0
  "$NOTIFY" "$status" "$title" "$detail" >/dev/null 2>&1 || true
}

notify start "开始部署 — upstream-balance" "host: cy16\ncommit: \`$SHORT_SHA\`"
TASK_NAME="upstream-balance-deploy-$SHORT_SHA-$ATTEMPT_ID"
STATUS_FILE="$RELEASE_DIR/deploy-$ATTEMPT_ID.status"
LOG_FILE="$RELEASE_DIR/deploy-$ATTEMPT_ID.log"
PID_FILE="$RELEASE_DIR/deploy-$ATTEMPT_ID.pid"
LAUNCH_LOG="$RELEASE_DIR/deploy-$ATTEMPT_ID.launch.log"
echo "REMOTE_TASK name=$TASK_NAME attempt=$ATTEMPT_ID status=$STATUS_FILE"
ssafe "$HOST" "command -v nohup >/dev/null && command -v setsid >/dev/null"
ssafe "$HOST" \
  "nohup setsid env RELEASE_SHA='$RELEASE_SHA' IMAGE_REF='$IMAGE_REF' \
    IMAGE_ARCHIVE='$REMOTE_IMAGE_ARCHIVE' IMAGE_ARCHIVE_SHA256='$IMAGE_ARCHIVE_SHA256' \
    RELEASE_DIR='$RELEASE_DIR' IMPORT_FILE='$REMOTE_IMPORT' \
    EXPECTED_OPENCODE='$EXPECTED_OPENCODE' ATTEMPT_ID='$ATTEMPT_ID' \
    /bin/bash '$RELEASE_DIR/scripts/remote-run-cy16.sh' \
    > '$LAUNCH_LOG' 2>&1 < /dev/null & printf '%s\n' \$! > '$PID_FILE'"
TASK_ACCEPTED=1

DEPLOY_STATUS=3
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
    DEPLOY_STATUS=$(awk -F= '/^exit_code=/{print $2}' <<< "$REMOTE_STATE")
    ssafe "$HOST" "tail -n 200 '$LOG_FILE'" || true
    break
  fi
  if [ "$REMOTE_STATE" = "MISSING" ]; then
    echo "FATAL: remote deployment unit disappeared without a status file" >&2
    DEPLOY_STATUS=4
    break
  fi
  sleep 5
done

if [ "$DEPLOY_STATUS" -ne 0 ]; then
  notify fail "部署未成功 — upstream-balance" "host: cy16\ncommit: \`$SHORT_SHA\`\nattempt: $ATTEMPT_ID\nexit: $DEPLOY_STATUS"
  if [ "$DEPLOY_STATUS" = "3" ]; then
    echo "FATAL: local wait timed out; the detached remote task may still be running. Check $STATUS_FILE" >&2
  else
    # The remote task is terminal (including MISSING), so local EXIT cleanup may
    # safely remove any staging files left before the remote trap initialized.
    TASK_ACCEPTED=0
  fi
  exit "$DEPLOY_STATUS"
fi

BACKUP_PATH=$(awk -F= '/^backup_path=/{print $2}' <<< "$REMOTE_STATE")
[[ "$BACKUP_PATH" =~ ^/home/ubuntu/upstream-balance/backups/([0-9]{8}T[0-9]{6}Z-[0-9a-f]{40})$ ]] || {
  notify fail "部署成功但异地备份失败 — upstream-balance" "host: cy16\ncommit: \`$SHORT_SHA\`\nreason: invalid backup path"
  echo "FATAL: remote deployment succeeded but did not return a valid backup path" >&2
  exit 5
}
BACKUP_ID=${BASH_REMATCH[1]}
mkdir -p "$OFFSITE_DIR"
chmod 700 "$OFFSITE_DIR"
REMOTE_BUNDLE="$RELEASE_DIR/backup-$BACKUP_ID.tar"
LOCAL_BUNDLE="$OFFSITE_DIR/$BACKUP_ID.tar"
REMOTE_BUNDLE_SHA=$(ssafe "$HOST" \
  "tar --create --file '$REMOTE_BUNDLE' --directory '/home/ubuntu/upstream-balance/backups' '$BACKUP_ID' && chmod 600 '$REMOTE_BUNDLE' && sha256sum '$REMOTE_BUNDLE' | cut -c1-64")
scpsafe "$HOST:$REMOTE_BUNDLE" "$LOCAL_BUNDLE"
chmod 600 "$LOCAL_BUNDLE"
LOCAL_BUNDLE_SHA=$(sha256sum "$LOCAL_BUNDLE" | awk '{print $1}')
ssafe "$HOST" "find '$REMOTE_BUNDLE' -maxdepth 0 -type f -delete"
[ "$LOCAL_BUNDLE_SHA" = "$REMOTE_BUNDLE_SHA" ] || {
  notify fail "部署成功但异地备份校验失败 — upstream-balance" "host: cy16\ncommit: \`$SHORT_SHA\`\nbackup: $BACKUP_ID"
  echo "FATAL: off-host backup checksum mismatch" >&2
  exit 5
}

notify success "部署成功 — upstream-balance" "host: cy16\ncommit: \`$SHORT_SHA\`\nurl: https://ai.sandboxai.top/upstream-balance/"
echo "DEPLOY_OK sha=$RELEASE_SHA backup=$BACKUP_ID offsite=$LOCAL_BUNDLE url=https://ai.sandboxai.top/upstream-balance/"
