"""
* Multi-Game Provider & Card View Tests — all offline (providers stubbed).
"""
# Third Party Imports
import pytest

# Local Imports
from web.shared import games, images
from web.tests.conftest import make_card


@pytest.fixture(autouse=True)
def _fast_provider_limiter():
    """Disable spacing delays so unit tests stay instant."""
    prev = games._provider_limiter.min_interval
    games._provider_limiter.set_interval(0)
    try:
        yield
    finally:
        games._provider_limiter.set_interval(prev)


class TestNormalization:

    def test_pokemon_normalization(self, monkeypatch):
        payload = {'data': [{
            'id': 'xy7-54', 'name': 'Gardevoir', 'number': '54',
            'set': {'id': 'xy7', 'name': 'Ancient Origins', 'releaseDate': '2015/08/12'},
            'images': {'small': 'https://img.example/xy7-54.png',
                       'large': 'https://img.example/xy7-54_hires.png'}}]}
        monkeypatch.setattr(games, '_get', lambda url, params, extra_headers=None: payload)
        (card,) = games.search_pokemon('Gardevoir')
        assert card['id'] == 'pkm-xy7-54'
        assert card['game'] == 'pokemon'
        assert card['set'] == 'xy7'
        assert card['set_name'] == 'Ancient Origins'
        assert card['released_at'] == '2015-08-12'
        assert card['images']['large'].endswith('_hires.png')

    def test_union_arena_requires_key(self, monkeypatch, tmp_path):
        monkeypatch.delenv('PROXYSHOP_APITCG_KEY', raising=False)
        monkeypatch.setattr(games, '_APITCG_KEY_FILE', str(tmp_path / 'missing'))
        with pytest.raises(games.ProviderError, match='apitcg.com'):
            games.search_union_arena('Yuji Itadori')

    def test_apitcg_key_strips_whitespace(self, monkeypatch):
        monkeypatch.setenv('PROXYSHOP_APITCG_KEY', '  secret-key\n')
        assert games._apitcg_key() == 'secret-key'

    def test_apitcg_key_reads_file(self, monkeypatch, tmp_path):
        key_file = tmp_path / 'apitcg.key'
        key_file.write_text('file-key\n', encoding='utf-8')
        monkeypatch.delenv('PROXYSHOP_APITCG_KEY', raising=False)
        monkeypatch.setattr(games, '_APITCG_KEY_FILE', str(key_file))
        assert games._apitcg_key() == 'file-key'

    def test_union_arena_normalization(self, monkeypatch):
        monkeypatch.setenv('PROXYSHOP_APITCG_KEY', 'k')
        payload = {'data': [{
            'id': 'UA13BT-001', 'code': 'UA13BT-001', 'name': 'Yuji Itadori',
            'set': {'name': 'Jujutsu Kaisen'},
            'images': {'small': 'https://img.example/ua.webp',
                       'large': 'https://img.example/ua-large.webp'}}]}
        monkeypatch.setattr(games, '_get', lambda url, params, extra_headers=None: payload)
        (card,) = games.search_union_arena('Yuji')
        assert card['id'] == 'ua-UA13BT-001'
        assert card['game'] == 'union-arena'
        assert card['set'] == 'Jujutsu Kaisen'

    def test_riftbound_normalization(self, monkeypatch):
        payload = [{
            'id': 'ogs-001-024',
            'name': 'Annie, Fiery',
            'set_id': 'OGS',
            'collector_number': 1,
            'rarity': 'epic',
            'faction': 'fury',
            'type': 'Unit',
            'stats': {'energy': 5, 'might': 4, 'power': 1},
            'image': 'https://cdn.example/annie.png',
            'image_thumb': {'small': 'https://cdn.example/annie-sm.webp'},
            'description': 'Bonus Damage.',
        }]
        monkeypatch.setattr(games, '_get', lambda url, params, extra_headers=None: (
            payload if url.endswith('/cards') else payload[0]))
        (card,) = games.search_riftbound('Annie')
        assert card['id'] == 'rb-ogs-001-024'
        assert card['game'] == 'riftbound'
        assert card['set'] == 'ogs'
        assert card['set_name'] == 'OGS'
        assert card['collector_number'] == '1'
        assert card['images']['large'].endswith('annie.png')
        assert card['provider_data']['domain'] == 'Fury'
        assert card['provider_data']['energyCost'] == 5
        assert 'Annie' in card['name']

    def test_riftbound_no_key_required(self, monkeypatch):
        monkeypatch.delenv('PROXYSHOP_APITCG_KEY', raising=False)
        monkeypatch.setattr(games, '_get', lambda url, params, extra_headers=None: [])
        assert games.search_riftbound('Annie') == []

    def test_riftbound_empty_query(self):
        assert games.search_riftbound('a') == []

    def test_apitcg_200_error_payload(self, monkeypatch):
        class FakeRes:
            status_code = 200
            text = '{"error":"API key is required"}'
            headers = {}
            def json(self):
                return {'error': 'API key is required'}
        monkeypatch.setattr(games.requests, 'get', lambda *a, **k: FakeRes())
        with pytest.raises(games.ProviderError, match='API key is required'):
            games._get('https://www.apitcg.com/api/union-arena/cards', {})

    def test_provider_retries_429(self, monkeypatch):
        calls = {'n': 0}
        sleeps = []

        class FakeRes:
            def __init__(self, code, payload=None):
                self.status_code = code
                self.headers = {'Retry-After': '0'}
                self.text = ''
                self._payload = payload or {'data': []}

            def json(self):
                return self._payload

        def fake_get(*a, **k):
            calls['n'] += 1
            if calls['n'] == 1:
                return FakeRes(429)
            return FakeRes(200, {'data': [{'id': 'x', 'name': 'Y'}]})

        monkeypatch.setattr(games.requests, 'get', fake_get)
        monkeypatch.setattr(games.time, 'sleep', lambda s: sleeps.append(s))
        payload = games._get('https://example.test/cards', {})
        assert calls['n'] == 2
        assert payload['data'][0]['name'] == 'Y'
        assert sleeps  # backed off once


class TestMultiGameDb:

    def test_game_column_partitions_search(self, carddb):
        carddb.store_card(make_card('mtg-1', 'Charizard, Dragon'), game='mtg')
        pkm = {'object': 'card', 'id': 'pkm-1', 'game': 'pokemon', 'name': 'Charizard',
               'set': 'base1', 'collector_number': '4', 'lang': 'en',
               'released_at': '1999-01-09',
               'images': {'large': 'https://img.example/char.png'}}
        carddb.store_card(pkm, game='pokemon')
        assert [c['id'] for c in carddb.search_local('Charizard', game='pokemon')] == ['pkm-1']
        assert [c['id'] for c in carddb.search_local('Charizard', game='mtg')] == ['mtg-1']

    def test_migration_adds_game_column(self, tmp_path):
        import sqlite3
        from web.shared.carddb import CardDB
        # Simulate a pre-multigame database (no game column)
        db_path = tmp_path / 'old.db'
        con = sqlite3.connect(db_path)
        con.executescript("""
            CREATE TABLE cards (
                id TEXT PRIMARY KEY, oracle_id TEXT, name TEXT NOT NULL,
                set_code TEXT NOT NULL, collector_number TEXT NOT NULL,
                lang TEXT NOT NULL DEFAULT 'en', released_at TEXT,
                json BLOB NOT NULL,
                fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
                source TEXT NOT NULL DEFAULT 'api');
            INSERT INTO cards (id, name, set_code, collector_number, json)
            VALUES ('old-1', 'Opt', 'dom', '60', '{"id":"old-1","name":"Opt","game":"mtg"}');
        """)
        con.commit()
        con.close()
        db = CardDB(db_path, offline=True)
        # Old rows default to mtg and remain searchable
        assert [c['id'] for c in db.search_local('Opt', game='mtg')] == ['old-1']


class TestNonMtgImages:

    def test_image_uri_maps_png_to_large(self):
        card = {'game': 'pokemon', 'id': 'pkm-1',
                'images': {'large': 'https://img.example/big.png',
                           'small': 'https://img.example/small.png'}}
        assert images.image_uri(card, 'png') == 'https://img.example/big.png'
        assert images.image_uri(card, 'large') == 'https://img.example/big.png'
        assert images.image_uri(card, 'art_crop') is None

    def test_extension_derived_from_url(self, tmp_path):
        class Session:
            def get(self, url, **kwargs):
                class Res:
                    status_code = 200
                    def iter_content(self, chunk_size):
                        yield b'RIFFxxxxWEBP'
                return Res()
        card = {'game': 'union-arena', 'id': 'ua-1',
                'images': {'large': 'https://img.example/card.webp'}}
        path = images.ensure_image(Session(), card, 'png', tmp_path)
        assert path.suffix == '.webp'
        # Cached lookup finds it despite the non-default extension
        assert images.ensure_image(Session(), card, 'png', tmp_path, offline=True) == path
