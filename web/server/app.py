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
from web.shared.carddb import CardDB
from web.shared.decklist import fetch_deck_url, parse_decklist_text
from web.shared.schema import (
    Capabilities, DeckCardLine, DeckImportReport, JobResult, JobStatus)

"""
* Configuration
"""

DATA_DIR = Path(os.environ.get('PROXYSHOP_DATA_DIR', 'data'))
WORKER_TOKEN = os.environ.get('PROXYSHOP_WORKER_TOKEN', 'dev-token')
OFFLINE = os.environ.get('PROXYSHOP_OFFLINE', '0') == '1'
MAX_UPLOAD_BYTES = int(os.environ.get('PROXYSHOP_MAX_UPLOAD_MB', '50')) * 1024 * 1024

JOBS_DIR = DATA_DIR / 'jobs'
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


def _resolve_card(name: str, set_code: Optional[str], number: Optional[str], lang: str) -> Optional[dict]:
    """Resolve a card through the local DB (cache-first, API fallback unless offline)."""
    if set_code and number:
        return carddb.get_card(set_code, number, lang)
    return carddb.find_card(name, set_code, lang)


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


def _search_cards(q: str, limit: int) -> tuple[list[dict], str]:
    """Local-first card search with live Scryfall fallback (cache-through)."""
    results = carddb.search_local(q, limit=limit)
    if results:
        return results, 'local'
    results = carddb.search_scryfall(q, limit=limit)
    return results, 'scryfall'


@app.get('/search', response_class=HTMLResponse)
def page_search(request: Request, q: str = ''):
    results, source = _search_cards(q, 60) if len(q) >= 2 else ([], 'local')
    return templates.TemplateResponse(request, 'search.html', {
        'q': q, 'results': results, 'source': source,
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
    art: UploadFile = File(...),
    idempotency_key: Optional[str] = Form(default=None, max_length=100),
):
    """Submit a render job with an art upload."""
    rate_limit(request, 'submit')

    suffix = Path(art.filename or 'art.png').suffix.lower()
    if suffix not in ALLOWED_ART_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f'Unsupported art file type {suffix!r}')

    # Resolve card data server-side so the worker can skip Scryfall entirely
    card = _resolve_card(card_name.strip(), set_code, collector_number, lang)
    card_json = json.dumps(card, separators=(',', ':')) if card else None

    job = store.submit(
        card_name=card_name.strip(),
        set_code=(set_code or None),
        collector_number=(collector_number or None),
        template_name=(template_name or None),
        lang=lang,
        card_json=card_json,
        idempotency_key=(idempotency_key or None))

    # If idempotent-replay returned an existing job, don't overwrite its art
    art_name = f'art{suffix}'
    art_path = _job_dir(job.id) / art_name
    if not art_path.exists():
        await _save_upload(art, art_path)
        store.set_art(job.id, art_name)

    return {'id': job.id, 'status': job.status, 'card_resolved': card is not None}


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
def api_card_search(request: Request, q: str, limit: int = 30):
    """Card search: local DB first, live Scryfall fallback (results cached)."""
    rate_limit(request, 'api')
    if len(q) < 2:
        return {'source': 'local', 'cards': []}
    results, source = _search_cards(q, min(limit, 100))
    return {
        'source': source,
        'cards': [
            {
                'id': c.get('id'),
                'name': c.get('name'),
                'set': c.get('set'),
                'collector_number': c.get('collector_number'),
                'released_at': c.get('released_at'),
            }
            for c in results]}


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
