"""
* Local Card Database ("offline Scryfall")
* SQLite-backed cache of Scryfall card objects with a bulk-data importer.
* Used by the NAS web server, the Windows worker, and (optionally) desktop
* Proxyshop via the `get_card_data` cache hook.
* Must never import from `src/` (Windows-only package). Stdlib + requests only.

Scryfall API etiquette implemented here (https://scryfall.com/docs/api):
    - Identifying User-Agent and Accept headers on every request.
    - Minimum 100ms between requests (their guidance is max ~10 req/sec).
    - Honor HTTP 429 Retry-After with bounded retries and backoff.
    - Prefer nightly bulk-data files over per-card API scraping.
"""
# Standard Library Imports
import calendar
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Union

# Third Party Imports
import requests

# Local Imports
from web.shared import cardquery

"""
* Constants
"""

SCRYFALL_API = 'https://api.scryfall.com'
BULK_DATA_URL = f'{SCRYFALL_API}/bulk-data'
COLLECTION_URL = f'{SCRYFALL_API}/cards/collection'

# Scryfall asks for an identifying User-Agent and an Accept header.
HEADERS = {
    'User-Agent': 'ProxyshopWeb/1.0 (+https://github.com/socrasteeze/Proxyshop)',
    'Accept': 'application/json;q=0.9,*/*;q=0.8'
}

# Scryfall guidance: insert 50-100ms of delay between requests.
MIN_REQUEST_INTERVAL = 0.1

# Max identifiers per /cards/collection request, per Scryfall docs.
COLLECTION_CHUNK = 75

# Bounded retries for 429 / transient failures.
MAX_RETRIES = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id TEXT PRIMARY KEY,
    oracle_id TEXT,
    name TEXT NOT NULL,
    set_code TEXT NOT NULL,
    collector_number TEXT NOT NULL,
    lang TEXT NOT NULL DEFAULT 'en',
    released_at TEXT,
    json BLOB NOT NULL,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    source TEXT NOT NULL DEFAULT 'api',
    game TEXT NOT NULL DEFAULT 'mtg'
);
CREATE INDEX IF NOT EXISTS idx_cards_set_num ON cards (set_code, collector_number, lang);
CREATE INDEX IF NOT EXISTS idx_cards_name ON cards (name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_cards_oracle ON cards (oracle_id);
CREATE INDEX IF NOT EXISTS idx_cards_game ON cards (game, name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_cards_game_set ON cards (game, set_code, name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_cards_fetched ON cards (fetched_at);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS prices (
    card_id TEXT PRIMARY KEY,
    usd REAL,
    usd_foil REAL,
    eur REAL,
    source TEXT NOT NULL DEFAULT 'scryfall',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS decks (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source_url TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS deck_cards (
    deck_id TEXT NOT NULL REFERENCES decks (id) ON DELETE CASCADE,
    card_id TEXT,
    card_name TEXT NOT NULL,
    qty INTEGER NOT NULL DEFAULT 1,
    board TEXT NOT NULL DEFAULT 'main'
);
CREATE INDEX IF NOT EXISTS idx_deck_cards_deck ON deck_cards (deck_id);
"""

# Full-text name index. Kept separate from SCHEMA so databases on SQLite
# builds without FTS5 still work (search falls back to LIKE scans).
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS cards_fts USING fts5(
    name,
    content='cards',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS cards_fts_ai AFTER INSERT ON cards BEGIN
    INSERT INTO cards_fts (rowid, name) VALUES (new.rowid, new.name);
END;
CREATE TRIGGER IF NOT EXISTS cards_fts_ad AFTER DELETE ON cards BEGIN
    INSERT INTO cards_fts (cards_fts, rowid, name) VALUES ('delete', old.rowid, old.name);
END;
CREATE TRIGGER IF NOT EXISTS cards_fts_au AFTER UPDATE OF name ON cards BEGIN
    INSERT INTO cards_fts (cards_fts, rowid, name) VALUES ('delete', old.rowid, old.name);
    INSERT INTO cards_fts (rowid, name) VALUES (new.rowid, new.name);
END;
"""

"""
* Rate-Limited Scryfall Session
"""


class ScryfallSession:
    """A requests session enforcing Scryfall's rate-limit etiquette.

    Thread-safe: a lock serializes the inter-request delay bookkeeping.
    """

    def __init__(self, min_interval: float = MIN_REQUEST_INTERVAL):
        self._session = requests.Session()
        self._session.headers.update(HEADERS)
        self._min_interval = min_interval
        self._last_request = 0.0
        self._lock = threading.Lock()

    def _throttle(self) -> None:
        with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        """Perform a throttled request, honoring 429 Retry-After with bounded retries."""
        kwargs.setdefault('timeout', 30)
        for attempt in range(MAX_RETRIES + 1):
            self._throttle()
            res = self._session.request(method, url, **kwargs)
            if res.status_code != 429:
                return res
            if attempt == MAX_RETRIES:
                return res
            # Honor Retry-After, fall back to exponential backoff
            try:
                delay = float(res.headers.get('Retry-After', 0))
            except (TypeError, ValueError):
                delay = 0
            time.sleep(max(delay, 0.5 * (2 ** attempt)))
        return res

    def get(self, url: str, **kwargs) -> requests.Response:
        return self.request('GET', url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        return self.request('POST', url, **kwargs)


"""
* Bulk File Parsing
"""


def iter_bulk_cards(path: Path) -> Iterator[dict]:
    """Iterate card objects from a Scryfall bulk-data JSON file.

    Scryfall bulk files are a single JSON array with one card object per line,
    so we stream line-by-line to avoid loading ~450MB+ into memory at once.
    Falls back to a full json.load for files not in that shape.
    """
    with open(path, 'r', encoding='utf-8') as f:
        first = f.read(1)
        if first != '[':
            raise ValueError(f'Not a JSON array: {path}')
        parsed_any = False
        failed = False
        for line in f:
            line = line.strip().rstrip(',')
            if not line or line == ']':
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                failed = True
                break
            if isinstance(obj, dict):
                parsed_any = True
                yield obj
        if failed or not parsed_any:
            # Not one-object-per-line: fall back to loading the whole array.
            with open(path, 'r', encoding='utf-8') as f2:
                data = json.load(f2)
            if not isinstance(data, list):
                raise ValueError(f'Unexpected bulk data shape in {path}')
            for obj in data:
                if isinstance(obj, dict):
                    yield obj


"""
* Card Database
"""


@dataclass
class CollectionResult:
    """Result of resolving a batch of card identifiers."""
    found: list[dict] = field(default_factory=list)
    missing: list[dict] = field(default_factory=list)
    from_cache: int = 0
    from_api: int = 0


class CardDB:
    """SQLite-backed local Scryfall card cache.

    Args:
        path: Path to the SQLite database file (parent dirs created).
        offline: When True, never touch the network — cache misses return None.
        session: Optionally inject a ScryfallSession (tests use a mock).
    """

    def __init__(
        self,
        path: Union[str, Path],
        offline: bool = False,
        session: Optional[ScryfallSession] = None
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.offline = offline
        self._session = session
        self._local = threading.local()
        with self._conn() as con:
            # Migrate BEFORE applying the schema: SCHEMA's index statements
            # reference columns that pre-multigame databases don't have yet.
            self._migrate(con)
            con.executescript(SCHEMA)
            con.execute('PRAGMA journal_mode=WAL')
            self._fts = self._init_fts(con)

    @staticmethod
    def _migrate(con: sqlite3.Connection) -> None:
        """Additive migrations for databases created by older versions."""
        cols = {r[1] for r in con.execute('PRAGMA table_info(cards)').fetchall()}
        if cols and 'game' not in cols:
            con.execute("ALTER TABLE cards ADD COLUMN game TEXT NOT NULL DEFAULT 'mtg'")
            con.commit()

    @staticmethod
    def _init_fts(con: sqlite3.Connection) -> bool:
        """Create (and backfill) the FTS5 name index; False when unsupported."""
        existed = bool(con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cards_fts'"
        ).fetchone())
        try:
            con.executescript(FTS_SCHEMA)
        except sqlite3.OperationalError:
            return False
        if not existed:
            # Existing rows predate the triggers: rebuild once on upgrade.
            con.execute("INSERT INTO cards_fts (cards_fts) VALUES ('rebuild')")
            con.commit()
        return True

    @staticmethod
    def _fts_query(text: str) -> str:
        """Build a prefix-match FTS5 query from free text ('ligh bol' style)."""
        tokens = [t.replace('"', '""') for t in text.split() if t]
        return ' '.join(f'"{t}"*' for t in tokens)

    @property
    def session(self) -> ScryfallSession:
        if self._session is None:
            self._session = ScryfallSession()
        return self._session

    def _conn(self) -> sqlite3.Connection:
        """Per-thread connection (SQLite objects can't cross threads).

        busy_timeout makes a blocked writer wait for the lock instead of
        raising 'database is locked' immediately — essential because the
        background cache thread and web-request threads write concurrently.
        """
        con = getattr(self._local, 'con', None)
        if con is None:
            con = sqlite3.connect(self.path, timeout=30.0)
            con.row_factory = sqlite3.Row
            con.execute('PRAGMA foreign_keys=ON')
            con.execute('PRAGMA busy_timeout=30000')
            self._local.con = con
        return con

    def close(self) -> None:
        con = getattr(self._local, 'con', None)
        if con is not None:
            con.close()
            self._local.con = None

    def commit(self) -> None:
        """Commit any pending writes on this thread's connection."""
        self._conn().commit()

    """
    * Storage
    """

    def store_card(
        self,
        card: dict,
        source: str = 'api',
        commit: bool = True,
        game: str = 'mtg'
    ) -> None:
        """Insert or update a card object (Scryfall-shaped; any game)."""
        con = self._conn()
        con.execute(
            """
            INSERT INTO cards (id, oracle_id, name, set_code, collector_number,
                               lang, released_at, json, fetched_at, source, game)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                oracle_id=excluded.oracle_id, name=excluded.name,
                set_code=excluded.set_code, collector_number=excluded.collector_number,
                lang=excluded.lang, released_at=excluded.released_at,
                json=excluded.json, fetched_at=excluded.fetched_at,
                source=excluded.source, game=excluded.game
            """, (
                card['id'],
                card.get('oracle_id'),
                card.get('name', ''),
                (card.get('set') or '').lower(),
                str(card.get('collector_number', '')),
                card.get('lang', 'en'),
                card.get('released_at'),
                json.dumps(card, separators=(',', ':')),
                source,
                card.get('game', game)
            ))
        self._store_scryfall_prices(card)
        if commit:
            con.commit()

    def _store_scryfall_prices(self, card: dict) -> None:
        """Extract the prices block embedded in a Scryfall card object."""
        prices = card.get('prices') or {}

        def _num(key: str) -> Optional[float]:
            try:
                return float(prices[key]) if prices.get(key) is not None else None
            except (TypeError, ValueError):
                return None

        usd, usd_foil, eur = _num('usd'), _num('usd_foil'), _num('eur')
        if usd is None and usd_foil is None and eur is None:
            return
        self.set_price(card['id'], usd=usd, usd_foil=usd_foil, eur=eur,
                       source='scryfall', commit=False)

    def set_price(
        self,
        card_id: str,
        usd: Optional[float] = None,
        usd_foil: Optional[float] = None,
        eur: Optional[float] = None,
        source: str = 'scryfall',
        commit: bool = True
    ) -> None:
        """Insert or update a card's price row (last writer wins)."""
        con = self._conn()
        con.execute(
            """
            INSERT INTO prices (card_id, usd, usd_foil, eur, source, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT (card_id) DO UPDATE SET
                usd=excluded.usd, usd_foil=excluded.usd_foil, eur=excluded.eur,
                source=excluded.source, updated_at=excluded.updated_at
            """, (card_id, usd, usd_foil, eur, source))
        if commit:
            con.commit()

    def get_prices(self, card_ids: list[str]) -> dict[str, dict]:
        """Fetch price rows for a batch of card ids. Returns {card_id: row}."""
        if not card_ids:
            return {}
        con = self._conn()
        marks = ','.join('?' * len(card_ids))
        rows = con.execute(
            f'SELECT * FROM prices WHERE card_id IN ({marks})', card_ids).fetchall()
        return {r['card_id']: dict(r) for r in rows}

    """
    * Lookups (cache first, then API unless offline)
    """

    def get_card(
        self,
        set_code: str,
        number: str,
        lang: str = 'en'
    ) -> Optional[dict]:
        """Look up a unique printing by set + collector number (+ lang).

        Mirrors Scryfall's /cards/:code/:number(/:lang) endpoint.
        """
        con = self._conn()
        row = con.execute(
            'SELECT json FROM cards WHERE set_code=? AND collector_number=? AND lang=?',
            (set_code.lower(), str(number), lang)).fetchone()
        if row:
            return json.loads(row['json'])
        if self.offline:
            return None
        # Cache miss: fetch from Scryfall
        url = f'{SCRYFALL_API}/cards/{set_code.lower()}/{number}'
        if lang != 'en':
            url += f'/{lang}'
        res = self.session.get(url)
        if res.status_code != 200:
            return None
        card = res.json()
        if card.get('object') != 'card':
            return None
        self.store_card(card)
        return card

    def find_card(
        self,
        name: str,
        set_code: Optional[str] = None,
        lang: str = 'en'
    ) -> Optional[dict]:
        """Look up a card by exact name (optionally within a set), newest first.

        Mirrors the behavior of Proxyshop's default named search
        (order: released, dir: asc returns oldest-first in the app's config;
        we return the newest printing which matches Scryfall search defaults).
        """
        con = self._conn()
        query = 'SELECT json FROM cards WHERE name=? COLLATE NOCASE AND lang=?'
        params: list[Any] = [name, lang]
        if set_code:
            query += ' AND set_code=?'
            params.append(set_code.lower())
        query += " ORDER BY released_at DESC LIMIT 1"
        row = con.execute(query, params).fetchone()
        if row:
            return json.loads(row['json'])
        if self.offline:
            return None
        # Cache miss: use the named-card endpoint (exact match)
        params_q: dict[str, str] = {'exact': name}
        if set_code:
            params_q['set'] = set_code.lower()
        res = self.session.get(f'{SCRYFALL_API}/cards/named', params=params_q)
        if res.status_code != 200:
            return None
        card = res.json()
        if card.get('object') != 'card':
            return None
        self.store_card(card)
        return card

    def get_by_id(self, card_id: str) -> Optional[dict]:
        """Fetch a cached card object by its Scryfall id (local only)."""
        row = self._conn().execute(
            'SELECT json FROM cards WHERE id=?', (card_id,)).fetchone()
        return json.loads(row['json']) if row else None

    def iter_by_game(
        self,
        game: str,
        *,
        offset: int = 0,
        batch: int = 200,
    ) -> Iterator[dict]:
        """Yield cached card objects for a game, ordered by id (stable resume)."""
        con = self._conn()
        offset = max(int(offset), 0)
        batch = max(int(batch), 1)
        while True:
            rows = con.execute(
                """
                SELECT json FROM cards
                WHERE game=?
                ORDER BY id ASC
                LIMIT ? OFFSET ?
                """, (game, batch, offset)).fetchall()
            if not rows:
                break
            for row in rows:
                yield json.loads(row['json'])
            offset += len(rows)
            if len(rows) < batch:
                break

    def count_by_game(self, game: str) -> int:
        """Return how many cards are stored for a game."""
        row = self._conn().execute(
            'SELECT COUNT(*) AS n FROM cards WHERE game=?', (game,)).fetchone()
        return int(row['n']) if row else 0

    def counts_by_game(self) -> dict[str, int]:
        """Return {game: count} for every game present in the DB."""
        rows = self._conn().execute(
            'SELECT game, COUNT(*) AS n FROM cards GROUP BY game').fetchall()
        return {str(r['game']): int(r['n']) for r in rows}

    # MTG: oracle_id (fallback to id). Other games: game + lower(name).
    _ART_GROUP_EXPR = (
        "CASE WHEN game = 'mtg' THEN COALESCE(NULLIF(oracle_id, ''), id) "
        "ELSE (game || '|' || lower(name)) END"
    )

    def list_gallery(
        self,
        *,
        game: Optional[str] = None,
        q: str = '',
        set_code: str = '',
        offset: int = 0,
        limit: int = 60,
        sort: str = 'name',
        group_arts: bool = False,
    ) -> tuple[list[dict], int]:
        """Browse locally cached cards (no network).

        Returns (cards, total_matching). Cards are lightweight projections
        built from the denormalized columns (no per-row JSON parsing) — use
        get_by_id() when the full provider object is needed.

        When group_arts is True, printings that share an art group collapse to
        the newest release (MTG: oracle_id; other games: name within game) and
        each row includes art_count.
        """
        con = self._conn()
        offset = max(int(offset), 0)
        limit = min(max(int(limit), 1), 200)
        where = ['1=1']
        params: list[Any] = []
        if game:
            where.append('game=?')
            params.append(game)
        if q.strip():
            # Game-scoped searches get full field/keyword syntax (t:, o:,
            # supertype:, 'supporter', …); cross-game falls back to name.
            if game:
                parsed = cardquery.parse_query(q, game)
                where_sql, where_params = cardquery.build_where(parsed, game)
                where.append(f'({where_sql})')
                params.extend(where_params)
            else:
                where.append('name LIKE ? COLLATE NOCASE')
                params.append(f'%{q.strip()}%')
        if set_code.strip():
            where.append('set_code=?')
            params.append(set_code.strip().lower())
        clause = ' AND '.join(where)
        order = {
            'name': 'name COLLATE NOCASE ASC, set_code ASC, collector_number ASC',
            'set': 'set_code ASC, collector_number ASC, name COLLATE NOCASE ASC',
            'newest': 'fetched_at DESC, name COLLATE NOCASE ASC',
            'id': 'id ASC',
        }.get(sort, 'name COLLATE NOCASE ASC, set_code ASC, collector_number ASC')

        def _row(r: sqlite3.Row) -> dict:
            return {
                'id': r['id'],
                'name': r['name'],
                'set': r['set_code'],
                'collector_number': r['collector_number'],
                'lang': r['lang'],
                'released_at': r['released_at'],
                'game': r['game'] or 'mtg',
                'art_count': int(r['art_count']) if 'art_count' in r.keys() else 1,
            }

        if not group_arts:
            total = con.execute(
                f'SELECT COUNT(*) AS n FROM cards WHERE {clause}', params
            ).fetchone()['n']
            rows = con.execute(
                f"""
                SELECT id, name, set_code, collector_number, lang, released_at, game,
                       1 AS art_count
                FROM cards
                WHERE {clause}
                ORDER BY {order}
                LIMIT ? OFFSET ?
                """, (*params, limit, offset)).fetchall()
            return [_row(r) for r in rows], int(total)

        group_expr = self._ART_GROUP_EXPR
        total = con.execute(
            f"""
            SELECT COUNT(*) AS n FROM (
                SELECT 1 FROM cards
                WHERE {clause}
                GROUP BY {group_expr}
            )
            """, params).fetchone()['n']
        # Newest printing wins within each art group; outer ORDER BY uses the
        # same sort keys as the ungrouped gallery.
        rows = con.execute(
            f"""
            SELECT id, name, set_code, collector_number, lang, released_at, game,
                   art_count, fetched_at
            FROM (
                SELECT id, name, set_code, collector_number, lang, released_at, game,
                       fetched_at,
                       COUNT(*) OVER (PARTITION BY {group_expr}) AS art_count,
                       ROW_NUMBER() OVER (
                           PARTITION BY {group_expr}
                           ORDER BY released_at DESC, fetched_at DESC, id ASC
                       ) AS rn
                FROM cards
                WHERE {clause}
            )
            WHERE rn = 1
            ORDER BY {order}
            LIMIT ? OFFSET ?
            """, (*params, limit, offset)).fetchall()
        return [_row(r) for r in rows], int(total)

    def search_local(
        self,
        text: str,
        limit: int = 50,
        game: Optional[str] = 'mtg',
    ) -> list[dict]:
        """Search the local DB (no network) by name, keyword, or field syntax.

        Plain text matches across the card's searchable fields for the game
        (name, type, rules text, subtypes, …) so 'supporter' finds Pokémon
        Trainer-Supporters. Scryfall-style tokens work too: 't:creature',
        'o:draw', 'set:xyz', 'supertype:trainer', 'artist:"john avon"'.
        Pass game=None to search across every game (name-only for sanity).
        Exact/prefix name matches rank first. Returns one row per printing.
        """
        text = (text or '').strip()
        if not text:
            return []
        con = self._conn()
        limit = max(int(limit), 1)

        scope = []
        scope_params: list[Any] = []
        if game:
            scope.append('game=?')
            scope_params.append(game)
            if game == 'mtg':
                scope.append("lang='en'")
        else:
            scope.append("(game != 'mtg' OR lang='en')")
        scope_clause = ' AND '.join(scope) if scope else '1=1'

        rank, rank_params = cardquery.name_rank_sql(text)
        order = f'{rank} ASC, name COLLATE NOCASE ASC, released_at DESC'

        parsed = cardquery.parse_query(text, game or 'mtg')
        # Cross-game or field-syntax queries take the structured path; a simple
        # single-word name query keeps the fast FTS path below.
        structured = bool(game) and (parsed.fields or len(parsed.terms) != 1)

        if structured:
            where_sql, where_params = cardquery.build_where(parsed, game or 'mtg')
            rows = con.execute(
                f"""
                SELECT id, game, json FROM cards
                WHERE {scope_clause} AND ({where_sql})
                ORDER BY {order}
                LIMIT ?
                """,
                (*scope_params, *where_params, *rank_params, limit)).fetchall()
            return self._rows_to_cards(rows, text)

        results: list[dict] = []
        seen: set[str] = set()

        # Fast path: FTS5 prefix-token match (word-boundary matches).
        if getattr(self, '_fts', False):
            match = self._fts_query(text)
            if match:
                try:
                    rows = con.execute(
                        f"""
                        SELECT id, game, json FROM cards
                        WHERE rowid IN (
                            SELECT rowid FROM cards_fts WHERE cards_fts MATCH ?
                        ) AND {scope_clause}
                        ORDER BY {order}
                        LIMIT ?
                        """,
                        (match, *scope_params, *rank_params, limit)).fetchall()
                except sqlite3.OperationalError:
                    rows = []
                for r in rows:
                    seen.add(r['id'])
                    card = json.loads(r['json'])
                    card.setdefault('game', r['game'] or 'mtg')
                    results.append(card)
        if len(results) >= limit:
            return results[:limit]

        # Fallback / top-up: blob LIKE scan (covers mid-word + field matches).
        blob_sql, blob_params = cardquery.build_where(parsed, game or 'mtg')
        rows = con.execute(
            f"""
            SELECT id, game, json FROM cards
            WHERE {scope_clause} AND ({blob_sql})
            ORDER BY {order}
            LIMIT ?
            """,
            (*scope_params, *blob_params, *rank_params, limit)).fetchall()
        for r in rows:
            if r['id'] in seen:
                continue
            card = json.loads(r['json'])
            card.setdefault('game', r['game'] or 'mtg')
            results.append(card)
            if len(results) >= limit:
                break

        needle = text.lower()

        def sort_key(card: dict) -> tuple:
            name = str(card.get('name') or '').lower()
            if name == needle:
                tier = 0
            elif name.startswith(needle):
                tier = 1
            else:
                tier = 2
            return tier, name, str(card.get('released_at') or '')

        results.sort(key=sort_key)
        return results[:limit]

    def _rows_to_cards(self, rows: list, text: str) -> list[dict]:
        """Deserialize (id, game, json) rows into card dicts, dedup by id."""
        out: list[dict] = []
        seen: set[str] = set()
        for r in rows:
            if r['id'] in seen:
                continue
            seen.add(r['id'])
            card = json.loads(r['json'])
            card.setdefault('game', r['game'] or 'mtg')
            out.append(card)
        return out

    def search_scryfall(self, text: str, limit: int = 30) -> list[dict]:
        """Name search against the live Scryfall API; results are cached.

        Used as the fallback when a local search comes up empty, so the local
        database grows organically with every browser search. Returns [] when
        offline, on error, or for no matches.
        """
        if self.offline:
            return []
        res = self.session.get(
            f'{SCRYFALL_API}/cards/search',
            params={'q': text, 'order': 'name'})
        if res.status_code != 200:
            return []
        data = res.json()
        if data.get('object') == 'error':
            return []
        cards = [c for c in data.get('data', []) if c.get('object') == 'card'][:limit]
        for card in cards:
            self.store_card(card, commit=False)
        self._conn().commit()
        return cards

    def list_scryfall_page(
        self,
        query: str,
        page: int = 1,
        *,
        store: bool = False,
    ) -> tuple[list[dict], Optional[int], bool]:
        """Fetch one Scryfall search page (175 cards). Does not auto-cache unless store=True.

        Returns (cards, total_cards, has_more).
        """
        if self.offline:
            return [], None, False
        page = max(int(page), 1)
        res = self.session.get(
            f'{SCRYFALL_API}/cards/search',
            params={'q': query, 'page': page, 'order': 'set'})
        if res.status_code == 404:
            return [], 0, False
        if res.status_code != 200:
            body = (res.text or '')[:200].strip()
            raise RuntimeError(
                f'Scryfall HTTP {res.status_code}'
                + (f': {body}' if body else ''))
        data = res.json()
        if data.get('object') == 'error':
            raise RuntimeError(str(data.get('details') or data.get('code') or 'Scryfall error'))
        cards = [c for c in data.get('data', []) if c.get('object') == 'card']
        if store:
            for card in cards:
                self.store_card(card, commit=False, game='mtg')
            self._conn().commit()
        total = data.get('total_cards')
        try:
            total_i = int(total) if total is not None else None
        except (TypeError, ValueError):
            total_i = None
        return cards, total_i, bool(data.get('has_more'))

    # Expansion-ish types float to the top of the empty set-picker list.
    _PREFERRED_SET_TYPES = frozenset({
        'expansion', 'core', 'draft_innovation', 'commander', 'masters',
        'funny', 'alchemy', 'masterpiece', 'arsenal',
    })

    def list_local_mtg_sets(self) -> list[dict]:
        """Distinct MTG sets already present in the local card cache."""
        rows = self._conn().execute(
            """
            SELECT set_code AS id,
                   COALESCE(
                       NULLIF(json_extract(json, '$.set_name'), ''),
                       set_code
                   ) AS name,
                   MAX(released_at) AS released_at,
                   COUNT(*) AS card_count
            FROM cards
            WHERE game='mtg' AND set_code != ''
            GROUP BY set_code
            """
        ).fetchall()
        return [{
            'id': str(r['id']).lower(),
            'name': r['name'] or r['id'],
            'released_at': r['released_at'],
            'card_count': int(r['card_count'] or 0),
            'set_type': 'local',
        } for r in rows]

    def _sort_set_rows(self, rows: list[dict]) -> list[dict]:
        """Newest first; playable set types before tokens/memorabilia."""
        preferred = self._PREFERRED_SET_TYPES
        rows = list(rows)
        rows.sort(key=lambda r: r.get('released_at') or '', reverse=True)
        rows.sort(key=lambda r: 0 if (r.get('set_type') or '') in preferred else 1)
        return rows

    def _load_sets_cache(self) -> tuple[list[dict], str]:
        raw = self.get_meta('scryfall_sets_json')
        cached_at = self.get_meta('scryfall_sets_at') or ''
        if not raw:
            return [], cached_at
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return [], cached_at
        if not isinstance(parsed, list):
            return [], cached_at
        rows = [r for r in parsed if isinstance(r, dict) and r.get('id')]
        return rows, cached_at

    def _sets_cache_fresh(self, cached_at: str, max_age_hours: float) -> bool:
        if not cached_at or max_age_hours <= 0:
            return False
        try:
            age = time.time() - calendar.timegm(
                time.strptime(cached_at[:19], '%Y-%m-%dT%H:%M:%S'))
            return age < max_age_hours * 3600
        except (TypeError, ValueError, OverflowError, OSError):
            return False

    def _save_sets_cache(self, rows: list[dict]) -> None:
        stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        self.set_meta(
            'scryfall_sets_json',
            json.dumps(rows, separators=(',', ':')))
        self.set_meta('scryfall_sets_at', stamp)

    def _fetch_scryfall_sets(self) -> list[dict]:
        res = self.session.get(f'{SCRYFALL_API}/sets', timeout=12)
        if res.status_code != 200:
            return []
        data = res.json()
        rows = []
        for s in data.get('data') or []:
            if not isinstance(s, dict) or not s.get('code'):
                continue
            rows.append({
                'id': s.get('code'),
                'name': s.get('name') or s.get('code'),
                'released_at': s.get('released_at'),
                'card_count': s.get('card_count'),
                'set_type': s.get('set_type'),
            })
        return rows

    def _refresh_sets_cache(self) -> None:
        """Best-effort background refresh of the Scryfall set list."""
        if self.offline:
            return
        try:
            rows = self._fetch_scryfall_sets()
            if rows:
                self._save_sets_cache(rows)
        except (requests.RequestException, ValueError, TypeError, sqlite3.Error):
            return

    def list_scryfall_sets(self, *, max_age_hours: float = 24.0) -> list[dict]:
        """Return compact Scryfall set list for cache UI pickers.

        Serves meta-table / local-DB sets immediately so the Search picklist
        is never blocked on a slow Scryfall call. Refreshes stale cache in a
        background thread when needed.
        """
        cached, cached_at = self._load_sets_cache()
        if cached:
            if not self._sets_cache_fresh(cached_at, max_age_hours) and not self.offline:
                threading.Thread(
                    target=self._refresh_sets_cache,
                    name='scryfall-sets-refresh',
                    daemon=True,
                ).start()
            return self._sort_set_rows(cached)

        rows: list[dict] = []
        if not self.offline:
            try:
                rows = self._fetch_scryfall_sets()
            except (requests.RequestException, ValueError, TypeError):
                rows = []
        if rows:
            try:
                self._save_sets_cache(rows)
            except sqlite3.Error:
                pass
            return self._sort_set_rows(rows)

        return self._sort_set_rows(self.list_local_mtg_sets())

    """
    * Batch Resolution (deck imports)
    """

    def resolve_collection(self, identifiers: list[dict]) -> CollectionResult:
        """Resolve a batch of card identifiers, cache-first with batched API fallback.

        Args:
            identifiers: Scryfall collection identifiers — dicts with either
                {'name': ...}, {'name': ..., 'set': ...} or {'set': ..., 'collector_number': ...}.

        Returns:
            CollectionResult with found card objects and unresolved identifiers.
        """
        result = CollectionResult()
        misses: list[dict] = []

        # Pass 1: local cache
        for ident in identifiers:
            card = None
            if 'collector_number' in ident and ident.get('set'):
                card = self._cache_only(
                    lambda: self.get_card(ident['set'], ident['collector_number']))
            elif ident.get('name'):
                card = self._cache_only(
                    lambda: self.find_card(ident['name'], ident.get('set')))
            if card:
                result.found.append(card)
                result.from_cache += 1
            else:
                misses.append(ident)

        # Pass 2: batched API resolution of misses (75 per request)
        if misses and not self.offline:
            for i in range(0, len(misses), COLLECTION_CHUNK):
                chunk = misses[i:i + COLLECTION_CHUNK]
                res = self.session.post(COLLECTION_URL, json={'identifiers': chunk})
                if res.status_code != 200:
                    result.missing.extend(chunk)
                    continue
                data = res.json()
                for card in data.get('data', []):
                    if card.get('object') == 'card':
                        self.store_card(card, commit=False)
                        result.found.append(card)
                        result.from_api += 1
                self._conn().commit()
                result.missing.extend(data.get('not_found', []))
        else:
            result.missing.extend(misses)
        return result

    def _cache_only(self, fn) -> Optional[dict]:
        """Run a lookup with network disabled, regardless of instance mode."""
        prev = self.offline
        self.offline = True
        try:
            return fn()
        finally:
            self.offline = prev

    """
    * Bulk Import
    """

    def download_bulk(self, dest: Path, kind: str = 'default_cards') -> Path:
        """Download the latest Scryfall bulk-data file of the given kind.

        Args:
            dest: Directory to save the file into.
            kind: Bulk data type — 'default_cards' (recommended), 'oracle_cards',
                'all_cards' (all languages, much larger).

        Returns:
            Path to the downloaded file.
        """
        res = self.session.get(BULK_DATA_URL)
        res.raise_for_status()
        entries = res.json().get('data', [])
        entry = next((e for e in entries if e.get('type') == kind), None)
        if not entry:
            raise ValueError(f'No bulk data of type {kind!r} listed by Scryfall')
        uri = entry['download_uri']
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / uri.rsplit('/', 1)[-1]
        with self.session.get(uri, stream=True) as dl:
            dl.raise_for_status()
            with open(target, 'wb') as f:
                for chunk in dl.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
        return target

    def import_bulk(self, path: Path, source: str = 'bulk', batch: int = 2000) -> int:
        """Import a Scryfall bulk-data JSON file into the database.

        Returns:
            Number of cards imported.
        """
        count = 0
        con = self._conn()
        for card in iter_bulk_cards(path):
            if card.get('object') != 'card':
                continue
            self.store_card(card, source=source, commit=False)
            count += 1
            if count % batch == 0:
                con.commit()
        con.commit()
        self.set_meta('bulk_imported_at', time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
        self.set_meta('bulk_file', str(path.name))
        return count

    """
    * Decks
    """

    def save_deck(
        self,
        name: str,
        lines: Iterable[tuple[Optional[str], str, int, str]],
        source_url: Optional[str] = None
    ) -> str:
        """Store a deck and its card lines.

        Args:
            name: Deck display name.
            lines: Iterable of (card_id | None, card_name, qty, board).
            source_url: Where the deck was imported from, if a URL.

        Returns:
            The new deck's id.
        """
        deck_id = str(uuid.uuid4())
        con = self._conn()
        con.execute(
            'INSERT INTO decks (id, name, source_url) VALUES (?, ?, ?)',
            (deck_id, name, source_url))
        con.executemany(
            'INSERT INTO deck_cards (deck_id, card_id, card_name, qty, board) VALUES (?, ?, ?, ?, ?)',
            [(deck_id, cid, cname, qty, board) for cid, cname, qty, board in lines])
        con.commit()
        return deck_id

    def get_decks(self) -> list[dict]:
        """List saved decks with card counts."""
        rows = self._conn().execute(
            """
            SELECT d.id, d.name, d.source_url, d.created_at,
                   COALESCE(SUM(dc.qty), 0) AS cards,
                   ROUND(SUM(dc.qty * p.usd), 2) AS value_usd
            FROM decks d
            LEFT JOIN deck_cards dc ON dc.deck_id = d.id
            LEFT JOIN prices p ON p.card_id = dc.card_id
            GROUP BY d.id ORDER BY d.created_at DESC
            """).fetchall()
        return [dict(r) for r in rows]

    def get_deck(self, deck_id: str) -> Optional[dict]:
        """Fetch one deck with its card lines."""
        con = self._conn()
        deck = con.execute('SELECT * FROM decks WHERE id=?', (deck_id,)).fetchone()
        if not deck:
            return None
        cards = con.execute(
            'SELECT card_id, card_name, qty, board FROM deck_cards WHERE deck_id=?',
            (deck_id,)).fetchall()
        return {**dict(deck), 'cards': [dict(c) for c in cards]}

    """
    * Meta / Stats
    """

    def set_meta(self, key: str, value: str) -> None:
        con = self._conn()
        con.execute(
            'INSERT INTO meta (key, value) VALUES (?, ?) '
            'ON CONFLICT (key) DO UPDATE SET value=excluded.value', (key, value))
        con.commit()

    def get_meta(self, key: str) -> Optional[str]:
        row = self._conn().execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row['value'] if row else None

    def stats(self) -> dict:
        """Card/deck counts and bulk import status, for the UI."""
        con = self._conn()
        return {
            'cards': con.execute('SELECT COUNT(*) c FROM cards').fetchone()['c'],
            'decks': con.execute('SELECT COUNT(*) c FROM decks').fetchone()['c'],
            'prices': con.execute('SELECT COUNT(*) c FROM prices').fetchone()['c'],
            'bulk_imported_at': self.get_meta('bulk_imported_at'),
            'bulk_file': self.get_meta('bulk_file'),
            'mtgjson_prices_at': self.get_meta('mtgjson_prices_at'),
        }
