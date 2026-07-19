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
    source TEXT NOT NULL DEFAULT 'api'
);
CREATE INDEX IF NOT EXISTS idx_cards_set_num ON cards (set_code, collector_number, lang);
CREATE INDEX IF NOT EXISTS idx_cards_name ON cards (name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_cards_oracle ON cards (oracle_id);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
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
            con.executescript(SCHEMA)
            con.execute('PRAGMA journal_mode=WAL')

    @property
    def session(self) -> ScryfallSession:
        if self._session is None:
            self._session = ScryfallSession()
        return self._session

    def _conn(self) -> sqlite3.Connection:
        """Per-thread connection (SQLite objects can't cross threads)."""
        con = getattr(self._local, 'con', None)
        if con is None:
            con = sqlite3.connect(self.path)
            con.row_factory = sqlite3.Row
            con.execute('PRAGMA foreign_keys=ON')
            self._local.con = con
        return con

    def close(self) -> None:
        con = getattr(self._local, 'con', None)
        if con is not None:
            con.close()
            self._local.con = None

    """
    * Storage
    """

    def store_card(self, card: dict, source: str = 'api', commit: bool = True) -> None:
        """Insert or update a Scryfall card object."""
        con = self._conn()
        con.execute(
            """
            INSERT INTO cards (id, oracle_id, name, set_code, collector_number,
                               lang, released_at, json, fetched_at, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)
            ON CONFLICT (id) DO UPDATE SET
                oracle_id=excluded.oracle_id, name=excluded.name,
                set_code=excluded.set_code, collector_number=excluded.collector_number,
                lang=excluded.lang, released_at=excluded.released_at,
                json=excluded.json, fetched_at=excluded.fetched_at, source=excluded.source
            """, (
                card['id'],
                card.get('oracle_id'),
                card.get('name', ''),
                (card.get('set') or '').lower(),
                str(card.get('collector_number', '')),
                card.get('lang', 'en'),
                card.get('released_at'),
                json.dumps(card, separators=(',', ':')),
                source
            ))
        if commit:
            con.commit()

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

    def search_local(self, text: str, limit: int = 50) -> list[dict]:
        """Substring name search against the local DB only (no network).

        Returns one row per name+set (deduplicated by printing), newest first.
        """
        con = self._conn()
        rows = con.execute(
            """
            SELECT json FROM cards
            WHERE name LIKE ? COLLATE NOCASE AND lang='en'
            ORDER BY name COLLATE NOCASE ASC, released_at DESC
            LIMIT ?
            """, (f'%{text}%', int(limit))).fetchall()
        return [json.loads(r['json']) for r in rows]

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
                   COALESCE(SUM(dc.qty), 0) AS cards
            FROM decks d LEFT JOIN deck_cards dc ON dc.deck_id = d.id
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
            'bulk_imported_at': self.get_meta('bulk_imported_at'),
            'bulk_file': self.get_meta('bulk_file'),
        }
