"""
* Multi-Game Card Providers
* Search providers for non-MTG trading card games, normalized into the same
* card shape the local database stores. MTG stays on the Scryfall path in
* carddb; these providers cover:
*   - pokemon      -> pokemontcg.io (free; optional API key raises limits)
*   - union-arena  -> apitcg.com   (community aggregator; free API key required)
*   - riftbound    -> riftscribe.gg (public; no API key)
* Must never import from `src/`.
"""
# Standard Library Imports
import os
import re
from pathlib import Path
from typing import Any, Callable, Optional

# Third Party Imports
import requests

# Local Imports
from web.shared.carddb import HEADERS

POKEMON_API = 'https://api.pokemontcg.io/v2'
# Canonical host — bare apitcg.com returns HTTP 308 → www.apitcg.com
APITCG_API = 'https://www.apitcg.com/api'
RIFTSCRIBE_API = 'https://riftscribe.gg/api'

# Optional in-container secret files (mounted by nas-update.sh)
_APITCG_KEY_FILE = os.environ.get(
    'PROXYSHOP_APITCG_KEY_FILE', '/run/secrets/proxyshop-apitcg-key')
_POKEMONTCG_KEY_FILE = os.environ.get(
    'PROXYSHOP_POKEMONTCG_KEY_FILE', '/run/secrets/proxyshop-pokemontcg-key')

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
    """Filesystem-safe id fragment (apitcg ids often contain '/')."""
    return re.sub(r'[^A-Za-z0-9._-]+', '-', str(value or '')).strip('-') or 'unknown'


def _get(url: str, params: dict, extra_headers: Optional[dict] = None) -> Any:
    headers = dict(HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    try:
        res = requests.get(
            url, params=params, headers=headers, timeout=30, allow_redirects=True)
    except requests.RequestException as e:
        raise ProviderError(f'Provider request failed: {e}') from e
    # Defend against clients/proxies that surface the 308 body instead of following
    if res.status_code in (301, 302, 307, 308):
        raise ProviderError(
            f'Provider redirected ({res.status_code}) to '
            f'{res.headers.get("Location") or "unknown"} — check APITCG_API host')
    if res.status_code in (401, 403):
        raise ProviderError('Provider rejected the request (check the API key).')
    if res.status_code == 429:
        raise ProviderError('Provider rate limit hit — try again in a minute.')
    if res.status_code >= 400:
        body = (res.text or '')[:200].strip()
        raise ProviderError(
            f'Provider HTTP {res.status_code}'
            + (f': {body}' if body else ''))
    try:
        payload = res.json()
    except ValueError as e:
        raise ProviderError('Provider returned non-JSON response') from e
    # apitcg often returns HTTP 200 with {"error": "..."} for auth failures
    if isinstance(payload, dict) and payload.get('error') and 'data' not in payload:
        raise ProviderError(str(payload.get('error')))
    if isinstance(payload, dict) and payload.get('success') is False:
        raise ProviderError(str(payload.get('error') or 'Provider request failed'))
    return payload


def _card_rows(payload: Any) -> list[dict]:
    """Normalize provider JSON into a list of card dicts.

    apitcg responses vary: ``{"data":[...]}``, bare ``[...]``, or null ``data``.
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
        card_set = c.get('set') or {}
        if not isinstance(card_set, dict):
            card_set = {}
        raw_id = str(c.get('id') or '')
        cards.append({
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
        })
    return cards


"""
* apitcg.com helpers (Union Arena, Riftbound)
"""


def _apitcg_key() -> str:
    """Return the apitcg.com API key or raise ProviderError."""
    key = _read_secret('PROXYSHOP_APITCG_KEY', _APITCG_KEY_FILE)
    if not key:
        raise ProviderError(
            'Union Arena needs a free apitcg.com API key — register at '
            'https://apitcg.com, save it with: '
            'echo \'YOUR_KEY\' > ~/.proxyshop-apitcg-key && chmod 600 ~/.proxyshop-apitcg-key '
            'then re-run nas-update.sh so the container picks it up. '
            '(Riftbound uses RiftScribe and does not need this key.)')
    return key


def _apitcg_not_found(exc: ProviderError) -> bool:
    """apitcg returns HTTP 500 + Spanish 'Datos no encontrados' for empty searches."""
    msg = str(exc).lower()
    return (
        'no encontrados' in msg
        or 'not found' in msg
        or 'no data' in msg
    )


def _apitcg_search(path: str, name: str, limit: int, *, tcg: str = '') -> Any:
    """Query an apitcg cards/products endpoint; treat empty-result 500s as [].

    Tries the game-specific cards route first, then the unified products route
    (``/api/products?tcg=…``), which some games populate more reliably.
    """
    key = _apitcg_key()
    names = [name]
    titled = name[:1].upper() + name[1:] if name else name
    if titled and titled not in names:
        names.append(titled)

    endpoints: list[tuple[str, dict]] = []
    for n in names:
        endpoints.append((f'{APITCG_API}/{path}', {'name': n}))
        endpoints.append((
            f'{APITCG_API}/{path}',
            {'name': n, 'limit': min(max(limit, 1), 50)}))
        if tcg:
            endpoints.append((
                f'{APITCG_API}/products',
                {'tcg': tcg, 'name': n, 'type': 'card'}))
            endpoints.append((
                f'{APITCG_API}/products',
                {'tcg': tcg, 'name': n, 'type': 'card',
                 'limit': min(max(limit, 1), 50)}))

    last_err: Optional[ProviderError] = None
    for url, params in endpoints:
        try:
            return _get(url, params=params, extra_headers={'x-api-key': key})
        except ProviderError as e:
            last_err = e
            if _apitcg_not_found(e):
                continue
            raise
    if last_err and _apitcg_not_found(last_err):
        return {'data': []}
    if last_err:
        raise last_err
    return {'data': []}


"""
* Union Arena (apitcg.com)
"""


def search_union_arena(name: str, limit: int = 20) -> list[dict]:
    """Search Union Arena cards by name via the apitcg.com aggregator.

    Requires a free API key from apitcg.com in env PROXYSHOP_APITCG_KEY.
    """
    data = _apitcg_search('union-arena/cards', name, limit, tcg='union-arena')
    cards = []
    for c in _card_rows(data):
        images = c.get('images') or {}
        set_info = c.get('set') or {}
        set_code = (set_info.get('name') if isinstance(set_info, dict) else str(set_info)) or ''
        raw_id = str(c.get('id') or c.get('code') or '')
        cards.append({
            'object': 'card',
            'game': 'union-arena',
            'id': f"ua-{_safe_id(raw_id)}",
            'name': c.get('name', ''),
            'set': set_code,
            'set_name': set_code,
            'collector_number': str(c.get('code', '')),
            'lang': 'en',
            'released_at': None,
            'images': images if isinstance(images, dict) else {},
            'provider_data': c,
        })
    return [c for c in cards if c['id'] not in ('ua-', 'ua-unknown') and c['name']]


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
    # Hydrate thin list rows so compose/editor get rules text + HQ art
    hydrated: list[dict] = []
    for card in cards[:limit]:
        detail_id = (card.get('provider_data') or {}).get('id') or (
            card.get('provider_data') or {}).get('card_id')
        if not detail_id:
            hydrated.append(card)
            continue
        try:
            detail = _get(f'{RIFTSCRIBE_API}/cards/{detail_id}', params={})
            if isinstance(detail, dict):
                merged = _normalize_riftbound_card({**(card.get('provider_data') or {}), **detail})
                hydrated.append(merged or card)
            else:
                hydrated.append(card)
        except ProviderError:
            hydrated.append(card)
    return hydrated


# Registry used by the server: game -> search callable
PROVIDERS: dict[str, Callable[[str, int], list[dict]]] = {
    'pokemon': search_pokemon,
    'union-arena': search_union_arena,
    'riftbound': search_riftbound,
}
