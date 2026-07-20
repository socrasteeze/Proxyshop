"""
* Card query parser + field/keyword search tests (offline).
"""
# Local Imports
from web.shared import cardquery
from web.tests.conftest import make_card


class TestParser:

    def test_plain_terms(self):
        p = cardquery.parse_query('lightning bolt', 'mtg')
        assert p.terms == ['lightning', 'bolt']
        assert p.fields == []

    def test_scryfall_fields(self):
        p = cardquery.parse_query('t:creature o:draw set:dom', 'mtg')
        assert ('type', 'creature') in p.fields
        assert ('oracle', 'draw') in p.fields
        assert ('set', 'dom') in p.fields
        assert p.terms == []

    def test_quoted_value(self):
        p = cardquery.parse_query('artist:"john avon"', 'mtg')
        assert ('artist', 'john avon') in p.fields

    def test_pokemon_supertype_field(self):
        p = cardquery.parse_query('supertype:trainer', 'pokemon')
        assert ('supertype', 'trainer') in p.fields

    def test_unknown_field_kept_as_text(self):
        # 'foo:' is not a known field → whole token becomes free text
        p = cardquery.parse_query('foo:bar', 'mtg')
        assert p.fields == []
        assert 'foo:bar' in p.terms

    def test_alias_resolution(self):
        p = cardquery.parse_query('type:instant text:flying', 'mtg')
        assert ('type', 'instant') in p.fields
        assert ('oracle', 'flying') in p.fields

    def test_build_where_empty(self):
        sql, params = cardquery.build_where(cardquery.parse_query('', 'mtg'), 'mtg')
        assert sql == '1=1'
        assert params == []


class TestTagOperators:

    def test_has_tag_op_positive(self):
        assert cardquery.has_tag_op('art:dragon')
        assert cardquery.has_tag_op('otag:removal')
        assert cardquery.has_tag_op('function:ramp')
        assert cardquery.has_tag_op('r:mythic art:dragon')  # among other tokens

    def test_has_tag_op_negation(self):
        assert cardquery.has_tag_op('-art:elf')

    def test_has_tag_op_negative(self):
        assert not cardquery.has_tag_op('lightning bolt')
        assert not cardquery.has_tag_op('t:creature o:draw')
        assert not cardquery.has_tag_op('artist:"john avon"')  # 'artist:' is not a tag op

    def test_normalize_tag(self):
        assert cardquery.normalize_tag('  Art:Dragon  ') == 'art:dragon'
        assert cardquery.normalize_tag('otag:Removal   is:Foil') == 'otag:removal is:foil'


class TestFieldSearch:
    """End-to-end against a real SQLite DB via CardDB.search_local."""

    def _seed_pokemon(self, carddb):
        prof = make_card('pkm-1', "Professor's Research", 'sv1', '189', lang='en')
        prof['game'] = 'pokemon'
        prof['provider_data'] = {
            'supertype': 'Trainer', 'subtypes': ['Supporter'], 'types': [],
            'rarity': 'Ultra Rare'}
        pikachu = make_card('pkm-2', 'Pikachu', 'sv1', '63', lang='en')
        pikachu['game'] = 'pokemon'
        pikachu['provider_data'] = {
            'supertype': 'Pokémon', 'subtypes': ['Basic'], 'types': ['Lightning'],
            'rarity': 'Common'}
        for c in (prof, pikachu):
            carddb.store_card(c, game='pokemon')

    def test_keyword_matches_subtype(self, carddb):
        self._seed_pokemon(carddb)
        # 'supporter' is a subtype, not in the name — must still match
        hits = carddb.search_local('supporter', game='pokemon')
        assert [c['id'] for c in hits] == ['pkm-1']

    def test_field_supertype(self, carddb):
        self._seed_pokemon(carddb)
        # 'pokemon' (no accent) must match the stored 'Pokémon' supertype
        hits = carddb.search_local('supertype:pokemon', game='pokemon')
        assert [c['id'] for c in hits] == ['pkm-2']

    def test_pokemon_type_field(self, carddb):
        self._seed_pokemon(carddb)
        hits = carddb.search_local('type:lightning', game='pokemon')
        assert [c['id'] for c in hits] == ['pkm-2']

    def _seed_mtg(self, carddb):
        bolt = make_card('m-1', 'Lightning Bolt', 'sta', '42')
        bolt['type_line'] = 'Instant'
        bolt['oracle_text'] = 'Lightning Bolt deals 3 damage to any target.'
        bolt['rarity'] = 'common'
        bear = make_card('m-2', 'Grizzly Bears', 'dom', '150')
        bear['type_line'] = 'Creature — Bear'
        bear['oracle_text'] = ''
        bear['rarity'] = 'common'
        for c in (bolt, bear):
            carddb.store_card(c, game='mtg')

    def test_mtg_type_field(self, carddb):
        self._seed_mtg(carddb)
        assert [c['id'] for c in carddb.search_local('t:creature', game='mtg')] == ['m-2']
        assert [c['id'] for c in carddb.search_local('t:instant', game='mtg')] == ['m-1']

    def test_mtg_oracle_field(self, carddb):
        self._seed_mtg(carddb)
        assert [c['id'] for c in carddb.search_local('o:damage', game='mtg')] == ['m-1']

    def test_plain_name_still_works(self, carddb):
        self._seed_mtg(carddb)
        assert [c['id'] for c in carddb.search_local('bolt', game='mtg')] == ['m-1']

    def test_combined_field_and_text(self, carddb):
        self._seed_mtg(carddb)
        # type:creature AND name contains 'bear'
        hits = carddb.search_local('t:creature bear', game='mtg')
        assert [c['id'] for c in hits] == ['m-2']

    def test_gallery_field_search(self, carddb):
        self._seed_pokemon(carddb)
        cards, total = carddb.list_gallery(game='pokemon', q='supertype:trainer')
        assert total == 1
        assert cards[0]['id'] == 'pkm-1'

    def _seed_riftbound(self, carddb):
        annie = make_card('rb-1', 'Annie, Fiery', 'ogs', '1')
        annie['game'] = 'riftbound'
        annie['provider_data'] = {
            'cardType': 'Unit', 'domain': 'Fury', 'rarity': 'Epic',
            'description': 'Deals bonus damage.', 'artist': 'Jane Doe',
            'flavorText': 'Burn it all.', 'riftbound_id': 'ogs-001'}
        for c in (annie,):
            carddb.store_card(c, game='riftbound')

    def test_riftbound_field_search(self, carddb):
        self._seed_riftbound(carddb)
        assert [c['id'] for c in carddb.search_local('t:unit', game='riftbound')] == ['rb-1']
        assert [c['id'] for c in carddb.search_local('domain:fury', game='riftbound')] == ['rb-1']
        assert [c['id'] for c in carddb.search_local('o:damage', game='riftbound')] == ['rb-1']
        assert [c['id'] for c in carddb.search_local('artist:jane', game='riftbound')] == ['rb-1']

    def test_union_arena_name_set_search(self, carddb):
        ua = make_card('ua-1', 'Gon Freecss', 'UE02BT', 'HTR-1-005')
        ua['game'] = 'union-arena'
        ua['set_name'] = 'Hunter x Hunter'
        ua['provider_data'] = {'card_no': 'UE02BT/HTR-1-005'}
        carddb.store_card(ua, game='union-arena')
        assert [c['id'] for c in carddb.search_local('gon', game='union-arena')] == ['ua-1']
        assert [c['id'] for c in carddb.search_local('set:hunter', game='union-arena')] == ['ua-1']

    def test_cross_game_keyword_search(self, carddb):
        self._seed_mtg(carddb)
        self._seed_pokemon(carddb)
        self._seed_riftbound(carddb)
        # A field query with no game selected uses the universal field set
        ids = {c['id'] for c in carddb.search_local('t:creature', game=None)}
        assert ids == {'m-2'}  # only the MTG creature
        # Keyword 'unit' matches the Riftbound cardType across games
        ids = {c['id'] for c in carddb.search_local('o:damage', game=None)}
        assert 'm-1' in ids and 'rb-1' in ids  # MTG oracle + Riftbound description

    def test_gallery_cross_game_field_search(self, carddb):
        self._seed_mtg(carddb)
        self._seed_riftbound(carddb)
        cards, total = carddb.list_gallery(q='t:unit')  # no game → cross-game
        assert total == 1 and cards[0]['id'] == 'rb-1'
