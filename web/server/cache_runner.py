"""
* In-process TCG cache runner for the web UI.
* Starts cache-game work on a background thread; stop uses the same flag file
* as the CLI so both can cooperate.
"""
# Standard Library Imports
import threading
from pathlib import Path
from typing import Optional

# Local Imports
from web.shared import games
from web.shared.carddb import CardDB
from web.shared.game_cache import (
    checkpoint_path, load_checkpoint, progress_dict, request_stop, run_cache_game)

_lock = threading.Lock()
_threads: dict[str, threading.Thread] = {}
_errors: dict[str, str] = {}


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

    with _lock:
        if is_running(game):
            return status(game, db=db, runs_dir=runs_dir)
        _errors.pop(game, None)

        def _target() -> None:
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
                    print_fn=lambda *a, **k: None,
                )
            except Exception as e:  # noqa: BLE001 — surface in status for UI
                _errors[game] = str(e)

        thread = threading.Thread(
            target=_target, name=f'cache-game-{game}', daemon=True)
        _threads[game] = thread
        thread.start()
    return status(game, db=db, runs_dir=runs_dir)


def stop(game: str, *, db: CardDB, runs_dir: Path) -> dict:
    request_stop(runs_dir, game)
    return status(game, db=db, runs_dir=runs_dir)
