"""
* Multi-Game Provider & Card View Tests — all offline (providers stubbed).
"""
# Third Party Imports
import pytest

# Local Imports
from web.shared import games, images
from web.tests.conftest import make_card


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

    def test_union_arena_requires_key(self, monkeypatch):
        monkeypatch.delenv('PROXYSHOP_APITCG_KEY', raising=False)
        with pytest.raises(games.ProviderError, match='apitcg.com'):
            games.search_union_arena('Yuji Itadori')

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
