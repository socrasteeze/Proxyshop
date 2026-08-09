"""
* Deploy update requests (server side of the host-run updater).
* The web app runs *inside* the proxyshop-web container, and nas-update.sh
  stops/rebuilds that very container — so the app can never run it directly.
  Instead the button here drops a request file on the shared /data volume and a
  small watcher on the NAS host (nas-watch.sh) picks it up and runs the script.
  State lives on /data precisely because the container is replaced mid-update.
* Must never import from `src/`.

Files, all under DATA_DIR/update:
    request.json   written by the app, consumed (renamed) by the watcher
    status.json    written by the watcher: state/started/finished/exit_code
    watch.json     watcher heartbeat, so the UI can tell it's actually running
    update.log     combined stdout/stderr of the last nas-update.sh run
"""
# Standard Library Imports
import json
import os
import time
import uuid
from pathlib import Path
from typing import Optional

# A heartbeat older than this means the host watcher isn't running, so a
# request would sit unread forever — the UI says so instead of pretending.
WATCHER_STALE_AFTER = 120.0

# Requests older than this are treated as abandoned (watcher died mid-run),
# so a stuck file can't block updates forever.
REQUEST_STALE_AFTER = 3600.0

STATES = ('idle', 'requested', 'running', 'ok', 'failed')


def update_dir(data_dir: Path) -> Path:
    return Path(data_dir) / 'update'


def _path(data_dir: Path, name: str) -> Path:
    return update_dir(data_dir) / name


def _read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, payload: dict) -> None:
    """Atomic write — the watcher polls this directory constantly."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.part')
    tmp.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    tmp.replace(path)


def _now() -> float:
    return time.time()


def _stamp(ts: Optional[float] = None) -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(ts if ts else _now()))


def watcher_seen_at(data_dir: Path) -> Optional[float]:
    """Epoch seconds of the watcher's last heartbeat, or None if never seen."""
    beat = _read_json(_path(data_dir, 'watch.json')) or {}
    try:
        return float(beat.get('at'))
    except (TypeError, ValueError):
        return None


def watcher_online(data_dir: Path) -> bool:
    seen = watcher_seen_at(data_dir)
    return bool(seen and (_now() - seen) < WATCHER_STALE_AFTER)


def pending_request(data_dir: Path) -> Optional[dict]:
    """The unclaimed request, ignoring one stale enough to be abandoned."""
    req = _read_json(_path(data_dir, 'request.json'))
    if not req:
        return None
    try:
        age = _now() - float(req.get('at') or 0)
    except (TypeError, ValueError):
        return None
    return None if age > REQUEST_STALE_AFTER else req


def request_update(data_dir: Path, *, requested_by: str = '') -> dict:
    """Ask the host watcher to run nas-update.sh. Returns the new status.

    Idempotent while one is outstanding: re-clicking the button during a
    pending or running update returns the existing state rather than queueing
    a second rebuild.
    """
    current = status(data_dir)
    if current['state'] in ('requested', 'running'):
        return current
    _write_json(_path(data_dir, 'request.json'), {
        'id': uuid.uuid4().hex[:12],
        'at': _now(),
        'requested_at': _stamp(),
        'requested_by': str(requested_by or '')[:64],
    })
    return status(data_dir)


def log_tail(data_dir: Path, limit: int = 200) -> list[str]:
    """Last lines of the running/most recent update, for the Settings page."""
    limit = max(1, min(int(limit or 200), 1000))
    path = _path(data_dir, 'update.log')
    try:
        # Read the tail only; a full rebuild log can be megabytes.
        size = path.stat().st_size
        with path.open('rb') as fh:
            if size > 256 * 1024:
                fh.seek(size - 256 * 1024)
                fh.readline()  # discard the partial first line
            text = fh.read().decode('utf-8', errors='replace')
    except OSError:
        return []
    return [ln for ln in text.splitlines() if ln][-limit:]


def status(data_dir: Path, *, include_log: bool = False) -> dict:
    """Combined view of the updater for the API/UI."""
    data_dir = Path(data_dir)
    st = _read_json(_path(data_dir, 'status.json')) or {}
    state = str(st.get('state') or 'idle')
    if state not in STATES:
        state = 'idle'
    pending = pending_request(data_dir)
    # An unclaimed request outranks a finished run: the watcher hasn't started
    # yet, so the last run's 'ok' would read as "already done".
    if pending and state not in ('running',):
        state = 'requested'
    seen = watcher_seen_at(data_dir)
    payload = {
        'state': state,
        'watcher_online': watcher_online(data_dir),
        'watcher_seen_at': _stamp(seen) if seen else None,
        'requested_at': (pending or {}).get('requested_at'),
        'started_at': st.get('started_at'),
        'finished_at': st.get('finished_at'),
        'exit_code': st.get('exit_code'),
        'message': st.get('message') or '',
    }
    if include_log:
        payload['log'] = log_tail(data_dir)
    return payload


def app_version() -> dict:
    """Best-effort build identity, so Settings can show what's deployed.

    nas-update.sh stamps these into the image; absent locally, which is fine.
    """
    return {
        'commit': (os.environ.get('PROXYSHOP_BUILD_COMMIT') or '')[:12],
        'built_at': os.environ.get('PROXYSHOP_BUILD_AT') or '',
        'branch': os.environ.get('PROXYSHOP_BUILD_BRANCH') or '',
    }


def asset_version(static_dir: Path) -> str:
    """Cache-busting token for /static URLs.

    StaticFiles sends ETag and Last-Modified but no Cache-Control, so a browser
    is free to apply heuristic freshness — roughly a tenth of the file's age.
    An asset untouched for a fortnight therefore stays cached for a day or more
    *without revalidating*, and a deploy in that window pairs freshly rendered
    HTML with pre-deploy CSS. That fails silently and confusingly: new markup
    renders unstyled rather than erroring, so it reads as a layout bug.

    Versioning the URL sidesteps the whole question — a deploy asks for a URL
    the cache has never seen. The build commit is the natural token; locally
    there is none, so fall back to the newest asset mtime, which changes
    exactly when an edit lands.
    """
    commit = (os.environ.get('PROXYSHOP_BUILD_COMMIT') or '').strip()
    if commit:
        return commit[:12]
    try:
        return str(int(max(
            p.stat().st_mtime for p in
            (Path(static_dir) / 'app.css', Path(static_dir) / 'app.js')
            if p.is_file())))
    except (ValueError, OSError):
        return 'dev'  # no assets to stamp; correctness doesn't depend on this
