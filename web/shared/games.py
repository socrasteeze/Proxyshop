"""
* Multi-Game Card Providers
* Search providers for non-MTG trading card games, normalized into the same
* card shape the local database stores. MTG stays on the Scryfall path in
* carddb; these providers cover:
*   - pokemon      -> pokemontcg.io (free; optional API key raises limits)
*   - union-arena  -> apitcg.com   (community aggregator; free API key required)
*   - riftbound    -> apitcg.com   (same key as Union Arena)
* Must never import from `src/`.
"""
# Standard Library Imports
import os
from typing import Callable, Optional

# Third Party Imports
import requests

# Local Imports
from web.shared.carddb import HEADERS

POKEMON_API = 'https://api.pokemontcg.io/v2'
APITCG_API = 'https://apitcg.com/api'

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


def _get(url: str, params: dict, extra_headers: Optional[dict] = None) -> dict:
    headers = dict(HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    res = requests.get(url, params=params, headers=headers, timeout=30)
    if res.status_code in (401, 403):
        raise ProviderError('Provider rejected the request (check the API key).')
    if res.status_code == 429:
        raise ProviderError('Provider rate limit hit — try again in a minute.')
    res.raise_for_status()
    return res.json()


"""
* Pokemon (pokemontcg.io)
"""


def search_pokemon(name: str, limit: int = 20) -> list[dict]:
    """Search Pokémon cards by name. Hi-res scans come from images.large.

    Optional env PROXYSHOP_POKEMONTCG_KEY raises the free-tier rate limits.
    """
    headers = {}
    key = os.environ.get('PROXYSHOP_POKEMONTCG_KEY')
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
    for c in data.get('data', []):
        card_set = c.get('set') or {}
        cards.append({
            'object': 'card',
            'game': 'pokemon',
            'id': f"pkm-{c.get('id', '')}",
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
    """Return PROXYSHOP_APITCG_KEY or raise ProviderError."""
    key = os.environ.get('PROXYSHOP_APITCG_KEY')
    if not key:
        raise ProviderError(
            'Riftbound and Union Arena need a free apitcg.com API key — register at '
            'https://apitcg.com and set PROXYSHOP_APITCG_KEY on the server.')
    return key


"""
* Union Arena (apitcg.com)
"""


def search_union_arena(name: str, limit: int = 20) -> list[dict]:
    """Search Union Arena cards by name via the apitcg.com aggregator.

    Requires a free API key from apitcg.com in env PROXYSHOP_APITCG_KEY.
    """
    data = _get(
        f'{APITCG_API}/union-arena/cards',
        params={'name': name, 'limit': min(limit, 50)},
        extra_headers={'x-api-key': _apitcg_key()})
    cards = []
    for c in data.get('data', []):
        images = c.get('images') or {}
        set_info = c.get('set') or {}
        set_code = (set_info.get('name') if isinstance(set_info, dict) else str(set_info)) or ''
        cards.append({
            'object': 'card',
            'game': 'union-arena',
            'id': f"ua-{c.get('id') or c.get('code', '')}",
            'name': c.get('name', ''),
            'set': set_code,
            'set_name': set_code,
            'collector_number': str(c.get('code', '')),
            'lang': 'en',
            'released_at': None,
            'images': images,
            'provider_data': c,
        })
    return [c for c in cards if c['id'] not in ('pkm-', 'ua-') and c['name']]


"""
* Riftbound (apitcg.com)
"""


def search_riftbound(name: str, limit: int = 20) -> list[dict]:
    """Search Riftbound cards by name via the apitcg.com aggregator.

    Requires a free API key from apitcg.com in env PROXYSHOP_APITCG_KEY
    (same key as Union Arena).
    """
    data = _get(
        f'{APITCG_API}/riftbound/cards',
        params={'name': name, 'limit': min(limit, 50)},
        extra_headers={'x-api-key': _apitcg_key()})
    cards = []
    for c in data.get('data', []):
        images = c.get('images') or {}
        set_info = c.get('set') or {}
        set_id = ''
        set_name = ''
        released = None
        if isinstance(set_info, dict):
            set_id = str(set_info.get('id') or '')
            set_name = str(set_info.get('name') or set_id)
            released = set_info.get('releaseDate')
        else:
            set_name = str(set_info or '')
        code = str(c.get('code') or c.get('number') or c.get('id') or '')
        cards.append({
            'object': 'card',
            'game': 'riftbound',
            'id': f"rb-{c.get('id') or code}",
            'name': c.get('name', ''),
            'set': set_id or set_name,
            'set_name': set_name or set_id,
            'collector_number': code,
            'lang': 'en',
            'released_at': released,
            'images': images,
            'provider_data': c,
        })
    return [c for c in cards if c['id'] not in ('rb-',) and c['name']]


# Registry used by the server: game -> search callable
PROVIDERS: dict[str, Callable[[str, int], list[dict]]] = {
    'pokemon': search_pokemon,
    'union-arena': search_union_arena,
    'riftbound': search_riftbound,
}
