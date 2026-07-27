"""
* Scraper probe tests — all offline (every fetch stubbed).
* The probe is a diagnostic, so what's tested is that it *reports* honestly:
* it must not raise on a dead host, and it must show 0 rows when the parser
* finds nothing rather than glossing over it.
"""
# Third Party Imports
import pytest

# Local Imports
from web.shared import probe

REAL_ISH_PAGE = '''
<form action="/cardlist/search" method="post">
  <input name="keyword" type="text">
  <select name="title_number" id="title_number">
    <option value="">Select Title</option>
    <option value="591101">Cardcaptor Sakura</option>
    <option value="591102">Fate/Grand Order</option>
    <option value="591103">Hololive</option>
  </select>
  <select name="rarity"><option value="RR">RR</option></select>
</form>
<ul class="cardlist">
  <li><img src="/cardlist/cardimages/CCS_WX01_001.png" alt="CCS/WX01-001 Sakura"></li>
  <li><img src="/images/common/logo.png" alt="logo"></li>
</ul>
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
        assert [v for v, _ in found['title_number']] == ['591101', '591102', '591103']
        assert found['rarity'] == [('RR', 'RR')]

    def test_image_dirs_rank_card_art_by_frequency(self):
        assert dict(probe._image_dirs(REAL_ISH_PAGE)) == {
            '/cardlist/cardimages': 1, '/images/common': 1}

    def test_describe_reports_form_target_and_fields(self, lines, collect):
        probe._describe(REAL_ISH_PAGE, collect, deep=True)
        report = '\n'.join(lines)
        assert 'form POST /cardlist/search' in report
        assert 'keyword' in report and 'title_number' in report
        assert 'img tags: 2' in report


class TestProbeRun:

    @pytest.fixture()
    def stub(self, monkeypatch):
        """Serve one page for every request, recording what was asked for."""
        calls: list[tuple[str, str]] = []

        def fake_fetch(url, params=None, *, method='GET', browser=False):
            calls.append((method, url))
            return ('200 text/html 1.0KB', url, REAL_ISH_PAGE)

        monkeypatch.setattr(probe, '_fetch', fake_fetch)
        return calls

    def test_weiss_probe_reports_titles_and_rows(self, stub, lines, collect):
        assert probe.probe_game('weiss-schwarz', collect) == 0
        report = '\n'.join(lines)
        assert '_parse_ws_titles found: 3' in report
        assert 'parser rows: 1' in report
        # Both locales, and every request shape, get exercised
        assert 'https://en.ws-tcg.com/cardlist' in report
        assert 'https://ws-tcg.com/cardlist' in report
        assert ('POST', 'https://en.ws-tcg.com/cardlist/search') in stub

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
