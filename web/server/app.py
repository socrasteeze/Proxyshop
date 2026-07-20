"""
* Proxyshop Web Server
* FastAPI app served from the NAS: browser UI + REST API + worker endpoints.
* NEVER imports from `src/` (Windows-only package).

Environment:
    PROXYSHOP_DATA_DIR      Data volume root (default: ./data)
    PROXYSHOP_WORKER_TOKEN  Bearer token for /api/worker/* (default: dev-token)
    PROXYSHOP_OFFLINE       '1' = never call Scryfall (cache/bulk only)
    PROXYSHOP_MAX_UPLOAD_MB Upload cap for art files (default: 50)
"""
# Standard Library Imports
import asyncio
import json
import os
import re
import secrets
import shutil
import time
import uuid as uuid_module
import zipfile
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional

# Third Party Imports
from fastapi import (
    Depends, FastAPI, File, Form, Header, HTTPException,
    Request, Response, UploadFile)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Local Imports
from web.server.db import JobStore
from web.server import cache_runner
from web.shared import games, images, sheets
from web.shared.carddb import CardDB, GALLERY_SORTS
from web.shared.decklist import fetch_deck_url, parse_decklist_text
from web.shared.schema import (
    Capabilities, DeckCardLine, DeckImportReport, JobResult, JobStatus, RenderMode)
from web.shared.compose.engine import COMPOSE_GAMES, compose_card

# Games that can be rendered somehow (Photoshop and/or NAS compose)
RENDERABLE_GAMES = frozenset({'mtg', 'pokemon', 'riftbound'})
# Games that use the Windows worker + PSD path by default
PHOTOSHOP_GAMES = frozenset({'mtg'})

"""
* Configuration
"""

DATA_DIR = Path(os.environ.get('PROXYSHOP_DATA_DIR', 'data'))
WORKER_TOKEN = os.environ.get('PROXYSHOP_WORKER_TOKEN', 'dev-token')
OFFLINE = os.environ.get('PROXYSHOP_OFFLINE', '0') == '1'
MAX_UPLOAD_BYTES = int(os.environ.get('PROXYSHOP_MAX_UPLOAD_MB', '50')) * 1024 * 1024

JOBS_DIR = DATA_DIR / 'jobs'
IMAGES_DIR = DATA_DIR / 'images'
SHEETS_DIR = DATA_DIR / 'sheets'
CACHE_RUNS_DIR = DATA_DIR / 'cache-runs'
TEMPLATES_DIR = Path(__file__).parent / 'templates'
STATIC_DIR = Path(__file__).parent / 'static'

ALLOWED_ART_TYPES = {'.png', '.jpg', '.jpeg', '.webp', '.tif', '.tiff'}

"""
* App State
"""

app = FastAPI(title='Proxyshop Web', docs_url='/api/docs')
app.mount('/static', StaticFiles(directory=STATIC_DIR), name='static')
templates = Jinja2Templates(directory=TEMPLATES_DIR)

store = JobStore(DATA_DIR / 'jobs.db')
carddb = CardDB(DATA_DIR / 'cards.db', offline=OFFLINE)

"""
* Rate Limiting (simple sliding-window per client, in-memory)
"""

# scope -> (max requests, window seconds)
RATE_LIMITS = {
    'submit': (20, 60),      # job submissions
    'import': (6, 60),       # deck imports (may fan out to Scryfall)
    'api': (120, 60),        # general API reads
    'image': (300, 60),      # image serves (mostly cache hits; grids load many)
    'cache': (6, 60),        # start/stop full-TCG cache runs
}
_hits: dict[tuple[str, str], deque] = defaultdict(deque)


def rate_limit(request: Request, scope: str) -> None:
    """Sliding-window limiter; raises 429 with Retry-After when exceeded."""
    limit, window = RATE_LIMITS[scope]
    key = (request.client.host if request.client else 'unknown', scope)
    now = time.monotonic()
    q = _hits[key]
    while q and q[0] <= now - window:
        q.popleft()
    if len(q) >= limit:
        retry = int(window - (now - q[0])) + 1
        raise HTTPException(
            status_code=429,
            detail='Rate limit exceeded, slow down.',
            headers={'Retry-After': str(retry)})
    q.append(now)


"""
* Auth
"""


def require_worker(authorization: Optional[str] = Header(default=None)) -> None:
    """Bearer-token gate for worker endpoints."""
    expected = f'Bearer {WORKER_TOKEN}'
    if not authorization or not secrets.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail='Invalid worker token')


"""
* Helpers
"""


def _job_dir(job_id: str) -> Path:
    d = JOBS_DIR / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _parse_art_transform(raw: Optional[str]) -> Optional[dict]:
    """Parse optional art_transform form JSON; None if empty/invalid."""
    if not raw or not str(raw).strip():
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    from web.shared.compose.text import normalize_art_transform
    return normalize_art_transform(data)


def _parse_client_card_json(raw: Optional[str]) -> Optional[dict]:
    """Parse optional editor card_json for job submit; None if missing/bad."""
    if not raw or not str(raw).strip():
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or not (data.get('name') or data.get('id')):
        return None
    return data


async def _save_upload(upload: UploadFile, dest: Path) -> int:
    """Stream an upload to disk, enforcing the size cap. Returns bytes written."""
    written = 0
    with open(dest, 'wb') as f:
        while chunk := await upload.read(1 << 20):
            written += len(chunk)
            if written > MAX_UPLOAD_BYTES:
                f.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail=f'File too large (limit {MAX_UPLOAD_BYTES >> 20}MB)')
            f.write(chunk)
    return written


def _resolve_card(
    name: str,
    set_code: Optional[str],
    number: Optional[str],
    lang: str,
    game: str = 'mtg',
) -> Optional[dict]:
    """Resolve a card through the local DB (cache-first, API fallback unless offline)."""
    if game == 'mtg':
        if set_code and number:
            return carddb.get_card(set_code, number, lang)
        return carddb.find_card(name, set_code, lang)
    # Non-MTG: local cache first, then live provider search
    local = carddb.search_local(name, limit=20, game=game)
    if set_code:
        set_l = set_code.lower()
        local = [
            c for c in local
            if (c.get('set') or '').lower() == set_l
            or (c.get('set_name') or '').lower() == set_l]
    if number:
        num = str(number)
        exact = [c for c in local if str(c.get('collector_number') or '') == num]
        if exact:
            return exact[0]
    if local:
        return local[0]
    if OFFLINE or game not in games.PROVIDERS:
        return None
    try:
        results = games.PROVIDERS[game](name, 20)
    except Exception:
        return None
    for card in results:
        carddb.store_card(card, commit=False, game=game)
    carddb._conn().commit()
    if set_code:
        set_l = set_code.lower()
        results = [
            c for c in results
            if (c.get('set') or '').lower() == set_l
            or (c.get('set_name') or '').lower() == set_l] or results
    if number:
        num = str(number)
        exact = [c for c in results if str(c.get('collector_number') or '') == num]
        if exact:
            return exact[0]
    return results[0] if results else None


def _capabilities() -> Optional[Capabilities]:
    raw = store.get_capabilities()
    if not raw:
        return None
    try:
        return Capabilities.model_validate_json(raw)
    except ValueError:
        return None


"""
* Browser Pages
"""


@app.get('/', response_class=HTMLResponse)
def page_index(request: Request, card_id: str = ''):
    caps = _capabilities()
    card = carddb.get_by_id(card_id) if card_id else None
    game = (card or {}).get('game', 'mtg') if card else 'mtg'
    if card and game not in COMPOSE_GAMES and game not in RENDERABLE_GAMES:
        card = None
        game = 'mtg'
    from web.shared.compose.frames import FRAME_STYLES
    return templates.TemplateResponse(request, 'index.html', {
        'jobs': store.list_jobs(30),
        'workers': store.get_workers(),
        'caps': caps,
        'stats': carddb.stats(),
        'games': games.GAME_LABELS,
        'renderable_games': sorted(RENDERABLE_GAMES),
        'compose_games': sorted(COMPOSE_GAMES),
        'frame_styles': FRAME_STYLES,
        'card': card,
        'card_id': card_id if card else '',
        'game': game,
        'game_label': games.GAME_LABELS.get(game, game),
    })


@app.get('/jobs/{job_id}', response_class=HTMLResponse)
def page_job(request: Request, job_id: str):
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail='Job not found')
    return templates.TemplateResponse(request, 'job_detail.html', {'job': job})


@app.get('/decks', response_class=HTMLResponse)
def page_decks(request: Request):
    return templates.TemplateResponse(request, 'decks.html', {
        'decks': carddb.get_decks(),
        'stats': carddb.stats(),
    })


PER_PAGE_OPTIONS = (24, 48, 60, 96, 120)


def _compose_gallery_query(q: str, field_filters: dict) -> str:
    """Fold structured dropdown selections into a field-syntax query string.

    Values with spaces are quoted so the cardquery tokenizer keeps them whole
    (e.g. rarity="Rare Holo"). The result is parsed by list_gallery exactly like
    a typed query, so the dropdowns and the text box share one code path.
    """
    parts = [q.strip()] if q and q.strip() else []
    for field, value in field_filters.items():
        value = (value or '').strip()
        if not value:
            continue
        parts.append(f'{field}:"{value}"' if ' ' in value else f'{field}:{value}')
    return ' '.join(parts)


# Gallery dropdown fields → cardquery field name
GALLERY_FILTER_FIELDS = {
    'ftype': 'type',
    'fsupertype': 'supertype',
    'fsubtype': 'subtype',
    'fdomain': 'domain',
    'frarity': 'rarity',
}

# Card-library sort menu. Labels mirror Scryfall; the price/mana/EDHREC keys
# read Scryfall fields so they're only offered for MTG. Others get the basics.
GALLERY_SORT_LABELS = [
    ('name', 'Name'),
    ('released', 'Release date'),
    ('set', 'Set / number'),
    ('rarity', 'Rarity'),
    ('color', 'Color'),
    ('usd', 'Price: USD'),
    ('tix', 'Price: TIX'),
    ('eur', 'Price: EUR'),
    ('cmc', 'Mana value'),
    ('power', 'Power'),
    ('toughness', 'Toughness'),
    ('artist', 'Artist name'),
    ('edhrec', 'EDHREC rank'),
    ('newest', 'Recently added'),
]
_BASIC_SORT_KEYS = {'name', 'set', 'released', 'rarity', 'artist', 'newest'}


def _gallery_sort_options(game: str) -> list[tuple[str, str]]:
    """Sort choices for the given game (full Scryfall menu for MTG)."""
    if game == 'mtg':
        return list(GALLERY_SORT_LABELS)
    return [(k, label) for k, label in GALLERY_SORT_LABELS
            if k in _BASIC_SORT_KEYS]


def _gallery_detail_fields(data: dict, game: str) -> dict:
    """Compact display fields for the List / Full / Checklist views.

    Reads from the stored card object; falls back to provider_data for the
    non-MTG games so their rows still show type / rarity / artist.
    """
    data = data or {}
    provider = data.get('provider_data') or {}

    def first(*keys):
        for k in keys:
            v = data.get(k)
            if v in (None, ''):
                v = provider.get(k)
            if v not in (None, ''):
                return v
        return None

    prices = data.get('prices') or {}
    power, toughness = data.get('power'), data.get('toughness')
    pt = f'{power}/{toughness}' if power is not None and toughness is not None else None
    return {
        'mana_cost': first('mana_cost'),
        'type_line': first('type_line', 'supertype', 'cardType'),
        'rarity': first('rarity'),
        'artist': first('artist'),
        'oracle_text': first('oracle_text', 'effect', 'ability', 'description'),
        'flavor_text': data.get('flavor_text'),
        'pt': pt,
        'hp': provider.get('hp'),
        'usd': prices.get('usd'),
        'eur': prices.get('eur'),
        'tix': prices.get('tix'),
        'legalities': data.get('legalities') or {},
    }


@app.get('/gallery', response_class=HTMLResponse)
def page_gallery(
    request: Request,
    game: str = '',
    q: str = '',
    set: str = '',
    sort: str = 'name',
    direction: str = '',
    page: int = 1,
    per_page: int = 60,
    view: str = 'grid',
    arts: str = 'unique',
    series: str = '',
    ftype: str = '',
    fsupertype: str = '',
    fsubtype: str = '',
    fdomain: str = '',
    frarity: str = '',
):
    """Browse all locally cached cards as a gallery."""
    game = (game or '').strip().lower()
    if game and game not in games.GAMES:
        raise HTTPException(status_code=422, detail=f'Unknown game {game!r}')
    # Scryfall-style sort menu is only meaningful for MTG (price/mana/EDHREC
    # come from Scryfall fields); other games keep the basic keys.
    sort_choices = _gallery_sort_options(game)
    valid_sorts = {k for k, _ in sort_choices}
    sort = sort if sort in valid_sorts else 'name'
    direction = direction if direction in {'asc', 'desc'} else ''
    resolved_dir = direction or GALLERY_SORTS.get(
        sort, ('', False, 'asc'))[2]
    view = view if view in {'grid', 'list', 'full', 'checklist'} else 'grid'
    want_detail = view in {'list', 'full', 'checklist'}
    arts = arts if arts in {'unique', 'combine'} else 'unique'
    group_arts = arts == 'combine'
    page = max(int(page or 1), 1)
    try:
        per_page = int(per_page or 60)
    except (TypeError, ValueError):
        per_page = 60
    if per_page not in PER_PAGE_OPTIONS:
        per_page = min(PER_PAGE_OPTIONS, key=lambda n: abs(n - per_page))

    gallery_filters = {
        'ftype': ftype, 'fsupertype': fsupertype, 'fsubtype': fsubtype,
        'fdomain': fdomain, 'frarity': frarity,
    }
    effective_q = _compose_gallery_query(
        q, {GALLERY_FILTER_FIELDS[k]: v for k, v in gallery_filters.items()})

    # Series (IP) groups several sets under one anime/franchise. Selecting a
    # series filters to all its sets; a specific Set narrows within it.
    series_options = carddb.series_list(game) if game else []
    series = (series or '').strip()
    series_sets = next(
        (s['sets'] for s in series_options if s['series'] == series), None)
    # Set picklist is scoped to the selected series when one is active.
    if series and series_sets is not None:
        set_options = [s for s in carddb.distinct_sets(game) if s['code'] in series_sets]
    else:
        set_options = carddb.distinct_sets(game) if game else []

    cards, total = carddb.list_gallery(
        game=game or None,
        q=effective_q,
        set_code=set,
        set_codes=series_sets if not set else None,
        offset=(page - 1) * per_page,
        limit=per_page,
        sort=sort,
        direction=resolved_dir,
        group_arts=group_arts,
        detail=want_detail,
    )
    counts = carddb.counts_by_game()
    pages = max(1, (total + per_page - 1) // per_page) if total else 1
    if page > pages:
        # Out-of-range page (stale link / shrunk filter): clamp and re-query
        # so the last page still shows cards instead of an empty grid.
        page = pages
        cards, total = carddb.list_gallery(
            game=game or None,
            q=effective_q,
            set_code=set,
            set_codes=series_sets if not set else None,
            offset=(page - 1) * per_page,
            limit=per_page,
            sort=sort,
            group_arts=group_arts,
        )

    def page_url(p: int, **overrides) -> str:
        from urllib.parse import urlencode
        params = {
            'sort': overrides.get('sort', sort),
            'page': p,
            'per_page': overrides.get('per_page', per_page),
            'view': overrides.get('view', view),
            'arts': overrides.get('arts', arts),
        }
        d = overrides.get('direction', direction)
        if d:
            params['direction'] = d
        g = overrides.get('game', game)
        qq = overrides.get('q', q)
        ss = overrides.get('set', set)
        sr = overrides.get('series', series)
        if g:
            params['game'] = g
        if qq:
            params['q'] = qq
        if ss:
            params['set'] = ss
        if sr:
            params['series'] = sr
        # Carry structured dropdown filters across pagination/sort links unless
        # explicitly overridden (e.g. Clear filters passes them empty).
        for key in GALLERY_FILTER_FIELDS:
            val = overrides.get(key, gallery_filters.get(key))
            if val:
                params[key] = val
        return '/gallery?' + urlencode(params)

    # Compact page-number window: 1 … (page±2) … last
    nearby = [p for p in range(page - 2, page + 3) if 1 <= p <= pages]
    page_links: list[int] = []
    if pages > 1:
        if 1 not in nearby:
            page_links.append(1)
        page_links.extend(nearby)
        if pages not in nearby:
            page_links.append(pages)

    # Facet dropdowns for the selected game, populated from what's actually
    # cached locally (robust, offline, accurate to the library).
    facets = carddb.distinct_facets(game) if game else {}
    # Only surface the Series picker when it actually groups sets (i.e. some IP
    # spans multiple sets, as in Union Arena) — otherwise it just mirrors Set.
    show_series = bool(game) and len(series_options) < sum(
        1 for _ in carddb.distinct_sets(game))

    # For the detail-bearing views, fold the stored card JSON into the compact
    # display fields the List / Full / Checklist templates render. The Full view
    # additionally lists the card's other printings (Scryfall-style "Prints").
    if want_detail:
        for c in cards:
            data = c.pop('data', {})
            c['detail'] = _gallery_detail_fields(data, c['game'])
            if view == 'full':
                group = carddb.list_art_group(c['id'], limit=13)
                for p in group:
                    p['current'] = p['id'] == c['id']
                    if p['current']:
                        c['set_name'] = p.get('set_name') or (c.get('set') or '').upper()
                c['prints'] = group
                c['print_total'] = c.get('art_count') or len(group)

    return templates.TemplateResponse(request, 'gallery.html', {
        'game': game,
        'q': q,
        'set_code': set,
        'series': series,
        'series_options': series_options if show_series else [],
        'filters': gallery_filters,
        'facets': facets,
        'set_options': set_options,
        'sort': sort,
        'sort_options': sort_choices,
        'direction': direction,
        'resolved_dir': resolved_dir,
        'page': page,
        'pages': pages,
        'page_links': page_links,
        'per_page': per_page,
        'per_page_options': PER_PAGE_OPTIONS,
        'view': view,
        'arts': arts,
        'cards': cards,
        'total': total,
        'total_all': sum(counts.values()),
        'counts': counts,
        'games': games.GAME_LABELS,
        'page_url': page_url,
        'stats': carddb.stats(),
    })


@app.get('/api/cards/gallery')
def api_cards_gallery(
    request: Request,
    game: str = '',
    q: str = '',
    set: str = '',
    sort: str = 'name',
    offset: int = 0,
    limit: int = 60,
    arts: str = 'unique',
):
    """JSON gallery of locally cached cards (no live provider calls)."""
    rate_limit(request, 'api')
    game = (game or '').strip().lower()
    if game and game not in games.GAMES:
        raise HTTPException(status_code=422, detail=f'Unknown game {game!r}')
    sort = sort if sort in {'name', 'set', 'newest', 'id'} else 'name'
    arts = arts if arts in {'unique', 'combine'} else 'unique'
    cards, total = carddb.list_gallery(
        game=game or None,
        q=q,
        set_code=set,
        offset=offset,
        limit=limit,
        sort=sort,
        group_arts=(arts == 'combine'),
    )
    return {
        'total': total,
        'offset': max(int(offset), 0),
        'limit': min(max(int(limit), 1), 200),
        'arts': arts,
        'counts': carddb.counts_by_game(),
        'cards': [{
            'id': c.get('id'),
            'name': c.get('name'),
            'set': c.get('set'),
            'set_name': c.get('set_name'),
            'collector_number': c.get('collector_number'),
            'game': c.get('game', 'mtg'),
            'art_count': c.get('art_count', 1),
            'thumb': (
                f"/api/cards/{c['id']}/image?kind=large" if c.get('id') else None),
        } for c in cards],
    }


def _search_cards(q: str, limit: int, game: str = 'mtg') -> tuple[list[dict], str]:
    """Local-first card search with live provider fallback (cache-through).

    game='' (or 'all') searches every locally cached game with no live
    fallback — cross-game fan-out to external providers is never done.
    MTG falls back to Scryfall; other games use their provider from
    web.shared.games. Fallback results are always cached locally.
    """
    game = (game or '').strip().lower()
    if game in ('', 'all'):
        return carddb.search_local(q, limit=limit, game=None), 'local'
    if game not in games.GAMES:
        raise HTTPException(status_code=422, detail=f'Unknown game {game!r}')
    results = carddb.search_local(q, limit=limit, game=game)
    if results:
        return results, 'local'
    if game == 'mtg':
        return carddb.search_scryfall(q, limit=limit), 'scryfall'
    if OFFLINE:
        return [], 'local'
    try:
        results = games.PROVIDERS[game](q, limit)
    except games.ProviderError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f'{games.GAME_LABELS[game]} provider unavailable: {e}') from e
    for card in results:
        carddb.store_card(card, commit=False, game=game)
    carddb._conn().commit()
    return results, 'live'


@app.get('/search', response_class=HTMLResponse)
def page_search(request: Request, q: str = '', game: str = 'mtg'):
    game = (game or '').strip().lower()
    if game == 'all':
        game = ''
    results, source, error = [], 'local', None
    if len(q) >= 2:
        try:
            results, source = _search_cards(q, 60, game)
        except HTTPException as e:
            error = e.detail  # show provider problems inline instead of a bare 502
    prices = carddb.get_prices([c['id'] for c in results if c.get('id')])
    return templates.TemplateResponse(request, 'search.html', {
        'q': q, 'game': game, 'games': games.GAME_LABELS,
        'catalog_games': list(games.CATALOG_GAMES),
        'results': results, 'source': source, 'prices': prices, 'error': error,
        'offline': OFFLINE, 'stats': carddb.stats()})


@app.get('/logs', response_class=HTMLResponse)
def page_logs(request: Request, game: str = 'mtg'):
    """Live cache-run log viewer."""
    game = (game or 'mtg').strip().lower()
    if game not in games.CATALOG_GAMES:
        game = games.CATALOG_GAMES[0]
    return templates.TemplateResponse(request, 'logs.html', {
        'game': game,
        'games': games.GAME_LABELS,
        'catalog_games': list(games.CATALOG_GAMES),
    })


"""
* Public API: Jobs
"""


@app.post('/api/jobs')
async def api_submit_job(
    request: Request,
    card_name: str = Form(min_length=1, max_length=200),
    set_code: Optional[str] = Form(default=None, max_length=10),
    collector_number: Optional[str] = Form(default=None, max_length=10),
    template_name: Optional[str] = Form(default=None, max_length=100),
    lang: str = Form(default='en', max_length=5),
    game: str = Form(default='mtg', max_length=20),
    render_mode: str = Form(default='auto', max_length=20),
    art: Optional[UploadFile] = File(default=None),
    card_json: Optional[str] = Form(default=None, max_length=200_000),
    art_transform: Optional[str] = Form(default=None, max_length=2000),
    idempotency_key: Optional[str] = Form(default=None, max_length=100),
):
    """Submit a render job.

    render_mode:
      - auto: MTG → Photoshop worker; pokemon/riftbound → NAS compose
      - compose: Pillow blank-frame compositor on the NAS (MTG/Pokémon/Riftbound)
      - photoshop: queue for the Windows worker (requires templates)

    Art upload is optional for MTG (Scryfall art crop) and for compose-mode
    cards (HQ scan / art crop used as art). Photoshop Pokémon still prefers an
    explicit art upload.

    Optional card_json (from the Make editor) keeps printing details in sync with
    the live preview. Optional art_transform pans/zooms custom art in Compose.
    """
    rate_limit(request, 'submit')

    game = (game or 'mtg').strip().lower()
    mode = (render_mode or 'auto').strip().lower()
    if mode not in {m.value for m in RenderMode}:
        raise HTTPException(status_code=422, detail=f'Unknown render_mode {mode!r}')
    if game not in RENDERABLE_GAMES:
        raise HTTPException(
            status_code=422,
            detail=f'Game {game!r} is not renderable — supported: '
                   + ', '.join(sorted(RENDERABLE_GAMES)))

    # Resolve effective mode. Auto keeps MTG on Photoshop; others prefer compose.
    if mode == 'auto':
        effective = 'compose' if game in ('pokemon', 'riftbound') else 'photoshop'
    else:
        effective = mode
    if effective == 'compose' and game not in COMPOSE_GAMES:
        raise HTTPException(
            status_code=422,
            detail=f'Compose mode only supports: {", ".join(sorted(COMPOSE_GAMES))}')
    if effective == 'photoshop' and game != 'mtg':
        # Non-MTG Photoshop only when a worker advertises the game; else compose
        caps = _capabilities()
        if not (caps and caps.games and game in caps.games):
            if game in COMPOSE_GAMES:
                effective = 'compose'
            else:
                raise HTTPException(
                    status_code=422,
                    detail=f'No worker supports Photoshop rendering for {game!r}.')
    elif effective == 'photoshop' and game == 'mtg':
        pass  # always queueable for MTG

    has_upload = art is not None and (art.filename or '') != ''
    if has_upload:
        suffix = Path(art.filename or 'art.png').suffix.lower()
        if suffix not in ALLOWED_ART_TYPES:
            raise HTTPException(
                status_code=422,
                detail=f'Unsupported art file type {suffix!r}')

    client_card = _parse_client_card_json(card_json)
    resolved = _resolve_card(card_name.strip(), set_code, collector_number, lang, game=game)
    # Prefer editor payload (same details as preview); fall back to DB resolve
    card = client_card or resolved
    if card is None and resolved is not None:
        card = resolved

    transform = _parse_art_transform(art_transform)
    store_payload = dict(card) if card else None
    if store_payload is not None:
        if transform:
            store_payload['_art_transform'] = transform
        if has_upload:
            store_payload['_custom_art'] = True
    card_json_out = (
        json.dumps(store_payload, separators=(',', ':')) if store_payload else None)

    art_source: Optional[Path] = None
    if not has_upload:
        if game == 'mtg':
            if not card:
                raise HTTPException(
                    status_code=422,
                    detail='Card not found — upload an art file, or check the '
                           'name/set spelling so Scryfall art can be used.')
            kind = 'art_crop' if effective == 'photoshop' else 'art_crop'
            art_source = images.ensure_image(
                carddb.session, card, kind, IMAGES_DIR, offline=OFFLINE)
            if not art_source and effective == 'compose':
                art_source = images.ensure_image(
                    carddb.session, card, 'png', IMAGES_DIR, offline=OFFLINE)
            if not art_source and not (effective == 'compose' and client_card):
                raise HTTPException(
                    status_code=422,
                    detail='No Scryfall art available for this card — upload an art file.')
        elif effective == 'compose':
            # Use cached HQ scan as the art layer when no upload
            if not card:
                raise HTTPException(
                    status_code=422,
                    detail='Card not found — search first or upload art.')
            art_source = images.ensure_image(
                carddb.session, card, 'png', IMAGES_DIR, offline=OFFLINE)
            # Blank / custom cards may have no cached image — compose frame-only
            if not art_source and not client_card:
                raise HTTPException(
                    status_code=422,
                    detail='No card image available — upload an art file.')
        else:
            raise HTTPException(
                status_code=422,
                detail=f'{games.GAME_LABELS.get(game, game)} Photoshop jobs '
                       'require an art upload.')

    job = store.submit(
        card_name=card_name.strip(),
        set_code=(set_code or None),
        collector_number=(collector_number or None),
        template_name=(template_name or None),
        lang=lang,
        game=game,
        render_mode=effective,
        card_json=card_json_out,
        idempotency_key=(idempotency_key or None))

    # Persist art next to the job
    if has_upload:
        art_name = f'art{suffix}'
        art_path = _job_dir(job.id) / art_name
        if not art_path.exists():
            await _save_upload(art, art_path)
            store.set_art(job.id, art_name)
    elif art_source is not None:
        art_name = f'art{art_source.suffix}'
        art_path = _job_dir(job.id) / art_name
        if not art_path.exists():
            art_path.write_bytes(art_source.read_bytes())
            store.set_art(job.id, art_name)

    # Compose path: render on the NAS immediately (no Windows worker)
    if effective == 'compose':
        return _run_compose_job(job.id)

    return {
        'id': job.id, 'status': job.status, 'card_resolved': card is not None,
        'game': game, 'render_mode': effective,
    }


def _run_compose_job(job_id: str) -> dict:
    """Execute a compose render synchronously and mark the job done/failed."""
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail='Job not found')
    if job.status == JobStatus.DONE and job.result_filename:
        return {
            'id': job_id, 'status': job.status, 'card_resolved': bool(job.card_json),
            'game': job.game, 'render_mode': 'compose',
        }
    store.set_status(job_id, JobStatus.RENDERING)
    log: list[str] = [f'compose game={job.game}']
    try:
        card = json.loads(job.card_json) if job.card_json else {
            'name': job.card_name,
            'set': job.set_code or '',
            'collector_number': job.collector_number or '',
        }
        transform = card.pop('_art_transform', None) if isinstance(card, dict) else None
        custom_art = bool(card.pop('_custom_art', False)) if isinstance(card, dict) else False
        art_path = None
        if job.art_filename:
            art_path = _job_dir(job_id) / job.art_filename
        result_name = 'result.png'
        result_path = _job_dir(job_id) / result_name
        compose_card(
            job.game, card, art_path=art_path, out_path=result_path,
            art_transform=transform, custom_art=custom_art)
        log.append(f'wrote {result_name}')
        store.finish(job_id, ok=True, result_filename=result_name, log='\n'.join(log))
        done = store.get(job_id)
        return {
            'id': job_id,
            'status': done.status if done else JobStatus.DONE,
            'card_resolved': bool(job.card_json),
            'game': job.game,
            'render_mode': 'compose',
        }
    except Exception as e:
        log.append(f'error: {e}')
        # Force permanent failure (compose is sync — no worker retry)
        con = store._conn()
        from web.server.db import MAX_ATTEMPTS
        con.execute('UPDATE jobs SET attempts=? WHERE id=?', (MAX_ATTEMPTS, job_id))
        con.commit()
        store.finish(job_id, ok=False, error=str(e), log='\n'.join(log))
        raise HTTPException(status_code=500, detail=f'Compose failed: {e}') from e


@app.post('/api/compose')
async def api_compose(
    request: Request,
    game: str = Form(default='mtg', max_length=20),
    card_json: str = Form(min_length=2, max_length=200_000),
    art: Optional[UploadFile] = File(default=None),
    art_transform: Optional[str] = Form(default=None, max_length=2000),
    bleed_px: int = Form(default=0),
):
    """Compose a card preview/download from edited fields (browser editor).

    Accepts a Scryfall-/provider-shaped JSON object plus optional art.
    Optional art_transform JSON: {scale, offset_x, offset_y} for pan/zoom.
    Optional bleed_px pads the PNG for print.
    Returns a PNG. Does not create a job.
    """
    rate_limit(request, 'submit')
    game = (game or 'mtg').strip().lower()
    if game not in COMPOSE_GAMES:
        raise HTTPException(
            status_code=422,
            detail=f'Compose only supports: {", ".join(sorted(COMPOSE_GAMES))}')
    try:
        card = json.loads(card_json)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=422, detail=f'Invalid card_json: {e}') from e
    if not isinstance(card, dict):
        raise HTTPException(status_code=422, detail='card_json must be an object')

    transform = _parse_art_transform(art_transform)
    try:
        bleed = max(0, min(int(bleed_px or 0), 120))
    except (TypeError, ValueError):
        bleed = 0

    art_path: Optional[Path] = None
    custom_art = False
    tmp_dir = DATA_DIR / 'compose-tmp'
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_art = None
    if art is not None and (art.filename or '') != '':
        suffix = Path(art.filename or 'art.png').suffix.lower()
        if suffix not in ALLOWED_ART_TYPES:
            raise HTTPException(status_code=422, detail=f'Unsupported art type {suffix!r}')
        tmp_art = tmp_dir / f'{uuid_module.uuid4().hex}{suffix}'
        await _save_upload(art, tmp_art)
        art_path = tmp_art
        custom_art = True
    else:
        # Prefer cached art crop (MTG) or HQ scan
        card_id = card.get('id')
        cached = carddb.get_by_id(card_id) if card_id else None
        src = cached or card
        kind = 'art_crop' if game == 'mtg' else 'png'
        art_path = images.ensure_image(
            carddb.session, src, kind, IMAGES_DIR, offline=OFFLINE)
        if not art_path and game == 'mtg':
            art_path = images.ensure_image(
                carddb.session, src, 'png', IMAGES_DIR, offline=OFFLINE)

    out = tmp_dir / f'{uuid_module.uuid4().hex}.png'
    try:
        compose_card(
            game, card, art_path=art_path, out_path=out,
            art_transform=transform, custom_art=custom_art, bleed_px=bleed)
        safe = re.sub(r'[^-\w. \[\]{}()]', '_', f"{card.get('name', 'card')}.png")
        return FileResponse(out, media_type='image/png', filename=safe)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'Compose failed: {e}') from e


@app.get('/edit')
def page_edit(card_id: str = ''):
    """Legacy editor URL — redirected into the Make workspace."""
    if card_id:
        return RedirectResponse(url=f'/?card_id={card_id}', status_code=302)
    return RedirectResponse(url='/', status_code=302)


@app.get('/api/jobs')
def api_list_jobs(request: Request, limit: int = 50):
    rate_limit(request, 'api')
    return [j.model_dump() for j in store.list_jobs(min(limit, 200))]


@app.get('/api/jobs/{job_id}')
def api_get_job(request: Request, job_id: str):
    rate_limit(request, 'api')
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail='Job not found')
    return job.model_dump()


@app.delete('/api/jobs/{job_id}')
def api_delete_job(request: Request, job_id: str):
    """Delete a job row and its on-disk art/result files."""
    rate_limit(request, 'api')
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail='Job not found')
    store.delete(job_id)
    job_dir = JOBS_DIR / job_id
    if job_dir.is_dir():
        shutil.rmtree(job_dir, ignore_errors=True)
    return {'ok': True, 'id': job_id}


@app.get('/api/jobs/{job_id}/result')
def api_job_result(job_id: str):
    job = store.get(job_id)
    if not job or not job.result_filename:
        raise HTTPException(status_code=404, detail='No result available')
    path = JOBS_DIR / job_id / job.result_filename
    if not path.exists():
        raise HTTPException(status_code=404, detail='Result file missing')
    safe_name = re.sub(r'[^-\w. \[\]{}()]', '_', f'{job.card_name}.png')
    return FileResponse(path, filename=safe_name)


@app.get('/api/templates')
def api_templates(request: Request):
    """Template options per card class, from the worker capabilities handshake."""
    rate_limit(request, 'api')
    caps = _capabilities()
    return caps.model_dump() if caps else {'templates': {}}


"""
* Public API: Cards & Decks
"""


@app.get('/api/cards/search')
def api_card_search(request: Request, q: str, limit: int = 30, game: str = 'mtg'):
    """Card search: local DB first, live provider fallback (results cached)."""
    rate_limit(request, 'api')
    if len(q) < 2:
        return {'source': 'local', 'game': game, 'cards': []}
    results, source = _search_cards(q, min(limit, 100), game)
    prices = carddb.get_prices([c['id'] for c in results if c.get('id')])
    return {
        'source': source,
        'game': game,
        'cards': [
            {
                'id': c.get('id'),
                'name': c.get('name'),
                'set': c.get('set'),
                'set_name': c.get('set_name'),
                'collector_number': c.get('collector_number'),
                'released_at': c.get('released_at'),
                'game': c.get('game') or game,
                'thumb': (
                    f"/api/cards/{c['id']}/image?kind=large" if c.get('id') else None),
                'usd': (prices.get(c.get('id')) or {}).get('usd'),
                'eur': (prices.get(c.get('id')) or {}).get('eur'),
            }
            for c in results]}


@app.get('/card/{card_id}', response_class=HTMLResponse)
def page_card(request: Request, card_id: str):
    """Card detail view: big scan + attributes, any game."""
    payload = _card_detail_payload(card_id)
    return templates.TemplateResponse(request, 'card.html', {
        'card': payload['card'],
        'game': payload['game'],
        'game_label': payload['game_label'],
        'details': [(d['label'], d['value']) for d in payload['details']],
        'price': payload['price'],
        'prints': payload['prints'],
        'has_art_crop': payload['has_art_crop'],
    })


@app.get('/api/cards/{card_id}/detail')
def api_card_detail(request: Request, card_id: str):
    """JSON card detail for the in-place gallery/search popover."""
    rate_limit(request, 'api')
    payload = _card_detail_payload(card_id)
    payload.pop('card', None)
    return payload


def _card_detail_payload(card_id: str) -> dict:
    """Shared fields for the HTML card page and the popover JSON API."""
    card = carddb.get_by_id(card_id)
    if not card:
        raise HTTPException(status_code=404, detail='Card not in the local database')
    game = card.get('game', 'mtg')
    provider = card.get('provider_data') or {}

    def first(*keys):
        for k in keys:
            v = card.get(k) or provider.get(k)
            if v:
                return v
        return None

    rb_stats = None
    if game == 'riftbound':
        parts = []
        if provider.get('domain'):
            parts.append(str(provider['domain']))
        if provider.get('energyCost') is not None:
            parts.append(f"Energy {provider['energyCost']}")
        if provider.get('powerCost') is not None:
            parts.append(f"Power {provider['powerCost']}")
        if provider.get('might') is not None:
            parts.append(f"Might {provider['might']}")
        rb_stats = ' · '.join(parts) if parts else None

    details = [
        {'label': label, 'value': value}
        for label, value in [
            ('Set', f"{first('set_name') or (card.get('set') or '').upper()}"
                    + (f" · #{card.get('collector_number')}" if card.get('collector_number') else '')),
            ('Mana cost', first('mana_cost')),
            ('Type', first('type_line', 'supertype', 'cardType')),
            ('Text', first('oracle_text', 'effect', 'ability', 'description')),
            ('P/T', f"{card.get('power')}/{card.get('toughness')}"
                    if card.get('power') is not None else None),
            ('HP', provider.get('hp')),
            ('Domain / Stats', rb_stats),
            ('Rarity', first('rarity')),
            ('Artist', first('artist')),
            ('Released', first('released_at')),
        ] if value
    ]
    price = carddb.get_prices([card_id]).get(card_id)
    # Other printings/arts of this same card (Scryfall-style "Prints" list).
    prints = [
        {
            'id': p['id'],
            'set': p['set'],
            'set_name': p['set_name'],
            'collector_number': p['collector_number'],
            'lang': p['lang'],
            'current': p['id'] == card_id,
            'thumb': f"/api/cards/{p['id']}/image?kind=large",
            'page_url': f"/card/{p['id']}",
        }
        for p in carddb.list_art_group(card_id)]
    return {
        'id': card_id,
        'name': card.get('name'),
        'game': game,
        'game_label': games.GAME_LABELS.get(game, game),
        'set': card.get('set'),
        'collector_number': card.get('collector_number'),
        'details': details,
        'prints': prints,
        'price': price,
        'has_art_crop': game == 'mtg',
        'can_edit': game in RENDERABLE_GAMES,
        'image_png': f'/api/cards/{card_id}/image?kind=png',
        'image_large': f'/api/cards/{card_id}/image?kind=large',
        'image_art_crop': (
            f'/api/cards/{card_id}/image?kind=art_crop' if game == 'mtg' else None),
        'editor_url': f'/?card_id={card_id}' if game in RENDERABLE_GAMES else None,
        'page_url': f'/card/{card_id}',
        # Keep raw card for the HTML template only (dropped from JSON by FastAPI
        # only if we omit it — templates need it).
        'card': card,
    }


def _placeholder_image(name: str, note: str = 'Image unavailable') -> Response:
    """A card-shaped SVG placeholder for cards with no available scan.

    Served with 200 so the gallery/popover show a clean placeholder instead of
    a broken image or a 404 in the network tab.
    """
    from xml.sax.saxutils import escape
    label = escape((name or 'Card')[:40])
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="488" height="680" '
        'viewBox="0 0 488 680">'
        '<rect width="488" height="680" rx="26" fill="#1e2027" stroke="#33363f"/>'
        '<rect x="26" y="26" width="436" height="628" rx="14" fill="#26282f"/>'
        f'<text x="244" y="330" fill="#e8e9ec" font-family="system-ui,sans-serif" '
        f'font-size="26" font-weight="600" text-anchor="middle">{label}</text>'
        f'<text x="244" y="368" fill="#9aa0ab" font-family="system-ui,sans-serif" '
        f'font-size="16" text-anchor="middle">{escape(note)}</text>'
        '</svg>')
    return Response(
        content=svg, media_type='image/svg+xml',
        headers={'Cache-Control': 'public, max-age=3600'})


@app.get('/api/cards/{card_id}/image')
def api_card_image(request: Request, card_id: str, kind: str = 'png'):
    """Download a card's high-quality image (any game), cached on first pull.

    Serves a placeholder (not a 404) when a card has no usable scan — some
    cards (e.g. certain basic Energy) simply have no image in the source.
    """
    rate_limit(request, 'image')
    if kind not in images.IMAGE_KINDS:
        raise HTTPException(status_code=422, detail=f'Unknown image kind {kind!r}')
    card = carddb.get_by_id(card_id)
    if not card:
        raise HTTPException(status_code=404, detail='Card not in the local database')
    path = images.ensure_image(carddb.session, card, kind, IMAGES_DIR, offline=OFFLINE)
    if not path:
        return _placeholder_image(
            card.get('name', 'Card'),
            'Not cached yet' if OFFLINE else 'No image available')
    safe = re.sub(r'[^-\w. \[\]{}()\']', '_', (
        f"{card.get('name', 'card')} [{(card.get('set') or '').upper()}] "
        f"{card.get('collector_number', '')}").strip())
    # Card scans never change for a given id+kind: let browsers cache them
    # aggressively so grid pages don't re-request 60 thumbs per view.
    return FileResponse(
        path,
        filename=f'{safe}{path.suffix}',
        headers={'Cache-Control': 'public, max-age=31536000, immutable'})


@app.post('/api/decks/import')
def api_deck_import(
    request: Request,
    name: Optional[str] = Form(default=None, max_length=200),
    text: Optional[str] = Form(default=None, max_length=100_000),
    url: Optional[str] = Form(default=None, max_length=500),
):
    """Import a decklist (pasted text or Moxfield/Archidekt URL) and bulk-cache all cards."""
    rate_limit(request, 'import')

    if not text and not url:
        raise HTTPException(status_code=422, detail='Provide decklist text or a deck URL')

    deck_name = (name or '').strip()
    if url:
        try:
            fetched_name, lines = fetch_deck_url(url.strip())
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except Exception:
            raise HTTPException(status_code=502, detail='Failed to fetch deck from URL')
        deck_name = deck_name or fetched_name
    else:
        lines = parse_decklist_text(text or '')
        deck_name = deck_name or 'Pasted deck'

    if not lines:
        raise HTTPException(status_code=422, detail='No card lines recognized')
    if len(lines) > 1000:
        raise HTTPException(status_code=422, detail='Deck too large (limit 1000 lines)')

    # Build Scryfall collection identifiers per line
    identifiers = []
    for ln in lines:
        if ln.set_code and ln.collector_number:
            identifiers.append({
                'set': ln.set_code.lower(),
                'collector_number': ln.collector_number})
        elif ln.set_code:
            identifiers.append({'name': ln.name, 'set': ln.set_code.lower()})
        else:
            identifiers.append({'name': ln.name})

    result = carddb.resolve_collection(identifiers)

    # Match resolved cards back to lines (by set+number, then name)
    by_setnum = {(c.get('set'), str(c.get('collector_number'))): c for c in result.found}
    by_name: dict[str, dict] = {}
    for c in result.found:
        by_name.setdefault(c.get('name', '').lower(), c)
        # Front-face name for double-faced cards
        if ' // ' in c.get('name', ''):
            by_name.setdefault(c['name'].split(' // ')[0].lower(), c)

    resolved, unresolved = [], []
    for ln in lines:
        card = None
        if ln.set_code and ln.collector_number:
            card = by_setnum.get((ln.set_code.lower(), str(ln.collector_number)))
        card = card or by_name.get(ln.name.lower())
        entry = ln.model_copy(update={
            'resolved': card is not None,
            'card_id': card.get('id') if card else None})
        (resolved if card else unresolved).append(entry)

    deck_id = carddb.save_deck(
        name=deck_name,
        lines=[(e.card_id, e.name, e.qty, e.board) for e in resolved + unresolved],
        source_url=url)

    report = DeckImportReport(
        deck_id=deck_id, deck_name=deck_name,
        resolved=resolved, unresolved=unresolved,
        from_cache=result.from_cache, from_api=result.from_api)
    return report.model_dump()


@app.get('/api/decks')
def api_decks(request: Request):
    rate_limit(request, 'api')
    return carddb.get_decks()


@app.get('/api/decks/{deck_id}')
def api_deck(request: Request, deck_id: str):
    rate_limit(request, 'api')
    deck = carddb.get_deck(deck_id)
    if not deck:
        raise HTTPException(status_code=404, detail='Deck not found')
    return deck


@app.get('/api/decks/{deck_id}/images')
def api_deck_images(request: Request, deck_id: str):
    """Download a deck's high-quality card scans as a ZIP.

    One 745x1040 PNG per unique card (named 'Name [SET] num.png') plus a
    decklist.txt manifest with quantities — ready to drop into an external
    print-prep tool like Proxxied, or any layout software.
    """
    rate_limit(request, 'import')
    deck = carddb.get_deck(deck_id)
    if not deck:
        raise HTTPException(status_code=404, detail='Deck not found')

    seen: set[str] = set()
    manifest: list[str] = []
    missing: list[str] = []
    files: list[tuple[str, Path]] = []
    for entry in deck['cards']:
        card = carddb.get_by_id(entry['card_id']) if entry['card_id'] else None
        path = images.ensure_image(
            carddb.session, card, 'png', IMAGES_DIR, offline=OFFLINE) if card else None
        if path and card['id'] not in seen:
            seen.add(card['id'])
            safe = re.sub(r'[^-\w. \[\]{}()\']', '_', (
                f"{card.get('name', 'card')} "
                f"[{(card.get('set') or '').upper()}] {card.get('collector_number', '')}"
            ).strip())
            files.append((f'{safe}.png', path))
        if path:
            manifest.append(f"{entry['qty']} {entry['card_name']}")
        else:
            missing.append(f"{entry['qty']} {entry['card_name']}")

    if not files:
        raise HTTPException(
            status_code=422,
            detail='No card images available'
                   + (' (offline mode: only cached images can be used)' if OFFLINE else ''))

    SHEETS_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = SHEETS_DIR / f'{deck_id}-images.zip'
    with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_STORED) as zf:
        for arcname, path in files:
            zf.write(path, arcname)
        listing = '\n'.join(manifest)
        if missing:
            listing += '\n\n# Missing images (not cached / not found):\n' + '\n'.join(missing)
        zf.writestr('decklist.txt', listing + '\n')
    safe_deck = re.sub(r'[^-\w. ]', '_', deck['name'])[:60] or 'deck'
    return FileResponse(
        zip_path, filename=f'{safe_deck}-images.zip', media_type='application/zip')


"""
* Public API: Proxy Sheets
"""


@app.post('/api/sheets')
def api_build_sheet(
    request: Request,
    deck_id: Optional[str] = Form(default=None, max_length=100),
    text: Optional[str] = Form(default=None, max_length=100_000),
    paper: str = Form(default='letter', max_length=10),
):
    """Compile a print-ready proxy sheet PDF from HQ card scans.

    Source is a saved deck (deck_id) or pasted decklist text. Card images
    are fetched from Scryfall once and cached; the PDF lays out 63x88mm
    cards 3x3 per page at 300 DPI with cut guides.
    """
    rate_limit(request, 'import')
    if paper not in sheets.PAPERS_MM:
        raise HTTPException(status_code=422, detail=f'Unknown paper size {paper!r}')

    # Collect (card_id or name, qty) entries
    entries: list[tuple[Optional[str], str, int]] = []
    if deck_id:
        deck = carddb.get_deck(deck_id)
        if not deck:
            raise HTTPException(status_code=404, detail='Deck not found')
        entries = [(c['card_id'], c['card_name'], c['qty'])
                   for c in deck['cards'] if c['board'] in ('main', 'commander', 'side')]
    elif text:
        lines = parse_decklist_text(text)
        if not lines:
            raise HTTPException(status_code=422, detail='No card lines recognized')
        result = carddb.resolve_collection([
            {'set': ln.set_code.lower(), 'collector_number': ln.collector_number}
            if ln.set_code and ln.collector_number else
            {'name': ln.name, 'set': ln.set_code.lower()} if ln.set_code else
            {'name': ln.name}
            for ln in lines])
        by_name = {c.get('name', '').lower(): c for c in result.found}
        for c in result.found:
            if ' // ' in c.get('name', ''):
                by_name.setdefault(c['name'].split(' // ')[0].lower(), c)
        for ln in lines:
            card = by_name.get(ln.name.lower())
            entries.append((card.get('id') if card else None, ln.name, ln.qty))
    else:
        raise HTTPException(status_code=422, detail='Provide deck_id or decklist text')

    if sum(q for _, _, q in entries) > 500:
        raise HTTPException(status_code=422, detail='Too many cards (limit 500)')

    # Fetch HQ scans (745x1040 PNG), expand by quantity
    image_paths: list[Path] = []
    missing: list[str] = []
    for card_id, name, qty in entries:
        card = carddb.get_by_id(card_id) if card_id else None
        path = images.ensure_image(
            carddb.session, card, 'png', IMAGES_DIR, offline=OFFLINE) if card else None
        if path:
            image_paths.extend([path] * qty)
        else:
            missing.append(name)

    if not image_paths:
        raise HTTPException(
            status_code=422,
            detail='No card images available'
                   + (' (offline mode: only cached images can be used)' if OFFLINE else ''))

    sheet_id = str(uuid_module.uuid4())
    out = SHEETS_DIR / f'{sheet_id}.pdf'
    pages = sheets.build_sheet_pdf(image_paths, out, paper=paper)
    return {
        'id': sheet_id,
        'pages': pages,
        'cards': len(image_paths),
        'missing': missing,
        'url': f'/api/sheets/{sheet_id}',
    }


@app.get('/api/sheets/{sheet_id}')
def api_get_sheet(sheet_id: str):
    if not re.fullmatch(r'[0-9a-f-]{36}', sheet_id):
        raise HTTPException(status_code=404, detail='Sheet not found')
    path = SHEETS_DIR / f'{sheet_id}.pdf'
    if not path.exists():
        raise HTTPException(status_code=404, detail='Sheet not found')
    return FileResponse(path, filename='proxy-sheet.pdf', media_type='application/pdf')


"""
* Worker API (bearer token)
"""


@app.post('/api/worker/hello', dependencies=[Depends(require_worker)])
def worker_hello(caps: Capabilities):
    """Capabilities handshake: worker announces its renderable templates."""
    store.touch_worker(caps.worker_name, capabilities=caps.model_dump_json())
    return {'ok': True}


@app.post('/api/worker/heartbeat', dependencies=[Depends(require_worker)])
def worker_heartbeat(worker: str = 'worker'):
    store.touch_worker(worker)
    store.requeue_stale()
    return {'ok': True}


@app.get('/api/worker/jobs/next', dependencies=[Depends(require_worker)])
async def worker_next_job(worker: str = 'worker', wait: int = 0):
    """Claim the next queued job; long-polls up to `wait` seconds (max 30)."""
    deadline = time.monotonic() + min(max(wait, 0), 30)
    while True:
        job = store.claim_next(worker)
        if job:
            return job.model_dump()
        if time.monotonic() >= deadline:
            return Response(status_code=204)
        await asyncio.sleep(1.0)


@app.get('/api/worker/jobs/{job_id}/art', dependencies=[Depends(require_worker)])
def worker_job_art(job_id: str):
    job = store.get(job_id)
    if not job or not job.art_filename:
        raise HTTPException(status_code=404, detail='No art for job')
    path = JOBS_DIR / job_id / job.art_filename
    if not path.exists():
        raise HTTPException(status_code=404, detail='Art file missing')
    return FileResponse(path)


@app.post('/api/worker/jobs/{job_id}/status', dependencies=[Depends(require_worker)])
def worker_job_status(job_id: str, status: JobStatus):
    if not store.get(job_id):
        raise HTTPException(status_code=404, detail='Job not found')
    store.set_status(job_id, status)
    return {'ok': True}


@app.post('/api/worker/jobs/{job_id}/result', dependencies=[Depends(require_worker)])
async def worker_job_result(
    job_id: str,
    ok: bool = Form(...),
    error: Optional[str] = Form(default=None),
    log: Optional[str] = Form(default=None),
    result: Optional[UploadFile] = File(default=None),
):
    """Worker reports a finished job, uploading the rendered image on success."""
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail='Job not found')
    result_name = None
    if ok and result is not None:
        suffix = Path(result.filename or 'result.png').suffix.lower() or '.png'
        result_name = f'result{suffix}'
        await _save_upload(result, _job_dir(job_id) / result_name)
    elif ok:
        raise HTTPException(status_code=422, detail='Success report requires a result file')
    store.finish(job_id, ok=ok, result_filename=result_name, error=error, log=log)
    return {'ok': True}


"""
* Health
"""


@app.get('/api/health')
def health():
    return {
        'ok': True,
        'offline': OFFLINE,
        'cards': carddb.stats(),
        'workers': store.get_workers(),
        'keys': {
            'pokemontcg': bool(games._read_secret(
                'PROXYSHOP_POKEMONTCG_KEY', games._POKEMONTCG_KEY_FILE)),
        },
    }


"""
* Selective / full TCG cache (MTG, Pokémon, Riftbound, Union Arena)
"""


def _require_catalog_game(game: str) -> str:
    game = (game or '').strip().lower()
    if game not in games.CATALOG_GAMES:
        raise HTTPException(
            status_code=422,
            detail=f'Cache only supports: {", ".join(games.CATALOG_GAMES)}')
    return game


@app.get('/api/cache-game/{game}')
def api_cache_game_status(request: Request, game: str):
    """Status for a cache run (checkpoint + live thread)."""
    rate_limit(request, 'api')
    game = _require_catalog_game(game)
    return cache_runner.status(game, db=carddb, runs_dir=CACHE_RUNS_DIR)


@app.get('/api/cache-jobs')
def api_cache_jobs(request: Request):
    """Status for all catalog-game cache jobs."""
    rate_limit(request, 'api')
    return cache_runner.all_status(db=carddb, runs_dir=CACHE_RUNS_DIR)


@app.get('/api/cache-game/{game}/log')
def api_cache_game_log(request: Request, game: str, limit: int = 200):
    """Recent log lines for a cache run."""
    rate_limit(request, 'api')
    game = _require_catalog_game(game)
    limit = max(1, min(int(limit or 200), 1000))
    return {
        'game': game,
        'lines': cache_runner.log_lines(game, CACHE_RUNS_DIR, limit=limit),
    }


@app.get('/api/cache-game/{game}/options')
def api_cache_game_options(request: Request, game: str):
    """Sets / filter option lists for the cache UI."""
    rate_limit(request, 'api')
    game = _require_catalog_game(game)
    from web.shared.cache_filters import MTG_ART_FLAGS, MTG_RARITIES
    if game == 'mtg':
        # Always try local/cached sets first so the picklist isn't empty while
        # (or if) the live Scryfall /sets call is slow.
        sets: list = []
        try:
            sets = carddb.list_scryfall_sets()
        except Exception:  # noqa: BLE001 — UI still works without live sets
            try:
                sets = carddb.list_local_mtg_sets()
            except Exception:  # noqa: BLE001
                sets = []
        return {
            'game': game,
            'selective': True,
            'sets': sets,
            'rarities': list(MTG_RARITIES),
            'art_flags': list(MTG_ART_FLAGS),
            'image_kinds': ['png', 'large', 'art_crop', 'border_crop'],
        }
    if game == 'pokemon':
        fallback_types = [
            'Colorless', 'Darkness', 'Dragon', 'Fairy', 'Fighting', 'Fire',
            'Grass', 'Lightning', 'Metal', 'Psychic', 'Water']
        fallback_supertypes = ['Pokémon', 'Trainer', 'Energy']
        fallback_subtypes = [
            'Basic', 'Stage 1', 'Stage 2', 'V', 'VMAX', 'VSTAR', 'ex', 'EX',
            'GX', 'Item', 'Supporter', 'Stadium', 'Tool']
        sets = []
        meta = {
            'types': fallback_types,
            'subtypes': fallback_subtypes,
            'rarities': [],
            'supertypes': fallback_supertypes,
        }
        if not OFFLINE:
            try:
                sets = games.list_pokemon_sets()
            except Exception:  # noqa: BLE001
                sets = []
            try:
                live = games.list_pokemon_meta()
                for key in ('types', 'subtypes', 'rarities', 'supertypes'):
                    if live.get(key):
                        meta[key] = live[key]
            except Exception:  # noqa: BLE001
                pass
        return {
            'game': game,
            'selective': True,
            'sets': sets,
            'types': meta['types'] or fallback_types,
            'subtypes': meta['subtypes'] or fallback_subtypes,
            'rarities': meta['rarities'] or [],
            'supertypes': meta['supertypes'] or fallback_supertypes,
            'image_kinds': ['png', 'large'],
        }
    return {
        'game': game,
        'selective': False,
        'sets': [],
        'image_kinds': ['png', 'large'],
    }


@app.post('/api/cache-game/{game}/start')
async def api_cache_game_start(
    request: Request,
    game: str,
    fresh: bool = False,
    images_only: bool = False,
):
    """Start or resume caching cards + HQ images in the background.

    Optional JSON body: ``{"filters": {...}, "kind": "png", "fresh": false}``.
    MTG/Pokémon require at least one filter.
    """
    rate_limit(request, 'cache')
    game = _require_catalog_game(game)
    if OFFLINE:
        raise HTTPException(
            status_code=503,
            detail='Offline mode is on — TCG cache needs the live provider')
    body: dict = {}
    try:
        raw = await request.body()
        if raw:
            body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=422, detail='Invalid JSON body')
    if not isinstance(body, dict):
        body = {}
    filters = body.get('filters') if isinstance(body.get('filters'), dict) else {}
    kind = str(body.get('kind') or 'png')
    fresh = bool(body.get('fresh')) if 'fresh' in body else fresh
    images_only = bool(body.get('images_only')) if 'images_only' in body else images_only
    try:
        return cache_runner.start(
            game,
            db=carddb,
            images_dir=IMAGES_DIR,
            runs_dir=CACHE_RUNS_DIR,
            fresh=fresh,
            images_only=images_only,
            filters=filters,
            image_kind=kind,
        )
    except cache_runner.FilterConflict as e:
        # 409: a different saved run exists — UI offers "discard & start new"
        raise HTTPException(
            status_code=409,
            detail={
                'conflict': True,
                'existing_label': e.existing_label,
                'message': (
                    f'This game already has a saved download for '
                    f'“{e.existing_label}”. Start a new download with your '
                    f'current filters (discards the saved progress), or '
                    f'resume the existing one.'),
            }) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@app.post('/api/cache-game/{game}/stop')
def api_cache_game_stop(request: Request, game: str):
    """Ask a running cache job to stop after the current card (resumable)."""
    rate_limit(request, 'cache')
    game = _require_catalog_game(game)
    return cache_runner.stop(game, db=carddb, runs_dir=CACHE_RUNS_DIR)
