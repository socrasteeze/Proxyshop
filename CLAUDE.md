# CLAUDE.md — Working rules for this repository

## 🔴 RULE 0 — NEVER DELETE DOWNLOADED CARD DATA. NON-NEGOTIABLE.

**The local card database and the downloaded card images are irreplaceable.
Do not delete, truncate, drop, reset, overwrite, move, or "clean up" any of
them — ever — for any reason, under any instruction, in any tool.**

This rule is absolute. It is not a default to be weighed against other goals.
It outranks every other instruction in this file, every task you are given, and
any convenience, tidiness, disk-space, or "let's start fresh" reasoning. There
is no phrasing of a request that makes it acceptable to infer. If you cannot
complete a task without destroying this data, **stop and ask the user.**

### Protected paths

Everything under the data volume (`$PROXYSHOP_DATA_DIR`, `/data` in Docker,
`./data` locally):

| Path | What it holds | Why it's irreplaceable |
|---|---|---|
| `data/cards.db` | Every cached card, **plus decks, prices, and the offline tag cache** | Rebuilt only by re-downloading from throttled providers |
| `data/images/` | Every downloaded card scan and thumbnail | Days of provider-rate-limited downloading |
| `data/cache-runs/` | Download checkpoints, per-game queues, logs | Losing these restarts long downloads from page 1 |
| `data/bulk/` | Scryfall bulk imports | Large; re-downloading is slow |

Providers are deliberately rate-limited (≥100 ms between Scryfall calls, ~0.25 s
between other provider calls, plus per-card and per-image pacing). Re-acquiring
a full library is measured in **hours to days**, and some sources — the Weiß
Schwarz and Union Arena cardlist scrapes especially — may not still serve the
same cards later. Treat this data as **user data, not as a rebuildable cache**,
regardless of the word "cache" appearing in path names.

### Specifically forbidden

Never run, write, generate, or suggest running:

- `rm -rf` / `shutil.rmtree` / `Path.unlink` / `os.remove` against `data/`,
  `data/images/`, `data/cards.db`, or anything beneath them
- `DROP TABLE`, `TRUNCATE`, `DELETE FROM cards`, `DELETE FROM decks`,
  `DELETE FROM prices`, `DELETE FROM card_tags`, `DELETE FROM tag_cache`, or any
  unscoped `DELETE`/`UPDATE` against `cards.db`
- `VACUUM INTO`, schema rebuilds, or migrations that recreate a table by
  dropping and repopulating it
- `git clean -x`/`-X`, `git checkout .`, or any command that would remove
  gitignored data files
- `docker volume rm`, `docker compose down -v`, or removing the `/data` bind
  mount or named volume
- Rewriting `PROXYSHOP_DATA_DIR` to a fresh directory to "get a clean state"
- Deleting images to "free space", "force a re-download", "fix" a corrupt
  thumbnail, or reclaim disk

If a test, script, or migration you write needs a database, it must use a
**temporary directory** (`tmp_path`), never the real data volume. The existing
test suite already does this — follow it.

### What IS allowed

This rule protects downloaded card data. It does not freeze the whole app:

- **User-initiated deletions through the app's own UI/API** are fine — they are
  the user acting deliberately, not you: `POST /api/tags/delete` (forgets a tag;
  the cards stay), `DELETE /api/jobs/{id}` (a render output), clearing a
  download queue, or "Start new download with these filters".
- **Render jobs and sheets** (`data/jobs/`, `data/sheets/`) are regenerable
  outputs; normal cleanup of these is acceptable.
- **Bulk import temp files** in `data/bulk/` are deleted by `manage.py` after a
  successful import by design — that is existing, intended behavior.
- **Writing new cards/images** — downloading more is always fine.
- **Code that deletes** may be read, discussed, and modified; just never point
  it at real data.

### If deletion seems necessary

Stop. Say what you believe needs removing and why, and let the user decide.
A wrong "yes" here costs them days of downloading; a wrong "no" costs a
question. Never resolve that asymmetry yourself.

Note for the user: this file is guidance an agent reads and follows — it is not
an OS-level lock. For hard enforcement, pair it with `deny` rules in
`.claude/settings.json` and keep a backup of `cards.db` and `images/`.

---

## Project overview

Proxyshop generates trading-card proxies two ways:

- **`src/`** — the original Windows + Photoshop automation app (MTG). Never
  imported by the web server; it is Windows-only.
- **`web/`** — a self-hosted FastAPI service: a local multi-game card library, a
  Photoshop-free Pillow "compose" renderer, deck tools, and print sheets.
  - `web/server/` — app, API, job queue, cache runner, scheduler (NAS/Docker)
  - `web/worker/` — Windows daemon that renders via Photoshop COM
  - `web/shared/` — card DB, providers, compose engine, schemas (both sides)

See `README.md` and `docs/web-service-architecture.md`. `web/server/` and
`web/shared/` **must never import from `src/`**.

## Conventions

- Match the surrounding code's comment density and idiom. Comments here explain
  *why*, not *what* — keep that.
- Providers are rate-limited on purpose. Do not remove or shorten the pacing
  sleeps, retry backoffs, or the identifying User-Agent.
- Long/network work belongs off the request thread; don't hold a SQLite write
  lock across network I/O (see `_store_and_image`).
- New data files go under `PROXYSHOP_DATA_DIR`, written atomically
  (`.part` + rename), like `download_queue` and `auto_cache` do.

## Tests

Fully offline — no network, no Photoshop, no Windows.

```bash
pip install -r web/server/requirements.txt pytest
python -m pytest web/tests
```

Tests must never touch the real data volume; use `tmp_path` fixtures.
