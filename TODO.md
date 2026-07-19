# Offline-Cache UX Improvements

## Backend

- [x] **cache_runner.py** — log capture + all_status
  - [x] Add module-level `_logs: dict[str, deque]` with `deque(maxlen=400)` per game
  - [x] Implement `log(game, runs_dir, *parts)` — append to deque + file (rotate at 512 KB)
  - [x] Implement `log_lines(game, runs_dir, limit=200)` — serve from deque or tail-read file
  - [x] Implement `all_status(*, db, runs_dir)` — status dict for all catalog games
  - [x] Replace `print_fn=lambda *a, **k: None` in `start()._target` with `print_fn=lambda *a, **k: log(...)`
  - [x] Add lifecycle log lines: run started, run crashed, stop requested

- [x] **game_cache.py** — richer progress messages
  - [x] Add `print_fn` param to `_store_and_image()` (default print); `_run_images_only` already had it
  - [x] Emit `!! image failed: {name} ({id})` on image failures
  - [x] Pass `print_fn` through from `_run_catalog`

- [x] **app.py** — endpoints + Logs page
  - [x] Add `GET /api/cache-jobs` → `{'jobs': {}, 'any_running': bool}`
  - [x] Add `GET /api/cache-game/{game}/log?limit=200` → `{'game', 'lines'}`
  - [x] Add `GET /logs` route → `page_logs()` rendering `templates/logs.html`

## Frontend

- [x] **search.html** — restructure cache panel
  - [x] Replace `<h2>Offline cache</h2>` with `.panel-header` (h2 + button row on right)
  - [x] Rename `#cache-start` button to **"Download cards"**; small TCGs say "Download all cards"
  - [x] Move `#cache-start`, `#cache-resume`, `#cache-stop` into header `.btn-row`
  - [x] Delete old `.btn-row` at lines 137–142
  - [x] Add `<div id="cache-jobs" class="jobs-strip" hidden>` below header (job chips)
  - [x] Add inline log viewer: `<details id="cache-log-wrap">` with `<pre id="cache-log">` below status
  - [x] Add collapsed `<details class="cache-advanced">` with "Start over from scratch" button
  - [x] Implement `refreshLog()` — fetch `/api/cache-game/{g}/log`, join lines, auto-scroll
  - [x] Implement localStorage persist for game picklist (`proxyshop.search.game`)

- [x] **logs.html** (new)
  - [x] Create dedicated Logs page with full-height log viewer
  - [x] Add game switcher (pills or select)
  - [x] Poll `/api/cache-game/{game}/log` every 3 s while running
  - [x] Show jobs summary strip at top

- [x] **base.html** — nav + badge
  - [x] Add "Logs" link in nav (or add to existing tabs)
  - [x] Add `<span id="nav-cache-badge" class="nav-badge" hidden>` in Search link

- [x] **app.js** — shared helpers
  - [x] Add `wireCacheBadge()` — fetch `/api/cache-jobs` on load, re-poll every 10 s while running
  - [x] Add `renderJobChips(container, jobs, onSelect)` helper (used by search.html + logs.html)

- [x] **app.css** — new styles
  - [x] `.jobs-strip` — flex row, wrap, gap
  - [x] `.job-chip` — pill button, running variant with accent
  - [x] `.cache-log` — monospace, max-height 16rem, overflow auto
  - [x] `.cache-advanced` — collapsible section styling
  - [x] `.nav-badge` — accent pill badge

## Verification

Run: `python -m uvicorn web.server.app:app --port 8000`

- [ ] Start small Riftbound cache → inline log shows timestamped events; `curl /api/cache-game/riftbound/log` works
- [ ] Jobs strip shows running job; clicking chip switches picklist
- [ ] Nav badge appears while running, clears when idle
- [ ] Logs tab shows live-updating log with game switcher
- [ ] Pokémon selected → hard refresh → still Pokémon (localStorage)
- [ ] Filter toggles → "Download cards" button doesn't move
- [ ] "Start over" only in collapsed Advanced section; confirm warns about restart
- [ ] Stop/resume works; errors appear in status + log
