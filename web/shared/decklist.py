"""
* Decklist Parsing & Deck-Site Fetchers
* Parses pasted decklist text (plain / MTGA / MTGO styles) and fetches public
* decks from Moxfield and Archidekt.
* Must never import from `src/`. Stdlib + requests only.
"""
# Standard Library Imports
import re
from typing import Optional
from urllib.parse import urlparse

# Third Party Imports
import requests

# Local Imports
from web.shared.schema import DeckCardLine

"""
* Text Parsing
"""

# "4 Lightning Bolt", "4x Lightning Bolt", "Lightning Bolt"
# with optional MTGA-style suffix: "(STA) 42" and/or foil/etc flags ignored
_LINE = re.compile(
    r'^\s*(?:(?P<qty>\d+)\s*[xX]?\s+)?'
    r'(?P<name>[^([]+?)'
    r'(?:\s*\((?P<set>[A-Za-z0-9]{2,6})\)\s*(?P<num>[\w-]+)?)?'
    r'\s*$'
)

# Section headers seen in MTGA/MTGO/site exports
_BOARD_HEADERS = {
    'deck': 'main',
    'mainboard': 'main',
    'main': 'main',
    'commander': 'commander',
    'commanders': 'commander',
    'companion': 'companion',
    'sideboard': 'side',
    'side': 'side',
    'maybeboard': 'maybe',
    'considering': 'maybe',
    'tokens': 'tokens',
}


def parse_decklist_text(text: str) -> list[DeckCardLine]:
    """Parse pasted decklist text into card lines.

    Tolerates plain lists ("4 Lightning Bolt"), MTGA exports
    ("4 Lightning Bolt (STA) 42" with Deck/Sideboard headers), MTGO-style
    blank-line sideboard separation, "SB:" prefixes, and comment lines.
    """
    lines: list[DeckCardLine] = []
    board = 'main'
    saw_cards = False
    for raw in text.splitlines():
        line = raw.strip()

        # Blank line after cards = MTGO-style sideboard break
        if not line:
            if saw_cards:
                board = 'side'
            continue

        # Comments
        if line.startswith(('#', '//')):
            continue

        # "SB: 2 Duress" prefix (MTGO .dek text style)
        current_board = board
        if line.upper().startswith('SB:'):
            current_board = 'side'
            line = line[3:].strip()

        # Section headers ("Sideboard", "Deck", "Commander", possibly "Sideboard (15)")
        header = re.sub(r'\s*\(\d+\)\s*$', '', line).strip().lower()
        if header in _BOARD_HEADERS:
            board = _BOARD_HEADERS[header]
            continue

        m = _LINE.match(line)
        if not m or not m.group('name'):
            continue
        name = m.group('name').strip()
        if not name:
            continue
        lines.append(DeckCardLine(
            qty=int(m.group('qty') or 1),
            name=name,
            set_code=(m.group('set') or None),
            collector_number=(m.group('num') or None),
            board=current_board))
        saw_cards = True
    return lines


"""
* Deck-Site Fetchers
"""

# Shared headers — identify ourselves politely to third-party APIs too.
_HEADERS = {
    'User-Agent': 'ProxyshopWeb/1.0 (+https://github.com/socrasteeze/Proxyshop)',
    'Accept': 'application/json'
}


def _get_json(url: str) -> dict:
    res = requests.get(url, headers=_HEADERS, timeout=30)
    res.raise_for_status()
    return res.json()


def fetch_moxfield(deck_id: str) -> tuple[str, list[DeckCardLine]]:
    """Fetch a public Moxfield deck by its public id.

    Uses the unofficial-but-stable public JSON endpoint.
    """
    data = _get_json(f'https://api.moxfield.com/v2/decks/all/{deck_id}')
    name = data.get('name') or f'Moxfield {deck_id}'
    lines: list[DeckCardLine] = []
    board_map = {
        'commanders': 'commander',
        'companions': 'companion',
        'mainboard': 'main',
        'sideboard': 'side',
    }
    for key, board in board_map.items():
        for entry in (data.get(key) or {}).values():
            card = entry.get('card') or {}
            lines.append(DeckCardLine(
                qty=int(entry.get('quantity', 1)),
                name=card.get('name', ''),
                set_code=card.get('set'),
                collector_number=card.get('cn'),
                board=board))
    return name, [ln for ln in lines if ln.name]


def fetch_archidekt(deck_id: str) -> tuple[str, list[DeckCardLine]]:
    """Fetch a public Archidekt deck by numeric id."""
    data = _get_json(f'https://archidekt.com/api/decks/{deck_id}/')
    name = data.get('name') or f'Archidekt {deck_id}'
    lines: list[DeckCardLine] = []
    for entry in data.get('cards', []):
        card = entry.get('card') or {}
        oracle = card.get('oracleCard') or {}
        edition = card.get('edition') or {}
        categories = [c.lower() for c in (entry.get('categories') or [])]
        board = 'main'
        if 'commander' in categories:
            board = 'commander'
        elif 'sideboard' in categories:
            board = 'side'
        elif 'maybeboard' in categories:
            board = 'maybe'
        lines.append(DeckCardLine(
            qty=int(entry.get('quantity', 1)),
            name=oracle.get('name', ''),
            set_code=edition.get('editioncode'),
            collector_number=card.get('collectorNumber'),
            board=board))
    return name, [ln for ln in lines if ln.name]


def fetch_deck_url(url: str) -> tuple[str, list[DeckCardLine]]:
    """Dispatch a deck URL to the right site fetcher.

    Supported: moxfield.com/decks/<id>, archidekt.com/decks/<id>[/name].

    Raises:
        ValueError: If the URL isn't from a supported site.
    """
    parsed = urlparse(url if '//' in url else f'https://{url}')
    host = (parsed.hostname or '').lower().removeprefix('www.')
    parts = [p for p in parsed.path.split('/') if p]

    if host.endswith('moxfield.com'):
        if len(parts) >= 2 and parts[0] == 'decks':
            return fetch_moxfield(parts[1])
        raise ValueError('Unrecognized Moxfield URL — expected moxfield.com/decks/<id>')

    if host.endswith('archidekt.com'):
        if len(parts) >= 2 and parts[0] == 'decks':
            deck_id = parts[1].split('#')[0]
            return fetch_archidekt(deck_id)
        raise ValueError('Unrecognized Archidekt URL — expected archidekt.com/decks/<id>')

    raise ValueError(f'Unsupported deck site: {host or url!r} (supported: Moxfield, Archidekt)')
