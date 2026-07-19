"""
* In-process TCG cache runner for the web UI.
* Starts cache-game work on a background thread; stop uses the same flag file
* as the CLI so both can cooperate.
* Captures print_fn output into a per-game ring buffer + rotating log file.
"""
# Standard Library Imports
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Local Imports
from web.shared import games
from web.shared.carddb import CardDB
from web.shared.game_cache import (
    checkpoint_path, filter_conflict, load_checkpoint, progress_dict,
    request_stop, run_cache_game)


class FilterConflict(Exception):
    """A saved run with different filters would be clobbered by a new start.

    Carries the existing run's human label so the UI can offer to discard it.
    """

    def __init__(self, existing_label: str):
        self.existing_label = existing_label
        super().__init__(
            f'A different download is already saved for this game '
            f'({existing_label}).')

_lock = threading.Lock()
_threads: dict[str, threading.Thread] = {}
_errors: dict[str, str] = {}

_log_lock = threading.Lock()
_logs: dict[str, deque] = {}
_LOG_MAXLEN = 400
_LOG_FILE_MAX = 512 * 1024  # rotate at 512 KB


def is_running(game: str) -> bool:
    t = _threads.get(game)
    return bool(t and t.is_alive())


def status(game: str, *, db: CardDB, runs_dir: Path) -> dict:
    progress = load_checkpoint(checkpoint_path(runs_dir, game))
    payload = progress_dict(
        progress,
        db_count=db.count_by_game(game),
        running=is_running(game))
    err = _errors.get(game)
    if err and not payload.get('running'):
        payload['error'] = err
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


def start(
    game: str,
    *,
    db: CardDB,
    images_dir: Path,
    runs_dir: Path,
    fresh: bool = False,
    images_only: bool = False,
    filters: Optional[dict] = None,
    image_kind: str = 'png',
) -> dict:
    """Start or resume a cache run in the background."""
    if game not in games.CATALOG_GAMES:
        raise ValueError(f'Unsupported game {game!r}')
    # Validate filters synchronously so the UI gets a 422 instead of a silent thread error
    from web.shared.cache_filters import (
        SELECTIVE_GAMES, build_provider_query, filters_require_selection,
        normalize_filters)
    if not images_only and game in SELECTIVE_GAMES:
        norm = normalize_filters(game, filters)
        if filters_require_selection(game, norm):
            raise ValueError(
                f'{game} cache needs filters (set, type, rarity, art, …) '
                'so it does not dump the entire catalog')
        build_provider_query(game, norm)

    # Detect a filter mismatch up front so the UI can offer to discard the old
    # run, instead of the background thread crashing with a CLI-flavored error.
    if not fresh and not images_only:
        existing = filter_conflict(runs_dir, game, filters)
        if existing:
            raise FilterConflict(existing)

    with _lock:
        if is_running(game):
            return status(game, db=db, runs_dir=runs_dir)
        _errors.pop(game, None)

        def _target() -> None:
            log(game, runs_dir, f'==> run started ({game})')
            try:
                run_cache_game(
                    db=db,
                    game=game,
                    images_dir=images_dir,
                    runs_dir=runs_dir,
                    fresh=fresh,
                    images_only=images_only,
                    filters=filters,
                    image_kind=image_kind,
                    use_signals=False,
                    print_fn=lambda *a, **k: log(game, runs_dir, *a),
                )
            except Exception as e:  # noqa: BLE001 — surface in status for UI
                _errors[game] = str(e)
                log(game, runs_dir, f'==> run crashed: {e}')

        thread = threading.Thread(
            target=_target, name=f'cache-game-{game}', daemon=True)
        _threads[game] = thread
        thread.start()
    return status(game, db=db, runs_dir=runs_dir)


def stop(game: str, *, db: CardDB, runs_dir: Path) -> dict:
    log(game, runs_dir, '==> stop requested')
    request_stop(runs_dir, game)
    return status(game, db=db, runs_dir=runs_dir)
