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
#   nohup sh nas-watch.sh >/dev/null 2>&1 &
#
# Only one instance runs at a time — a second start exits immediately with
# "Already running", so it's safe to launch this from cron unconditionally:
#   */5 * * * * nohup sh /home/<you>/nas-watch.sh >/dev/null 2>&1 &
# That doubles as a keep-alive: if the watcher dies, the next tick restarts it.
#
# Check it once:  DATA_DIR=/Volume1/proxyshop/data ONE_SHOT=1 sh nas-watch.sh
# ============================================================================
set -eu

# --- edit to match nas-update.sh -------------------------------------------
DATA_DIR="${DATA_DIR:-/Volume1/proxyshop/data}"   # same host path mounted at /data
UPDATE_SCRIPT="${UPDATE_SCRIPT:-$HOME/nas-update.sh}"
POLL_SECONDS="${POLL_SECONDS:-15}"
# ----------------------------------------------------------------------------

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
  write_status running "" "Running $UPDATE_SCRIPT"

  {
    echo "==================================================================="
    echo "[$(now_iso)] update requested via web UI"
  } > "$LOG"

  if [ ! -f "$UPDATE_SCRIPT" ]; then
    echo "ERROR: $UPDATE_SCRIPT not found. Set UPDATE_SCRIPT=/path/to/nas-update.sh" >> "$LOG"
    write_status failed 1 "Update script not found"
    return
  fi

  # nas-update.sh stops this container; its own re-exec guard handles being
  # overwritten mid-run. Never let a failure kill the watcher (set -e).
  if sh "$UPDATE_SCRIPT" >> "$LOG" 2>&1; then
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

echo "==> Watching $REQUEST (every ${POLL_SECONDS}s); update script: $UPDATE_SCRIPT"

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
