#!/bin/sh
# ============================================================================
# Proxyshop Web — host-side update watcher
#
# Watches the shared data volume for an update request dropped by the web UI
# (Settings → "Update & restart") and runs nas-update.sh on its behalf.
#
# Why this exists: the web app runs *inside* the proxyshop-web container, and
# nas-update.sh stops and rebuilds that very container. It cannot run the
# script itself — the process would be killed halfway through. So the button
# writes a request file, and this loop (running on the NAS host, outside
# Docker) does the work and survives the restart.
#
# One-time setup on the NAS:
#   nohup sh ~/proxyshop-web/nas-watch.sh >/dev/null 2>&1 &
#
# Only one instance runs at a time — a second start exits immediately with
# "Already running", so it's safe to launch this from cron unconditionally:
#   */5 * * * * nohup sh /home/<you>/proxyshop-web/nas-watch.sh >/dev/null 2>&1 &
# That doubles as a keep-alive: if the watcher dies, the next tick restarts it.
#
# Check it once:  DATA_DIR=/Volume1/proxyshop/data ONE_SHOT=1 sh nas-watch.sh
# ============================================================================
set -eu

# Some NAS local/web terminals omit HOME; SSH login shells set it. Resolve it
# before the "$HOME/..." config lines below (set -u would abort otherwise).
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
    echo "Run:  export HOME=/home/<youruser> && sh nas-watch.sh"
    exit 1
  fi
  export HOME
fi

# --- edit to match nas-update.sh -------------------------------------------
DATA_DIR="${DATA_DIR:-/Volume1/proxyshop/data}"   # same host path mounted at /data
APP_DIR="${APP_DIR:-$HOME/proxyshop-web}"         # nas-update.sh's install target
POLL_SECONDS="${POLL_SECONDS:-15}"
# ----------------------------------------------------------------------------

# Where nas-update.sh lives. Set UPDATE_SCRIPT to pin an exact path; otherwise
# look in the places it actually ends up. nas-update.sh installs the whole repo
# — this file included — into APP_DIR, so the watcher started per the docs
# (`sh ~/proxyshop-web/nas-watch.sh`) finds it right beside itself. $HOME is the
# last resort: the hand-copied script that bootstraps the very first deploy.
SELF_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd || echo .)"
CANDIDATES="$SELF_DIR/nas-update.sh $APP_DIR/nas-update.sh $HOME/nas-update.sh"

# Resolved per run, not once at startup, so a watcher launched before the first
# install picks the script up as soon as it lands.
resolve_update_script() {
  if [ -n "${UPDATE_SCRIPT:-}" ]; then
    echo "$UPDATE_SCRIPT"
    return 0
  fi
  for _c in $CANDIDATES; do
    if [ -f "$_c" ]; then
      echo "$_c"
      return 0
    fi
  done
  return 1
}

DIR="$DATA_DIR/update"
REQUEST="$DIR/request.json"
CLAIMED="$DIR/request.claimed.json"
STATUS="$DIR/status.json"
BEAT="$DIR/watch.json"
LOG="$DIR/update.log"
LOCK="$DIR/watch.lock"

mkdir -p "$DIR"

now_epoch() { date +%s; }
now_iso()   { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Atomic write, so the app never reads a half-written file. The temp name
# carries the pid: two watchers sharing one scratch file would interleave
# their writes and publish corrupt JSON.
write_file() {
  # $1 = destination, stdin = contents
  cat > "$1.$$.part"
  mv "$1.$$.part" "$1"
}

# Single instance. `mkdir` is atomic on POSIX, so it doubles as a lock; the pid
# inside lets a later run tell "already running" from "killed without cleanup".
acquire_lock() {
  if mkdir "$LOCK" 2>/dev/null; then
    echo $$ > "$LOCK/pid"
    trap 'rm -rf "$LOCK"' EXIT INT TERM
    return 0
  fi
  owner="$(cat "$LOCK/pid" 2>/dev/null || true)"
  if [ -n "$owner" ] && kill -0 "$owner" 2>/dev/null; then
    return 1  # a live watcher holds it
  fi
  # Stale lock from a killed watcher — take it over.
  rm -rf "$LOCK"
  mkdir "$LOCK" 2>/dev/null || return 1
  echo $$ > "$LOCK/pid"
  trap 'rm -rf "$LOCK"' EXIT INT TERM
  return 0
}

heartbeat() {
  printf '{"at": %s, "pid": %s}\n' "$(now_epoch)" "$$" | write_file "$BEAT"
}

write_status() {
  # $1 = state, $2 = exit code (or ""), $3 = message
  printf '{"state": "%s", "started_at": "%s", "finished_at": "%s", "exit_code": %s, "message": "%s"}\n' \
    "$1" "${STARTED_AT:-}" "${2:+$(now_iso)}" "${2:-null}" "$3" | write_file "$STATUS"
}

run_update() {
  STARTED_AT="$(now_iso)"
  script="$(resolve_update_script || true)"
  write_status running "" "Running ${script:-nas-update.sh}"

  {
    echo "==================================================================="
    echo "[$(now_iso)] update requested via web UI"
  } > "$LOG"

  if [ -z "$script" ] || [ ! -f "$script" ]; then
    {
      echo "ERROR: nas-update.sh not found. Looked in:"
      for c in ${UPDATE_SCRIPT:-$CANDIDATES}; do echo "  $c"; done
      echo "Set UPDATE_SCRIPT=/path/to/nas-update.sh to point at it directly."
    } >> "$LOG"
    write_status failed 1 "Update script not found"
    return
  fi

  # nas-update.sh stops this container; its own re-exec guard handles being
  # overwritten mid-run. Never let a failure kill the watcher (set -e).
  if sh "$script" >> "$LOG" 2>&1; then
    echo "[$(now_iso)] update finished OK" >> "$LOG"
    write_status ok 0 "Update complete"
  else
    code=$?
    echo "[$(now_iso)] update FAILED (exit $code)" >> "$LOG"
    write_status failed "$code" "Update failed — see the log above"
  fi
}

if ! acquire_lock; then
  echo "==> Already running (pid $(cat "$LOCK/pid" 2>/dev/null || echo '?')) — nothing to do."
  exit 0  # exit 0 so a cron keep-alive doesn't treat this as a failure
fi

found="$(resolve_update_script || true)"
echo "==> Watching $REQUEST (every ${POLL_SECONDS}s)"
if [ -n "$found" ]; then
  echo "==> Update script: $found"
else
  echo "==> WARNING: no nas-update.sh found yet. Looked in:"
  for c in ${UPDATE_SCRIPT:-$CANDIDATES}; do echo "      $c"; done
fi

while :; do
  heartbeat
  # Claim by renaming: it's atomic, so the app immediately sees the request as
  # taken and won't re-fire it while the rebuild runs. Tested via `if` so a
  # miss (or a concurrent watcher) can't trip `set -e` and kill the loop.
  if [ -f "$REQUEST" ] && mv "$REQUEST" "$CLAIMED" 2>/dev/null; then
    run_update
    rm -f "$CLAIMED"
  fi
  if [ -n "${ONE_SHOT:-}" ]; then
    break
  fi
  sleep "$POLL_SECONDS"
done
