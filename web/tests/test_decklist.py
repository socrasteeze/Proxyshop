"""
* Decklist Parser Tests — plain/MTGA/MTGO formats, boards, URL dispatch.
"""
# Third Party Imports
import pytest

# Local Imports
from web.shared.decklist import fetch_deck_url, parse_decklist_text


class TestPlainLists:

    def test_qty_and_name(self):
        lines = parse_decklist_text('4 Lightning Bolt\n2x Sol Ring\nOpt')
        assert [(ln.qty, ln.name) for ln in lines] == [
            (4, 'Lightning Bolt'), (2, 'Sol Ring'), (1, 'Opt')]

    def test_set_and_number(self):
        (ln,) = parse_decklist_text('1 Sol Ring (C21) 125')
        assert (ln.name, ln.set_code, ln.collector_number) == ('Sol Ring', 'C21', '125')

    def test_set_without_number(self):
        (ln,) = parse_decklist_text('1 Sol Ring (C21)')
        assert (ln.set_code, ln.collector_number) == ('C21', None)

    def test_comments_and_blanks_before_cards_skipped(self):
        lines = parse_decklist_text('// my deck\n# comment\n\n4 Duress')
        assert len(lines) == 1
        assert lines[0].board == 'main'


class TestBoards:

    def test_mtga_headers(self):
        text = 'Deck\n4 Lightning Bolt (STA) 42\n\nSideboard\n2 Duress (STA) 15'
        lines = parse_decklist_text(text)
        assert lines[0].board == 'main'
        assert lines[1].board == 'side'

    def test_commander_header(self):
        text = 'Commander\n1 Atraxa, Praetors\' Voice\nDeck\n99 Forest'
        lines = parse_decklist_text(text)
        assert lines[0].board == 'commander'
        assert lines[1].board == 'main'

    def test_blank_line_separates_sideboard(self):
        lines = parse_decklist_text('4 Bolt\n\n2 Duress')
        assert lines[0].board == 'main'
        assert lines[1].board == 'side'

    def test_sb_prefix(self):
        lines = parse_decklist_text('4 Bolt\nSB: 2 Duress')
        assert lines[1].board == 'side'
        assert lines[1].name == 'Duress'

    def test_sideboard_header_with_count(self):
        lines = parse_decklist_text('4 Bolt\nSideboard (15)\n2 Duress')
        assert lines[1].board == 'side'


class TestUrlDispatch:

    def test_unsupported_site(self):
        with pytest.raises(ValueError, match='Unsupported deck site'):
            fetch_deck_url('https://example.com/decks/123')

    def test_bad_moxfield_path(self):
        with pytest.raises(ValueError, match='Moxfield'):
            fetch_deck_url('https://moxfield.com/users/somebody')

    def test_moxfield_dispatch(self, monkeypatch):
        import web.shared.decklist as dl
        monkeypatch.setattr(dl, 'fetch_moxfield', lambda deck_id: (f'deck-{deck_id}', []))
        name, lines = fetch_deck_url('https://www.moxfield.com/decks/AbC123')
        assert name == 'deck-AbC123'

    def test_archidekt_dispatch(self, monkeypatch):
        import web.shared.decklist as dl
        monkeypatch.setattr(dl, 'fetch_archidekt', lambda deck_id: (f'deck-{deck_id}', []))
        name, lines = fetch_deck_url('archidekt.com/decks/987654/my_cool_deck')
        assert name == 'deck-987654'


class TestSiteParsing:

    def test_moxfield_shape(self, monkeypatch):
        import web.shared.decklist as dl
        payload = {
            'name': 'My Deck',
            'commanders': {'c1': {'quantity': 1, 'card': {'name': 'Atraxa', 'set': '2x2', 'cn': '190'}}},
            'mainboard': {'m1': {'quantity': 4, 'card': {'name': 'Opt', 'set': 'dom', 'cn': '60'}}},
            'sideboard': {},
        }
        monkeypatch.setattr(dl, '_get_json', lambda url: payload)
        name, lines = dl.fetch_moxfield('abc')
        assert name == 'My Deck'
        boards = {ln.name: ln.board for ln in lines}
        assert boards == {'Atraxa': 'commander', 'Opt': 'main'}

    def test_archidekt_shape(self, monkeypatch):
        import web.shared.decklist as dl
        payload = {
            'name': 'Deck A',
            'cards': [
                {'quantity': 1, 'categories': ['Commander'],
                 'card': {'oracleCard': {'name': 'Atraxa'}, 'edition': {'editioncode': '2x2'},
                          'collectorNumber': '190'}},
                {'quantity': 4, 'categories': [],
                 'card': {'oracleCard': {'name': 'Opt'}, 'edition': {'editioncode': 'dom'},
                          'collectorNumber': '60'}},
            ]}
        monkeypatch.setattr(dl, '_get_json', lambda url: payload)
        name, lines = dl.fetch_archidekt('987')
        assert {ln.name: ln.board for ln in lines} == {'Atraxa': 'commander', 'Opt': 'main'}
