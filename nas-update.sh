#!/bin/sh
# ============================================================================
# Proxyshop Web — NAS update & deploy script
#
# Fetches a source snapshot of this repo from GitHub over HTTPS (no git
# needed on the NAS), rebuilds the proxyshop-web Docker image, and restarts
# the container. Safe to re-run any time; run remotely via nas-refresh.bat.
#
# One-time setup on the NAS:
#   1. GitHub PAT with repo read access:  echo "<token>" > ~/.gh-token
#      chmod 600 ~/.gh-token
#   2. First run (from anywhere):         sh nas-update.sh
#
# Test the fetch/install path without docker:  DRY_RUN=1 sh nas-update.sh
# ============================================================================
set -eu

# --- edit these per app ---
REPO="socrasteeze/Proxyshop"                # GitHub owner/name (private OK)
BRANCH="claude/template-creation-enhancements-ifj9ws"  # switch to main after merge
APP_NAME="proxyshop-web"                    # container name + image tag
APP_DIR="$HOME/proxyshop-web"               # where code lands on the NAS
TOKEN_FILE="$HOME/.gh-token"                # PAT file, chmod 600 (shared across apps)
PORT="8000:8000"                            # host:container
CONTAINER_USER="0:0"                        # match owning uid:gid of your mounts (ls -n)
DATA_DIR="/Volume1/proxyshop/data"          # TerraMaster: /Volume1 (capital V!)
WORKER_TOKEN_FILE="$HOME/.proxyshop-worker-token"  # server<->worker shared secret
# --------------------------

# --- self-overwrite guard: keep verbatim -----------------------------------
# This script overwrites its own directory. /bin/sh reads scripts lazily, so
# re-exec from a /tmp copy first or the interpreter can die mid-run.
if [ -z "${UPDATER_REEXEC:-}" ]; then
  _self_copy="$(mktemp)"
  cp "$0" "$_self_copy"
  UPDATER_REEXEC=1 exec sh "$_self_copy" "$@"
fi
# ----------------------------------------------------------------------------

echo "==> Deploying $APP_NAME from $REPO@$BRANCH"

# --- fetch ------------------------------------------------------------------
[ -f "$TOKEN_FILE" ] || {
  echo "ERROR: token file $TOKEN_FILE not found."
  echo "Create it:  echo '<github_pat>' > $TOKEN_FILE && chmod 600 $TOKEN_FILE"
  exit 1
}
TOKEN="$(cat "$TOKEN_FILE")"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if [ -n "${LOCAL_TARBALL:-}" ]; then
  # Test hook: use a pre-made tarball instead of hitting GitHub
  cp "$LOCAL_TARBALL" "$TMP/app.tar.gz"
else
  DL="https://api.github.com/repos/$REPO/tarball/$BRANCH"
  echo "==> Fetching $DL"
  curl -fSL -H "Authorization: Bearer $TOKEN" "$DL" -o "$TMP/app.tar.gz"
fi

mkdir "$TMP/src"
tar -xzf "$TMP/app.tar.gz" -C "$TMP/src"
# GitHub tarballs contain a single top-level directory; resolve it
SRC=""
for d in "$TMP/src"/*/; do SRC="$d"; break; done
[ -n "$SRC" ] && [ -f "${SRC}web/server/Dockerfile" ] || {
  echo "ERROR: unexpected tarball layout (no web/server/Dockerfile found)"; exit 1;
}

# --- install ----------------------------------------------------------------
echo "==> Installing to $APP_DIR"
mkdir -p "$APP_DIR"
if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete "$SRC" "$APP_DIR"/
else
  # No rsync on this box: wipe and copy (no NAS-local config lives in APP_DIR)
  rm -rf "$APP_DIR"
  mkdir -p "$APP_DIR"
  cp -a "$SRC". "$APP_DIR"/
fi

# --- seed secrets & data dirs before first run ------------------------------
if [ ! -f "$WORKER_TOKEN_FILE" ]; then
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24 > "$WORKER_TOKEN_FILE"
  else
    head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$WORKER_TOKEN_FILE"
  fi
  chmod 600 "$WORKER_TOKEN_FILE"
  echo "==> Generated worker token at $WORKER_TOKEN_FILE"
  echo "    Use it on the Windows render machine:"
  echo "    python -m web.worker.daemon --server http://<nas>:8000 --token $(cat "$WORKER_TOKEN_FILE")"
fi
mkdir -p "$DATA_DIR"

if [ -n "${DRY_RUN:-}" ]; then
  echo "==> DRY_RUN set — skipping docker build/run/verify. Install complete."
  exit 0
fi

# --- build & run ------------------------------------------------------------
echo "==> Building image $APP_NAME:latest"
docker build -t "$APP_NAME:latest" -f "$APP_DIR/web/server/Dockerfile" "$APP_DIR"

echo "==> Restarting container"
docker stop "$APP_NAME" 2>/dev/null || true
docker rm   "$APP_NAME" 2>/dev/null || true
docker run -d --name "$APP_NAME" --restart unless-stopped \
  -p "$PORT" \
  --user "$CONTAINER_USER" \
  -e PROXYSHOP_WORKER_TOKEN="$(cat "$WORKER_TOKEN_FILE")" \
  -e PROXYSHOP_OFFLINE=0 \
  -e PROXYSHOP_MAX_UPLOAD_MB=50 \
  -v "$DATA_DIR":/data \
  "$APP_NAME:latest"

# --- verify -----------------------------------------------------------------
HOST_PORT="${PORT%%:*}"
echo "==> Waiting for health check on port $HOST_PORT"
i=0
while [ $i -lt 15 ]; do
  if curl -fsS "http://127.0.0.1:$HOST_PORT/api/health" >/dev/null 2>&1; then
    echo "==> OK: $APP_NAME is up — http://<nas>:$HOST_PORT"
    echo "    First deploy? Import the card database once:"
    echo "    docker exec $APP_NAME python -m web.server.manage bulk-download"
    exit 0
  fi
  i=$((i + 1))
  sleep 2
done
echo "ERROR: health check failed after 30s. Recent logs:"
docker logs --tail 40 "$APP_NAME" || true
exit 1
