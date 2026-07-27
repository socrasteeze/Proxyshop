"""
* Scraper probe tests — all offline (every fetch stubbed).
* The probe is a diagnostic, so what's tested is that it *reports* honestly:
* it must not raise on a dead host, must show 0 rows when the parser finds
* nothing, and must submit the form the page actually ships rather than
* guessed parameters.
"""
# Third Party Imports
import pytest

# Local Imports
from web.shared import probe

BASE = 'https://en.ws-tcg.com/cardlist/'

REAL_ISH_PAGE = '''
<form action="/cardlist/search/" method="post">
  <input name="keyword" type="text">
  <input name="keyword_type[]" type="checkbox">
  <select name="title_number" id="title_number">
    <option value="">Select Title</option>
    <option value="591101">Cardcaptor Sakura</option>
    <option value="591102">Fate/Grand Order</option>
  </select>
  <select name="show_page_count"><option value="30">30</option></select>
</form>
<script src="/assets/js/cardlist.js"></script>
<ul class="cardlist">
  <li><img src="/cardlist/cardimages/CCS_WX01_001.png" alt="CCS/WX01-001 Sakura"></li>
  <li><img src="/images/common/logo.svg" alt="logo"></li>
</ul>
'''

# What a JS-driven cardlist really looks like: dropdowns present but empty,
# because their options arrive by XHR after load.
EMPTY_DROPDOWN_PAGE = '''
<form action="/cardlist/search/" method="get">
  <input name="keyword"><select name="title_number"></select>
  <select name="expansion"></select>
</form>
<script src="/assets/js/search.js"></script>
'''


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Keep the suite offline and instant: no HEAD probes, no request spacing."""
    monkeypatch.setattr(probe.games, '_ws_url_exists', lambda url: False)
    prev = probe.games._provider_limiter.min_interval
    probe.games._provider_limiter.set_interval(0)
    try:
        yield
    finally:
        probe.games._provider_limiter.set_interval(prev)


@pytest.fixture()
def lines():
    return []


@pytest.fixture()
def collect(lines):
    return lines.append


class TestSummarizers:

    def test_selects_are_named_and_counted(self):
        found = dict(probe._selects(REAL_ISH_PAGE))
        assert [v for v, _ in found['title_number']] == ['591101', '591102']
        assert found['show_page_count'] == [('30', '30')]

    def test_forms_resolve_relative_actions(self):
        (form,) = probe._forms(REAL_ISH_PAGE, BASE)
        assert form['method'] == 'POST'
        assert form['action'] == 'https://en.ws-tcg.com/cardlist/search/'
        assert 'keyword_type[]' in form['fields']

    def test_card_signals_count_the_evidence(self):
        assert probe._card_signals(REAL_ISH_PAGE) == (
            'img=2 cardimages=1 cardno=0 card_no=0')

    def test_describe_reports_form_target_and_fields(self, lines, collect):
        probe._describe(REAL_ISH_PAGE, collect, deep=True, base_url=BASE)
        report = '\n'.join(lines)
        assert 'form POST https://en.ws-tcg.com/cardlist/search/' in report
        assert 'keyword' in report and 'title_number' in report
        assert 'signals: img=2' in report

    def test_search_form_prefers_the_keyword_form(self):
        page = '<form action="/a"><input name="x"></form>' + REAL_ISH_PAGE
        form = probe._search_form(page, BASE)
        assert form['action'] == 'https://en.ws-tcg.com/cardlist/search/'


class TestProbeRun:

    @pytest.fixture()
    def stub(self, monkeypatch):
        """Serve one page for every request, recording what was asked for."""
        calls: list[dict] = []

        def fake_fetch(url, params=None, *, method='GET', variant='browser'):
            calls.append({'url': url, 'params': params or {},
                          'method': method, 'variant': variant})
            return ('200 text/html 1.0KB', url, REAL_ISH_PAGE)

        monkeypatch.setattr(probe, '_fetch', fake_fetch)
        return calls

    def test_weiss_probe_reports_titles_and_rows(self, stub, lines, collect):
        assert probe.probe_game('weiss-schwarz', collect) == 0
        report = '\n'.join(lines)
        assert '_parse_ws_titles found: 2' in report
        assert 'parser rows: 1' in report
        assert 'https://en.ws-tcg.com/cardlist' in report
        assert 'https://ws-tcg.com/cardlist' in report

    def test_header_matrix_tries_one_variable_at_a_time(self, stub, lines, collect):
        probe.probe_game('weiss-schwarz', collect)
        root = 'https://en.ws-tcg.com/cardlist/'
        variants = [c['variant'] for c in stub if c['url'] == root]
        assert set(probe.HEADER_VARIANTS) <= set(variants)
        assert 'header matrix on root:' in '\n'.join(lines)

    def test_submit_sends_every_field_the_form_declares(self, stub, lines, collect):
        probe.probe_game('weiss-schwarz', collect)
        submits = [c for c in stub
                   if c['url'].endswith('/cardlist/search/') and c['method'] == 'POST']
        assert submits, 'the discovered form was never submitted'
        sent = submits[0]['params']
        # Every declared field is present — a browser submits the whole form —
        # and no invented parameter (the old code's `cmd=search`) is added.
        assert set(sent) == {'keyword', 'keyword_type[]', 'title_number',
                             'show_page_count'}
        assert 'cmd' not in sent
        assert sent['keyword'] == 'Sakura'

    def test_empty_dropdowns_are_reported_not_glossed(self, monkeypatch,
                                                      lines, collect):
        monkeypatch.setattr(
            probe, '_fetch',
            lambda url, params=None, **kw: ('200 text/html 2.0KB', url,
                                            EMPTY_DROPDOWN_PAGE))
        probe.probe_game('weiss-schwarz', collect)
        report = '\n'.join(lines)
        assert 'title_number[0]' in report
        assert '_parse_ws_titles found: 0' in report
        assert 'parser rows: 0' in report

    def test_probe_survives_a_dead_host(self, monkeypatch, lines, collect):
        monkeypatch.setattr(
            probe, '_fetch',
            lambda url, params=None, **kw: ('FAILED ConnectionError: down', url, ''))
        assert probe.probe_game('weiss-schwarz', collect) == 0
        report = '\n'.join(lines)
        assert 'FAILED ConnectionError' in report
        assert '_parse_ws_titles found: 0' in report

    def test_union_arena_probe_runs(self, stub, lines, collect):
        assert probe.probe_game('union-arena', collect) == 0
        assert '_parse_ua_series found:' in '\n'.join(lines)

    def test_unknown_game_is_rejected(self, lines, collect):
        assert probe.probe_game('mtg', collect) == 1
        assert 'No probe for' in lines[0]
