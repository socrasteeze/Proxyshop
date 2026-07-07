"""
* Job Store
* SQLite-backed render job queue with claim/lease/requeue semantics.
* Single-writer friendly (WAL); claiming is one atomic UPDATE...RETURNING.
"""
# Standard Library Imports
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Optional, Union

# Local Imports
from web.shared.schema import Job, JobStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'queued',
    card_name TEXT NOT NULL,
    set_code TEXT,
    collector_number TEXT,
    template_name TEXT,
    lang TEXT NOT NULL DEFAULT 'en',
    card_json TEXT,
    art_filename TEXT,
    result_filename TEXT,
    error TEXT,
    log TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    idempotency_key TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    claimed_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idem ON jobs (idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS workers (
    name TEXT PRIMARY KEY,
    last_seen TEXT NOT NULL DEFAULT (datetime('now')),
    capabilities TEXT
);
"""

# Requeue jobs whose worker went silent for this long.
LEASE_MINUTES = 15

# Max render attempts before a job is failed permanently.
MAX_ATTEMPTS = 2


class JobStore:
    """SQLite job queue for render jobs."""

    def __init__(self, path: Union[str, Path]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self._conn() as con:
            con.executescript(SCHEMA)
            con.execute('PRAGMA journal_mode=WAL')

    def _conn(self) -> sqlite3.Connection:
        con = getattr(self._local, 'con', None)
        if con is None:
            con = sqlite3.connect(self.path)
            con.row_factory = sqlite3.Row
            self._local.con = con
        return con

    @staticmethod
    def _to_job(row: sqlite3.Row) -> Job:
        d = {k: row[k] for k in row.keys() if k != 'idempotency_key'}
        return Job(**d)

    """
    * Submission
    """

    def submit(
        self,
        card_name: str,
        set_code: Optional[str] = None,
        collector_number: Optional[str] = None,
        template_name: Optional[str] = None,
        lang: str = 'en',
        card_json: Optional[str] = None,
        art_filename: Optional[str] = None,
        idempotency_key: Optional[str] = None
    ) -> Job:
        """Queue a new render job.

        If an idempotency key is supplied and a job with that key already
        exists, the existing job is returned instead of creating a duplicate.
        """
        con = self._conn()
        if idempotency_key:
            row = con.execute(
                'SELECT * FROM jobs WHERE idempotency_key=?', (idempotency_key,)).fetchone()
            if row:
                return self._to_job(row)
        job_id = str(uuid.uuid4())
        con.execute(
            """
            INSERT INTO jobs (id, card_name, set_code, collector_number,
                              template_name, lang, card_json, art_filename, idempotency_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (job_id, card_name, set_code, collector_number,
             template_name, lang, card_json, art_filename, idempotency_key))
        con.commit()
        return self.get(job_id)

    """
    * Worker Lifecycle
    """

    def claim_next(self, worker: str) -> Optional[Job]:
        """Atomically claim the oldest queued job for a worker."""
        con = self._conn()
        self.requeue_stale()
        row = con.execute(
            """
            UPDATE jobs SET status='claimed', claimed_at=datetime('now'),
                            attempts=attempts + 1
            WHERE id = (
                SELECT id FROM jobs WHERE status='queued'
                ORDER BY created_at ASC LIMIT 1)
            RETURNING *
            """).fetchone()
        con.commit()
        if row:
            self.touch_worker(worker)
        return self._to_job(row) if row else None

    def set_status(self, job_id: str, status: JobStatus) -> None:
        con = self._conn()
        con.execute('UPDATE jobs SET status=? WHERE id=?', (status.value, job_id))
        con.commit()

    def set_art(self, job_id: str, art_filename: str) -> None:
        """Record the stored art filename for a job (first write wins)."""
        con = self._conn()
        con.execute(
            'UPDATE jobs SET art_filename=? WHERE id=? AND art_filename IS NULL',
            (art_filename, job_id))
        con.commit()

    def finish(
        self,
        job_id: str,
        ok: bool,
        result_filename: Optional[str] = None,
        error: Optional[str] = None,
        log: Optional[str] = None
    ) -> None:
        """Record a job result from the worker."""
        con = self._conn()
        if ok:
            con.execute(
                """
                UPDATE jobs SET status='done', result_filename=?, log=?,
                                error=NULL, finished_at=datetime('now')
                WHERE id=?
                """, (result_filename, log, job_id))
        else:
            # Retry if attempts remain, else fail permanently
            con.execute(
                """
                UPDATE jobs SET
                    status = CASE WHEN attempts >= ? THEN 'failed' ELSE 'queued' END,
                    error=?, log=?,
                    finished_at = CASE WHEN attempts >= ? THEN datetime('now') ELSE NULL END,
                    claimed_at = NULL
                WHERE id=?
                """, (MAX_ATTEMPTS, error, log, MAX_ATTEMPTS, job_id))
        con.commit()

    def requeue_stale(self) -> int:
        """Requeue claimed/rendering jobs whose lease expired (dead worker).

        Jobs that already burned MAX_ATTEMPTS fail instead of requeueing.
        """
        con = self._conn()
        cur = con.execute(
            f"""
            UPDATE jobs SET
                status = CASE WHEN attempts >= ? THEN 'failed' ELSE 'queued' END,
                error = CASE WHEN attempts >= ? THEN 'Worker lease expired' ELSE error END,
                claimed_at = NULL
            WHERE status IN ('claimed', 'rendering')
              AND claimed_at < datetime('now', '-{LEASE_MINUTES} minutes')
            """, (MAX_ATTEMPTS, MAX_ATTEMPTS))
        con.commit()
        return cur.rowcount

    """
    * Workers
    """

    def touch_worker(self, name: str, capabilities: Optional[str] = None) -> None:
        """Record a worker heartbeat (and optionally its capabilities JSON)."""
        con = self._conn()
        if capabilities is not None:
            con.execute(
                """
                INSERT INTO workers (name, last_seen, capabilities)
                VALUES (?, datetime('now'), ?)
                ON CONFLICT (name) DO UPDATE SET
                    last_seen=datetime('now'), capabilities=excluded.capabilities
                """, (name, capabilities))
        else:
            con.execute(
                """
                INSERT INTO workers (name, last_seen) VALUES (?, datetime('now'))
                ON CONFLICT (name) DO UPDATE SET last_seen=datetime('now')
                """, (name,))
        con.commit()

    def get_workers(self) -> list[dict]:
        rows = self._conn().execute(
            """
            SELECT name, last_seen, capabilities,
                   (last_seen >= datetime('now', '-2 minutes')) AS online
            FROM workers ORDER BY last_seen DESC
            """).fetchall()
        return [dict(r) for r in rows]

    def get_capabilities(self) -> Optional[str]:
        """Capabilities JSON from the most recently seen worker."""
        row = self._conn().execute(
            """
            SELECT capabilities FROM workers
            WHERE capabilities IS NOT NULL ORDER BY last_seen DESC LIMIT 1
            """).fetchone()
        return row['capabilities'] if row else None

    """
    * Queries
    """

    def get(self, job_id: str) -> Optional[Job]:
        row = self._conn().execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        return self._to_job(row) if row else None

    def list_jobs(self, limit: int = 50) -> list[Job]:
        rows = self._conn().execute(
            'SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?', (int(limit),)).fetchall()
        return [self._to_job(r) for r in rows]
