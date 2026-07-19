# Proxyshop Web Service — Architecture & Setup Guide

Run Proxyshop as a self-hosted web service: a browser UI hosted on your NAS,
with optional Photoshop rendering on a Windows machine (or VM). The NAS also
runs a **Compose** engine (Pillow) so MTG / Pokémon / Riftbound proxies can be
previewed and downloaded without Windows.

Pages: **Editor** (`/`), **Card library** (`/gallery`), **Decks** (`/decks`),
**Search** (`/search`), **Logs** (`/logs`). See the
[README Web section](../README.md#-proxyshop-web-nas) for a short overview.

## Why this shape?

Proxyshop does not render cards itself — it remote-controls a real, installed
copy of Adobe Photoshop through Windows COM automation. That means:

- **A NAS can never render.** Photoshop only runs on Windows/macOS, and the
  automation bridge (`photoshop-python-api`) is Windows-only.
- **The web parts don't need Windows.** Queueing, card data, decklists, art
  uploads, and result downloads are plain web-app territory.

So the system splits in two:

```
 Browser ──HTTPS──▶ NAS (Docker)                        Windows PC/VM
                    ┌─────────────────────────┐          ┌─────────────────────────┐
                    │ FastAPI web app         │          │ proxyshop-worker daemon │
                    │ SQLite: jobs + card DB  │◀──poll───│ (in Proxyshop venv,     │
                    │ /data: art, results,    │  outbound│  logged-in session)     │
                    │        bulk card data   │──art────▶│ Photoshop via COM       │
                    └─────────────────────────┘◀─result──└─────────────────────────┘
              (both devices on the same Tailscale tailnet)
```

The worker only makes **outbound** HTTPS calls — no ports are opened on the
Windows machine. If the worker is offline, jobs simply wait in the queue.

## Components

| Path | Runs on | Purpose |
|---|---|---|
| `web/server/` | NAS (Docker) | Web UI, REST API, job queue, card DB, Compose engine, offline cache. Never imports `src/`. |
| `web/worker/` | Windows | Claims jobs, renders via the Proxyshop pipeline, uploads results. |
| `web/shared/` | Both | API schemas, card database, decklist parsing, Compose (Pillow). |

### Job lifecycle

`queued → claimed → rendering → done | failed`

- Claiming is a single atomic SQLite transaction — safe with multiple workers.
- If a worker dies mid-job, the lease expires (15 min) and the job re-queues.
- Each job gets at most 2 attempts, then fails with its error message shown in the UI.
- Duplicate submissions are prevented with idempotency keys (the browser form
  generates one per page load).

### Local card database ("offline Scryfall")

All card data flows through `web/shared/carddb.py`, an SQLite cache:

- **Fetch-through**: the first lookup of a card hits Scryfall and stores the
  full card object; every later lookup is instant and offline.
- **Bulk import**: download Scryfall's nightly bulk file once (~450MB) and
  virtually every Magic card ever printed resolves locally:

  ```bash
  docker compose -f web/server/docker-compose.yml exec proxyshop-web \
      python -m web.server.manage bulk-download
  ```

- **Deck import**: paste a decklist (plain / MTGA / MTGO formats) or a public
  Moxfield / Archidekt URL on the *Decks* page — every card is resolved
  (cache first, then Scryfall's batch endpoint at 75 cards per request) and
  saved as a named deck.
- **Browser search with live fallback**: the *Card Search* page and the
  render form's autocomplete search the local DB first and fall back to a
  live Scryfall search when nothing matches — fallback results are cached,
  so the database grows with every search.
- **High-quality images**: full-card scans (745×1040 PNG) and art crops are
  fetched on demand and cached under `/data/images/` — each image downloads
  exactly once.
- **Multi-game search ("self-hosted Scryfall")**: the *Card Search* page is a
  visual card browser — pick a game, search, see a grid of card images, click
  through to a detail view with the full-size scan, attributes, prices, and
  HQ download buttons. Supported games: **MTG** (Scryfall),
  **Pokémon** ([pokemontcg.io](https://pokemontcg.io) — works keyless; a free
  key in `PROXYSHOP_POKEMONTCG_KEY` raises rate limits), **Union Arena**
  ([official NA + JP cardlists](https://www.unionarena-tcg.com/na/cardlist/) —
  no key; English and Japanese printings; card images are Bandai 600×837 PNGs),
  and
  **Riftbound** ([Riftcodex](https://riftcodex.com/) — public REST API, no key;
  official Riot 744×1039 arts; DotGG supplies Chinese Arcane Box promos; official
  JA/KO gallery names enrich search). Note: JP/CN *language card faces* are not
  separately published — EN/JA share the same art URLs.
  Everything found online is cached locally, so the
  browser works offline for anything you've seen before. Photoshop rendering
  supports **MTG** (and **Pokémon** when PSDs are installed on the Windows
  worker). **Pokémon** and **Riftbound** also support **Compose** mode — a
  pokecardmaker-style Pillow renderer that runs on the NAS with no Photoshop
  (procedural frames, or drop blank PNGs into `web/shared/compose/frames/`).
  Union Arena remains search/image only.
- **Full TCG cache (NAS)**: small games can be mirrored into the local DB +
  `/data/images/` with stop/resume:

  ```bash
  # Start / resume (Ctrl+C or --stop pauses after the current card)
  docker exec -it proxyshop-web \
      python -m web.server.manage cache-game --game riftbound

  # From another shell: ask the running job to stop
  docker exec proxyshop-web \
      python -m web.server.manage cache-game --game riftbound --stop

  docker exec proxyshop-web \
      python -m web.server.manage cache-game --game riftbound --status
  ```

  Or use **Search → Offline cache** in the browser (same stop/resume
  checkpoints under `/data/cache-runs/`). **MTG** and **Pokémon** require
  filters (set, type, rarity, art/frame flags, tags, regulation, …) so the
  cache does not pull an entire game; Riftbound/Union Arena can still mirror
  their full catalogs. Examples:

  ```bash
  docker exec -it proxyshop-web python -m web.server.manage cache-game \
      --game mtg --set mh3 --art showcase,borderless
  docker exec -it proxyshop-web python -m web.server.manage cache-game \
      --game pokemon --set sv3 --type Fire
  ```

  Bulk cache is rate-limited by default (provider spacing ≈0.35s, page gap
  ≈0.75s, per-card/image gap ≈0.4s) and retries HTTP 429 with `Retry-After`.
  Tune with env vars: `PROXYSHOP_PROVIDER_INTERVAL`,
  `PROXYSHOP_CACHE_PROVIDER_INTERVAL`, `PROXYSHOP_CACHE_PAGE_INTERVAL`,
  `PROXYSHOP_CACHE_CARD_INTERVAL`, `PROXYSHOP_CACHE_IMAGE_INTERVAL`.

  Re-run the start command to resume from the checkpoint under
  `/data/cache-runs/`. Use `--fresh` to start over, `--images-only` to fill
  missing images for cards already in the DB. Riftbound uses public Riftcodex
  (no key) plus DotGG Arcane Box promos. Full MTG dumps still use
  `manage bulk-download`.

  **Cache UI:** Search → Offline cache shows a header with Download / Resume /
  Stop, an inline run log, job chips for other games, and Advanced → Start over.
  The nav **Search** link shows a badge while any cache job is running; the
  **Logs** page (`/logs`) is a full live viewer with game switcher. APIs:
  `GET /api/cache-jobs`, `GET /api/cache-game/{game}/log`.
- **Card library (`/gallery`)**: browse the local DB with list/grid views,
  unique-vs-combine arts, per-page size, and a card popover (detail + open in
  editor). Cross-game FTS search is shared with Search.
- **Art-less rendering**: submitting an MTG render job without an art upload
  automatically uses the card's Scryfall art crop as the render input.
  Compose-mode Pokémon/Riftbound jobs can use the cached HQ scan as art when
  no upload is provided. Photoshop Pokémon jobs still prefer an art upload.
- **Dual-path rendering** (`render_mode`):
  - `auto` — MTG → Photoshop worker; Pokémon/Riftbound → NAS compose
  - `compose` — Pillow blank-frame compositor on the NAS for **MTG, Pokémon,
    and Riftbound** (no Windows needed)
  - `photoshop` — queue for the Windows worker + PSD templates

  **Make / Editor (`/`)** — primary art-replace workflow:
  1. Pick an existing printing (search, gallery, or `?card_id=`).
  2. Optionally upload replacement art — **frame, name, rules, set, and other
     details stay the same**; only the art window changes.
  3. Pan/zoom the art; preview with Compose; download PNG or queue (Compose or
     Photoshop). Queue accepts the same `card_json` + `art_transform` as the
     live preview so results match.

  Also on Make: blank card, JSON import/export, compose frame styles
  (default / borderless / fullart / wide by game), layer toggles (art / text /
  footer), `{W}`/`{T}` symbol expansion in Compose text, and optional print
  bleed padding. Legacy `/edit?card_id=…` redirects here.

  Optional blank PNGs go under `web/shared/compose/frames/` (see README there);
  otherwise procedural tinted frames are generated.
- **Print prep**: each saved deck has *Download images* — a ZIP of unique HQ
  scans plus a `decklist.txt` manifest, ready for
  [Proxxied](https://proxxied.com/) or any print-prep tool — and *PDF sheet*,
  a built-in quick layout (63×88mm cards, 3×3 per page, 300 DPI, cut guides).
- Scryfall etiquette is built in: identifying User-Agent, ≥100ms between
  requests, honoring `429 Retry-After`, bulk files preferred over API calls.
- Set `PROXYSHOP_OFFLINE=1` to forbid all live Scryfall calls.
- **Prices**: every cached card's Scryfall prices (USD/EUR) are stored
  automatically and shown on the search page and as estimated deck values.
  For richer aggregated paper prices (TCGplayer/Cardmarket via
  [MTGJSON](https://mtgjson.com)), run:

  ```bash
  docker compose -f web/server/docker-compose.yml exec proxyshop-web \
      python -m web.server.manage mtgjson-prices
  ```

  MTGJSON files are large; the importer stream-parses them, so it stays
  NAS-friendly. Schedule it weekly alongside `bulk-download` if you want
  fresh prices.

Desktop Proxyshop can share the same cache: set `PROXYSHOP_CARD_CACHE=cache_first`
(and optionally `PROXYSHOP_CARD_DB=path/to/cards.db`) before launching, and
`get_card_data` consults the local DB before touching the API.

### Secrets & local data (do not commit)

Keep these **off** the git tree — they can expose your NAS, GitHub account, or
worker:

| Item | Where it should live |
|---|---|
| GitHub PAT | `~/.gh-token` (`chmod 600`) — never in the repo |
| Worker token | `PROXYSHOP_WORKER_TOKEN` / `~/.proxyshop-worker-token` |
| Provider keys | e.g. `~/.proxyshop-pokemontcg-key` |
| Runtime DB / images / jobs | Docker volume or local `./data/` (gitignored) |
| `.env` | Local only (already gitignored) |

Docs use placeholders (`<nas>`, `<token>`, `YOUR_POKEMONTCG_KEY`). Do not paste
real hostnames, IPs, passwords, or tokens into README/CHANGELOG commits.

### API hygiene

- Rate limits on submissions (20/min), deck imports (6/min) and API reads
  (120/min) per client, with `Retry-After` on 429.
- Upload size cap (default 50MB, `PROXYSHOP_MAX_UPLOAD_MB`), art file-type
  allowlist, strict pydantic validation.
- Worker endpoints require a bearer token (`PROXYSHOP_WORKER_TOKEN`).

---

## Part 1 — NAS setup (Docker)

1. Clone this repository onto the NAS (or just copy the `web/` folder).
2. Pick a worker token and start the stack from the repo root:

   ```bash
   PROXYSHOP_WORKER_TOKEN=$(openssl rand -hex 24)
   echo "Worker token: $PROXYSHOP_WORKER_TOKEN"   # save this for the Windows setup
   docker compose -f web/server/docker-compose.yml up -d --build
   ```

3. Open `http://<nas-ip>:8000` — the UI should load with "No workers registered yet."
4. (Recommended) Import the card database:

   ```bash
   docker compose -f web/server/docker-compose.yml exec proxyshop-web \
       python -m web.server.manage bulk-download
   ```

5. (Optional) Schedule a weekly refresh with your NAS's task scheduler /cron:

   ```
   docker compose -f /path/to/Proxyshop/web/server/docker-compose.yml \
       exec -T proxyshop-web python -m web.server.manage bulk-download
   ```

All state lives in the `proxyshop-data` Docker volume (`/data` in the
container): `jobs.db`, `cards.db`, and per-job art/results under `/data/jobs/`.

### Alternative: one-command deploy with `nas-update.sh`

If your NAS has no git (TerraMaster TOS, Synology DSM), use the bundled
`nas-update.sh` instead of the compose flow above. It fetches a source
snapshot from GitHub over HTTPS with a personal access token, rebuilds the
image, restarts the container, and health-checks it.

One-time setup on the NAS:

```sh
# GitHub -> Settings -> Developer settings -> PAT (classic, `repo` scope)
echo "<your_token>" > ~/.gh-token && chmod 600 ~/.gh-token
# Copy nas-update.sh onto the NAS once (scp or paste), then:
sh nas-update.sh
```

The first run generates the worker token at `~/.proxyshop-worker-token`
(printed once — use it on the Windows machine), creates the data directory
(`/Volume1/proxyshop/data`, bind-mounted as `/data`), installs the code to
`~/proxyshop-web`, and starts the container.

**Provider API keys** live in `$HOME` files (never in the code tree — the
script overwrites itself on update). Create them once, then **re-run the
update script** so the container restarts with them injected. Writing the
file alone does not update a running container.

```sh
# Optional — raises pokemontcg.io rate limits (free key from https://dev.pokemontcg.io):
# Union Arena (official cardlist) and Riftbound (Riftcodex) need no key.
echo 'YOUR_POKEMONTCG_KEY' > ~/.proxyshop-pokemontcg-key
chmod 600 ~/.proxyshop-pokemontcg-key
# Required: restart the container with the key
sh ~/proxyshop-web/nas-update.sh
# Confirm: curl -s http://127.0.0.1:8000/api/health | grep pokemontcg
```

After that, refresh from your Windows desktop with one command —
`nas-refresh.bat` (edit `NAS_HOST` at the top once; add an SSH key for a
passwordless run):

```
ssh-keygen -t ed25519
type %USERPROFILE%\.ssh\id_ed25519.pub | ssh <user>@<nas> "cat >> ~/.ssh/authorized_keys"
nas-refresh.bat
```

Notes:
- The script deploys from the branch named in its config block (`main` by
  default).
- TerraMaster mounts live under `/Volume1` (capital V), Synology under
  `/volume1` — the script defaults to TerraMaster; check with `df -h`.
- `PermissionError` on `/data` means the container user doesn't match the
  volume owner: check `ls -n /Volume1/proxyshop` and adjust
  `CONTAINER_USER` in the script.
- Test the fetch/install path on any Linux box with `DRY_RUN=1 sh nas-update.sh`.

## Part 2 — Remote access (Tailscale, recommended)

Do **not** port-forward the app to the open internet. Instead:

1. Install [Tailscale](https://tailscale.com) on the NAS, the Windows render
   machine, and your phone/laptop (free tier covers this).
2. Reach the UI from anywhere at `http://<nas-tailscale-name>:8000`.

This gives you WireGuard-encrypted access with zero exposed ports. If you
later want to share with friends outside your tailnet, put Cloudflare Tunnel +
Access (or a reverse proxy with authentication) in front — but treat that as a
separate hardening project.

## Part 3 — Windows render machine setup

Requirements: Windows 10/11, Photoshop 2017–2024 installed and launched at
least once, Python 3.10–3.12.

1. **Install Proxyshop from source** (see the main README "Setup Guide
   (Python Environment)") and run the GUI once to download the templates and
   install the fonts you plan to use.
2. **Test the render spike** (M0) — from the Proxyshop repo root:

   ```powershell
   # Put a test art file somewhere, e.g. "Lightning Bolt [STA].png"
   python -m web.worker.daemon --server http://<nas>:8000 --token <token> --once
   ```

   Submit a job from the browser and watch it render. Common first-run issues:
   Photoshop showing a modal dialog (dismiss it), missing fonts (install from
   `fonts/`), template not downloaded (run the GUI updater).
3. **Run it permanently** — Task Scheduler:
   - Create a task "Proxyshop Worker" → *Run only when user is logged on*
     (Photoshop COM automation requires an interactive session).
   - Trigger: *At log on*.
   - Action: `python.exe` with arguments
     `-m web.worker.daemon --server http://<nas>:8000 --token <token>`,
     *Start in*: the Proxyshop folder.
   - Settings: restart on failure every 1 minute.
   - Configure the machine to auto-login and not lock/sleep. A dedicated
     Windows VM (Synology VMM / QNAP Virtualization Station / Unraid, 8GB+ RAM)
     works well; Photoshop renders fine software-only for this workload.

Environment variables can replace the CLI flags: `PROXYSHOP_SERVER_URL`,
`PROXYSHOP_WORKER_TOKEN`, `PROXYSHOP_WORKER_NAME`.

### Worker behavior

- On startup the worker sends a **capabilities handshake** — the template
  dropdown in the UI always reflects what this worker can actually render
  (only installed templates are offered).
- A **watchdog** enforces a 10-minute per-job ceiling; a hung Photoshop is
  force-restarted and the job fails with a clear error. Photoshop is also
  proactively restarted every 25 renders to head off memory creep.
- Heartbeats every 30s let the server show online/offline status and requeue
  jobs from dead workers.
- `PROXYSHOP_NONINTERACTIVE=1` is set automatically so Proxyshop never blocks
  on a console prompt.

## Development without Windows

The whole stack minus real rendering runs anywhere:

```bash
pip install -r web/server/requirements.txt pytest
python -m pytest web/tests                            # offline test suite
uvicorn web.server.app:app --port 8000                # server
python -m web.worker.daemon --fake                    # worker with placeholder PNGs
```

The `--fake` worker exercises the full job lifecycle (claim → render →
upload → download) with generated placeholder images.

## Known limitations & risks

- **Photoshop COM stability** is the weakest link: busy dialogs, RPC errors,
  and scratch-disk exhaustion happen under long automation. The watchdog
  mitigates but can't eliminate this — expect an occasional failed job that
  succeeds on retry.
- The render machine must stay **logged in and unlocked** (auto-login VM
  recommended). Renders degrade or fail in detached/locked sessions.
- **Licensing**: automating your own Photoshop for personal use is fine. Do
  not operate this as a public rendering service — both Adobe's terms and
  Wizards' Fan Content Policy apply at that scale.
- Multi-worker mostly works (atomic claims, per-worker capabilities), but the
  UI currently shows one merged template list; treat multi-worker as
  experimental.

## API summary

Interactive docs live at `/api/docs`. Key endpoints:

| Endpoint | Method | Auth | Purpose |
|---|---|---|---|
| `/api/jobs` | POST | — | Submit render job (multipart: fields + optional art, `card_json`, `art_transform`) |
| `/api/jobs/{id}` | GET | — | Job status/detail |
| `/api/jobs/{id}/result` | GET | — | Download rendered PNG |
| `/api/compose` | POST | — | Live Compose preview/download (card_json + optional art / art_transform / bleed_px) |
| `/api/templates` | GET | — | Template options (from worker handshake) |
| `/api/cards/search?q=` | GET | — | Local card DB search (+ live fallback) |
| `/api/cards/gallery` | GET | — | Card library listing |
| `/api/decks/import` | POST | — | Import decklist text or Moxfield/Archidekt URL |
| `/api/decks/{id}/images` | GET | — | ZIP of HQ card scans + decklist manifest |
| `/api/sheets` | POST | — | Compile proxy sheet PDF (deck_id or text, paper=letter/a4) |
| `/api/sheets/{id}` | GET | — | Download compiled PDF |
| `/api/cache-game/{game}` | GET | — | Offline-cache status for one game |
| `/api/cache-game/{game}/start` | POST | — | Start/resume selective or full cache |
| `/api/cache-game/{game}/stop` | POST | — | Cooperative stop |
| `/api/cache-game/{game}/log` | GET | — | Recent cache-run log lines |
| `/api/cache-jobs` | GET | — | Status for all catalog-game cache jobs |
| `/api/worker/hello` | POST | Bearer | Capabilities handshake |
| `/api/worker/jobs/next?wait=25` | GET | Bearer | Long-poll claim |
| `/api/worker/jobs/{id}/art` | GET | Bearer | Download job art |
| `/api/worker/jobs/{id}/result` | POST | Bearer | Report result (+ PNG upload) |
| `/api/health` | GET | — | Health/stats |
