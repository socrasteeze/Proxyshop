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
from urllib.parse import urljoin

# Third Party Imports
import requests

# Local Imports
from web.shared import games
from web.shared.carddb import HEADERS

BROWSER_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
              'AppleWebKit/537.36 (KHTML, like Gecko) '
              'Chrome/126.0.0.0 Safari/537.36')
HTML_ACCEPT = {
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9,ja;q=0.8',
}

# The scrapers inherit the JSON providers' headers, and en.ws-tcg.com answers
# those with a 500. Swapping the whole header set at once can't say *which*
# header did it, so probe them one variable at a time.
HEADER_VARIANTS: dict[str, dict] = {
    'current': dict(HEADERS),
    'html-accept': {**HEADERS, **HTML_ACCEPT},
    'browser-ua': {**HEADERS, 'User-Agent': BROWSER_UA},
    'browser': {**HTML_ACCEPT, 'User-Agent': BROWSER_UA},
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
    r'([^"]+\.(?:png|jpe?g|webp|gif|svg))',
    re.IGNORECASE)
_SCRIPT_SRC_RE = re.compile(r'<script\b[^>]*\bsrc="([^"]+)"', re.IGNORECASE)
# Endpoint-ish strings inside JS — where a JS-populated dropdown gets its data.
_JS_ENDPOINT_RE = re.compile(
    r'''["'`]([^"'`\s]{0,140}?'''
    r'''(?:admin-ajax|wp-json|/api/|ajax|cardlist/search|/search/)'''
    r'''[^"'`\s]{0,140})["'`]''',
    re.IGNORECASE)


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
    variant: str = 'browser',
) -> tuple[str, str, str]:
    """Fetch without raising: probes must report failures, not inherit them.

    Returns (status line, final url, body). Uses the shared provider limiter so
    a probe is as polite as a real catalog run.
    """
    headers = dict(HEADER_VARIANTS.get(variant) or HEADER_VARIANTS['browser'])
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


def _forms(page_html: str, base_url: str) -> list[dict]:
    """Every form as {method, action, fields} — the shapes the site expects."""
    out: list[dict] = []
    for attrs, body in _FORM_RE.findall(page_html):
        action = _attr(attrs, 'action')
        out.append({
            'method': (_attr(attrs, 'method') or 'GET').upper(),
            'action': urljoin(base_url, action) if action else base_url,
            'fields': sorted(set(_FIELD_RE.findall(body))),
        })
    return out


def _image_dirs(page_html: str) -> list[tuple[str, int]]:
    """Most common image directories on the page — where the art really lives."""
    dirs: Counter = Counter()
    for src in _IMG_SRC_RE.findall(page_html):
        path = src.split('?', 1)[0]
        dirs[path.rsplit('/', 1)[0] or '/'] += 1
    return dirs.most_common(4)


def _card_signals(page_html: str) -> str:
    """Cheap evidence that a response contains cards at all."""
    return (f'img={len(_IMG_TAG_RE.findall(page_html))} '
            f'cardimages={page_html.count("cardimages")} '
            f'cardno={page_html.count("cardno=")} '
            f'card_no={page_html.count("card_no")}')


def _describe(page_html: str, print_fn: Callable[[str], None], *, deep: bool,
              base_url: str = '') -> None:
    """Print the structural summary of one fetched page."""
    print_fn(f'     signals: {_card_signals(page_html)}   dirs: ' + (', '.join(
        f'{d}({n})' for d, n in _image_dirs(page_html)) or 'none'))
    if not deep:
        return
    sel = _selects(page_html)
    print_fn('     selects: ' + (', '.join(
        f'{name}[{len(opts)}]' for name, opts in sel) or 'none'))
    for name, opts in sel:
        if opts:
            sample = ' | '.join(f'{v}={label[:24]}' for v, label in opts[:3])
            print_fn(f'       {name}: {sample}')
    for form in _forms(page_html, base_url):
        print_fn(f'     form {form["method"]} {form["action"]}')
        print_fn(f'       fields: {", ".join(form["fields"]) or "none"}')


def _search_form(page_html: str, base_url: str) -> Optional[dict]:
    """The form that looks like the card search (most fields, has a keyword)."""
    forms = _forms(page_html, base_url)
    keyworded = [f for f in forms if any(
        'keyword' in name or 'freeword' in name for name in f['fields'])]
    pool = keyworded or forms
    return max(pool, key=lambda f: len(f['fields'])) if pool else None


def _submit(
    label: str,
    form: dict,
    overrides: dict,
    parse: Callable[[str], list],
    print_fn: Callable[[str], None],
) -> int:
    """Submit a discovered form the way a browser would: every field, then edits.

    Guessing parameter names is what produced the invented ``cmd=search`` the
    provider still sends; submitting the form the page actually ships removes
    the guess.
    """
    params = {name: '' for name in form['fields']}
    params.update(overrides)
    status, final, body = _fetch(
        form['action'], params, method=form['method'])
    print_fn(f'  {label}: {form["method"]} {form["action"]}')
    print_fn(f'     set: {", ".join(f"{k}={v}" for k, v in overrides.items())}')
    print_fn(f'     {status}' + (f' -> {final}' if final != form['action'] else ''))
    if not body:
        return 0
    rows = parse(body)
    print_fn(f'     parser rows: {len(rows)}')
    _describe(body, print_fn, deep=False)
    if not rows:
        sample = re.search(r'<img\b[^>]*(?:card|thumb)[^>]*>', body, re.IGNORECASE)
        if sample:
            print_fn(f'     card-ish img: {sample.group(0)[:200]}')
    return len(rows)


def _hunt_endpoints(
    page_html: str,
    base_url: str,
    print_fn: Callable[[str], None],
    *,
    max_scripts: int = 4,
) -> None:
    """Follow the page's own scripts to find where JS-filled dropdowns load from.

    Empty ``title_number``/``expansion`` selects mean the options arrive by
    XHR after load, so the values the catalog needs are in the JS, not the HTML.
    """
    srcs = [urljoin(base_url, s) for s in _SCRIPT_SRC_RE.findall(page_html)]
    local = [s for s in srcs if 'ws-tcg.com' in s or 'unionarena-tcg.com' in s]
    print_fn(f'  scripts: {len(srcs)} total, {len(local)} same-origin')
    inline_hits = sorted(set(_JS_ENDPOINT_RE.findall(page_html)))[:6]
    if inline_hits:
        print_fn('     inline endpoints: ' + ' | '.join(h[:90] for h in inline_hits))
    for src in local[:max_scripts]:
        status, _final, body = _fetch(src)
        hits = sorted(set(_JS_ENDPOINT_RE.findall(body)))[:6]
        print_fn(f'     {src.rsplit("/", 1)[-1][:44]} — {status}')
        for hit in hits:
            print_fn(f'       {hit[:110]}')


def _report_json(body: str, print_fn: Callable[[str], None]) -> None:
    """Summarize a JSON payload: row count and the first row's keys."""
    try:
        payload = json.loads(body)
    except ValueError:
        print_fn(f'     non-JSON: {body.strip()[:160]!r}')
        return
    if isinstance(payload, dict):
        print_fn(f'     top-level keys: {", ".join(sorted(payload))[:200]}')
    rows = games._card_rows(payload)
    print_fn(f'     rows: {len(rows)}')
    if rows and isinstance(rows[0], dict):
        print_fn(f'     row keys: {", ".join(sorted(rows[0]))[:300]}')


def _header_matrix(url: str, print_fn: Callable[[str], None]) -> str:
    """Which header set does this host answer? Returns the best variant name.

    en.ws-tcg.com 500s on the providers' ``Accept: application/json``. Changing
    User-Agent and Accept together can't attribute that, so vary one at a time.
    """
    best, best_len = 'browser', -1
    for variant in HEADER_VARIANTS:
        status, _final, body = _fetch(url, variant=variant)
        print_fn(f'     {variant:12s} -> {status}')
        if status.startswith('200') and len(body) > best_len:
            best, best_len = variant, len(body)
    return best


def probe_weiss_schwarz(print_fn: Callable[[str], None] = print) -> None:
    """Probe both official Weiß Schwarz cardlists plus the image fallbacks."""
    print_fn('== weiss-schwarz ==')
    samples = {'en': ('Sakura', 'WX01'), 'ja': ('島風', 'S25')}
    for locale in games.WS_LOCALE_ORDER:
        cardlist = games._ws_cardlist(locale)
        root = f'{cardlist}/'
        print_fn(f'-- locale {locale} ({cardlist})')

        def parse(body: str, _loc=locale) -> list:
            return games._parse_ws_cardlist_html(body, locale=_loc)

        print_fn('  header matrix on root:')
        variant = _header_matrix(root, print_fn)
        print_fn(f'  best variant: {variant}')

        status, final, body = _fetch(root, variant=variant)
        print_fn(f'  root ({variant}): {status}'
                 + (f' -> {final}' if final != root else ''))
        _describe(body, print_fn, deep=True, base_url=root)
        titles = games._parse_ws_titles(body)
        print_fn(f'  _parse_ws_titles found: {len(titles)}'
                 + (f' (first {titles[0]})' if titles else ''))

        form = _search_form(body, root)
        if form is None:
            print_fn('  no search form on the page — nothing to submit')
        else:
            name_kw, code_kw = samples.get(locale, ('Sakura', 'WX01'))
            has = set(form['fields'])
            kw_type = 'keyword_type[]' if 'keyword_type[]' in has else ''
            over: dict = {'keyword': name_kw}
            if 'show_page_count' in has:
                over['show_page_count'] = '100'
            if kw_type:
                over[kw_type] = 'name'
            _submit('submit1 (name keyword)', form, dict(over), parse, print_fn)
            over['keyword'] = code_kw
            if kw_type:
                over[kw_type] = 'no'
            _submit('submit2 (code keyword)', form, dict(over), parse, print_fn)
            if 'view' in has:
                _submit('submit3 (text view)', form,
                        {**over, 'view': 'text'}, parse, print_fn)
            for field in ('title_number', 'expansion'):
                opts = dict(_selects(body)).get(field) or []
                if opts:
                    _submit(f'submit4 ({field}={opts[0][0]})', form,
                            {field: opts[0][0], 'show_page_count': '100'},
                            parse, print_fn)
                    break

        _hunt_endpoints(body, root, print_fn)

        origin = games._ws_origin(locale)
        status, _final, wp = _fetch(f'{origin}/wp-json/')
        print_fn(f'  wp-json: {status}')
        if wp.strip().startswith('{'):
            try:
                routes = list((json.loads(wp).get('routes') or {}))
            except ValueError:
                routes = []
            interesting = [r for r in routes if re.search(
                r'card|expansion|title|search|list', r, re.IGNORECASE)][:12]
            print_fn(f'     routes: {len(routes)}, card-ish: '
                     + (', '.join(interesting) or 'none'))

        decklog = games._ws_locale_meta(locale)['decklog']
        for method in ('GET', 'POST'):
            status, _final, body = _fetch(
                f'{decklog}/system/app/api/cardlist', {}, method=method)
            print_fn(f'  decklog {method}: {status}')
            if body.strip():
                _report_json(body, print_fn)

    probe_url = games._ws_image_url('CCS/WX01-001')
    print_fn(f'  official image HEAD {probe_url}: '
             f'{"200" if games._ws_url_exists(probe_url) else "unavailable"}')


def probe_union_arena(print_fn: Callable[[str], None] = print) -> None:
    """Probe both official Union Arena cardlists (same failure mode as WS)."""
    print_fn('== union-arena ==')
    for locale in games.UA_LOCALE_ORDER:
        cardlist = games._ua_cardlist(locale)
        root = f'{cardlist}/'
        print_fn(f'-- locale {locale} ({cardlist})')

        def parse(body: str, _loc=locale) -> list:
            return games._parse_ua_cardlist_html(body, locale=_loc)

        print_fn('  header matrix on root:')
        variant = _header_matrix(root, print_fn)
        print_fn(f'  best variant: {variant}')

        status, final, body = _fetch(root, variant=variant)
        print_fn(f'  root ({variant}): {status}'
                 + (f' -> {final}' if final != root else ''))
        _describe(body, print_fn, deep=True, base_url=root)
        series = games._parse_ua_series(body)
        print_fn(f'  _parse_ua_series found: {len(series)}'
                 + (f' (first {series[0]})' if series else ''))

        form = _search_form(body, root)
        if form is None:
            print_fn('  no search form on the page — nothing to submit')
        else:
            opts = dict(_selects(body)).get('series') or []
            over = {'series': opts[0][0]} if opts else {}
            if 'show_page_count' in set(form['fields']):
                over['show_page_count'] = '100'
            _submit('submit1 (first series)', form, over, parse, print_fn)
        _hunt_endpoints(body, root, print_fn)


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
