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
from web.shared import games, images, sheets
from web.shared.carddb import CardDB
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
def page_index(request: Request):
    caps = _capabilities()
    return templates.TemplateResponse(request, 'index.html', {
        'jobs': store.list_jobs(30),
        'workers': store.get_workers(),
        'caps': caps,
        'stats': carddb.stats(),
        'games': games.GAME_LABELS,
        'renderable_games': sorted(RENDERABLE_GAMES),
        'compose_games': sorted(COMPOSE_GAMES),
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


def _search_cards(q: str, limit: int, game: str = 'mtg') -> tuple[list[dict], str]:
    """Local-first card search with live provider fallback (cache-through).

    MTG falls back to Scryfall; other games use their provider from
    web.shared.games. Fallback results are always cached locally.
    """
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
    except Exception:
        raise HTTPException(
            status_code=502, detail=f'{games.GAME_LABELS[game]} provider unavailable')
    for card in results:
        carddb.store_card(card, commit=False, game=game)
    carddb._conn().commit()
    return results, 'live'


@app.get('/search', response_class=HTMLResponse)
def page_search(request: Request, q: str = '', game: str = 'mtg'):
    results, source, error = [], 'local', None
    if len(q) >= 2:
        try:
            results, source = _search_cards(q, 60, game)
        except HTTPException as e:
            error = e.detail  # show provider problems inline instead of a bare 502
    prices = carddb.get_prices([c['id'] for c in results if c.get('id')])
    return templates.TemplateResponse(request, 'search.html', {
        'q': q, 'game': game, 'games': games.GAME_LABELS,
        'results': results, 'source': source, 'prices': prices, 'error': error,
        'offline': OFFLINE, 'stats': carddb.stats()})


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

    card = _resolve_card(card_name.strip(), set_code, collector_number, lang, game=game)
    card_json = json.dumps(card, separators=(',', ':')) if card else None

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
            if not art_source:
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
            if not art_source:
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
        card_json=card_json,
        idempotency_key=(idempotency_key or None))

    # Persist art next to the job
    if has_upload:
        art_name = f'art{suffix}'
        art_path = _job_dir(job.id) / art_name
        if not art_path.exists():
            await _save_upload(art, art_path)
            store.set_art(job.id, art_name)
    else:
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
        art_path = None
        if job.art_filename:
            art_path = _job_dir(job_id) / job.art_filename
        result_name = 'result.png'
        result_path = _job_dir(job_id) / result_name
        compose_card(job.game, card, art_path=art_path, out_path=result_path)
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
):
    """Compose a card preview/download from edited fields (browser editor).

    Accepts a Scryfall-/provider-shaped JSON object plus optional art.
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

    art_path: Optional[Path] = None
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
        compose_card(game, card, art_path=art_path, out_path=out)
        safe = re.sub(r'[^-\w. \[\]{}()]', '_', f"{card.get('name', 'card')}.png")
        return FileResponse(out, media_type='image/png', filename=safe)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'Compose failed: {e}') from e


@app.get('/edit', response_class=HTMLResponse)
def page_edit(request: Request, card_id: str = ''):
    """Browser card editor — load a cached card, edit fields/art, compose PNG."""
    card = carddb.get_by_id(card_id) if card_id else None
    game = (card or {}).get('game', 'mtg') if card else 'mtg'
    if card and game not in COMPOSE_GAMES:
        raise HTTPException(
            status_code=422,
            detail=f'Editor only supports {", ".join(sorted(COMPOSE_GAMES))}')
    return templates.TemplateResponse(request, 'editor.html', {
        'card': card,
        'card_id': card_id,
        'game': game,
        'game_label': games.GAME_LABELS.get(game, game),
        'games': games.GAME_LABELS,
        'compose_games': sorted(COMPOSE_GAMES),
    })


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
                'usd': (prices.get(c.get('id')) or {}).get('usd'),
                'eur': (prices.get(c.get('id')) or {}).get('eur'),
            }
            for c in results]}


@app.get('/card/{card_id}', response_class=HTMLResponse)
def page_card(request: Request, card_id: str):
    """Card detail view: big scan + attributes, any game."""
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

    # Riftbound stats live on provider_data (domain/energy/might/powerCost)
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

    details = [(label, value) for label, value in [
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
    ] if value]
    price = carddb.get_prices([card_id]).get(card_id)
    return templates.TemplateResponse(request, 'card.html', {
        'card': card, 'game': game, 'game_label': games.GAME_LABELS.get(game, game),
        'details': details, 'price': price,
        'has_art_crop': game == 'mtg',
    })


@app.get('/api/cards/{card_id}/image')
def api_card_image(request: Request, card_id: str, kind: str = 'png'):
    """Download a card's high-quality image (any game), cached on first pull."""
    rate_limit(request, 'image')
    if kind not in images.IMAGE_KINDS:
        raise HTTPException(status_code=422, detail=f'Unknown image kind {kind!r}')
    card = carddb.get_by_id(card_id)
    if not card:
        raise HTTPException(status_code=404, detail='Card not in the local database')
    path = images.ensure_image(carddb.session, card, kind, IMAGES_DIR, offline=OFFLINE)
    if not path:
        raise HTTPException(
            status_code=404,
            detail='Image unavailable'
                   + (' (offline mode: only cached images can be served)' if OFFLINE else ''))
    safe = re.sub(r'[^-\w. \[\]{}()\']', '_', (
        f"{card.get('name', 'card')} [{(card.get('set') or '').upper()}] "
        f"{card.get('collector_number', '')}").strip())
    return FileResponse(path, filename=f'{safe}{path.suffix}')


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
    }
