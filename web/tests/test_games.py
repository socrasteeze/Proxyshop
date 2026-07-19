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


UA_SEARCH_HTML = '''
<ul class="cardlistCol">
  <li class="cardImgCol">
    <a class="modalCardDataOpen" data-type="iframe"
       href="./detail_iframe.php?card_no=UE01BT/BLC-1-001">
      <img class="lazy" src="/na/images/cardlist/parts/dummy.gif"
           data-src="/na/images/cardlist/card/UE01BT_BLC-1-001.png?v3"
           alt="UE01BT/BLC-1-001 Asguiaro Ebern">
    </a>
  </li>
  <li class="cardImgCol">
    <a class="modalCardDataOpen" data-type="iframe"
       href="./detail_iframe.php?card_no=UE02BT/HTR-1-005">
      <img class="lazy" src="/na/images/cardlist/parts/dummy.gif"
           data-src="/na/images/cardlist/card/UE02BT_HTR-1-005.png?v3"
           alt="UE02BT/HTR-1-005 Gon Freecss">
    </a>
  </li>
</ul>
'''

UA_JP_SEARCH_HTML = '''
<ul class="cardlistCol">
  <li class="cardImgCol">
    <a class="modalCardDataOpen" data-type="iframe"
       href="./detail_iframe.php?card_no=EX01BT/HTR-2-014">
      <img class="lazy" src="/jp/images/cardlist/parts/dummy.gif"
           data-src="/jp/images/cardlist/card/EX01BT_HTR-2-014.png?v8"
           alt="EX01BT/HTR-2-014 ゴン＝フリークス">
    </a>
  </li>
</ul>
'''

UA_SERIES_HTML = '''
<select name="series" id="series">
  <option value="">Select Product</option>
  <option value="591101">BLEACH: Thousand-Year Blood War [UE01BT]</option>
  <option value="591102">HUNTER X HUNTER [UE02BT]</option>
</select>
'''

UA_JP_SERIES_HTML = '''
<select name="series" id="series">
  <option value="">商品を選択</option>
  <option value="570201">HUNTERxHUNTER Vol.2 [EX01BT]</option>
</select>
'''


def _ua_fake_fetch(url, params=None):
    """Stub cardlist HTML: NA vs JP by URL path."""
    params = params or {}
    if '/jp/' in url:
        if 'series' in params or 'freewords' in params:
            return UA_JP_SEARCH_HTML
        return UA_JP_SERIES_HTML
    if 'series' in params or 'freewords' in params:
        return UA_SEARCH_HTML
    return UA_SERIES_HTML


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

    def test_ua_image_url(self):
        assert games._ua_image_url('UE01BT/BLC-1-001') == (
            'https://www.unionarena-tcg.com/na/images/cardlist/card/UE01BT_BLC-1-001.png')
        assert games._ua_image_url('EX01BT/HTR-2-014', locale='ja') == (
            'https://www.unionarena-tcg.com/jp/images/cardlist/card/EX01BT_HTR-2-014.png')

    def test_ua_name_from_parallel_alt(self):
        assert games._ua_name_from_alt(
            'UE02BT/HTR-1-006 Gon Freecss', 'UE02BT/HTR-1-006_p1') == 'Gon Freecss'

    def test_parse_ua_cardlist_html(self):
        rows = games._parse_ua_cardlist_html(UA_SEARCH_HTML, locale='en')
        assert len(rows) == 2
        assert rows[0]['card_no'] == 'UE01BT/BLC-1-001'
        assert rows[0]['name'] == 'Asguiaro Ebern'
        assert rows[0]['image'].endswith('/na/images/cardlist/card/UE01BT_BLC-1-001.png')
        assert rows[1]['name'] == 'Gon Freecss'

    def test_parse_ua_jp_cardlist_html(self):
        rows = games._parse_ua_cardlist_html(UA_JP_SEARCH_HTML, locale='ja')
        assert len(rows) == 1
        assert rows[0]['card_no'] == 'EX01BT/HTR-2-014'
        assert rows[0]['image'].endswith('/jp/images/cardlist/card/EX01BT_HTR-2-014.png')
        assert 'ゴン' in rows[0]['name']

    def test_union_arena_no_key_required(self, monkeypatch):
        monkeypatch.setattr(games, '_ua_fetch_html', _ua_fake_fetch)
        monkeypatch.setattr(games, '_ua_series_cache', None)
        cards = games.search_union_arena('Asguiaro')
        assert len(cards) >= 2
        assert cards[0]['game'] == 'union-arena'

    def test_union_arena_empty_query(self):
        assert games.search_union_arena('a') == []

    def test_union_arena_normalization(self, monkeypatch):
        monkeypatch.setattr(games, '_ua_fetch_html', _ua_fake_fetch)
        monkeypatch.setattr(games, '_ua_series_cache', None)
        (card,) = games.search_union_arena('Asguiaro', limit=1)
        assert card['id'] == 'ua-UE01BT-BLC-1-001'
        assert card['game'] == 'union-arena'
        assert card['lang'] == 'en'
        assert card['name'] == 'Asguiaro Ebern'
        assert card['collector_number'] == 'BLC-1-001'
        assert card['set'] == 'UE01BT'
        assert '/na/images/cardlist/card/UE01BT_BLC-1-001.png' in card['images']['large']
        assert card['images']['small'] == card['images']['large']

    def test_union_arena_includes_japanese(self, monkeypatch):
        monkeypatch.setattr(games, '_ua_fetch_html', _ua_fake_fetch)
        monkeypatch.setattr(games, '_ua_series_cache', None)
        cards = games.search_union_arena('Gon', limit=10)
        langs = {c['lang'] for c in cards}
        assert 'en' in langs
        assert 'ja' in langs
        ja = next(c for c in cards if c['lang'] == 'ja')
        assert ja['id'].startswith('ua-ja-')
        assert '/jp/images/' in ja['images']['large']

    def test_list_union_arena_page(self, monkeypatch):
        monkeypatch.setattr(games, '_ua_fetch_html', _ua_fake_fetch)
        monkeypatch.setattr(games, '_ua_series_cache', None)
        cards, total = games.list_union_arena_page(page=1)
        # 2 NA series + 1 JP series
        assert total == 3
        assert len(cards) == 2
        assert cards[0]['lang'] == 'en'
        assert cards[0]['set_name'] == 'BLEACH: Thousand-Year Blood War [UE01BT]'
        jp_cards, total2 = games.list_union_arena_page(page=3)
        assert total2 == 3
        assert len(jp_cards) == 1
        assert jp_cards[0]['lang'] == 'ja'
        assert jp_cards[0]['id'].startswith('ua-ja-')
        empty, total3 = games.list_union_arena_page(page=4)
        assert empty == []
        assert total3 == 3

    def test_riftbound_normalization(self, monkeypatch):
        payload = {
            'items': [{
                'riftbound_id': 'ogs-001-024',
                'name': 'Annie, Fiery',
                'collector_number': 1,
                'set': {'set_id': 'OGS', 'label': 'OGS'},
                'classification': {
                    'type': 'Unit', 'rarity': 'Epic', 'domain': ['Fury']},
                'attributes': {'energy': 5, 'might': 4, 'power': 1},
                'text': {'plain': 'Bonus Damage.', 'flavour': None},
                'media': {
                    'image_url': 'https://cdn.example/annie.png?q=80',
                    'artist': 'Test',
                },
                'metadata': {},
                'tags': [],
            }],
            'total': 1,
        }
        monkeypatch.setattr(games, '_get', lambda url, params, extra_headers=None: payload)
        monkeypatch.setattr(games, '_rb_enrich_search_hits', lambda cards, q, limit: cards[:limit])
        (card,) = games.search_riftbound('Annie')
        assert card['id'] == 'rb-ogs-001-024'
        assert card['game'] == 'riftbound'
        assert card['set'] == 'ogs'
        assert card['set_name'] == 'OGS'
        assert card['collector_number'] == '1'
        assert card['images']['large'] == 'https://cdn.example/annie.png'
        assert card['provider_data']['domain'] == 'Fury'
        assert card['provider_data']['energyCost'] == 5
        assert 'Annie' in card['name']

    def test_riftbound_arc_normalize(self):
        card = games._normalize_dotgg_arc_card({
            'id': 'ARC-001',
            'name': 'Vi - Destructive (Chinese Arcane Box Set Promo)',
            'set_name': 'Arcane Box Set',
            'type': 'Legend',
            'color': ['Fury'],
            'cost': '1',
            'might': '3',
            'image': 'https://static.dotgg.gg/riftbound/cards/ARC-001.webp',
            'rarity': 'Promo',
            'effect': 'Test',
        })
        assert card is not None
        assert card['id'] == 'rb-arc-ARC-001'
        assert card['set'] == 'arc'
        assert 'Chinese Arcane' in card['name']
        assert card['images']['large'].endswith('ARC-001.webp')

    def test_riftbound_no_key_required(self, monkeypatch):
        monkeypatch.setattr(
            games, '_get',
            lambda url, params, extra_headers=None: {'items': [], 'total': 0})
        monkeypatch.setattr(games, '_rb_enrich_search_hits', lambda cards, q, limit: cards)
        assert games.search_riftbound('Annie') == []

    def test_riftbound_localized_variant(self, monkeypatch):
        en = games._normalize_riftcodex_card({
            'riftbound_id': 'ogs-001-024',
            'name': 'Annie - Fiery',
            'collector_number': 1,
            'set': {'set_id': 'OGS', 'label': 'OGS'},
            'classification': {'type': 'Unit', 'rarity': 'Epic', 'domain': ['Fury']},
            'attributes': {'energy': 5, 'might': 4, 'power': 1},
            'text': {'plain': 'x', 'flavour': None},
            'media': {'image_url': 'https://cdn.example/annie.png'},
            'metadata': {},
            'tags': [],
        })
        monkeypatch.setattr(games, '_rb_locale_index', lambda locale, force=False: {
            'ogs-001-024': {
                'name': 'アニー - フィアリー',
                'image': 'https://cdn.example/annie.png',
                'public_code': 'OGS-001/024',
                'id': 'ogs-001-024',
            }
        })
        variant = games._rb_localized_variant(en, 'ja-jp', 'ja')
        assert variant is not None
        assert variant['id'] == 'rb-ja-ogs-001-024'
        assert variant['lang'] == 'ja'
        assert 'アニー' in variant['name']
        assert variant['images']['large'] == en['images']['large']

    def test_provider_200_error_payload(self, monkeypatch):
        class FakeRes:
            status_code = 200
            text = '{"error":"API key is required"}'
            headers = {}
            def json(self):
                return {'error': 'API key is required'}
        monkeypatch.setattr(games.requests, 'get', lambda *a, **k: FakeRes())
        with pytest.raises(games.ProviderError, match='API key is required'):
            games._get('https://example.test/api/cards', {})

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
