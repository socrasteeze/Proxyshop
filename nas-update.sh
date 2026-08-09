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
#   3. Optional — updates from the web UI (Settings → "Update & restart"):
#      nohup sh "$HOME/proxyshop-web/nas-watch.sh" >/dev/null 2>&1 &
#      The app can't run this script itself (it lives inside the container
#      this rebuilds), so nas-watch.sh runs it on the host. See that file.
#
# Test the fetch/install path without docker:  DRY_RUN=1 sh nas-update.sh
# Test the install path without GitHub (no PAT needed):
#   LOCAL_TARBALL=/path/to/app.tar.gz DRY_RUN=1 sh nas-update.sh
#
# First boot after a search-schema change rebuilds the card search index over
# the whole library before the app serves a single request — minutes, not
# seconds, on a large one. The health check waits DEPLOY_TIMEOUT seconds
# (default 600); raise it for a very large library:
#   DEPLOY_TIMEOUT=1800 sh nas-update.sh
# ============================================================================
set -eu

# Some NAS local/web terminals omit HOME; SSH login shells set it.
# Resolve before any "$HOME/..." config lines below (set -u would abort otherwise).
if [ -z "${HOME:-}" ]; then
  _user="$(id -un 2>/dev/null || true)"
  if [ -n "$_user" ] && command -v getent >/dev/null 2>&1; then
    HOME="$(getent passwd "$_user" | cut -d: -f6)" || true
  fi
  if [ -z "${HOME:-}" ] && [ -n "$_user" ] && [ -d "/home/$_user" ]; then
    HOME="/home/$_user"
  fi
  if [ -z "${HOME:-}" ]; then
    echo "ERROR: HOME is unset in this shell."
    echo "Run:  export HOME=/home/<youruser> && sh nas-update.sh"
    exit 1
  fi
  export HOME
fi

# --- edit these per app ---
REPO="socrasteeze/Proxyshop"                # GitHub owner/name (private OK)
BRANCH="main"                               # branch to deploy from
APP_NAME="proxyshop-web"                    # container name + image tag
APP_DIR="$HOME/proxyshop-web"               # where code lands on the NAS
TOKEN_FILE="$HOME/.gh-token"                # PAT file, chmod 600 (shared across apps)
PORT="8000:8000"                            # host:container
CONTAINER_USER="0:0"                        # match owning uid:gid of your mounts (ls -n)
DATA_DIR="/Volume1/proxyshop/data"          # TerraMaster: /Volume1 (capital V!)
WORKER_TOKEN_FILE="$HOME/.proxyshop-worker-token"  # server<->worker shared secret
POKEMONTCG_KEY_FILE="$HOME/.proxyshop-pokemontcg-key"  # optional (raises pokemontcg.io limits)
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

# Move to a guaranteed-valid directory. If this script is launched from a dir
# that was deleted/replaced (a stale shell), rsync aborts with
# "getcwd(): No such file or directory" before it can copy anything — which
# silently prevents the container from ever rebuilding. All paths below are
# absolute, so the working directory doesn't otherwise matter.
cd "$HOME" 2>/dev/null || cd / || true

echo "==> Deploying $APP_NAME from $REPO@$BRANCH"

# --- fetch ------------------------------------------------------------------
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if [ -n "${LOCAL_TARBALL:-}" ]; then
  # Test hook: use a pre-made tarball instead of hitting GitHub
  cp "$LOCAL_TARBALL" "$TMP/app.tar.gz"
else
  # Only the GitHub fetch needs the PAT, so the check belongs here — a
  # LOCAL_TARBALL run must not demand a token it will never send.
  [ -f "$TOKEN_FILE" ] || {
    echo "ERROR: token file $TOKEN_FILE not found."
    echo "Create it:  echo '<github_pat>' > $TOKEN_FILE && chmod 600 $TOKEN_FILE"
    exit 1
  }
  TOKEN="$(cat "$TOKEN_FILE")"
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

# Build identity for the Settings page. GitHub names the tarball's top-level
# directory "<owner>-<repo>-<short sha>", so the commit comes free.
BUILD_COMMIT="$(basename "$SRC" | sed 's/.*-//')"
BUILD_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "==> Build $BRANCH@$BUILD_COMMIT ($BUILD_AT)"

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

# Provider API keys — optional files, empty means "no key" to the app.
# Strip CR/LF so `echo key > file` does not break header auth. Gate on -f first:
# a missing file makes the shell itself fail the `<` redirect before tr ever
# runs, and dash reports that straight to the real stderr — the `2>/dev/null`
# on this same command is already active by then, so it doesn't catch it.
if [ -f "$POKEMONTCG_KEY_FILE" ]; then
  POKEMONTCG_KEY="$(tr -d '\r\n' < "$POKEMONTCG_KEY_FILE" 2>/dev/null || true)"
else
  POKEMONTCG_KEY=""
fi
[ -n "$POKEMONTCG_KEY" ] && echo "==> Pokémon TCG key: found" \
                         || echo "==> Pokémon TCG key: none (keyless rate limits)"

if [ -n "${DRY_RUN:-}" ]; then
  echo "==> DRY_RUN set — skipping docker build/run/verify. Install complete."
  exit 0
fi

# --- build & run ------------------------------------------------------------
# TerraMaster keeps the docker binary off the default PATH — only a *login*
# shell picks it up. Cron runs with PATH=/usr/bin:/bin and
# `ssh host "sh nas-update.sh"` is non-login, so both would die below with
# "docker: command not found". Today that is masked by nas-watch.sh having been
# started by hand from a login shell; the moment cron restarts it, the deploy
# breaks. Resolve docker here instead of trusting the caller's environment.
if ! command -v docker >/dev/null 2>&1; then
  for _dir in ${DOCKER_BIN_DIR:-} /Volume1/@apps/DockerEngine/dockerd/bin \
              /usr/local/bin /opt/bin /usr/bin; do
    if [ -x "$_dir/docker" ]; then
      PATH="$_dir:$PATH"
      export PATH
      echo "==> Found docker in $_dir (added to PATH)"
      break
    fi
  done
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker not found. Looked on PATH and in:"
  echo "  ${DOCKER_BIN_DIR:+$DOCKER_BIN_DIR (DOCKER_BIN_DIR), }/Volume1/@apps/DockerEngine/dockerd/bin, /usr/local/bin, /opt/bin, /usr/bin"
  echo "Point at it directly:  DOCKER_BIN_DIR=/path/to/bin sh nas-update.sh"
  exit 1
fi

echo "==> Building image $APP_NAME:latest"
docker build -t "$APP_NAME:latest" -f "$APP_DIR/web/server/Dockerfile" "$APP_DIR"

# Optional secret mounts (file fallback inside the container)
VOLUME_ARGS=""
[ -f "$POKEMONTCG_KEY_FILE" ] && VOLUME_ARGS="$VOLUME_ARGS -v $POKEMONTCG_KEY_FILE:/run/secrets/proxyshop-pokemontcg-key:ro"

# Scheduled-refresh tuning, forwarded only when the caller actually set it.
# Passing these unconditionally would be wrong: an *empty* value is not the same
# as an unset one to `auto_cache.scheduled_games()`, which falls back to its
# defaults only when the variable is absent. `-e PROXYSHOP_AUTO_CACHE_GAMES=`
# would therefore schedule nothing at all, silently.
AUTO_CACHE_ARGS=""
[ -n "${PROXYSHOP_AUTO_CACHE_GAMES:-}" ] && AUTO_CACHE_ARGS="$AUTO_CACHE_ARGS -e PROXYSHOP_AUTO_CACHE_GAMES=$PROXYSHOP_AUTO_CACHE_GAMES"
[ -n "${PROXYSHOP_AUTO_CACHE_HOURS:-}" ] && AUTO_CACHE_ARGS="$AUTO_CACHE_ARGS -e PROXYSHOP_AUTO_CACHE_HOURS=$PROXYSHOP_AUTO_CACHE_HOURS"

echo "==> Restarting container"
docker stop "$APP_NAME" 2>/dev/null || true
docker rm   "$APP_NAME" 2>/dev/null || true
# Scheduled catalog re-walks (Union Arena / Weiß Schwarz) start with this
# deploy. The app already defaults them on; naming the variable here keeps that
# visible in the script that turns it on, and `:-` keeps it overridable:
#   PROXYSHOP_AUTO_CACHE=0 sh nas-update.sh              # off
#   PROXYSHOP_AUTO_CACHE_GAMES=union-arena sh nas-update.sh   # narrow (see above)
#   PROXYSHOP_AUTO_CACHE_HOURS=72 sh nas-update.sh            # re-time
# shellcheck disable=SC2086
docker run -d --name "$APP_NAME" --restart unless-stopped \
  -p "$PORT" \
  --user "$CONTAINER_USER" \
  -e PROXYSHOP_WORKER_TOKEN="$(tr -d '\r\n' < "$WORKER_TOKEN_FILE")" \
  -e PROXYSHOP_OFFLINE=0 \
  -e PROXYSHOP_AUTO_CACHE="${PROXYSHOP_AUTO_CACHE:-1}" \
  -e PROXYSHOP_MAX_UPLOAD_MB=50 \
  -e PROXYSHOP_POKEMONTCG_KEY="$POKEMONTCG_KEY" \
  -e PROXYSHOP_BUILD_COMMIT="$BUILD_COMMIT" \
  -e PROXYSHOP_BUILD_BRANCH="$BRANCH" \
  -e PROXYSHOP_BUILD_AT="$BUILD_AT" \
  -v "$DATA_DIR":/data \
  $VOLUME_ARGS \
  $AUTO_CACHE_ARGS \
  "$APP_NAME:latest"

# --- verify -----------------------------------------------------------------
# Wait on a deadline, not a fixed tick count. `CardDB.__init__` runs at import,
# so a first boot after a search-schema change rebuilds the whole card search
# index *before* uvicorn binds the port — a fixed 30s window reports a healthy
# deploy as failed, and the natural reaction (re-run the updater) stops the
# container mid-rebuild and starts it over.
#
# Waiting longer must not mean sitting out a real crash, so each pass asks
# whether the container is still running: a stopped one fails immediately.
HOST_PORT="${PORT%%:*}"
DEPLOY_TIMEOUT="${DEPLOY_TIMEOUT:-600}"
echo "==> Waiting for health check on port $HOST_PORT (up to ${DEPLOY_TIMEOUT}s)"
waited=0
while [ "$waited" -lt "$DEPLOY_TIMEOUT" ]; do
  if curl -fsS "http://127.0.0.1:$HOST_PORT/api/health" >/dev/null 2>&1; then
    echo "==> OK: $APP_NAME is up after ${waited}s — http://<nas>:$HOST_PORT"
    echo "    First deploy? Import the card database once:"
    echo "    docker exec $APP_NAME python -m web.server.manage bulk-download"
    echo "    Cache Riftbound (stop/resume safe):"
    echo "    docker exec -it $APP_NAME python -m web.server.manage cache-game --game riftbound"
    echo "    docker exec $APP_NAME python -m web.server.manage cache-game --game riftbound --stop"
    echo "    Union Arena / Riftbound search need no API key."
    exit 0
  fi
  # Missing container inspects as "false" too — either way it isn't coming up.
  running="$(docker inspect -f '{{.State.Running}}' "$APP_NAME" 2>/dev/null || echo false)"
  if [ "$running" != "true" ]; then
    echo "ERROR: $APP_NAME is not running (stopped after ${waited}s). Recent logs:"
    docker logs --tail 40 "$APP_NAME" || true
    exit 1
  fi
  sleep 2
  waited=$((waited + 2))
  # Minutes of silence reads as a hang; name the usual cause instead.
  if [ $((waited % 30)) -eq 0 ]; then
    echo "    still starting (${waited}s) — a first boot after a search-schema"
    echo "    change rebuilds the card search index before serving."
  fi
done
echo "ERROR: health check failed after ${DEPLOY_TIMEOUT}s. Recent logs:"
docker logs --tail 40 "$APP_NAME" || true
echo "    Still indexing a very large library? Retry with a longer wait:"
echo "    DEPLOY_TIMEOUT=1800 sh nas-update.sh"
exit 1
