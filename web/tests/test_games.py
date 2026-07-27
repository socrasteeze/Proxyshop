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


# Legacy flat cardimages (no cardno) — exercises the filename fallback parser.
WS_LEGACY_FLAT_HTML = '''
<ul class="cardlist">
  <li>
    <img src="/cardlist/cardimages/CCS_WX01_001.png?v=2"
         alt="CCS/WX01-001 Sakura">
  </li>
  <li>
    <img src="/cardlist/cardimages/CCS_WX01_002.png"
         alt="CCS/WX01-002">
    <p class="card_name">Tomoyo</p>
  </li>
</ul>
'''

# Current EN searchresults markup: cardno + nested WordPress image paths.
WS_SEARCH_HTML = '''
<script>var cur_page = 1; var max_page = 1;</script>
<ul class="p-cards__results-list cardlist-Result_List">
  <li><a href="/cardlist/?cardno=CCS/WX01-001&amp;expansion_name=92&amp;view=image">
    <img src="/wordpress/wp-content/images/cardimages/c/ccs_wx01/CCS_WX01_001.png"
         alt="Sakura decoding="async"/></a></li>
  <li><a href="/cardlist/?cardno=CCS/WX01-002&amp;expansion_name=92&amp;view=image">
    <img src="/wordpress/wp-content/images/cardimages/c/ccs_wx01/CCS_WX01_002.png"
         alt="Tomoyo"/></a></li>
</ul>
'''

WS_SEARCH_HTML_PAGE2 = '''
<li class="ex-item"><a href="/cardlist/?cardno=CCS/WX01-003&amp;expansion_name=92&amp;view=image">
  <img src="/wordpress/wp-content/images/cardimages/c/ccs_wx01/CCS_WX01_003.png"
       alt="Touya"/></a></li>
'''

WS_TITLE_HTML = '''
<select name="title" id="title">
  <option value="">Select Title</option>
  <option value="100">Ignored Title Dropdown</option>
</select>
<select name="expansion_name" id="expansion">
  <option value="">Select Expansion</option>
  <option value="92">Cardcaptor Sakura : Clear Card</option>
  <option value="93">Fate/Grand Order</option>
</select>
<select name="rarity"><option value="RR">RR</option></select>
'''

WS_JP_FILTER_OPTIONS = {
    'expansions': [
        {'id': 113, 'name': '艦隊これくしょん -艦これ-', 'disp_flg': 1},
    ],
    'sides': [],
}

WS_JP_SEARCH_JSON = {
    'items': [{
        'id': 7350,
        'card_number': 'KC/S25-001',
        'card_name': '島風',
        'title_number': 'KC',
        'picture': 'k/kc_s25/kc_s25_001.png',
        'expansion': 113,
    }],
    'total': 1,
    'page': 1,
    'limit': 50,
    'page_count': 1,
}


def _ws_fake_fetch(url, params=None):
    """Stub EN cardlist HTML: root expansions, searchresults, cardsearch_ex."""
    params = params or {}
    if 'cardsearch_ex' in url:
        return WS_SEARCH_HTML_PAGE2 if int(params.get('page') or 1) >= 2 else ''
    if 'searchresults' in url:
        html = WS_SEARCH_HTML
        # Multi-page expansion when tests ask for expansion 92 with max_page>1
        if str(params.get('expansion_name') or '') == '92':
            html = html.replace('max_page = 1', 'max_page = 2')
        return html
    return WS_TITLE_HTML


def _ws_fake_jp_api(path, params=None):
    """Stub JP Cake CardListUser JSON."""
    params = params or {}
    if path.endswith('/filter-options'):
        return WS_JP_FILTER_OPTIONS
    if path.endswith('/searchJson'):
        return WS_JP_SEARCH_JSON
    raise games.ProviderError(f'unexpected JP API path {path}')


@pytest.fixture
def ws_offline(monkeypatch):
    """Stub every Weiß Schwarz network path (cardlist, Cake API, DeckLog)."""
    monkeypatch.setattr(games, '_ws_fetch_html', _ws_fake_fetch)
    monkeypatch.setattr(games, '_ws_jp_api', _ws_fake_jp_api)
    monkeypatch.setattr(games, '_ws_title_cache', None)
    monkeypatch.setattr(games, '_ws_decklog_index', lambda locale='en', force=False: {})
    monkeypatch.setattr(games, '_ws_url_exists', lambda url: False)


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

    def test_ws_image_url(self):
        assert games._ws_image_url('CCS/WX01-001') == (
            'https://en.ws-tcg.com/cardlist/cardimages/CCS_WX01_001.png')
        assert games._ws_image_url('KC/S25-001', locale='ja') == (
            'https://ws-tcg.com/cardlist/cardimages/KC_S25_001.png')

    def test_ws_code_filename_round_trip(self):
        for code in ('CCS/WX01-001', 'KC/S25-001', 'FGO/S75-E001'):
            assert games._ws_code_from_filename(
                games._ws_image_filename(code)) == code

    def test_parse_ws_cardlist_html(self):
        rows = games._parse_ws_cardlist_html(WS_SEARCH_HTML, locale='en')
        assert len(rows) == 2
        assert rows[0]['code'] == 'CCS/WX01-001'
        assert rows[0]['name'] == 'Sakura'
        assert '/wordpress/wp-content/images/cardimages/' in rows[0]['image']
        assert rows[0]['image'].endswith('CCS_WX01_001.png')
        assert rows[1]['name'] == 'Tomoyo'

    def test_parse_ws_cardlist_html_legacy_flat(self):
        """Old flat cardimages/FILE.png pages still parse via filename fallback."""
        rows = games._parse_ws_cardlist_html(WS_LEGACY_FLAT_HTML, locale='en')
        assert len(rows) == 2
        assert rows[0]['code'] == 'CCS/WX01-001'
        assert rows[0]['name'] == 'Sakura'
        assert rows[0]['image'].endswith('/cardlist/cardimages/CCS_WX01_001.png')
        assert rows[1]['name'] == 'Tomoyo'

    def test_weiss_schwarz_empty_query(self):
        assert games.search_weiss_schwarz('a') == []

    def test_weiss_schwarz_normalization(self, ws_offline):
        (card,) = games.search_weiss_schwarz('Sakura', limit=1)
        assert card['id'] == 'ws-CCS-WX01-001'
        assert card['game'] == 'weiss-schwarz'
        assert card['lang'] == 'en'
        assert card['name'] == 'Sakura'
        assert card['collector_number'] == 'WX01-001'
        assert card['set'] == 'CCS'
        assert card['images']['large'].endswith(
            '/wordpress/wp-content/images/cardimages/c/ccs_wx01/CCS_WX01_001.png')
        assert card['images']['small'] == card['images']['large']
        assert images.image_uri(card, 'large') == card['images']['large']

    def test_weiss_schwarz_includes_japanese(self, ws_offline):
        cards = games.search_weiss_schwarz('Sakura', limit=10)
        langs = {c['lang'] for c in cards}
        assert 'en' in langs
        assert 'ja' in langs
        ja = next(c for c in cards if c['lang'] == 'ja')
        assert ja['id'].startswith('ws-ja-')
        assert ja['name'] == '島風'
        assert ja['images']['large'] == (
            'https://ws-tcg.com/wordpress/wp-content/images/cardlist/'
            'k/kc_s25/kc_s25_001.png')

    def test_ws_locale_ids_never_collide(self):
        assert games._ws_card_id('CCS/WX01-001', 'en') != (
            games._ws_card_id('CCS/WX01-001', 'ja'))

    def test_ws_best_image_prefers_scraped_official(self, monkeypatch):
        """High-res official art wins even when DeckLog/Encore also have a URL."""
        monkeypatch.setattr(
            games, '_ws_decklog_index',
            lambda locale='en', force=False: {
                'CCS/WX01-001': 'https://decklog.example/CCS_WX01_001.png'})
        monkeypatch.setattr(
            games, '_ws_encoredecks_image',
            lambda code: 'https://www.encoredecks.com/images/EN/WX01/001.gif')
        scraped = (
            'https://en.ws-tcg.com/wordpress/wp-content/images/cardimages/'
            'c/ccs_wx01/CCS_WX01_001.png')
        assert games._ws_best_image('CCS/WX01-001', 'en', scraped) == scraped

    def test_ws_best_image_falls_back_to_encoredecks(self, monkeypatch):
        monkeypatch.setattr(games, '_ws_decklog_index', lambda locale='en', force=False: {})
        monkeypatch.setattr(games, '_ws_url_exists', lambda url: False)
        monkeypatch.setattr(
            games, '_ws_encoredecks_image',
            lambda code: 'https://www.encoredecks.com/images/EN/WX01/001.gif')
        assert games._ws_best_image('CCS/WX01-001', 'en') == (
            'https://www.encoredecks.com/images/EN/WX01/001.gif')

    def test_ws_decklog_failure_degrades(self, monkeypatch):
        """A dead DeckLog must not fail the card — fall through to official."""
        def _boom(url, params, extra_headers=None):
            raise games.ProviderError('decklog down')
        monkeypatch.setattr(games, '_get', _boom)
        monkeypatch.setattr(games, '_ws_decklog_cache', {})
        assert games._ws_decklog_index('en', force=True) == {}

    def test_list_weiss_schwarz_page(self, ws_offline):
        cards, total = games.list_weiss_schwarz_page(page=1)
        # 2 EN expansions + 1 JP Cake expansion; page 1 pulls max_page=2 fragments
        assert total == 3
        assert [c['collector_number'] for c in cards] == [
            'WX01-001', 'WX01-002', 'WX01-003']
        assert cards[0]['lang'] == 'en'
        assert cards[0]['set_name'] == 'Cardcaptor Sakura : Clear Card'
        jp_cards, total2 = games.list_weiss_schwarz_page(page=3)
        assert total2 == 3
        assert len(jp_cards) == 1
        assert jp_cards[0]['lang'] == 'ja'
        assert jp_cards[0]['id'].startswith('ws-ja-')
        empty, total3 = games.list_weiss_schwarz_page(page=4)
        assert empty == []
        assert total3 == 3

    def test_ws_titles_prefer_expansion_not_title(self):
        """Only expansion_name/expansion counts — title + rarity are ignored.

        Mixing title and expansion ID spaces made the catalog walk wrong filters
        and report success having stored nothing.
        """
        page = '''
        <select name="rarity"><option value="RR">RR</option>
          <option value="SR">SR</option><option value="C">C</option></select>
        <select name="title" id="title">
          <option value="100">Ignored Title</option>
        </select>
        <select name="expansion_name" id="expansion">
          <option value="">Select Expansion</option>
          <option value="92">Cardcaptor Sakura</option>
        </select>
        <select name="show_page_count"><option value="50">50</option>
          <option value="100">100</option></select>
        '''
        assert games._parse_ws_titles(page) == [('92', 'Cardcaptor Sakura')]

    def test_ws_titles_fall_back_when_no_named_select(self):
        """An unfamiliar layout degrades to the old behaviour, not to empty."""
        page = '<select><option value="WX01">Cardcaptor Sakura</option></select>'
        assert games._parse_ws_titles(page) == [('WX01', 'Cardcaptor Sakura')]

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

    def test_image_download_network_error_is_non_fatal(self, tmp_path):
        import requests

        class BadSession:
            def get(self, url, **kwargs):
                raise requests.exceptions.ConnectionError('dropped')

        card = {'game': 'pokemon', 'id': 'pkm-x',
                'images': {'large': 'https://img.example/x.png'}}
        # A network error mid-image returns None (counted as a failure), never raises
        assert images.ensure_image(BadSession(), card, 'png', tmp_path) is None
        # No stray .part file left behind
        assert list(tmp_path.glob('*.part')) == []


class TestProviderRetries:

    def test_request_retries_transient_then_succeeds(self, monkeypatch):
        import requests
        monkeypatch.setattr(games.time, 'sleep', lambda *_: None)
        monkeypatch.setattr(games, 'PROVIDER_MAX_RETRIES', 3)
        calls = {'n': 0}

        class Res:
            status_code = 200
            def json(self):
                return {'data': []}

        def flaky_get(url, **kwargs):
            calls['n'] += 1
            if calls['n'] < 3:
                raise requests.exceptions.ReadTimeout('read timed out')
            return Res()

        monkeypatch.setattr(games.requests, 'get', flaky_get)
        res = games._request('https://api.example/cards')
        assert res.status_code == 200
        assert calls['n'] == 3  # failed twice, succeeded on the third

    def test_request_gives_up_after_max_retries(self, monkeypatch):
        import requests
        monkeypatch.setattr(games.time, 'sleep', lambda *_: None)
        monkeypatch.setattr(games, 'PROVIDER_MAX_RETRIES', 2)

        def always_drop(url, **kwargs):
            raise requests.exceptions.ConnectionError('remote disconnected')

        monkeypatch.setattr(games.requests, 'get', always_drop)
        with pytest.raises(games.ProviderError, match='connection error'):
            games._request('https://api.example/cards')

    @staticmethod
    def _res(code, headers=None):
        class Res:
            status_code = code
            text = 'upstream boom'
            def __init__(self):
                self.headers = dict(headers or {})
            def json(self):
                return {'data': []}
        return Res()

    @pytest.mark.parametrize('code', [500, 502, 503, 504, 408, 429, 522])
    def test_transient_statuses_are_retried(self, monkeypatch, code):
        """pokemontcg.io 5xx blips must not fail the run on the first hit."""
        monkeypatch.setattr(games.time, 'sleep', lambda *_: None)
        monkeypatch.setattr(games, 'PROVIDER_MAX_RETRIES', 3)
        calls = {'n': 0}

        def flaky_get(url, **kwargs):
            calls['n'] += 1
            return self._res(code if calls['n'] < 3 else 200,
                             {'Retry-After': '0'})

        monkeypatch.setattr(games.requests, 'get', flaky_get)
        assert games._request('https://api.example/cards').status_code == 200
        assert calls['n'] == 3

    def test_server_error_raises_after_max_retries(self, monkeypatch):
        monkeypatch.setattr(games.time, 'sleep', lambda *_: None)
        monkeypatch.setattr(games, 'PROVIDER_MAX_RETRIES', 2)
        calls = {'n': 0}

        def always_500(url, **kwargs):
            calls['n'] += 1
            return self._res(503)

        monkeypatch.setattr(games.requests, 'get', always_500)
        with pytest.raises(games.ProviderError, match='Provider HTTP 503'):
            games._request('https://api.example/cards')
        assert calls['n'] == 3  # initial attempt + 2 retries

    @pytest.mark.parametrize('code', [400, 404, 422])
    def test_client_errors_are_not_retried(self, monkeypatch, code):
        """A bad request won't fix itself — fail fast instead of backing off."""
        monkeypatch.setattr(games.time, 'sleep', lambda *_: None)
        calls = {'n': 0}

        def bad(url, **kwargs):
            calls['n'] += 1
            return self._res(code)

        monkeypatch.setattr(games.requests, 'get', bad)
        with pytest.raises(games.ProviderError, match=f'Provider HTTP {code}'):
            games._request('https://api.example/cards')
        assert calls['n'] == 1

    @pytest.mark.parametrize('code', [401, 403])
    def test_auth_errors_are_not_retried(self, monkeypatch, code):
        monkeypatch.setattr(games.time, 'sleep', lambda *_: None)
        calls = {'n': 0}

        def denied(url, **kwargs):
            calls['n'] += 1
            return self._res(code)

        monkeypatch.setattr(games.requests, 'get', denied)
        with pytest.raises(games.ProviderError, match='check the API key'):
            games._request('https://api.example/cards')
        assert calls['n'] == 1

    def test_retry_after_is_capped(self, monkeypatch):
        """A huge (or bogus) Retry-After can't park the worker for minutes."""
        from web.shared.carddb import MAX_BACKOFF
        res = self._res(503, {'Retry-After': '3600'})
        assert games._retry_after_seconds(res, 0) == MAX_BACKOFF
        # HTTP-date and junk values fall back to exponential backoff
        for raw in ('Wed, 21 Oct 2015 07:28:00 GMT', '', 'soon'):
            delay = games._retry_after_seconds(self._res(503, {'Retry-After': raw}), 1)
            assert 0 < delay <= MAX_BACKOFF
        # Exponential backoff is capped too
        assert games._retry_after_seconds(self._res(503), 99) == MAX_BACKOFF

    def test_rate_limit_message_stays_friendly(self, monkeypatch):
        monkeypatch.setattr(games.time, 'sleep', lambda *_: None)
        monkeypatch.setattr(games, 'PROVIDER_MAX_RETRIES', 1)
        monkeypatch.setattr(
            games.requests, 'get',
            lambda url, **kw: self._res(429, {'Retry-After': '0'}))
        with pytest.raises(games.ProviderError, match='rate limit'):
            games._request('https://api.example/cards')
