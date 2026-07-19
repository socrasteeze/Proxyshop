"""
* Multi-Game Card Providers
* Search providers for non-MTG trading card games, normalized into the same
* card shape the local database stores. MTG stays on the Scryfall path in
* carddb; these providers cover:
*   - pokemon      -> pokemontcg.io (free; optional API key raises limits)
*   - union-arena  -> unionarena-tcg.com NA+JP cardlists (public; no API key)
*   - riftbound    -> riftscribe.gg (public; no API key)
* Must never import from `src/`.
"""
# Standard Library Imports
import html as html_lib
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urljoin

# Third Party Imports
import requests

# Local Imports
from web.shared.carddb import HEADERS

POKEMON_API = 'https://api.pokemontcg.io/v2'
RIFTSCRIBE_API = 'https://riftscribe.gg/api'
UA_ORIGIN = 'https://www.unionarena-tcg.com'
# Official cardlist locales: English (NA) + Japanese
UA_LOCALES = {
    'en': {'path': 'na', 'lang': 'en'},
    'ja': {'path': 'jp', 'lang': 'ja'},
}
UA_LOCALE_ORDER = ('en', 'ja')

# Optional in-container secret files (mounted by nas-update.sh)
_POKEMONTCG_KEY_FILE = os.environ.get(
    'PROXYSHOP_POKEMONTCG_KEY_FILE', '/run/secrets/proxyshop-pokemontcg-key')

# Polite default pacing for all live provider calls (~4 req/s).
# Override with PROXYSHOP_PROVIDER_INTERVAL (seconds).
PROVIDER_INTERVAL = float(os.environ.get('PROXYSHOP_PROVIDER_INTERVAL', '0.25'))
PROVIDER_MAX_RETRIES = int(os.environ.get('PROXYSHOP_PROVIDER_MAX_RETRIES', '5'))

# Games supported by the search/image layer ('mtg' is handled by carddb)
GAMES = ('mtg', 'pokemon', 'union-arena', 'riftbound')

GAME_LABELS = {
    'mtg': 'Magic: The Gathering',
    'pokemon': 'Pokémon',
    'union-arena': 'Union Arena',
    'riftbound': 'Riftbound',
}


class ProviderError(RuntimeError):
    """A provider could not be queried (missing key, upstream failure)."""


class RateLimiter:
    """Thread-safe minimum spacing between outbound provider requests."""

    def __init__(self, min_interval: float = PROVIDER_INTERVAL):
        self._min_interval = max(float(min_interval), 0.0)
        self._last_request = 0.0
        self._lock = threading.Lock()

    @property
    def min_interval(self) -> float:
        return self._min_interval

    def set_interval(self, seconds: float) -> None:
        with self._lock:
            self._min_interval = max(float(seconds), 0.0)

    def wait(self) -> None:
        with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()


# Shared limiter for search + catalog + hydrate traffic
_provider_limiter = RateLimiter(PROVIDER_INTERVAL)


def _retry_after_seconds(res: requests.Response, attempt: int) -> float:
    """Honor Retry-After when present; otherwise exponential backoff."""
    raw = res.headers.get('Retry-After')
    if raw:
        try:
            return max(float(raw), 1.0)
        except (TypeError, ValueError):
            pass
    return min(60.0, 1.5 * (2 ** attempt))


def _read_secret(env_name: str, file_path: str) -> str:
    """Read a secret from env (preferred) or an optional mounted file.

    Strips whitespace/newlines so `echo key > file` works reliably.
    """
    value = (os.environ.get(env_name) or '').strip()
    if value:
        return value
    try:
        return Path(file_path).read_text(encoding='utf-8').strip()
    except OSError:
        return ''


def _safe_id(value: str) -> str:
    """Filesystem-safe id fragment (provider ids often contain '/')."""
    return re.sub(r'[^A-Za-z0-9._-]+', '-', str(value or '')).strip('-') or 'unknown'


def _request(
    url: str,
    params: Optional[dict] = None,
    extra_headers: Optional[dict] = None,
) -> requests.Response:
    """Throttled GET with 429 Retry-After backoff."""
    headers = dict(HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    last_err: Optional[ProviderError] = None
    for attempt in range(PROVIDER_MAX_RETRIES + 1):
        _provider_limiter.wait()
        try:
            res = requests.get(
                url, params=params or {}, headers=headers, timeout=30,
                allow_redirects=True)
        except requests.RequestException as e:
            raise ProviderError(f'Provider request failed: {e}') from e
        if res.status_code in (301, 302, 307, 308):
            raise ProviderError(
                f'Provider redirected ({res.status_code}) to '
                f'{res.headers.get("Location") or "unknown"} — check API host')
        if res.status_code in (401, 403):
            raise ProviderError('Provider rejected the request (check the API key).')
        if res.status_code == 429:
            last_err = ProviderError('Provider rate limit hit — backing off.')
            if attempt >= PROVIDER_MAX_RETRIES:
                raise ProviderError(
                    'Provider rate limit hit — try again in a minute.') from last_err
            time.sleep(_retry_after_seconds(res, attempt))
            continue
        if res.status_code >= 400:
            body = (res.text or '')[:200].strip()
            raise ProviderError(
                f'Provider HTTP {res.status_code}'
                + (f': {body}' if body else ''))
        return res
    if last_err:
        raise last_err
    raise ProviderError('Provider request failed')


def _get(url: str, params: dict, extra_headers: Optional[dict] = None) -> Any:
    res = _request(url, params=params, extra_headers=extra_headers)
    try:
        payload = res.json()
    except ValueError as e:
        raise ProviderError('Provider returned non-JSON response') from e
    # Some providers return HTTP 200 with {"error": "..."} for auth failures
    if isinstance(payload, dict) and payload.get('error') and 'data' not in payload:
        raise ProviderError(str(payload.get('error')))
    if isinstance(payload, dict) and payload.get('success') is False:
        raise ProviderError(str(payload.get('error') or 'Provider request failed'))
    return payload


def _card_rows(payload: Any) -> list[dict]:
    """Normalize provider JSON into a list of card dicts.

    Responses vary: ``{"data":[...]}``, ``{"cards":[...]}``, bare ``[...]``, or null.
    """
    if payload is None:
        return []
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get('data')
        if rows is None:
            rows = payload.get('cards')
        if rows is None:
            rows = []
    else:
        raise ProviderError(f'Unexpected provider payload type: {type(payload).__name__}')
    if not isinstance(rows, list):
        raise ProviderError('Provider "data" field was not a list')
    return [c for c in rows if isinstance(c, dict)]


"""
* Pokemon (pokemontcg.io)
"""


def _normalize_pokemon_card(c: dict) -> Optional[dict]:
    card_set = c.get('set') or {}
    if not isinstance(card_set, dict):
        card_set = {}
    raw_id = str(c.get('id') or '')
    if not raw_id or not c.get('name'):
        return None
    return {
        'object': 'card',
        'game': 'pokemon',
        'id': f"pkm-{_safe_id(raw_id)}",
        'name': c.get('name', ''),
        'set': card_set.get('id', ''),
        'set_name': card_set.get('name', ''),
        'collector_number': str(c.get('number', '')),
        'lang': 'en',
        'released_at': (card_set.get('releaseDate') or '').replace('/', '-'),
        'images': c.get('images') or {},
        'provider_data': c,
    }


def search_pokemon(name: str, limit: int = 20) -> list[dict]:
    """Search Pokémon cards by name. Hi-res scans come from images.large.

    Optional env PROXYSHOP_POKEMONTCG_KEY raises the free-tier rate limits.
    """
    headers = {}
    key = _read_secret('PROXYSHOP_POKEMONTCG_KEY', _POKEMONTCG_KEY_FILE)
    if key:
        headers['X-Api-Key'] = key
    data = _get(
        f'{POKEMON_API}/cards',
        params={
            'q': f'name:"*{name}*"',
            'pageSize': min(limit, 50),
            'orderBy': '-set.releaseDate'},
        extra_headers=headers)
    cards = []
    for c in _card_rows(data):
        normalized = _normalize_pokemon_card(c)
        if normalized:
            cards.append(normalized)
    return cards


def list_pokemon_page(
    query: str,
    page: int = 1,
    page_size: int = 250,
) -> tuple[list[dict], Optional[int]]:
    """Fetch one pokemontcg.io search page for selective caching."""
    headers = {}
    key = _read_secret('PROXYSHOP_POKEMONTCG_KEY', _POKEMONTCG_KEY_FILE)
    if key:
        headers['X-Api-Key'] = key
    page = max(int(page), 1)
    page_size = min(max(int(page_size), 1), 250)
    data = _get(
        f'{POKEMON_API}/cards',
        params={
            'q': query,
            'page': page,
            'pageSize': page_size,
            'orderBy': 'set.releaseDate,number'},
        extra_headers=headers)
    cards: list[dict] = []
    for c in _card_rows(data):
        normalized = _normalize_pokemon_card(c)
        if normalized:
            cards.append(normalized)
    total: Optional[int] = None
    if isinstance(data, dict) and data.get('totalCount') is not None:
        try:
            total = int(data['totalCount'])
        except (TypeError, ValueError):
            total = None
    return cards, total


def list_pokemon_sets() -> list[dict]:
    """Compact Pokémon set list for cache UI pickers."""
    headers = {}
    key = _read_secret('PROXYSHOP_POKEMONTCG_KEY', _POKEMONTCG_KEY_FILE)
    if key:
        headers['X-Api-Key'] = key
    data = _get(
        f'{POKEMON_API}/sets',
        params={'orderBy': '-releaseDate', 'pageSize': 250},
        extra_headers=headers)
    rows = []
    for s in _card_rows(data):
        if not s.get('id'):
            continue
        rows.append({
            'id': s.get('id'),
            'name': s.get('name') or s.get('id'),
            'released_at': (s.get('releaseDate') or '').replace('/', '-'),
            'card_count': s.get('total') or s.get('printedTotal'),
            'series': s.get('series'),
        })
    return rows


def list_pokemon_meta() -> dict:
    """Types / subtypes / rarities / supertypes for filter dropdowns."""
    headers = {}
    key = _read_secret('PROXYSHOP_POKEMONTCG_KEY', _POKEMONTCG_KEY_FILE)
    if key:
        headers['X-Api-Key'] = key
    fallback_types = [
        'Colorless', 'Darkness', 'Dragon', 'Fairy', 'Fighting', 'Fire',
        'Grass', 'Lightning', 'Metal', 'Psychic', 'Water']
    fallback_supertypes = ['Pokémon', 'Trainer', 'Energy']

    def _list(path: str) -> list[str]:
        try:
            payload = _get(f'{POKEMON_API}/{path}', params={}, extra_headers=headers)
        except ProviderError:
            return []
        if isinstance(payload, dict):
            rows = payload.get('data') or []
        elif isinstance(payload, list):
            rows = payload
        else:
            rows = []
        return [str(x) for x in rows if x]

    return {
        'types': _list('types') or fallback_types,
        'subtypes': _list('subtypes'),
        'rarities': _list('rarities'),
        'supertypes': _list('supertypes') or fallback_supertypes,
    }


"""
* Union Arena (official NA + JP cardlists — no API key)
"""

_UA_CARD_RE = re.compile(
    r'card_no=([^"\'&\s>]+)'
    r'[\s\S]*?'
    r'data-src="([^"]*images/cardlist/card/[^"]+)"'
    r'[^>]*\balt="([^"]*)"',
    re.IGNORECASE,
)
_UA_SERIES_OPTION_RE = re.compile(
    r'<option\b[^>]*\bvalue="(\d+)"[^>]*>([^<]*)</option>',
    re.IGNORECASE,
)

# Cached as list of (locale, series_id, label)
_ua_series_cache: Optional[list[tuple[str, str, str]]] = None
_ua_series_lock = threading.Lock()


def _ua_locale_meta(locale: str) -> dict:
    meta = UA_LOCALES.get(locale) or UA_LOCALES['en']
    return meta


def _ua_cardlist(locale: str = 'en') -> str:
    return f"{UA_ORIGIN}/{_ua_locale_meta(locale)['path']}/cardlist"


def _ua_images_base(locale: str = 'en') -> str:
    return f"{UA_ORIGIN}/{_ua_locale_meta(locale)['path']}/images/cardlist/card"


def _ua_image_url(card_no: str, locale: str = 'en') -> str:
    """Build the official cardlist PNG URL from a card_no like UE01BT/BLC-1-001."""
    filename = str(card_no or '').replace('/', '_')
    if not filename.lower().endswith('.png'):
        filename = f'{filename}.png'
    return f'{_ua_images_base(locale)}/{filename}'


def _ua_absolute_image(src: str, card_no: str = '', *, locale: str = 'en') -> str:
    """Resolve a cardlist image src to an absolute URL (drop cache-buster query)."""
    raw = (src or '').strip()
    if raw:
        if raw.startswith('http://') or raw.startswith('https://'):
            return raw.split('?', 1)[0]
        if raw.startswith('/'):
            return urljoin(f'{UA_ORIGIN}/', raw.lstrip('/')).split('?', 1)[0]
        # Relative paths are rooted under the locale site (na/ or jp/)
        return urljoin(
            f"{UA_ORIGIN}/{_ua_locale_meta(locale)['path']}/", raw
        ).split('?', 1)[0]
    return _ua_image_url(card_no, locale=locale) if card_no else ''


def _ua_name_from_alt(alt: str, card_no: str) -> str:
    text = html_lib.unescape(alt or '').strip()
    if card_no and text.startswith(card_no):
        return text[len(card_no):].strip() or text
    # Parallel alts often use the base card_no (without _pN) as the prefix
    base = re.sub(r'_p\d+$', '', card_no or '', flags=re.IGNORECASE)
    if base and base != card_no and text.startswith(base):
        return text[len(base):].strip() or text
    # Fallback: strip any leading PRODUCT/CODE token
    stripped = re.sub(r'^[A-Z0-9]+(?:/[A-Z0-9._-]+)?\s+', '', text)
    return stripped.strip() or text


def _ua_card_id(card_no: str, locale: str = 'en') -> str:
    """Stable id; JP gets a lang prefix so it never collides with NA."""
    safe = _safe_id(card_no)
    if locale == 'ja':
        return f'ua-ja-{safe}'
    return f'ua-{safe}'


def _parse_ua_cardlist_html(page_html: str, *, locale: str = 'en') -> list[dict]:
    """Extract card rows from official cardlist search/series HTML."""
    rows: list[dict] = []
    seen: set[str] = set()
    for match in _UA_CARD_RE.finditer(page_html or ''):
        card_no = html_lib.unescape(match.group(1)).strip()
        if not card_no or card_no in seen:
            continue
        seen.add(card_no)
        image = _ua_absolute_image(
            html_lib.unescape(match.group(2)), card_no, locale=locale)
        name = _ua_name_from_alt(match.group(3), card_no)
        product = card_no.split('/', 1)[0] if '/' in card_no else ''
        code = card_no.split('/', 1)[1] if '/' in card_no else card_no
        rows.append({
            'card_no': card_no,
            'code': code,
            'name': name,
            'set_name': product,
            'image': image or _ua_image_url(card_no, locale=locale),
            'locale': locale,
        })
    return rows


def _parse_ua_series(page_html: str) -> list[tuple[str, str]]:
    """Parse product series options from the cardlist filter form."""
    series: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in _UA_SERIES_OPTION_RE.finditer(page_html or ''):
        series_id = match.group(1).strip()
        label = html_lib.unescape(match.group(2)).strip()
        if not series_id or series_id in seen:
            continue
        seen.add(series_id)
        series.append((series_id, label))
    return series


def _ua_fetch_html(url: str, params: Optional[dict] = None) -> str:
    res = _request(url, params=params)
    return res.text or ''


def _ua_series_list(*, force: bool = False) -> list[tuple[str, str, str]]:
    """Return (locale, series_id, label) for NA then JP product dropdowns."""
    global _ua_series_cache
    with _ua_series_lock:
        if _ua_series_cache is not None and not force:
            return list(_ua_series_cache)
    combined: list[tuple[str, str, str]] = []
    errors: list[str] = []
    for locale in UA_LOCALE_ORDER:
        try:
            page_html = _ua_fetch_html(f'{_ua_cardlist(locale)}/')
            for series_id, label in _parse_ua_series(page_html):
                combined.append((locale, series_id, label))
        except ProviderError as e:
            errors.append(f'{locale}: {e}')
    if not combined:
        detail = '; '.join(errors) if errors else 'empty responses'
        raise ProviderError(f'Union Arena cardlist returned no product series ({detail})')
    with _ua_series_lock:
        _ua_series_cache = combined
        return list(combined)


def _normalize_union_arena_card(
    row: dict,
    *,
    set_name: str = '',
    locale: str = 'en',
) -> Optional[dict]:
    """Map a parsed official cardlist row into Proxyshop's cached card shape."""
    card_no = str(row.get('card_no') or row.get('id') or '').strip()
    name = str(row.get('name') or '').strip()
    if not card_no or not name:
        return None
    locale = str(row.get('locale') or locale or 'en')
    if locale not in UA_LOCALES:
        locale = 'en'
    lang = _ua_locale_meta(locale)['lang']
    code = str(row.get('code') or '')
    if not code:
        code = card_no.split('/', 1)[1] if '/' in card_no else card_no
    product = str(row.get('set_name') or '')
    if not product and '/' in card_no:
        product = card_no.split('/', 1)[0]
    label = (set_name or product or '').strip()
    image = (
        _ua_absolute_image(str(row.get('image') or ''), card_no, locale=locale)
        or _ua_image_url(card_no, locale=locale)
    )
    cardlist = _ua_cardlist(locale)
    provider = {
        'card_no': card_no,
        'code': code,
        'name': name,
        'locale': locale,
        'set': {'name': label} if label else {},
        'images': {'small': image, 'large': image},
        'url': f'{cardlist}/detail_iframe.php?card_no={card_no}',
    }
    return {
        'object': 'card',
        'game': 'union-arena',
        'id': _ua_card_id(card_no, locale),
        'name': name,
        'set': label,
        'set_name': label,
        'collector_number': code,
        'lang': lang,
        'released_at': None,
        'images': {'small': image, 'large': image},
        'provider_data': provider,
    }


def search_union_arena(name: str, limit: int = 20) -> list[dict]:
    """Search Union Arena cards via official NA + JP cardlists (no API key).

    Always queries both locales. When both return hits, results are interleaved
    so Japanese printings are not crowded out by a large English match set.
    """
    q = (name or '').strip()
    if len(q) < 2:
        return []
    limit = max(limit, 1)
    by_locale: dict[str, list[dict]] = {loc: [] for loc in UA_LOCALE_ORDER}
    seen: set[str] = set()
    for locale in UA_LOCALE_ORDER:
        page_html = _ua_fetch_html(
            f'{_ua_cardlist(locale)}/index.php',
            params={'search': 'true', 'freewords': q})
        for row in _parse_ua_cardlist_html(page_html, locale=locale):
            card = _normalize_union_arena_card(row, locale=locale)
            if not card or card['id'] in seen:
                continue
            seen.add(card['id'])
            by_locale[locale].append(card)
            if len(by_locale[locale]) >= limit:
                break

    # Interleave so both EN and JA show up when both matched
    cards: list[dict] = []
    buckets = [by_locale[loc] for loc in UA_LOCALE_ORDER]
    indexes = [0] * len(buckets)
    while len(cards) < limit:
        progressed = False
        for i, bucket in enumerate(buckets):
            if indexes[i] < len(bucket):
                cards.append(bucket[indexes[i]])
                indexes[i] += 1
                progressed = True
                if len(cards) >= limit:
                    break
        if not progressed:
            break
    return cards


def list_union_arena_page(page: int = 1, limit: int = 50) -> tuple[list[dict], Optional[int]]:
    """Fetch one Union Arena catalog page (one official product series).

    ``page`` is 1-based over the combined NA+JP product dropdowns. ``limit`` is
    unused (the site returns the full series in one HTML response). Returns
    ``(cards, series_count)``.
    """
    del limit  # full series per page; client-side pager is irrelevant
    series = _ua_series_list()
    page = max(page, 1)
    if page > len(series):
        return [], len(series)
    locale, series_id, series_name = series[page - 1]
    page_html = _ua_fetch_html(
        f'{_ua_cardlist(locale)}/index.php',
        params={'search': 'true', 'series': series_id})
    cards: list[dict] = []
    seen: set[str] = set()
    for row in _parse_ua_cardlist_html(page_html, locale=locale):
        card = _normalize_union_arena_card(row, set_name=series_name, locale=locale)
        if not card or card['id'] in seen:
            continue
        seen.add(card['id'])
        cards.append(card)
    return cards, len(series)


"""
* Riftbound (riftscribe.gg — public, no API key)
"""


def _normalize_riftbound_card(c: dict) -> Optional[dict]:
    """Map a RiftScribe card object into Proxyshop's cached card shape."""
    raw_id = str(c.get('id') or c.get('card_id') or '')
    if not raw_id:
        return None
    name = c.get('name') or ''
    if not name:
        return None

    thumbs = c.get('image_thumb') or {}
    if not isinstance(thumbs, dict):
        thumbs = {}
    art = c.get('art') if isinstance(c.get('art'), dict) else {}
    art_thumbs = art.get('image_thumb') if isinstance(art.get('image_thumb'), dict) else {}
    large = (
        c.get('image')
        or art.get('image')
        or thumbs.get('large')
        or art_thumbs.get('large')
        or c.get('thumbnail_url')
        or '')
    small = (
        thumbs.get('small')
        or art_thumbs.get('small')
        or c.get('thumbnail_url')
        or large)

    stats = c.get('stats') if isinstance(c.get('stats'), dict) else {}
    faction = str(c.get('faction') or '')
    domain = faction[:1].upper() + faction[1:] if faction else ''
    card_type = str(c.get('type') or c.get('cardType') or '')
    num = c.get('collector_number')
    if num is None or num == '':
        # ids look like ogs-001-024 → collector 001
        parts = raw_id.split('-')
        num = parts[1] if len(parts) >= 2 else raw_id
    set_id = str(c.get('set_id') or '').upper()

    provider = dict(c)
    # Aliases expected by the compose renderer / editor
    provider.setdefault('domain', domain)
    provider.setdefault('cardType', card_type)
    provider.setdefault('energyCost', stats.get('energy'))
    provider.setdefault('powerCost', stats.get('power'))
    provider.setdefault('might', stats.get('might'))
    provider.setdefault('description', c.get('description') or '')
    provider.setdefault('flavorText', c.get('flavor_text') or '')
    provider.setdefault('rarity', c.get('rarity') or '')
    provider.setdefault('code', str(num))
    provider.setdefault('number', str(num))
    provider.setdefault('set', {'id': set_id.lower(), 'name': set_id})
    if art.get('artist'):
        provider.setdefault('artist', art.get('artist'))

    return {
        'object': 'card',
        'game': 'riftbound',
        'id': f"rb-{_safe_id(raw_id)}",
        'name': name,
        'set': set_id.lower(),
        'set_name': set_id,
        'collector_number': str(num),
        'lang': 'en',
        'released_at': None,
        'images': {
            'small': small,
            'large': large,
        },
        'provider_data': provider,
    }


def _hydrate_riftbound(card: dict) -> dict:
    """Fetch full RiftScribe detail for a thin list row (best-effort)."""
    detail_id = (card.get('provider_data') or {}).get('id') or (
        card.get('provider_data') or {}).get('card_id')
    if not detail_id:
        return card
    try:
        detail = _get(f'{RIFTSCRIBE_API}/cards/{detail_id}', params={})
    except ProviderError:
        return card
    if not isinstance(detail, dict):
        return card
    merged = _normalize_riftbound_card({**(card.get('provider_data') or {}), **detail})
    return merged or card


def search_riftbound(name: str, limit: int = 20) -> list[dict]:
    """Search Riftbound cards via RiftScribe (no API key required)."""
    q = (name or '').strip()
    if len(q) < 2:
        return []
    data = _get(
        f'{RIFTSCRIBE_API}/cards',
        params={'q': q, 'limit': min(max(limit, 1), 50)})
    cards: list[dict] = []
    for c in _card_rows(data):
        normalized = _normalize_riftbound_card(c)
        if normalized:
            cards.append(normalized)
    return [_hydrate_riftbound(card) for card in cards[:limit]]


def list_riftbound_page(
    offset: int = 0,
    limit: int = 50,
    *,
    hydrate: bool = False,
) -> tuple[list[dict], Optional[int]]:
    """Fetch one RiftScribe catalog page.

    Returns (cards, total_hint) where total_hint comes from X-Total-Count when present.
    """
    limit = min(max(limit, 1), 100)
    offset = max(offset, 0)
    res = _request(
        f'{RIFTSCRIBE_API}/cards',
        params={'limit': limit, 'offset': offset})
    try:
        payload = res.json()
    except ValueError as e:
        raise ProviderError('Provider returned non-JSON response') from e
    cards: list[dict] = []
    for c in _card_rows(payload):
        normalized = _normalize_riftbound_card(c)
        if normalized:
            cards.append(_hydrate_riftbound(normalized) if hydrate else normalized)
    total: Optional[int] = None
    raw_total = res.headers.get('X-Total-Count') or res.headers.get('x-total-count')
    if raw_total:
        try:
            total = int(raw_total)
        except ValueError:
            total = None
    return cards, total


# Games that support cache-game (selective for mtg/pokemon; full for small TCGs)
CATALOG_GAMES = ('mtg', 'pokemon', 'riftbound', 'union-arena')

# Registry used by the server: game -> search callable
PROVIDERS: dict[str, Callable[[str, int], list[dict]]] = {
    'pokemon': search_pokemon,
    'union-arena': search_union_arena,
    'riftbound': search_riftbound,
}
