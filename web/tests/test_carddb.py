"""
* Card DB Tests — cache hits/misses, bulk import, batch resolution, decks.
"""
# Standard Library Imports
import json

# Local Imports
from web.shared.carddb import CardDB, CollectionResult
from web.tests.conftest import make_card


class TestStoreAndLookup:

    def test_store_and_get_by_set_number(self, carddb):
        carddb.store_card(make_card('id-1', 'Damnation', 'tsr', '106'))
        card = carddb.get_card('TSR', '106')
        assert card and card['name'] == 'Damnation'

    def test_get_miss_offline_returns_none(self, carddb):
        assert carddb.get_card('xxx', '999') is None

    def test_find_by_name_newest_first(self, carddb):
        carddb.store_card(make_card('id-old', 'Lightning Bolt', 'lea', '161', released='1993-08-05'))
        carddb.store_card(make_card('id-new', 'Lightning Bolt', 'sta', '42', released='2021-04-23'))
        card = carddb.find_card('Lightning Bolt')
        assert card['id'] == 'id-new'

    def test_find_by_name_and_set(self, carddb):
        carddb.store_card(make_card('id-old', 'Lightning Bolt', 'lea', '161'))
        carddb.store_card(make_card('id-new', 'Lightning Bolt', 'sta', '42'))
        card = carddb.find_card('Lightning Bolt', set_code='LEA')
        assert card['id'] == 'id-old'

    def test_find_name_case_insensitive(self, carddb):
        carddb.store_card(make_card('id-1', 'Sol Ring', 'c21', '125'))
        assert carddb.find_card('sol ring') is not None

    def test_upsert_replaces(self, carddb):
        carddb.store_card(make_card('id-1', 'Duress', 'usg', '132'))
        updated = make_card('id-1', 'Duress', 'usg', '132')
        updated['released_at'] = '1999-01-01'
        carddb.store_card(updated)
        card = carddb.get_card('usg', '132')
        assert card['released_at'] == '1999-01-01'

    def test_search_local(self, carddb):
        carddb.store_card(make_card('id-1', 'Lightning Bolt', 'sta', '42'))
        carddb.store_card(make_card('id-2', 'Lightning Helix', 'rav', '213'))
        carddb.store_card(make_card('id-3', 'Sol Ring', 'c21', '125'))
        names = {c['name'] for c in carddb.search_local('Lightning')}
        assert names == {'Lightning Bolt', 'Lightning Helix'}

    def test_search_local_ranks_exact_and_prefix_first(self, carddb):
        carddb.store_card(make_card('id-1', 'Greater Bolt', 'aaa', '1'))
        carddb.store_card(make_card('id-2', 'Bolt', 'bbb', '2'))
        carddb.store_card(make_card('id-3', 'Bolt of Ruin', 'ccc', '3'))
        names = [c['name'] for c in carddb.search_local('Bolt')]
        assert names == ['Bolt', 'Bolt of Ruin', 'Greater Bolt']

    def test_search_local_substring_midword(self, carddb):
        # FTS prefix tokens can't find mid-word fragments; LIKE fallback must
        carddb.store_card(make_card('id-1', 'Lightning Bolt', 'sta', '42'))
        assert [c['id'] for c in carddb.search_local('ightn')] == ['id-1']

    def test_search_local_all_games(self, carddb):
        carddb.store_card(make_card('mtg-1', 'Charizard Dragon', 'xyz', '1'))
        pkm = make_card('pkm-1', 'Charizard', 'base1', '4')
        pkm['game'] = 'pokemon'
        carddb.store_card(pkm, game='pokemon')
        ids = {c['id'] for c in carddb.search_local('charizard', game=None)}
        assert ids == {'mtg-1', 'pkm-1'}
        # Scoped search still isolates games
        assert [c['id'] for c in carddb.search_local('charizard', game='pokemon')] == ['pkm-1']

    def test_search_local_respects_limit(self, carddb):
        for i in range(10):
            carddb.store_card(make_card(f'id-{i}', f'Bolt Variant {i}', 'sta', str(i)))
        assert len(carddb.search_local('Bolt', limit=5)) == 5

    def test_list_gallery_filters_and_pages(self, carddb):
        carddb.store_card(make_card('id-1', 'Lightning Bolt', 'sta', '42'))
        carddb.store_card(make_card('id-2', 'Sol Ring', 'c21', '125'))
        pkm = make_card('pkm-1', 'Pikachu', 'base1', '58')
        pkm['game'] = 'pokemon'
        carddb.store_card(pkm, game='pokemon')
        cards, total = carddb.list_gallery(game='mtg', limit=10)
        assert total == 2
        assert all(c.get('game', 'mtg') == 'mtg' for c in cards)
        cards, total = carddb.list_gallery(q='pika')
        assert total == 1
        assert cards[0]['id'] == 'pkm-1'
        assert carddb.counts_by_game()['pokemon'] == 1
        page, total = carddb.list_gallery(limit=1, offset=0, sort='name')
        assert total == 3
        assert len(page) == 1

    def test_list_gallery_returns_light_projection(self, carddb):
        carddb.store_card(make_card('id-1', 'Sol Ring', 'c21', '125'))
        cards, total = carddb.list_gallery()
        assert total == 1
        c = cards[0]
        assert c['id'] == 'id-1'
        assert c['name'] == 'Sol Ring'
        assert c['set'] == 'c21'
        assert c['collector_number'] == '125'
        assert c['game'] == 'mtg'
        assert c['art_count'] == 1

    def test_list_gallery_combine_arts_by_oracle(self, carddb):
        # Same oracle_id → one group when combining; newest release wins
        a = make_card('id-old', 'Lightning Bolt', 'lea', '161', released='1993-08-05')
        b = make_card('id-new', 'Lightning Bolt', 'sta', '42', released='2021-04-23')
        carddb.store_card(a)
        carddb.store_card(b)
        unique, u_total = carddb.list_gallery(group_arts=False)
        assert u_total == 2
        combined, c_total = carddb.list_gallery(group_arts=True)
        assert c_total == 1
        assert len(combined) == 1
        assert combined[0]['id'] == 'id-new'
        assert combined[0]['art_count'] == 2

    def test_list_gallery_combine_non_mtg_by_name(self, carddb):
        a = make_card('pkm-1', 'Pikachu', 'base1', '58', released='1999-01-01')
        a['game'] = 'pokemon'
        a['oracle_id'] = None
        b = make_card('pkm-2', 'Pikachu', 'xy1', '42', released='2014-02-05')
        b['game'] = 'pokemon'
        b['oracle_id'] = None
        carddb.store_card(a, game='pokemon')
        carddb.store_card(b, game='pokemon')
        cards, total = carddb.list_gallery(game='pokemon', group_arts=True)
        assert total == 1
        assert cards[0]['id'] == 'pkm-2'
        assert cards[0]['art_count'] == 2

    def test_list_gallery_combine_paginates_groups(self, carddb):
        for i in range(5):
            carddb.store_card(make_card(
                f'a-{i}', f'Card {i}', 'aaa', str(i), released=f'2020-01-0{i+1}'))
            carddb.store_card(make_card(
                f'b-{i}', f'Card {i}', 'bbb', str(i), released=f'2021-01-0{i+1}'))
        page, total = carddb.list_gallery(group_arts=True, limit=2, offset=0, sort='name')
        assert total == 5
        assert len(page) == 2
        page2, _ = carddb.list_gallery(group_arts=True, limit=2, offset=2, sort='name')
        assert len(page2) == 2
        assert {c['id'] for c in page}.isdisjoint({c['id'] for c in page2})

    def test_fts_index_survives_upsert_and_delete_rebuild(self, tmp_path):
        # Existing DB without FTS gets backfilled on open
        path = tmp_path / 'cards.db'
        db = CardDB(path, offline=True)
        db.store_card(make_card('id-1', 'Lightning Bolt', 'sta', '42'))
        db.close()
        db2 = CardDB(path, offline=True)
        assert [c['id'] for c in db2.search_local('Lightning')] == ['id-1']
        # Renames stay searchable under the new name only
        renamed = make_card('id-1', 'Shock', 'sta', '42')
        db2.store_card(renamed)
        assert db2.search_local('Lightning') == []
        assert [c['id'] for c in db2.search_local('Shock')] == ['id-1']


class TestBulkImport:

    def test_import_bulk_line_format(self, carddb, bulk_file):
        count = carddb.import_bulk(bulk_file)
        assert count == 4
        assert carddb.get_card('sta', '42')['name'] == 'Lightning Bolt'
        assert carddb.stats()['bulk_imported_at'] is not None
        assert carddb.stats()['cards'] == 4

    def test_import_bulk_compact_format(self, carddb, tmp_path):
        # Whole array on one line — exercises the json.load fallback
        cards = [make_card('z-1', 'Opt', 'dom', '60'), make_card('z-2', 'Ponder', 'c18', '92')]
        path = tmp_path / 'compact.json'
        path.write_text(json.dumps(cards), encoding='utf-8')
        assert carddb.import_bulk(path) == 2
        assert carddb.find_card('Ponder') is not None

    def test_import_skips_non_cards(self, carddb, tmp_path):
        path = tmp_path / 'mixed.json'
        rows = [json.dumps(make_card('y-1', 'Opt', 'dom', '60')),
                json.dumps({'object': 'error', 'details': 'nope'})]
        path.write_text('[\n' + ',\n'.join(rows) + '\n]', encoding='utf-8')
        assert carddb.import_bulk(path) == 1


class TestResolveCollection:

    def test_all_from_cache(self, carddb, bulk_file):
        carddb.import_bulk(bulk_file)
        result = carddb.resolve_collection([
            {'name': 'Lightning Bolt'},
            {'set': 'c21', 'collector_number': '125'},
        ])
        assert len(result.found) == 2
        assert result.from_cache == 2
        assert result.from_api == 0
        assert result.missing == []

    def test_offline_misses_reported(self, carddb):
        result = carddb.resolve_collection([{'name': 'Black Lotus'}])
        assert result.found == []
        assert result.missing == [{'name': 'Black Lotus'}]

    def test_api_fallback_batches(self, carddb, monkeypatch):
        """Misses go to /cards/collection in one batch and get cached."""
        carddb.offline = False
        calls = []

        class FakeResponse:
            status_code = 200
            def json(self):
                return {
                    'data': [make_card('api-1', 'Black Lotus', 'lea', '232')],
                    'not_found': [{'name': 'Not A Card'}]}

        class FakeSession:
            def post(self, url, **kwargs):
                calls.append(kwargs['json'])
                return FakeResponse()

        carddb._session = FakeSession()
        result = carddb.resolve_collection([
            {'name': 'Black Lotus'}, {'name': 'Not A Card'}])
        assert len(calls) == 1
        assert len(calls[0]['identifiers']) == 2
        assert result.from_api == 1
        assert result.missing == [{'name': 'Not A Card'}]
        # Cached for next time
        carddb.offline = True
        assert carddb.find_card('Black Lotus') is not None


class TestSetList:

    def test_local_mtg_sets_from_cache(self, carddb):
        carddb.store_card(make_card('id-1', 'Lightning Bolt', 'sta', '42', released='2021-04-23'))
        carddb.store_card(make_card('id-2', 'Sol Ring', 'c21', '125', released='2021-04-23'))
        carddb.store_card(make_card('id-3', 'Bolt', 'sta', '42a', released='2021-04-23'))
        rows = carddb.list_local_mtg_sets()
        by_id = {r['id']: r for r in rows}
        assert set(by_id) == {'sta', 'c21'}
        assert by_id['sta']['card_count'] == 2
        assert by_id['sta']['name'] == 'STA'

    def test_list_scryfall_sets_offline_uses_local(self, carddb):
        carddb.store_card(make_card('id-1', 'Lightning Bolt', 'mh3', '1', released='2024-06-14'))
        rows = carddb.list_scryfall_sets()
        assert len(rows) == 1
        assert rows[0]['id'] == 'mh3'

    def test_list_scryfall_sets_serves_meta_cache(self, carddb):
        payload = [
            {'id': 'mh3', 'name': 'Modern Horizons 3', 'released_at': '2024-06-14',
             'card_count': 300, 'set_type': 'expansion'},
            {'id': 'tkn', 'name': 'Tokens', 'released_at': '2024-06-14',
             'card_count': 10, 'set_type': 'token'},
        ]
        carddb.set_meta('scryfall_sets_json', json.dumps(payload))
        carddb.set_meta('scryfall_sets_at', '2099-01-01T00:00:00Z')
        rows = carddb.list_scryfall_sets()
        assert [r['id'] for r in rows] == ['mh3', 'tkn']


class TestDecks:

    def test_save_and_get_deck(self, carddb):
        deck_id = carddb.save_deck(
            'Test Deck',
            [('id-1', 'Lightning Bolt', 4, 'main'), (None, 'Mystery Card', 1, 'side')],
            source_url='https://moxfield.com/decks/abc')
        deck = carddb.get_deck(deck_id)
        assert deck['name'] == 'Test Deck'
        assert len(deck['cards']) == 2
        decks = carddb.get_decks()
        assert decks[0]['cards'] == 5  # total quantity
