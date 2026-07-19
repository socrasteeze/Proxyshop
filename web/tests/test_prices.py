"""
* Price Tests — Scryfall price extraction, MTGJSON importer, deck values.
"""
# Standard Library Imports
import json

# Third Party Imports
import pytest

# Local Imports
from web.tests.conftest import make_card

try:
    from web.shared import mtgjson
    HAS_IJSON = True
except ImportError:
    HAS_IJSON = False


class TestScryfallPriceExtraction:

    def test_store_card_extracts_prices(self, carddb):
        card = make_card('id-1', 'Sol Ring', 'c21', '125')
        card['prices'] = {'usd': '1.49', 'usd_foil': '4.20', 'eur': '1.10', 'tix': None}
        carddb.store_card(card)
        prices = carddb.get_prices(['id-1'])
        assert prices['id-1']['usd'] == pytest.approx(1.49)
        assert prices['id-1']['usd_foil'] == pytest.approx(4.20)
        assert prices['id-1']['eur'] == pytest.approx(1.10)
        assert prices['id-1']['source'] == 'scryfall'

    def test_no_prices_block_no_row(self, carddb):
        carddb.store_card(make_card('id-2', 'Opt', 'dom', '60'))
        assert carddb.get_prices(['id-2']) == {}

    def test_null_prices_no_row(self, carddb):
        card = make_card('id-3', 'Duress', 'usg', '132')
        card['prices'] = {'usd': None, 'eur': None}
        carddb.store_card(card)
        assert carddb.get_prices(['id-3']) == {}


class TestDeckValue:

    def test_deck_value_sums_qty_times_usd(self, carddb):
        bolt = make_card('id-b', 'Lightning Bolt', 'sta', '42')
        bolt['prices'] = {'usd': '2.00'}
        carddb.store_card(bolt)
        carddb.save_deck('Deck', [('id-b', 'Lightning Bolt', 4, 'main'),
                                  (None, 'Unknown Card', 1, 'main')])
        deck = carddb.get_decks()[0]
        assert deck['value_usd'] == pytest.approx(8.00)

    def test_deck_value_none_without_prices(self, carddb):
        carddb.save_deck('Deck', [(None, 'Mystery', 1, 'main')])
        assert carddb.get_decks()[0]['value_usd'] is None


@pytest.mark.skipif(not HAS_IJSON, reason='ijson not installed')
class TestMtgjsonImport:

    @pytest.fixture()
    def mtgjson_files(self, tmp_path, carddb):
        """Card DB with two cards + matching MTGJSON identifier/price files."""
        carddb.store_card(make_card('scry-1', 'Lightning Bolt', 'sta', '42'))
        carddb.store_card(make_card('scry-2', 'Sol Ring', 'c21', '125'))

        identifiers = {
            'meta': {'version': 'test'},
            'data': {
                'uuid-1': {'name': 'Lightning Bolt',
                           'identifiers': {'scryfallId': 'scry-1'}},
                'uuid-2': {'name': 'Sol Ring',
                           'identifiers': {'scryfallId': 'scry-2'}},
                'uuid-3': {'name': 'Unknown Elsewhere',
                           'identifiers': {'scryfallId': 'scry-nope'}},
            }}
        prices = {
            'meta': {'version': 'test'},
            'data': {
                'uuid-1': {'paper': {
                    'tcgplayer': {'currency': 'USD', 'retail': {
                        'normal': {'2026-01-01': 1.00, '2026-01-02': 1.25},
                        'foil': {'2026-01-02': 3.50}}},
                    'cardmarket': {'currency': 'EUR', 'retail': {
                        'normal': {'2026-01-02': 0.95}}}}},
                'uuid-2': {'paper': {}},          # no paper prices
                'uuid-3': {'paper': {
                    'tcgplayer': {'retail': {'normal': {'2026-01-02': 9.99}}}}},
            }}
        pi = tmp_path / 'AllIdentifiers.json'
        pp = tmp_path / 'AllPricesToday.json'
        pi.write_text(json.dumps(identifiers), encoding='utf-8')
        pp.write_text(json.dumps(prices), encoding='utf-8')
        return pi, pp

    def test_import_updates_known_cards_latest_date(self, carddb, mtgjson_files):
        pi, pp = mtgjson_files
        count = mtgjson.import_prices(carddb, pi, pp)
        assert count == 1  # only uuid-1 maps to a known card and has prices
        row = carddb.get_prices(['scry-1'])['scry-1']
        assert row['usd'] == pytest.approx(1.25)      # latest date wins
        assert row['usd_foil'] == pytest.approx(3.50)
        assert row['eur'] == pytest.approx(0.95)
        assert row['source'] == 'mtgjson'
        assert carddb.get_meta('mtgjson_prices_at') is not None

    def test_unknown_uuid_ignored(self, carddb, mtgjson_files):
        pi, pp = mtgjson_files
        mtgjson.import_prices(carddb, pi, pp)
        assert carddb.get_prices(['scry-nope']) == {}

    def test_mtgjson_overrides_scryfall_price(self, carddb, mtgjson_files):
        card = make_card('scry-1', 'Lightning Bolt', 'sta', '42')
        card['prices'] = {'usd': '0.10'}
        carddb.store_card(card)
        pi, pp = mtgjson_files
        mtgjson.import_prices(carddb, pi, pp)
        assert carddb.get_prices(['scry-1'])['scry-1']['usd'] == pytest.approx(1.25)
