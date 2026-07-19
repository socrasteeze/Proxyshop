"""
* Selective / full-catalog cache for TCGs
* Stores provider cards into the local SQLite DB and optionally downloads
* HQ images under /data/images/. MTG and Pokémon require filters so dumps
* stay scoped. Checkpoints progress so NAS runs can stop and resume safely.
* Must never import from `src/`.
"""
# Standard Library Imports
import json
import os
import signal
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Optional

# Local Imports
from web.shared import games, images
from web.shared.cache_filters import (
    CACHEABLE_GAMES, SELECTIVE_GAMES, build_provider_query, describe_filters,
    filters_equal, filters_require_selection, friendly_filters,
    normalize_filters)
from web.shared.carddb import CardDB

STOP_SUFFIX = '.stop'
CHECKPOINT_VERSION = 1

# Extra pacing on top of provider throttling — keeps bulk cache polite.
# Override via env (seconds). Defaults are intentionally conservative.
CACHE_PAGE_INTERVAL = float(os.environ.get('PROXYSHOP_CACHE_PAGE_INTERVAL', '0.75'))
CACHE_CARD_INTERVAL = float(os.environ.get('PROXYSHOP_CACHE_CARD_INTERVAL', '0.4'))
CACHE_IMAGE_INTERVAL = float(os.environ.get('PROXYSHOP_CACHE_IMAGE_INTERVAL', '0.4'))
# While a full-catalog run is active, slow the shared provider limiter a bit more.
CACHE_PROVIDER_INTERVAL = float(os.environ.get('PROXYSHOP_CACHE_PROVIDER_INTERVAL', '0.35'))


@dataclass
class CacheProgress:
    version: int = CHECKPOINT_VERSION
    game: str = ''
    mode: str = 'catalog'          # catalog | images-only
    status: str = 'running'        # running | stopped | done
    offset: int = 0                # catalog offset or DB offset
    page_size: int = 50
    page: int = 1                  # 1-based pages (UA / Pokémon / Scryfall)
    total_hint: Optional[int] = None
    stored: int = 0
    images_ok: int = 0
    images_fail: int = 0
    images_skip: int = 0
    hydrate: bool = True
    download_images: bool = True
    image_kind: str = 'png'
    filters: dict = None  # type: ignore[assignment]
    query: str = ''
    current: str = ''      # name of the card currently being fetched
    updated_at: str = ''
    message: str = ''

    def __post_init__(self) -> None:
        if self.filters is None:
            self.filters = {}

    def touch(self, message: str = '') -> None:
        self.updated_at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        if message:
            self.message = message


class StopRequested(Exception):
    """Raised when the operator asks the cache run to stop."""


def checkpoint_path(runs_dir: Path, game: str) -> Path:
    return runs_dir / f'{game}.json'


def stop_path(runs_dir: Path, game: str) -> Path:
    return runs_dir / f'{game}{STOP_SUFFIX}'


def load_checkpoint(path: Path) -> Optional[CacheProgress]:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    known = {f.name for f in fields(CacheProgress)}
    filtered = {k: v for k, v in data.items() if k in known}
    return CacheProgress(**filtered)


def save_checkpoint(path: Path, progress: CacheProgress) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    progress.touch()
    tmp = path.with_suffix(path.suffix + '.part')
    tmp.write_text(
        json.dumps(asdict(progress), indent=2, sort_keys=True) + '\n',
        encoding='utf-8')
    tmp.replace(path)


def request_stop(runs_dir: Path, game: str) -> Path:
    """Create the stop flag checked by a running cache-game process."""
    path = stop_path(runs_dir, game)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('stop\n', encoding='utf-8')
    return path


def clear_stop(runs_dir: Path, game: str) -> None:
    stop_path(runs_dir, game).unlink(missing_ok=True)


def reset_checkpoint(runs_dir: Path, game: str) -> None:
    checkpoint_path(runs_dir, game).unlink(missing_ok=True)
    clear_stop(runs_dir, game)


def filter_conflict(runs_dir: Path, game: str, filters: Optional[dict]) -> Optional[str]:
    """Return the saved run's filter label if it conflicts with new filters.

    A conflict means an in-progress (running/stopped, has stored cards or
    advanced past page 1) checkpoint exists whose filters differ from the
    requested ones — so resuming would mix two different downloads. Returns
    None when there's no conflict (no checkpoint, same filters, or the saved
    run is empty/finished and can be safely replaced).
    """
    game = (game or '').strip().lower()
    progress = load_checkpoint(checkpoint_path(runs_dir, game))
    if not progress:
        return None
    norm = normalize_filters(game, filters)
    if filters_equal(progress.filters, norm):
        return None
    if progress.status in ('running', 'stopped') and (
        progress.stored or progress.offset or progress.page > 1
    ):
        return describe_filters(game, progress.filters or {}, progress.query or '')
    return None


class _StopWatch:
    """Cooperative stop via stop-flag file, and optionally SIGINT/SIGTERM (CLI)."""

    def __init__(self, runs_dir: Path, game: str, *, use_signals: bool = True):
        self.runs_dir = runs_dir
        self.game = game
        self.use_signals = use_signals
        self._flag = False
        self._prev_int = None
        self._prev_term = None

    def __enter__(self) -> '_StopWatch':
        clear_stop(self.runs_dir, self.game)
        if not self.use_signals:
            return self

        def _handler(signum, frame):  # noqa: ARG001
            self._flag = True
            print(f'\n==> Stop signal received ({signum}); finishing current card…')

        self._prev_int = signal.getsignal(signal.SIGINT)
        self._prev_term = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        if self.use_signals and self._prev_int is not None:
            signal.signal(signal.SIGINT, self._prev_int)
            signal.signal(signal.SIGTERM, self._prev_term)

    def check(self) -> None:
        if self._flag or stop_path(self.runs_dir, self.game).is_file():
            raise StopRequested('stop requested')


def progress_dict(progress: Optional[CacheProgress], *, db_count: int = 0, running: bool = False) -> dict:
    """JSON-serializable status payload for CLI/API/UI."""
    if not progress:
        return {
            'game': None,
            'status': 'idle',
            'running': False,
            'db_count': db_count,
        }
    return {
        'game': progress.game,
        'status': 'running' if running else progress.status,
        'running': running,
        'mode': progress.mode,
        'offset': progress.offset,
        'page': progress.page,
        'total_hint': progress.total_hint,
        'stored': progress.stored,
        'images_ok': progress.images_ok,
        'images_skip': progress.images_skip,
        'images_fail': progress.images_fail,
        'filters': progress.filters or {},
        'query': progress.query or '',
        'query_label': describe_filters(
            progress.game, progress.filters or {}, progress.query or ''),
        'label': friendly_filters(
            progress.game, progress.filters or {}, progress.query or ''),
        'current': progress.current or '',
        'updated_at': progress.updated_at,
        'message': progress.message,
        'db_count': db_count,
    }


def _polite_sleep(seconds: float, watch: Optional['_StopWatch'] = None) -> None:
    """Sleep in short chunks so stop requests are noticed quickly."""
    if seconds <= 0:
        return
    end = time.monotonic() + seconds
    while True:
        if watch is not None:
            watch.check()
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.25, remaining))


def _fetch_catalog_page(
    game: str,
    progress: CacheProgress,
    db: CardDB,
) -> tuple[list[dict], Optional[int], bool]:
    """Return (cards, total_hint, has_more)."""
    if game == 'riftbound':
        cards, total = games.list_riftbound_page(
            offset=progress.offset,
            limit=progress.page_size,
            hydrate=progress.hydrate)
        # Continue while the running offset hasn't reached the combined total
        # (Riftcodex + ARC). Do NOT stop on a short page: the last Riftcodex
        # page is usually partial where it meets the ARC source, and stopping
        # there is what capped the catalog (~950) before ARC cards were fetched.
        has_more = bool(cards) and (
            total is None or progress.offset + len(cards) < (total or 0))
        if not cards:
            has_more = False
        return cards, total, has_more
    if game == 'union-arena':
        cards, total = games.list_union_arena_page(
            page=progress.page,
            limit=progress.page_size)
        # page indexes product series; total is series count
        has_more = total is not None and progress.page < total
        return cards, total, has_more
    if game == 'mtg':
        return db.list_scryfall_page(progress.query, page=progress.page, store=False)
    if game == 'pokemon':
        cards, total = games.list_pokemon_page(
            progress.query,
            page=progress.page,
            page_size=min(progress.page_size, 250))
        if not cards:
            return cards, total, False
        if total is not None:
            return cards, total, (progress.page * min(progress.page_size, 250)) < total
        return cards, total, len(cards) >= min(progress.page_size, 250)
    raise games.ProviderError(f'No catalog provider for game {game!r}')


def _store_and_image(
    db: CardDB,
    images_dir: Path,
    card: dict,
    progress: CacheProgress,
    watch: Optional['_StopWatch'] = None,
    print_fn=print,
) -> None:
    # Phase 1 — write all DB rows for this card, then commit immediately so
    # the write lock is NOT held across the (slow, network-bound) image
    # downloads below. Holding a transaction open across image fetches is what
    # let a whole page block concurrent web writes into 'database is locked'.
    to_image: list[dict] = [card]
    db.store_card(card, source='catalog', game=progress.game, commit=False)
    progress.stored += 1
    # Riftbound: also store official JA/KO name rows (same art URL, searchable)
    if (
        progress.game == 'riftbound'
        and card.get('lang') == 'en'
        and not str(card.get('id') or '').startswith('rb-arc-')
    ):
        for locale, lang in (('ja-jp', 'ja'), ('ko-kr', 'ko')):
            try:
                variant = games._rb_localized_variant(card, locale, lang)
            except games.ProviderError:
                variant = None
            if not variant:
                continue
            db.store_card(variant, source='catalog', game=progress.game, commit=False)
            progress.stored += 1
            to_image.append(variant)
    db.commit()  # release the write lock before any network image fetch

    if not progress.download_images:
        _polite_sleep(CACHE_CARD_INTERVAL, watch)
        return

    # Phase 2 — download images (no open DB transaction)
    for entry in to_image:
        existing = images.cached_image_path(images_dir, entry['id'], progress.image_kind)
        if existing:
            progress.images_skip += 1
            _polite_sleep(CACHE_CARD_INTERVAL * 0.25, watch)
            continue
        _polite_sleep(CACHE_IMAGE_INTERVAL, watch)
        path = images.ensure_image(
            db.session, entry, progress.image_kind, images_dir, offline=False)
        if path:
            progress.images_ok += 1
        else:
            progress.images_fail += 1
            name = entry.get('name') or entry.get('id') or '?'
            print_fn(f"!! image failed: {name} ({entry.get('id')})")
        _polite_sleep(CACHE_CARD_INTERVAL, watch)


def run_cache_game(
    *,
    db: CardDB,
    game: str,
    images_dir: Path,
    runs_dir: Path,
    download_images: bool = True,
    hydrate: bool = True,
    image_kind: str = 'png',
    page_size: int = 50,
    images_only: bool = False,
    fresh: bool = False,
    filters: Optional[dict] = None,
    use_signals: bool = True,
    print_fn=print,
    on_progress=None,
) -> CacheProgress:
    """Cache a TCG catalog (+ images) with stop/resume support.

    MTG and Pokémon require selective filters (set/type/rarity/…). Small TCGs
    (Riftbound/Union Arena) may run unfiltered full-catalog mirrors.
    """
    game = (game or '').strip().lower()
    if game not in CACHEABLE_GAMES and game not in games.CATALOG_GAMES:
        raise ValueError(
            f'Unsupported game {game!r}; choose from: '
            + ', '.join(games.CATALOG_GAMES))
    if image_kind not in images.IMAGE_KINDS:
        raise ValueError(f'Unknown image kind {image_kind!r}')

    # Scryfall pages are fixed at 175; Pokémon max pageSize is 250
    if game == 'mtg':
        page_size = 175
    elif game == 'pokemon':
        page_size = min(max(page_size, 1), 250)

    norm_filters = normalize_filters(game, filters)
    query = ''
    if not images_only and game in SELECTIVE_GAMES:
        if filters_require_selection(game, norm_filters):
            raise ValueError(
                f'{game} cache needs filters (set, type, rarity, art, …) '
                'so it does not dump the entire catalog')
        query = build_provider_query(game, norm_filters)
    elif not images_only and game in ('riftbound', 'union-arena'):
        query = norm_filters.get('q') or ''

    runs_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)
    ck_path = checkpoint_path(runs_dir, game)

    progress = load_checkpoint(ck_path)
    if progress and not fresh and not filters_equal(progress.filters, norm_filters):
        if progress.status in ('running', 'stopped') and (progress.stored or progress.offset or progress.page > 1):
            raise ValueError(
                f'Active {game} checkpoint uses different filters '
                f'({describe_filters(game, progress.filters or {}, progress.query)}). '
                'Resume that run, or pass fresh=True / --fresh to start a new filter.')
        # Done (or empty) checkpoint with different filters → start new
        fresh = True

    if fresh:
        reset_checkpoint(runs_dir, game)
        progress = None

    if progress and progress.status == 'done' and not fresh:
        if filters_equal(progress.filters, norm_filters):
            print_fn(
                f'==> {game}: previous run already done for this filter. '
                'Use --fresh to start over.')
            return progress
        fresh = True
        reset_checkpoint(runs_dir, game)
        progress = None

    if progress is None:
        progress = CacheProgress(
            game=game,
            mode='images-only' if images_only else 'catalog',
            page_size=page_size,
            hydrate=hydrate,
            download_images=download_images,
            image_kind=image_kind,
            filters=norm_filters,
            query=query,
        )
    else:
        progress.mode = 'images-only' if images_only else progress.mode
        if images_only:
            progress.mode = 'images-only'
        progress.hydrate = hydrate
        progress.download_images = download_images
        progress.image_kind = image_kind
        progress.page_size = page_size
        progress.filters = progress.filters or norm_filters
        if not progress.query:
            progress.query = query
        if game in SELECTIVE_GAMES and not images_only and not progress.query:
            progress.query = build_provider_query(game, progress.filters)
        progress.status = 'running'
        label = (
            f'page {progress.page}'
            if game != 'riftbound'
            else f'offset {progress.offset}')
        print_fn(
            f'==> Resuming {game} ({describe_filters(game, progress.filters, progress.query)}) '
            f'from {label} (stored={progress.stored}, images_ok={progress.images_ok})')

    progress.touch('starting')
    save_checkpoint(ck_path, progress)
    print_fn(f'==> Query: {progress.query or "(full catalog)"}')

    prev_provider = games._provider_limiter.min_interval
    prev_image = getattr(db.session, '_min_interval', None)
    games._provider_limiter.set_interval(max(prev_provider, CACHE_PROVIDER_INTERVAL))
    if prev_image is not None:
        db.session._min_interval = max(prev_image, CACHE_IMAGE_INTERVAL)

    try:
        with _StopWatch(runs_dir, game, use_signals=use_signals) as watch:
            if progress.mode == 'images-only':
                _run_images_only(db, images_dir, progress, watch, print_fn, on_progress)
            else:
                _run_catalog(db, images_dir, progress, watch, print_fn, on_progress)
            progress.current = ''
            progress.status = 'done'
            progress.touch('complete')
            save_checkpoint(ck_path, progress)
            clear_stop(runs_dir, game)
            print_fn(
                f'==> Done {game}: stored={progress.stored} '
                f'images_ok={progress.images_ok} skip={progress.images_skip} '
                f'fail={progress.images_fail}')
    except StopRequested as e:
        progress.status = 'stopped'
        progress.touch(str(e))
        save_checkpoint(ck_path, progress)
        clear_stop(runs_dir, game)
        label = (
            f'page {progress.page}'
            if game != 'riftbound'
            else f'offset {progress.offset}')
        print_fn(
            f'==> Stopped {game} at {label}. Re-run the same command to resume.')
    except Exception as e:
        progress.status = 'stopped'
        progress.touch(f'error: {e}')
        save_checkpoint(ck_path, progress)
        clear_stop(runs_dir, game)
        raise
    finally:
        games._provider_limiter.set_interval(prev_provider)
        if prev_image is not None:
            db.session._min_interval = prev_image
    return progress


def _run_catalog(
    db: CardDB,
    images_dir: Path,
    progress: CacheProgress,
    watch: _StopWatch,
    print_fn,
    on_progress=None,
) -> None:
    while True:
        watch.check()
        cards, total, has_more = _fetch_catalog_page(progress.game, progress, db)
        if total is not None:
            progress.total_hint = total
        if not cards:
            # Empty product series (or transient miss): advance while more remain
            if has_more:
                if progress.game == 'riftbound':
                    progress.offset += progress.page_size
                else:
                    progress.page += 1
                save_checkpoint(checkpoint_path(watch.runs_dir, progress.game), progress)
                _polite_sleep(CACHE_PAGE_INTERVAL, watch)
                continue
            break
        try:
            for card in cards:
                watch.check()
                progress.current = str(card.get('name') or '')
                if on_progress:
                    on_progress(progress)
                _store_and_image(db, images_dir, card, progress, watch, print_fn)
        finally:
            # One commit per provider page keeps stops/errors durable without
            # per-card WAL churn.
            db.commit()
        if progress.game == 'riftbound':
            progress.offset += len(cards)
        else:
            progress.page += 1
        save_checkpoint(checkpoint_path(watch.runs_dir, progress.game), progress)
        label = (
            f'offset={progress.offset}'
            if progress.game == 'riftbound'
            else f'page={progress.page - 1}')
        total_bit = f'/{progress.total_hint}' if progress.total_hint else ''
        print_fn(
            f'… {progress.game} {label}{total_bit} '
            f'stored={progress.stored} images_ok={progress.images_ok}')
        if not has_more:
            break
        _polite_sleep(CACHE_PAGE_INTERVAL, watch)


def _run_images_only(
    db: CardDB,
    images_dir: Path,
    progress: CacheProgress,
    watch: _StopWatch,
    print_fn,
    on_progress=None,
) -> None:
    total = db.count_by_game(progress.game)
    progress.total_hint = total
    processed = 0
    for card in db.iter_by_game(progress.game, offset=progress.offset, batch=100):
        watch.check()
        processed += 1
        progress.offset += 1
        progress.current = str(card.get('name') or '')
        if on_progress:
            on_progress(progress)
        if not progress.download_images:
            continue
        existing = images.cached_image_path(
            images_dir, card['id'], progress.image_kind)
        if existing:
            progress.images_skip += 1
        else:
            _polite_sleep(CACHE_IMAGE_INTERVAL, watch)
            path = images.ensure_image(
                db.session, card, progress.image_kind, images_dir, offline=False)
            if path:
                progress.images_ok += 1
            else:
                progress.images_fail += 1
                name = card.get('name') or card.get('id') or '?'
                print_fn(f"!! image failed: {name} ({card.get('id')})")
            _polite_sleep(CACHE_CARD_INTERVAL, watch)
        if processed % 25 == 0:
            save_checkpoint(checkpoint_path(watch.runs_dir, progress.game), progress)
            print_fn(
                f'… {progress.game} images {progress.offset}/{total} '
                f'ok={progress.images_ok} skip={progress.images_skip} '
                f'fail={progress.images_fail}')
    save_checkpoint(checkpoint_path(watch.runs_dir, progress.game), progress)
