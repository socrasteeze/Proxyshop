"""
* Live probe for the HTML-scraping providers (Weiß Schwarz, Union Arena)
* Prints what the official cardlist sites actually serve, so "cached 0 cards"
* can be diagnosed from evidence instead of guesswork.
*
* Why this exists: those providers scrape HTML, so a layout change on the
* official site looks exactly like an empty catalog — the run reports success
* and stores nothing. The offline tests can't catch it (their fixtures are our
* own markup), and the sites block most datacenter IPs, so this has to run
* from the NAS:
*   docker exec proxyshop-web python -m web.server.manage probe-game \
*       --game weiss-schwarz
* Read-only: it fetches pages and counts things. Nothing is stored.
* Must never import from `src/`.
"""
# Standard Library Imports
import html as html_lib
import json
import re
from collections import Counter
from typing import Callable, Optional

# Third Party Imports
import requests

# Local Imports
from web.shared import games
from web.shared.carddb import HEADERS

# The scrapers inherit the JSON providers' headers (Accept: application/json).
# Whether that changes what a cardlist page returns is exactly the kind of
# thing worth A/B-ing here, so probes can send browser-ish headers instead.
BROWSER_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                   'AppleWebKit/537.36 (KHTML, like Gecko) '
                   'Chrome/126.0.0.0 Safari/537.36'),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9,ja;q=0.8',
}

_SELECT_RE = re.compile(r'<select\b([^>]*)>(.*?)</select>', re.IGNORECASE | re.DOTALL)
_OPTION_RE = re.compile(
    r'<option\b[^>]*\bvalue="([^"]*)"[^>]*>([^<]*)</option>', re.IGNORECASE)
_FORM_RE = re.compile(r'<form\b([^>]*)>(.*?)</form>', re.IGNORECASE | re.DOTALL)
_FIELD_RE = re.compile(
    r'<(?:input|select|textarea)\b[^>]*\bname="([^"]+)"', re.IGNORECASE)
_IMG_TAG_RE = re.compile(r'<img\b[^>]*>', re.IGNORECASE)
_IMG_SRC_RE = re.compile(
    r'\b(?:src|data-src|data-original|data-lazy|data-echo)="'
    r'([^"]+\.(?:png|jpe?g|webp|gif))',
    re.IGNORECASE)
_SCRIPT_URL_RE = re.compile(
    r'''["'](/[^"'\s]*(?:api|search|ajax|cardlist)[^"'\s]*)["']''', re.IGNORECASE)


def _attr(attrs: str, name: str) -> str:
    m = re.search(rf'\b{name}="([^"]*)"', attrs or '', re.IGNORECASE)
    return html_lib.unescape(m.group(1)) if m else ''


def _kb(text: str) -> str:
    return f'{len(text.encode("utf-8", "replace")) / 1024:.1f}KB'


def _fetch(
    url: str,
    params: Optional[dict] = None,
    *,
    method: str = 'GET',
    browser: bool = False,
) -> tuple[str, str, str]:
    """Fetch without raising: probes must report failures, not inherit them.

    Returns (status line, final url, body). Uses the shared provider limiter so
    a probe is as polite as a real catalog run.
    """
    headers = dict(BROWSER_HEADERS if browser else HEADERS)
    games._provider_limiter.wait()
    try:
        if method == 'POST':
            res = requests.post(url, data=params or {}, headers=headers,
                                timeout=30, allow_redirects=True)
        else:
            res = requests.get(url, params=params or {}, headers=headers,
                               timeout=30, allow_redirects=True)
    except requests.RequestException as e:
        return (f'FAILED {type(e).__name__}: {e}', url, '')
    ctype = (res.headers.get('Content-Type') or '?').split(';')[0]
    return (f'{res.status_code} {ctype} {_kb(res.text or "")}',
            res.url, res.text or '')


def _selects(page_html: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """(select name/id, [(option value, label)]) for every select on the page."""
    out: list[tuple[str, list[tuple[str, str]]]] = []
    for attrs, body in _SELECT_RE.findall(page_html):
        name = _attr(attrs, 'name') or _attr(attrs, 'id') or '(unnamed)'
        options = [
            (v.strip(), html_lib.unescape(label).strip())
            for v, label in _OPTION_RE.findall(body) if v.strip()
        ]
        out.append((name, options))
    return out


def _image_dirs(page_html: str) -> list[tuple[str, int]]:
    """Most common image directories on the page — where the art really lives."""
    dirs: Counter = Counter()
    for src in _IMG_SRC_RE.findall(page_html):
        path = src.split('?', 1)[0]
        dirs[path.rsplit('/', 1)[0] or '/'] += 1
    return dirs.most_common(5)


def _describe(page_html: str, print_fn: Callable[[str], None], *, deep: bool) -> None:
    """Print the structural summary of one fetched page."""
    imgs = _IMG_TAG_RE.findall(page_html)
    print_fn(f'     img tags: {len(imgs)}   image dirs: ' + (', '.join(
        f'{d}({n})' for d, n in _image_dirs(page_html)) or 'none'))
    if not deep:
        return
    sel = _selects(page_html)
    print_fn('     selects: ' + (', '.join(
        f'{name}[{len(opts)}]' for name, opts in sel) or 'none'))
    for name, opts in sel:
        if len(opts) >= 3:
            sample = ' | '.join(f'{v}={label[:28]}' for v, label in opts[:3])
            print_fn(f'       {name}: {sample}')
    for attrs, body in _FORM_RE.findall(page_html):
        action = _attr(attrs, 'action') or '(self)'
        method = (_attr(attrs, 'method') or 'GET').upper()
        fields = sorted(set(_FIELD_RE.findall(body)))
        print_fn(f'     form {method} {action} fields: {", ".join(fields) or "none"}')
    endpoints = sorted(set(_SCRIPT_URL_RE.findall(page_html)))[:6]
    if endpoints:
        print_fn(f'     script urls: {", ".join(endpoints)}')


def _attempt(
    label: str,
    method: str,
    url: str,
    params: dict,
    parse: Callable[[str], list],
    print_fn: Callable[[str], None],
    *,
    browser: bool = False,
    deep: bool = False,
) -> int:
    """Run one request shape and report how many cards the parser found."""
    status, final, body = _fetch(url, params, method=method, browser=browser)
    keys = ','.join(params) if params else '(none)'
    tag = ' browser-headers' if browser else ''
    print_fn(f'  {label}: {method} {url}{tag}')
    print_fn(f'     params: {keys}')
    print_fn(f'     {status}' + (f' -> {final}' if final != url else ''))
    if not body:
        return 0
    rows = parse(body)
    print_fn(f'     parser rows: {len(rows)}')
    _describe(body, print_fn, deep=deep)
    sample = _IMG_TAG_RE.search(body)
    if sample and not rows:
        print_fn(f'     sample img: {sample.group(0)[:220]}')
    if len(body) < 800:
        print_fn(f'     body: {body.strip()[:400]!r}')
    return len(rows)


def probe_weiss_schwarz(print_fn: Callable[[str], None] = print) -> None:
    """Probe both official Weiß Schwarz cardlists plus the image fallbacks."""
    print_fn('== weiss-schwarz ==')
    for locale in games.WS_LOCALE_ORDER:
        cardlist = games._ws_cardlist(locale)
        print_fn(f'-- locale {locale} ({cardlist})')

        def parse(body: str, _loc=locale) -> list:
            return games._parse_ws_cardlist_html(body, locale=_loc)

        status, final, root = _fetch(f'{cardlist}/')
        print_fn(f'  root: GET {cardlist}/')
        print_fn(f'     {status}' + (f' -> {final}' if final != f'{cardlist}/' else ''))
        _describe(root, print_fn, deep=True)
        titles = games._parse_ws_titles(root)
        print_fn(f'  _parse_ws_titles found: {len(titles)}'
                 + (f' (first {titles[0]})' if titles else ''))

        title_id = titles[0][0] if titles else ''
        base = {'cmd': 'search', 'show_page_count': 50}
        if title_id:
            base['title_number'] = title_id
        # One round trip should tell us which shape the site actually answers.
        _attempt('try1 (current code)', 'GET', f'{cardlist}/search', dict(base),
                 parse, print_fn, deep=True)
        _attempt('try2 (POST)', 'POST', f'{cardlist}/search', dict(base),
                 parse, print_fn)
        _attempt('try3 (root + params)', 'GET', f'{cardlist}/', dict(base),
                 parse, print_fn)
        _attempt('try4 (trailing slash)', 'GET', f'{cardlist}/search/', dict(base),
                 parse, print_fn)
        _attempt('try5 (browser headers)', 'GET', f'{cardlist}/search', dict(base),
                 parse, print_fn, browser=True)
        _attempt('try6 (keyword)', 'GET', f'{cardlist}/search',
                 {'cmd': 'search', 'keyword': 'Sakura', 'show_page_count': 50},
                 parse, print_fn)

        decklog = games._ws_locale_meta(locale)['decklog']
        status, _final, body = _fetch(f'{decklog}/system/app/api/cardlist')
        print_fn(f'  decklog: GET {decklog}/system/app/api/cardlist')
        print_fn(f'     {status}')
        if body:
            _report_json(body, print_fn)

    status, _final, body = _fetch(
        f'{games.ENCOREDECKS_API}/cards', {'cardcode': 'CCS/WX01-001'})
    print_fn(f'  encoredecks: GET {games.ENCOREDECKS_API}/cards?cardcode=CCS/WX01-001')
    print_fn(f'     {status}')
    if body:
        _report_json(body, print_fn)

    probe = games._ws_image_url('CCS/WX01-001')
    print_fn(f'  official image HEAD {probe}: '
             f'{"200" if games._ws_url_exists(probe) else "unavailable"}')


def _report_json(body: str, print_fn: Callable[[str], None]) -> None:
    """Summarize a JSON payload: row count and the first row's keys."""
    try:
        payload = json.loads(body)
    except ValueError:
        print_fn(f'     non-JSON: {body.strip()[:200]!r}')
        return
    if isinstance(payload, dict):
        print_fn(f'     top-level keys: {", ".join(sorted(payload))[:200]}')
    rows = games._card_rows(payload)
    print_fn(f'     rows: {len(rows)}')
    if rows and isinstance(rows[0], dict):
        print_fn(f'     row keys: {", ".join(sorted(rows[0]))[:300]}')


def probe_union_arena(print_fn: Callable[[str], None] = print) -> None:
    """Probe both official Union Arena cardlists (same failure mode as WS)."""
    print_fn('== union-arena ==')
    for locale in games.UA_LOCALE_ORDER:
        cardlist = games._ua_cardlist(locale)
        print_fn(f'-- locale {locale} ({cardlist})')

        def parse(body: str, _loc=locale) -> list:
            return games._parse_ua_cardlist_html(body, locale=_loc)

        status, final, root = _fetch(f'{cardlist}/')
        print_fn(f'  root: GET {cardlist}/')
        print_fn(f'     {status}' + (f' -> {final}' if final != f'{cardlist}/' else ''))
        _describe(root, print_fn, deep=True)
        series = games._parse_ua_series(root)
        print_fn(f'  _parse_ua_series found: {len(series)}'
                 + (f' (first {series[0]})' if series else ''))

        series_id = series[0][0] if series else ''
        params = {'series': series_id, 'show_page_count': 50} if series_id else {}
        _attempt('try1 (current code)', 'GET', f'{cardlist}/index.php', params,
                 parse, print_fn, deep=True)
        _attempt('try2 (POST)', 'POST', f'{cardlist}/index.php', dict(params),
                 parse, print_fn)


PROBES: dict[str, Callable[..., None]] = {
    'weiss-schwarz': probe_weiss_schwarz,
    'union-arena': probe_union_arena,
}


def probe_game(game: str, print_fn: Callable[[str], None] = print) -> int:
    probe = PROBES.get(game)
    if probe is None:
        print_fn(f'No probe for {game!r}. Available: {", ".join(sorted(PROBES))}')
        return 1
    probe(print_fn)
    return 0
