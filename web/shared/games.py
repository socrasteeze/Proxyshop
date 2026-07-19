"""
* Multi-Game Card Providers
* Search providers for non-MTG trading card games, normalized into the same
* card shape the local database stores. MTG stays on the Scryfall path in
* carddb; these providers cover:
*   - pokemon      -> pokemontcg.io (free; optional API key raises limits)
*   - union-arena  -> unionarena-tcg.com NA+JP cardlists (public; no API key)
*   - riftbound    -> riftcodex.com (+ DotGG ARC; official JA/KO names)
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
# RiftScribe lacked VEN/promos; Riftcodex is the primary catalog + Riot CDN arts.
RIFTCODEX_API = 'https://api.riftcodex.com'
DOTGG_RIFTBOUND_CARDS = 'https://api.dotgg.gg/cgfw/getcards'
RB_OFFICIAL_ORIGIN = 'https://playriftbound.com'
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
        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError) as e:
            # Transient network hiccups (read timeout, dropped connection) are
            # common on long catalog runs — retry with backoff instead of
            # crashing the whole download.
            last_err = ProviderError(f'Provider connection error: {e}')
            if attempt >= PROVIDER_MAX_RETRIES:
                raise last_err from e
            time.sleep(min(30.0, 1.5 * (2 ** attempt)))
            continue
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
* Riftbound (Riftcodex primary + DotGG ARC + official JA/KO names)
"""

_rb_arc_cache: Optional[list[dict]] = None
_rb_arc_lock = threading.Lock()
_rb_locale_cache: dict[str, dict[str, dict]] = {}
_rb_locale_lock = threading.Lock()
_rb_build_id: Optional[str] = None


def _rb_clean_image(url: str) -> str:
    """Prefer the bare Riot CDN URL (full 744x1039) without resize query params."""
    raw = (url or '').strip()
    if not raw:
        return ''
    # Keep accountingTag-free path so ensure_image caches one file per art
    return raw.split('?', 1)[0]


def _normalize_riftcodex_card(item: dict, *, lang: str = 'en') -> Optional[dict]:
    """Map a Riftcodex card object into Proxyshop's cached card shape."""
    if not isinstance(item, dict):
        return None
    raw_id = str(item.get('riftbound_id') or item.get('id') or '').strip()
    name = str(item.get('name') or '').strip()
    if not raw_id or not name:
        return None

    media = item.get('media') if isinstance(item.get('media'), dict) else {}
    attrs = item.get('attributes') if isinstance(item.get('attributes'), dict) else {}
    classification = (
        item.get('classification')
        if isinstance(item.get('classification'), dict) else {})
    text = item.get('text') if isinstance(item.get('text'), dict) else {}
    set_info = item.get('set') if isinstance(item.get('set'), dict) else {}
    metadata = item.get('metadata') if isinstance(item.get('metadata'), dict) else {}

    large = _rb_clean_image(str(media.get('image_url') or ''))
    set_id = str(set_info.get('set_id') or '').upper()
    set_label = str(set_info.get('label') or set_id)
    num = item.get('collector_number')
    if num is None or num == '':
        parts = raw_id.split('-')
        num = parts[1] if len(parts) >= 2 else raw_id

    domains = classification.get('domain') or []
    if isinstance(domains, list) and domains:
        domain = str(domains[0])
    else:
        domain = str(domains or '')
    if domain and domain == domain.lower():
        domain = domain[:1].upper() + domain[1:]
    card_type = str(classification.get('type') or '')
    rarity = str(classification.get('rarity') or '')

    provider = dict(item)
    provider.setdefault('domain', domain)
    provider.setdefault('cardType', card_type)
    provider.setdefault('energyCost', attrs.get('energy'))
    provider.setdefault('powerCost', attrs.get('power'))
    provider.setdefault('might', attrs.get('might'))
    provider.setdefault('description', text.get('plain') or '')
    provider.setdefault('flavorText', text.get('flavour') or '')
    provider.setdefault('rarity', rarity)
    provider.setdefault('code', str(num))
    provider.setdefault('number', str(num))
    provider.setdefault('set', {'id': set_id.lower(), 'name': set_label})
    if media.get('artist'):
        provider.setdefault('artist', media.get('artist'))
    provider['riftbound_id'] = raw_id
    provider['source'] = 'riftcodex'
    if metadata:
        provider['metadata'] = metadata

    card_id = f"rb-{_safe_id(raw_id)}"
    if lang != 'en':
        card_id = f"rb-{lang}-{_safe_id(raw_id)}"

    return {
        'object': 'card',
        'game': 'riftbound',
        'id': card_id,
        'name': name,
        'set': set_id.lower(),
        'set_name': set_label or set_id,
        'collector_number': str(num),
        'lang': lang,
        'released_at': None,
        'images': {
            'small': large,
            'large': large,
        },
        'provider_data': provider,
    }


def _normalize_dotgg_arc_card(c: dict) -> Optional[dict]:
    """Map a DotGG ARC (Chinese Arcane Box) row into Proxyshop shape."""
    raw_id = str(c.get('id') or '').strip()
    name = str(c.get('name') or '').strip()
    if not raw_id or not name:
        return None
    if not raw_id.upper().startswith('ARC'):
        return None
    image = _rb_clean_image(str(c.get('image') or ''))
    set_name = str(c.get('set_name') or 'Arcane Box Set')
    colors = c.get('color') or []
    domain = ''
    if isinstance(colors, list) and colors:
        domain = str(colors[0])
    elif isinstance(colors, str):
        domain = colors
    if domain and domain == domain.lower():
        domain = domain[:1].upper() + domain[1:]
    card_type = str(c.get('type') or '')
    num = raw_id.split('-', 1)[1] if '-' in raw_id else raw_id

    provider = dict(c)
    provider.setdefault('domain', domain)
    provider.setdefault('cardType', card_type)
    provider.setdefault('energyCost', c.get('cost'))
    provider.setdefault('might', c.get('might'))
    provider.setdefault('description', c.get('effect') or '')
    provider.setdefault('flavorText', c.get('flavor') or '')
    provider.setdefault('rarity', c.get('rarity') or '')
    provider.setdefault('code', str(num))
    provider.setdefault('number', str(num))
    provider.setdefault('set', {'id': 'arc', 'name': set_name})
    provider['riftbound_id'] = raw_id.lower()
    provider['source'] = 'dotgg-arc'

    return {
        'object': 'card',
        'game': 'riftbound',
        'id': f"rb-arc-{_safe_id(raw_id)}",
        'name': name,
        'set': 'arc',
        'set_name': set_name,
        'collector_number': str(num),
        'lang': 'en',
        'released_at': None,
        'images': {
            'small': image,
            'large': image,
        },
        'provider_data': provider,
    }


def _riftcodex_items(payload: Any) -> list[dict]:
    if isinstance(payload, dict):
        rows = payload.get('items')
        if isinstance(rows, list):
            return [c for c in rows if isinstance(c, dict)]
    return _card_rows(payload)


def _list_arc_cards(*, force: bool = False) -> list[dict]:
    """Chinese Arcane Box promos from DotGG (absent from Riftcodex)."""
    global _rb_arc_cache
    with _rb_arc_lock:
        if _rb_arc_cache is not None and not force:
            return list(_rb_arc_cache)
    payload = _get(DOTGG_RIFTBOUND_CARDS, params={'game': 'riftbound'})
    rows = payload if isinstance(payload, list) else _card_rows(payload)
    cards: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        card = _normalize_dotgg_arc_card(row)
        if not card or card['id'] in seen:
            continue
        seen.add(card['id'])
        cards.append(card)
    with _rb_arc_lock:
        _rb_arc_cache = cards
        return list(cards)


def _rb_official_build_id(*, force: bool = False) -> str:
    """Resolve the Next.js buildId for playriftbound.com card gallery."""
    global _rb_build_id
    if _rb_build_id and not force:
        return _rb_build_id
    res = _request(f'{RB_OFFICIAL_ORIGIN}/en-us/card-gallery/')
    match = re.search(r'"buildId"\s*:\s*"([^"]+)"', res.text or '')
    if not match:
        raise ProviderError('Could not resolve playriftbound.com buildId')
    _rb_build_id = match.group(1)
    return _rb_build_id


def _rb_walk_official_cards(obj: Any):
    """Yield card-like dicts from official gallery Next.js JSON."""
    if isinstance(obj, dict):
        if 'cardImage' in obj and 'name' in obj and (
                'publicCode' in obj or 'id' in obj):
            yield obj
        for value in obj.values():
            yield from _rb_walk_official_cards(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _rb_walk_official_cards(value)


def _rb_locale_index(locale: str, *, force: bool = False) -> dict[str, dict]:
    """Map riftbound id / publicCode → {name, image, public_code} for a locale."""
    locale = (locale or '').lower()
    with _rb_locale_lock:
        if locale in _rb_locale_cache and not force:
            return dict(_rb_locale_cache[locale])
    build = _rb_official_build_id(force=force)
    payload = _get(
        f'{RB_OFFICIAL_ORIGIN}/_next/data/{build}/{locale}/card-gallery.json',
        params={})
    index: dict[str, dict] = {}
    for card in _rb_walk_official_cards(payload):
        name = str(card.get('name') or '').strip()
        if not name:
            continue
        public_code = str(card.get('publicCode') or '').strip()
        raw_id = str(card.get('id') or '').strip().lower()
        image = ''
        img = card.get('cardImage')
        if isinstance(img, dict):
            image = _rb_clean_image(str(img.get('url') or ''))
        entry = {
            'name': name,
            'image': image,
            'public_code': public_code,
            'id': raw_id,
        }
        if raw_id:
            index[raw_id] = entry
        if public_code:
            # OGN-001/298 → also index ogn-001-298 style keys when possible
            index[public_code] = entry
            compact = public_code.replace('/', '-').lower()
            index[compact] = entry
    with _rb_locale_lock:
        _rb_locale_cache[locale] = index
        return dict(index)


def _rb_localized_variant(en_card: dict, locale: str, lang: str) -> Optional[dict]:
    """Build a lang-specific row from an EN card + official locale name map."""
    provider = en_card.get('provider_data') or {}
    raw_id = str(provider.get('riftbound_id') or '').lower()
    if not raw_id:
        return None
    index = _rb_locale_index(locale)
    entry = index.get(raw_id)
    if not entry:
        # try collector-style public code
        set_id = str(en_card.get('set') or '').upper()
        num = str(en_card.get('collector_number') or '')
        if set_id and num:
            entry = index.get(f'{set_id}-{num}') or index.get(
                f'{set_id}-{num.zfill(3)}')
    if not entry:
        return None
    loc_name = entry.get('name') or ''
    if not loc_name or loc_name == en_card.get('name'):
        return None
    clone = dict(en_card)
    clone['id'] = f"rb-{lang}-{_safe_id(raw_id)}"
    clone['name'] = loc_name
    clone['lang'] = lang
    image = entry.get('image') or (en_card.get('images') or {}).get('large') or ''
    clone['images'] = {'small': image, 'large': image}
    prov = dict(provider)
    prov['name'] = loc_name
    prov['locale'] = locale
    prov['source_name_en'] = en_card.get('name')
    clone['provider_data'] = prov
    return clone


def _rb_enrich_search_hits(cards: list[dict], query: str, limit: int) -> list[dict]:
    """Append JA/KO name matches and ARC hits for a live search query."""
    q = (query or '').strip().lower()
    if not q:
        return cards[:limit]
    seen = {c['id'] for c in cards}
    out = list(cards)

    # Localized official names (substring)
    for locale, lang in (('ja-jp', 'ja'), ('ko-kr', 'ko')):
        if len(out) >= limit:
            break
        try:
            index = _rb_locale_index(locale)
        except ProviderError:
            continue
        for entry in index.values():
            name = str(entry.get('name') or '')
            if q not in name.lower():
                continue
            raw_id = str(entry.get('id') or '').lower()
            if not raw_id:
                continue
            card_id = f'rb-{lang}-{_safe_id(raw_id)}'
            if card_id in seen:
                continue
            # Prefer pairing with an EN Riftcodex row when possible
            en_id = f'rb-{_safe_id(raw_id)}'
            en_card = next((c for c in cards if c.get('id') == en_id), None)
            if en_card:
                variant = _rb_localized_variant(en_card, locale, lang)
            else:
                variant = {
                    'object': 'card',
                    'game': 'riftbound',
                    'id': card_id,
                    'name': name,
                    'set': raw_id.split('-')[0] if '-' in raw_id else '',
                    'set_name': (raw_id.split('-')[0] if '-' in raw_id else '').upper(),
                    'collector_number': (
                        raw_id.split('-')[1] if '-' in raw_id else raw_id),
                    'lang': lang,
                    'released_at': None,
                    'images': {
                        'small': entry.get('image') or '',
                        'large': entry.get('image') or '',
                    },
                    'provider_data': {
                        'riftbound_id': raw_id,
                        'name': name,
                        'locale': locale,
                        'source': 'official-gallery',
                    },
                }
            if not variant or variant['id'] in seen:
                continue
            seen.add(variant['id'])
            out.append(variant)
            if len(out) >= limit:
                break

    if len(out) < limit:
        try:
            for card in _list_arc_cards():
                if q in card['name'].lower() and card['id'] not in seen:
                    seen.add(card['id'])
                    out.append(card)
                    if len(out) >= limit:
                        break
        except ProviderError:
            pass
    return out[:limit]


def search_riftbound(name: str, limit: int = 20) -> list[dict]:
    """Search Riftbound cards via Riftcodex (plus ARC / JA-KO name matches)."""
    q = (name or '').strip()
    if len(q) < 2:
        return []
    limit = min(max(limit, 1), 50)
    data = _get(
        f'{RIFTCODEX_API}/cards/name',
        params={'fuzzy': q, 'size': limit, 'page': 1})
    cards: list[dict] = []
    seen: set[str] = set()
    for item in _riftcodex_items(data):
        normalized = _normalize_riftcodex_card(item)
        if not normalized or normalized['id'] in seen:
            continue
        seen.add(normalized['id'])
        cards.append(normalized)
        if len(cards) >= limit:
            break
    return _rb_enrich_search_hits(cards, q, limit)


def list_riftbound_page(
    offset: int = 0,
    limit: int = 50,
    *,
    hydrate: bool = False,
) -> tuple[list[dict], Optional[int]]:
    """Fetch one Riftbound catalog page (Riftcodex, then ARC extras).

    ``hydrate`` is accepted for API compatibility; Riftcodex list rows are full.
    Returns (cards, total_hint) where total includes ARC promos after the
    Riftcodex catalog.
    """
    del hydrate  # Riftcodex payloads already include art + text
    limit = min(max(limit, 1), 100)
    offset = max(offset, 0)

    meta = _get(f'{RIFTCODEX_API}/cards', params={'page': 1, 'size': 1})
    rc_total = 0
    if isinstance(meta, dict) and meta.get('total') is not None:
        try:
            rc_total = int(meta['total'])
        except (TypeError, ValueError):
            rc_total = 0

    arc_cards: list[dict] = []
    try:
        arc_cards = _list_arc_cards()
    except ProviderError:
        arc_cards = []
    total = rc_total + len(arc_cards)

    if offset < rc_total:
        page = (offset // limit) + 1
        data = _get(
            f'{RIFTCODEX_API}/cards',
            params={'page': page, 'size': limit})
        cards: list[dict] = []
        for item in _riftcodex_items(data):
            normalized = _normalize_riftcodex_card(item)
            if normalized:
                cards.append(normalized)
        return cards, total

    arc_offset = offset - rc_total
    if arc_offset >= len(arc_cards):
        return [], total
    return arc_cards[arc_offset:arc_offset + limit], total


def list_riftbound_locale_page(
    offset: int = 0,
    limit: int = 50,
    *,
    locale: str = 'ja-jp',
    lang: str = 'ja',
) -> tuple[list[dict], Optional[int]]:
    """Optional helper: emit localized name rows for cache enrichment.

    Pairs official locale names with Riftcodex EN rows when possible.
    """
    limit = min(max(limit, 1), 100)
    offset = max(offset, 0)
    # Use EN catalog page, then localize names
    en_cards, total = list_riftbound_page(offset=offset, limit=limit)
    cards: list[dict] = []
    for en_card in en_cards:
        if en_card.get('id', '').startswith('rb-arc-'):
            continue
        variant = _rb_localized_variant(en_card, locale, lang)
        if variant:
            cards.append(variant)
    return cards, total


# Games that support cache-game (selective for mtg/pokemon; full for small TCGs)
CATALOG_GAMES = ('mtg', 'pokemon', 'riftbound', 'union-arena')

# Registry used by the server: game -> search callable
PROVIDERS: dict[str, Callable[[str, int], list[dict]]] = {
    'pokemon': search_pokemon,
    'union-arena': search_union_arena,
    'riftbound': search_riftbound,
}
