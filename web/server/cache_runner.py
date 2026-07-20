"""
* In-process TCG cache runner for the web UI.
* Each catalog game has a persisted download queue; a single background worker
* per game drains it one job at a time (games still run in parallel with each
* other). Stop uses the same flag file as the CLI so both can cooperate.
* Captures print_fn output into a per-game ring buffer + rotating log file.
"""
# Standard Library Imports
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Local Imports
from web.shared import download_queue, games
from web.shared.cache_filters import (
    build_provider_query, filters_equal, filters_require_selection,
    normalize_filters, SELECTIVE_GAMES)
from web.shared.carddb import CardDB
from web.shared.game_cache import (
    checkpoint_path, clear_stop, load_checkpoint, progress_dict,
    request_stop, reset_checkpoint, run_cache_game, stop_path)

_lock = threading.Lock()
_threads: dict[str, threading.Thread] = {}
_errors: dict[str, str] = {}
# Live per-card snapshot (current card, counts) updated in-memory by the run
# thread, so status polling shows the card being fetched without extra disk I/O.
_live: dict[str, dict] = {}

_log_lock = threading.Lock()
_logs: dict[str, deque] = {}
_LOG_MAXLEN = 400
_LOG_FILE_MAX = 512 * 1024  # rotate at 512 KB


def is_running(game: str) -> bool:
    t = _threads.get(game)
    return bool(t and t.is_alive())


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def _queue_view(game: str, items: list[dict], running: bool, runs_dir: Path) -> list[dict]:
    """Compact per-item view for the UI: the head carries the live state."""
    ck = load_checkpoint(checkpoint_path(runs_dir, game))
    out = []
    for i, it in enumerate(items):
        if i == 0:
            if running:
                state = 'running'
            elif ck and ck.status == 'stopped':
                state = 'stopped'
            elif ck and ck.status == 'done':
                state = 'done'
            else:
                state = 'queued'
        else:
            state = 'queued'
        out.append({
            'id': it.get('id'),
            'label': it.get('label') or '',
            'kind': it.get('kind') or 'png',
            'state': state,
            'position': i,
        })
    return out


def status(game: str, *, db: CardDB, runs_dir: Path) -> dict:
    progress = load_checkpoint(checkpoint_path(runs_dir, game))
    running = is_running(game)
    payload = progress_dict(
        progress,
        db_count=db.count_by_game(game),
        running=running)
    # While running, overlay the live in-memory snapshot (current card + fresh
    # counts) which is newer than the per-page checkpoint on disk.
    if running:
        live = _live.get(game)
        if live:
            payload.update({k: v for k, v in live.items() if v is not None})
    err = _errors.get(game)
    if err and not payload.get('running'):
        payload['error'] = err
    items = download_queue.load_queue(runs_dir, game)
    payload['queue'] = _queue_view(game, items, running, runs_dir)
    payload['queued_count'] = len(items)
    return payload


def all_status(*, db: CardDB, runs_dir: Path) -> dict:
    """Status dict for every catalog game, plus whether any job is running."""
    jobs = {}
    any_running = False
    for game in games.CATALOG_GAMES:
        st = status(game, db=db, runs_dir=runs_dir)
        # Ensure game key is always set (idle progress_dict leaves it None)
        st = {**st, 'game': game}
        jobs[game] = st
        if st.get('running'):
            any_running = True
    return {'jobs': jobs, 'any_running': any_running}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log_path(runs_dir: Path, game: str) -> Path:
    return Path(runs_dir) / f'{game}.log'


def log(game: str, runs_dir: Path, *parts) -> None:
    """Append a timestamped line to the in-memory deque and log file."""
    msg = ' '.join(str(p) for p in parts)
    if not msg:
        return
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    path = _log_path(runs_dir, game)
    with _log_lock:
        dq = _logs.setdefault(game, deque(maxlen=_LOG_MAXLEN))
        dq.append(line)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_file() and path.stat().st_size >= _LOG_FILE_MAX:
                bak = path.with_suffix('.log.1')
                bak.unlink(missing_ok=True)
                path.replace(bak)
            with path.open('a', encoding='utf-8') as fh:
                fh.write(line + '\n')
        except OSError:
            pass  # in-memory log still works if disk is full / read-only


def log_lines(game: str, runs_dir: Path, limit: int = 200) -> list[str]:
    """Return recent log lines from memory, or tail-read the file."""
    limit = max(1, min(int(limit or 200), 1000))
    with _log_lock:
        dq = _logs.get(game)
        if dq:
            return list(dq)[-limit:]
    path = _log_path(runs_dir, game)
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return []
    lines = [ln for ln in text.splitlines() if ln]
    return lines[-limit:]


# ---------------------------------------------------------------------------
# Queue worker
# ---------------------------------------------------------------------------

def _snapshot(game: str, progress) -> None:
    _live[game] = {
        'current': progress.current or '',
        'stored': progress.stored,
        'images_ok': progress.images_ok,
        'images_skip': progress.images_skip,
        'images_fail': progress.images_fail,
        'total_hint': progress.total_hint,
    }


def _worker(game: str, *, db: CardDB, images_dir: Path, runs_dir: Path) -> None:
    """Drain the game's queue one item at a time until empty or stopped."""
    while True:
        if stop_path(runs_dir, game).is_file():
            break  # queue paused by a stop request
        item = download_queue.head(runs_dir, game)
        if not item:
            break
        _errors.pop(game, None)
        _live.pop(game, None)
        # Resume the head if a matching, unfinished checkpoint exists (e.g. after
        # a restart); otherwise start it fresh.
        norm = normalize_filters(game, item.get('filters'))
        ck = load_checkpoint(checkpoint_path(runs_dir, game))
        resume = bool(
            ck and filters_equal(ck.filters, norm)
            and ck.status in ('running', 'stopped'))
        remaining = max(len(download_queue.load_queue(runs_dir, game)) - 1, 0)
        log(game, runs_dir,
            f'==> {"resuming" if resume else "starting"}: '
            f'{item.get("label") or "download"}'
            + (f'  ({remaining} more queued)' if remaining else ''))
        try:
            run_cache_game(
                db=db,
                game=game,
                images_dir=images_dir,
                runs_dir=runs_dir,
                fresh=not resume,
                images_only=bool(item.get('images_only')),
                filters=item.get('filters'),
                image_kind=item.get('kind') or 'png',
                use_signals=False,
                print_fn=lambda *a, **k: log(game, runs_dir, *a),
                on_progress=lambda pr: _snapshot(game, pr),
            )
        except Exception as e:  # noqa: BLE001 — surface in status for UI
            _errors[game] = str(e)
            log(game, runs_dir, f'==> run crashed: {e}')
            break  # leave the item queued; the user can remove/retry it
        final = load_checkpoint(checkpoint_path(runs_dir, game))
        if final and final.status == 'done':
            download_queue.pop_head(runs_dir, game, item.get('id'))
            reset_checkpoint(runs_dir, game)  # clean slate for the next item
            _live.pop(game, None)
            continue
        # Stopped by the user → pause here, leaving the head to resume later.
        break
    _live.pop(game, None)


def _ensure_worker(game: str, *, db: CardDB, images_dir: Path, runs_dir: Path) -> None:
    with _lock:
        if is_running(game):
            return
        clear_stop(runs_dir, game)
        _errors.pop(game, None)

        def _target() -> None:
            try:
                _worker(game, db=db, images_dir=images_dir, runs_dir=runs_dir)
            finally:
                _live.pop(game, None)

        thread = threading.Thread(
            target=_target, name=f'cache-worker-{game}', daemon=True)
        _threads[game] = thread
        thread.start()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def enqueue(
    game: str,
    *,
    db: CardDB,
    images_dir: Path,
    runs_dir: Path,
    filters: Optional[dict] = None,
    image_kind: str = 'png',
    images_only: bool = False,
    fresh: bool = False,
) -> dict:
    """Add a download to the game's queue and make sure the worker is running.

    Selective games (MTG / Pokémon) require at least one filter. ``fresh``
    discards the saved checkpoint + pending queue for a clean restart (only
    honored when nothing is currently running).
    """
    if game not in games.CATALOG_GAMES:
        raise ValueError(f'Unsupported game {game!r}')
    if not images_only and game in SELECTIVE_GAMES:
        norm = normalize_filters(game, filters)
        if filters_require_selection(game, norm):
            raise ValueError(
                f'{game} cache needs filters (set, type, rarity, art, …) '
                'so it does not dump the entire catalog')
        build_provider_query(game, norm)  # raises ValueError on an empty query

    if fresh and not is_running(game):
        reset_checkpoint(runs_dir, game)
        download_queue.save_queue(runs_dir, game, [])

    download_queue.enqueue(
        runs_dir, game, filters, kind=image_kind, images_only=images_only)
    _ensure_worker(game, db=db, images_dir=images_dir, runs_dir=runs_dir)
    return status(game, db=db, runs_dir=runs_dir)


def resume(game: str, *, db: CardDB, images_dir: Path, runs_dir: Path) -> dict:
    """Restart the worker so it continues draining a paused queue."""
    if download_queue.head(runs_dir, game):
        _ensure_worker(game, db=db, images_dir=images_dir, runs_dir=runs_dir)
    return status(game, db=db, runs_dir=runs_dir)


def stop(game: str, *, db: CardDB, runs_dir: Path) -> dict:
    """Pause the queue: stop the active job (resumable) and halt draining."""
    log(game, runs_dir, '==> stop requested')
    request_stop(runs_dir, game)
    return status(game, db=db, runs_dir=runs_dir)


def remove_item(game: str, item_id: str, *, db: CardDB, runs_dir: Path) -> dict:
    """Remove a queued item. The actively-running head can't be removed — stop
    it first. Removing a paused head also discards its partial checkpoint."""
    with _lock:
        if download_queue.is_head(runs_dir, game, item_id):
            if is_running(game):
                return status(game, db=db, runs_dir=runs_dir)  # no-op while live
            download_queue.remove(runs_dir, game, item_id)
            reset_checkpoint(runs_dir, game)
        else:
            download_queue.remove(runs_dir, game, item_id)
    return status(game, db=db, runs_dir=runs_dir)


def clear(game: str, *, db: CardDB, runs_dir: Path) -> dict:
    """Drop every pending item, leaving the active/head job untouched."""
    download_queue.clear_pending(runs_dir, game)
    return status(game, db=db, runs_dir=runs_dir)
