"""
* Scheduled catalog refresh for the complete-catalog games.
* Union Arena and Weiß Schwarz publish their entire catalogs with no filter
* language, so the only way to notice a new expansion (or a card added to an
* existing one) is to re-walk the catalog. A re-walk is cheap in bandwidth:
* `_store_and_image` upserts the card row and skips any image already on disk,
* so a refresh only downloads what is genuinely new.
* MTG and Pokémon are deliberately NOT scheduled — their downloads are
* filter-driven ("which slice do you want?"), so there is no correct catalog to
* refresh unattended.
* Must never import from `src/`.

Cooperation with the manual queue is the whole design constraint here; see
`should_skip()` for the cases this refuses to act on.
"""
# Standard Library Imports
import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

# Local Imports
from web.server import cache_runner
from web.shared import download_queue, games
from web.shared.game_cache import checkpoint_path, load_checkpoint, stop_path

# Games whose full catalog is small enough (and filter-free enough) to refresh
# unattended. Overridable with PROXYSHOP_AUTO_CACHE_GAMES.
DEFAULT_GAMES = ('union-arena', 'weiss-schwarz')

_STATE_FILE = 'auto-cache.json'
# How often the loop wakes to *consider* work. The real cadence is the
# per-game interval below; this just needs to be fine-grained enough that a due
# refresh starts promptly without polling hard.
_TICK_SECONDS = 900.0

_thread: Optional[threading.Thread] = None
_lock = threading.Lock()
_state_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def enabled() -> bool:
    return os.environ.get('PROXYSHOP_AUTO_CACHE', '1') == '1'


def scheduled_games() -> tuple[str, ...]:
    """Catalog games to refresh on a schedule (unknown names are dropped)."""
    raw = os.environ.get('PROXYSHOP_AUTO_CACHE_GAMES')
    names = (
        [g.strip().lower() for g in raw.split(',')]
        if raw is not None else list(DEFAULT_GAMES))
    return tuple(g for g in names if g in games.CATALOG_GAMES)


def interval_seconds() -> float:
    try:
        hours = float(os.environ.get('PROXYSHOP_AUTO_CACHE_HOURS', '24'))
    except ValueError:
        hours = 24.0
    return max(hours, 1.0) * 3600.0


def startup_delay_seconds() -> float:
    """Grace period before the first tick.

    Keeps a container restart loop (or a deploy) from stacking provider hits,
    and lets the app finish booting before any network work begins.
    """
    try:
        return max(float(os.environ.get('PROXYSHOP_AUTO_CACHE_STARTUP_DELAY', '300')), 0.0)
    except ValueError:
        return 300.0


# ---------------------------------------------------------------------------
# State (last-refresh stamps, persisted so restarts don't re-trigger)
# ---------------------------------------------------------------------------

def state_path(runs_dir: Path) -> Path:
    return Path(runs_dir) / _STATE_FILE


def load_state(runs_dir: Path) -> dict:
    p = state_path(runs_dir)
    with _state_lock:
        if not p.is_file():
            return {}
        try:
            data = json.loads(p.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}


def save_state(runs_dir: Path, state: dict) -> None:
    p = state_path(runs_dir)
    with _state_lock:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix('.json.part')
            tmp.write_text(json.dumps(state, indent=2) + '\n', encoding='utf-8')
            tmp.replace(p)
        except OSError:
            pass  # a missing stamp only costs one extra refresh


def _stamp(runs_dir: Path, game: str, now: float, note: str) -> None:
    state = load_state(runs_dir)
    state[game] = {
        'last_run': now,
        'last_run_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now)),
        'note': note,
    }
    save_state(runs_dir, state)


# ---------------------------------------------------------------------------
# Collision guards
# ---------------------------------------------------------------------------

def should_skip(runs_dir: Path, game: str) -> Optional[str]:
    """Reason this game must not be auto-refreshed right now, else None.

    The manual queue is the user's; a scheduled refresh is only ever allowed to
    *append* to an idle-or-busy queue, never to override an intent:

    - **Paused by the user.** Pressing Stop leaves the head item queued with a
      'stopped' checkpoint (the stop *flag* is consumed by the run itself, so
      the checkpoint is what persists). Enqueuing here would call
      `_ensure_worker`, which restarts the worker and resumes that very item —
      silently undoing the pause. Never do that.
    - **A stop is in flight.** The flag is still on disk and the worker is
      winding down; adding work now races that shutdown.
    - **The queue is stalled on an error.** The head failed and was left in
      place for the user to inspect/retry; piling on doesn't help.

    A *running* game is deliberately NOT skipped: `download_queue.enqueue`
    de-dupes by spec hash, so a refresh that is already queued or running is a
    no-op, and any other in-flight download simply finishes first.
    """
    if stop_path(runs_dir, game).is_file():
        return 'a stop is in flight'
    head = download_queue.head(runs_dir, game)
    if not head:
        return None  # idle queue — always safe to append
    if cache_runner.is_running(game):
        return None  # busy; enqueue appends (and de-dupes) harmlessly
    ck = load_checkpoint(checkpoint_path(runs_dir, game))
    if ck and ck.status == 'stopped':
        return 'paused by the user'
    return 'queue is stalled on an unfinished item'


def due(runs_dir: Path, game: str, now: float, interval: float) -> bool:
    """True when this game has never refreshed, or the interval has elapsed."""
    entry = load_state(runs_dir).get(game)
    if not isinstance(entry, dict):
        return True
    try:
        last = float(entry.get('last_run') or 0)
    except (TypeError, ValueError):
        return True
    return (now - last) >= interval


# ---------------------------------------------------------------------------
# Tick
# ---------------------------------------------------------------------------

def tick(
    *,
    db,
    images_dir: Path,
    runs_dir: Path,
    offline: bool = False,
    now: Optional[float] = None,
    enqueue_fn=None,
) -> list[dict]:
    """Consider every scheduled game once. Returns what was done, for tests/API.

    Never raises: a provider or disk problem on one game must not kill the
    scheduler thread or block the others.
    """
    if not enabled():
        return []
    if offline:
        return []  # offline mode forbids live provider calls by definition
    enqueue_fn = enqueue_fn or cache_runner.enqueue
    interval = interval_seconds()
    stamp = now if now is not None else time.time()
    actions: list[dict] = []
    for game in scheduled_games():
        if not due(runs_dir, game, stamp, interval):
            actions.append({'game': game, 'action': 'skipped', 'reason': 'not due'})
            continue
        reason = should_skip(runs_dir, game)
        if reason:
            # Not stamped: try again next tick, once the user's work settles.
            cache_runner.log(
                game, runs_dir, f'==> scheduled refresh deferred: {reason}')
            actions.append({'game': game, 'action': 'deferred', 'reason': reason})
            continue
        try:
            enqueue_fn(
                game,
                db=db,
                images_dir=images_dir,
                runs_dir=runs_dir,
                filters={},
                image_kind='png',
                images_only=False,
                fresh=False,  # never wipe the user's queue or checkpoint
            )
        except Exception as e:  # noqa: BLE001 — one bad game must not stop the rest
            cache_runner.log(game, runs_dir, f'!! scheduled refresh failed: {e}')
            actions.append({'game': game, 'action': 'error', 'reason': str(e)})
            continue
        _stamp(runs_dir, game, stamp, 'scheduled catalog refresh')
        cache_runner.log(
            game, runs_dir,
            '==> scheduled refresh queued (full catalog re-walk; '
            'cards and images already stored are skipped)')
        actions.append({'game': game, 'action': 'queued', 'reason': 'due'})
    return actions


def status(runs_dir: Path) -> dict:
    """Scheduler state for the API/UI."""
    interval = interval_seconds()
    state = load_state(runs_dir)
    now = time.time()
    out = {}
    for game in scheduled_games():
        entry = state.get(game) if isinstance(state.get(game), dict) else {}
        try:
            last = float(entry.get('last_run') or 0)
        except (TypeError, ValueError):
            last = 0.0
        out[game] = {
            'last_run_at': entry.get('last_run_at'),
            'next_due_in': max(int(last + interval - now), 0) if last else 0,
            'blocked_by': should_skip(runs_dir, game),
        }
    return {
        'enabled': enabled(),
        'interval_hours': round(interval / 3600.0, 2),
        'games': out,
    }


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------

def _loop(*, db, images_dir: Path, runs_dir: Path, offline: bool) -> None:
    time.sleep(startup_delay_seconds())
    while True:
        try:
            tick(db=db, images_dir=images_dir, runs_dir=runs_dir, offline=offline)
        except Exception:  # noqa: BLE001 — the loop must outlive any single tick
            pass
        time.sleep(_TICK_SECONDS)


def start(*, db, images_dir: Path, runs_dir: Path, offline: bool = False) -> bool:
    """Start the scheduler thread once. Returns True if it was started here."""
    global _thread
    if not enabled() or offline or not scheduled_games():
        return False
    with _lock:
        if _thread is not None and _thread.is_alive():
            return False
        _thread = threading.Thread(
            target=_loop,
            kwargs={
                'db': db, 'images_dir': images_dir,
                'runs_dir': runs_dir, 'offline': offline},
            name='auto-cache-scheduler',
            daemon=True)
        _thread.start()
    return True
