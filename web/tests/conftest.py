"""
* Test Fixtures
* Everything runs offline — no live network calls anywhere in this suite.
"""
# Standard Library Imports
import json
import sys
from pathlib import Path

# Third Party Imports
import pytest

# Ensure the repo root is importable when running `pytest web/tests` directly
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web.shared.carddb import CardDB  # noqa: E402
from web.server.db import JobStore  # noqa: E402


def make_card(
    card_id: str,
    name: str,
    set_code: str = 'tst',
    number: str = '1',
    lang: str = 'en',
    released: str = '2020-01-01'
) -> dict:
    """Minimal Scryfall-shaped card object."""
    return {
        'object': 'card',
        'id': card_id,
        'oracle_id': f'oracle-{name.lower().replace(" ", "-")}',
        'name': name,
        'set': set_code,
        'collector_number': number,
        'lang': lang,
        'released_at': released,
        'layout': 'normal',
    }


@pytest.fixture()
def carddb(tmp_path) -> CardDB:
    """Offline card DB — any network attempt would return None, never call out."""
    return CardDB(tmp_path / 'cards.db', offline=True)


@pytest.fixture()
def jobstore(tmp_path) -> JobStore:
    return JobStore(tmp_path / 'jobs.db')


@pytest.fixture()
def bulk_file(tmp_path) -> Path:
    """A small bulk-data file in Scryfall's one-card-per-line array format."""
    cards = [
        make_card('aaaa-1', 'Lightning Bolt', 'lea', '161', released='1993-08-05'),
        make_card('aaaa-2', 'Lightning Bolt', 'sta', '42', released='2021-04-23'),
        make_card('bbbb-1', 'Sol Ring', 'c21', '125', released='2021-04-23'),
        make_card('cccc-1', 'Duress', 'usg', '132', released='1998-10-12'),
    ]
    path = tmp_path / 'bulk.json'
    lines = ',\n'.join(json.dumps(c) for c in cards)
    path.write_text(f'[\n{lines}\n]\n', encoding='utf-8')
    return path
