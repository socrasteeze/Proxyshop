"""
* Selective cache filter helpers (MTG / Pokémon / small TCGs)
* Builds provider queries from structured filters so catalog caches don't
* have to dump an entire game. Must never import from `src/`.
"""
# Standard Library Imports
import json
import re
from typing import Any, Optional

# Games that must not run an unfiltered "cache everything" pass
SELECTIVE_GAMES = frozenset({'mtg', 'pokemon'})

# Games that support cache-game (selective or full small catalogs)
CACHEABLE_GAMES = ('mtg', 'pokemon', 'riftbound', 'union-arena')

# Scryfall `is:` art / printing flags exposed in the UI
MTG_ART_FLAGS = (
    'showcase',
    'borderless',
    'extended',
    'fullart',
    'textless',
    'retro',
    'universal',
    'boosterfun',
)

MTG_RARITIES = ('common', 'uncommon', 'rare', 'mythic', 'special', 'bonus')

POKEMON_TYPES = (
    'Colorless', 'Darkness', 'Dragon', 'Fairy', 'Fighting', 'Fire',
    'Grass', 'Lightning', 'Metal', 'Psychic', 'Water',
)

POKEMON_SUPERTYPES = ('Pokémon', 'Trainer', 'Energy')


def _clean(value: Any) -> str:
    return str(value or '').strip()


def _split_csv(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = re.split(r'[,|]+', str(value))
    return [s.strip() for s in items if s and str(s).strip()]


def normalize_filters(game: str, raw: Optional[dict] = None) -> dict:
    """Return a stable, JSON-friendly filter dict for checkpoints / API."""
    raw = raw or {}
    game = (game or '').strip().lower()
    out: dict[str, Any] = {}

    if game == 'mtg':
        if _clean(raw.get('set')):
            out['set'] = _clean(raw.get('set')).lower()
        if _clean(raw.get('type')):
            out['type'] = _clean(raw.get('type')).lower()
        if _clean(raw.get('rarity')):
            out['rarity'] = _clean(raw.get('rarity')).lower()
        arts = [a.lower() for a in _split_csv(raw.get('art') or raw.get('arts'))]
        arts = [a for a in arts if a in MTG_ART_FLAGS]
        if arts:
            out['art'] = arts
        if _clean(raw.get('artist')):
            out['artist'] = _clean(raw.get('artist'))
        year = _clean(raw.get('year'))
        if year.isdigit() and len(year) == 4:
            out['year'] = year
        if _clean(raw.get('tags')):
            # Free-form Scryfall fragments (otag:, atag:, is:, etc.)
            out['tags'] = _clean(raw.get('tags'))
        if _clean(raw.get('q')):
            out['q'] = _clean(raw.get('q'))
        return out

    if game == 'pokemon':
        if _clean(raw.get('set') or raw.get('set_id')):
            out['set'] = _clean(raw.get('set') or raw.get('set_id')).lower()
        types = _split_csv(raw.get('types') or raw.get('type'))
        if types:
            out['types'] = types
        subtypes = _split_csv(raw.get('subtypes') or raw.get('subtype'))
        if subtypes:
            out['subtypes'] = subtypes
        if _clean(raw.get('rarity')):
            out['rarity'] = _clean(raw.get('rarity'))
        if _clean(raw.get('supertype')):
            out['supertype'] = _clean(raw.get('supertype'))
        mark = _clean(raw.get('regulation') or raw.get('regulation_mark'))
        if mark:
            out['regulation'] = mark.upper()[:1] if len(mark) == 1 else mark
        if _clean(raw.get('name')):
            out['name'] = _clean(raw.get('name'))
        if _clean(raw.get('q')):
            out['q'] = _clean(raw.get('q'))
        return out

    # Small full-catalog games ignore structured filters
    if _clean(raw.get('q')):
        out['q'] = _clean(raw.get('q'))
    return out


def filters_require_selection(game: str, filters: dict) -> bool:
    """True when this game needs at least one selective filter."""
    return game in SELECTIVE_GAMES and not filters


def build_scryfall_query(filters: dict) -> str:
    """Assemble a Scryfall `q` string from structured filters."""
    parts: list[str] = []
    if filters.get('set'):
        parts.append(f"set:{filters['set']}")
    if filters.get('type'):
        parts.append(f"t:{filters['type']}")
    if filters.get('rarity'):
        parts.append(f"r:{filters['rarity']}")
    for flag in filters.get('art') or []:
        parts.append(f'is:{flag}')
    if filters.get('artist'):
        artist = filters['artist'].replace('"', '')
        parts.append(f'a:"{artist}"')
    if filters.get('year'):
        parts.append(f"year:{filters['year']}")
    if filters.get('tags'):
        parts.append(filters['tags'])
    if filters.get('q'):
        parts.append(filters['q'])
    # Unique printings by default keeps dumps smaller / more useful for proxies
    if 'unique:' not in ' '.join(parts).lower():
        parts.append('unique:prints')
    query = ' '.join(parts).strip()
    if not query or query == 'unique:prints':
        raise ValueError(
            'MTG cache needs at least one filter (set, type, rarity, art, '
            'artist, year, tags, or custom q). For a full dump use '
            '`manage bulk-download` instead.')
    return query


def _poke_quote(value: str) -> str:
    value = str(value)
    if any(ch.isspace() or ord(ch) > 127 for ch in value):
        return f'"{value}"'
    return value


def build_pokemon_query(filters: dict) -> str:
    """Assemble a pokemontcg.io Lucene `q` string from structured filters."""
    parts: list[str] = []
    if filters.get('name'):
        name = filters['name'].replace('"', '')
        parts.append(f'name:"*{name}*"')
    if filters.get('set'):
        parts.append(f"set.id:{filters['set']}")
    for t in filters.get('types') or []:
        parts.append(f'types:{_poke_quote(t)}')
    for st in filters.get('subtypes') or []:
        parts.append(f'subtypes:{_poke_quote(st)}')
    if filters.get('rarity'):
        parts.append(f"rarity:{_poke_quote(filters['rarity'])}")
    if filters.get('supertype'):
        parts.append(f"supertype:{_poke_quote(filters['supertype'])}")
    if filters.get('regulation'):
        parts.append(f"regulationMark:{filters['regulation']}")
    if filters.get('q'):
        parts.append(filters['q'])
    query = ' '.join(parts).strip()
    if not query:
        raise ValueError(
            'Pokémon cache needs at least one filter (set, type, subtype, '
            'rarity, regulation, name, or custom q).')
    return query


def build_provider_query(game: str, filters: dict) -> str:
    game = (game or '').strip().lower()
    if game == 'mtg':
        return build_scryfall_query(filters)
    if game == 'pokemon':
        return build_pokemon_query(filters)
    return filters.get('q') or ''


def filters_equal(a: Optional[dict], b: Optional[dict]) -> bool:
    return json.dumps(a or {}, sort_keys=True) == json.dumps(b or {}, sort_keys=True)


def describe_filters(game: str, filters: dict, query: str = '') -> str:
    if query:
        return query
    try:
        return build_provider_query(game, filters) or '(all)'
    except ValueError:
        return '(none)'


def _cap(value: Any) -> str:
    s = str(value or '')
    return s[:1].upper() + s[1:] if s else s


def friendly_filters(game: str, filters: Optional[dict], query: str = '') -> str:
    """Human-readable summary of a cache run's scope, for chips / status text.

    e.g. {'supertype': 'Trainer'} → 'Trainers', {'set': 'slp'} → 'Set SLP',
    {} → 'Full catalog'. Falls back to the raw query only if nothing else fits.
    """
    game = (game or '').strip().lower()
    f = filters or {}
    if not f:
        return 'Full catalog' if not query else query
    parts: list[str] = []

    if game == 'mtg':
        if f.get('set'):
            parts.append(f"Set {str(f['set']).upper()}")
        if f.get('type'):
            parts.append(f"{_cap(f['type'])}s")
        if f.get('rarity'):
            parts.append(_cap(f['rarity']))
        for flag in f.get('art') or []:
            parts.append(_cap(flag))
        if f.get('artist'):
            parts.append(f"by {f['artist']}")
        if f.get('year'):
            parts.append(str(f['year']))
        if f.get('tags'):
            parts.append(str(f['tags']))
        if f.get('q'):
            parts.append(str(f['q']))
    elif game == 'pokemon':
        # Trainer / Energy / Pokémon read fine as-is (no naive pluralization)
        if f.get('supertype'):
            parts.append(_cap(f['supertype']))
        for st in f.get('subtypes') or []:
            parts.append(_cap(st))
        for t in f.get('types') or []:
            parts.append(_cap(t))
        if f.get('set'):
            parts.append(f"Set {str(f['set']).upper()}")
        if f.get('rarity'):
            parts.append(_cap(f['rarity']))
        if f.get('regulation'):
            parts.append(f"Reg {f['regulation']}")
        if f.get('name'):
            parts.append(f'"{f["name"]}"')
        if f.get('q'):
            parts.append(str(f['q']))
    else:
        if f.get('q'):
            parts.append(str(f['q']))

    return ', '.join(parts) if parts else (query or 'Full catalog')
