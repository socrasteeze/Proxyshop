# Offline-Cache UX Improvements

Ref: [Plan](C:\Users\socrasteeze\.claude\plans\for-this-repo-there-s-tingly-koala.md)

## Backend

- [ ] **cache_runner.py** — log capture + all_status
  - [ ] Add module-level `_logs: dict[str, deque]` with `deque(maxlen=400)` per game
  - [ ] Implement `log(game, runs_dir, *parts)` — append to deque + file (rotate at 512 KB)
  - [ ] Implement `log_lines(game, runs_dir, limit=200)` — serve from deque or tail-read file
  - [ ] Implement `all_status(*, db, runs_dir)` — status dict for all catalog games
  - [ ] Replace `print_fn=lambda *a, **k: None` in `start()._target` with `print_fn=lambda *a, **k: log(...)`
  - [ ] Add lifecycle log lines: run started, run crashed, stop requested

- [ ] **game_cache.py** — richer progress messages
  - [ ] Add `print_fn` param to `_store_and_image()` and `_run_images_only()` (default no-op)
  - [ ] Emit `!! image failed: {name} ({id})` on image failures
  - [ ] Pass `print_fn` through from `_run_catalog` and `run_cache_game`

- [ ] **app.py** — endpoints + Logs page
  - [ ] Add `GET /api/cache-jobs` → `{'jobs': {}, 'any_running': bool}`
  - [ ] Add `GET /api/cache-game/{game}/log?limit=200` → `{'game', 'lines'}`
  - [ ] Add `GET /logs` route → `page_logs()` rendering `templates/logs.html`

## Frontend

- [ ] **search.html** — restructure cache panel
  - [ ] Replace `<h2>Offline cache</h2>` with `.panel-header` (h2 + button row on right)
  - [ ] Rename `#cache-start` button to **"Download cards"**; small TCGs say "Download all cards"
  - [ ] Move `#cache-start`, `#cache-resume`, `#cache-stop` into header `.btn-row`
  - [ ] Delete old `.btn-row` at lines 137–142
  - [ ] Add `<div id="cache-jobs" class="jobs-strip" hidden>` below header (job chips)
  - [ ] Add inline log viewer: `<details id="cache-log-wrap">` with `<pre id="cache-log">` below status
  - [ ] Add collapsed `<details class="cache-advanced">` with "Start over from scratch" button
  - [ ] Implement `refreshLog()` — fetch `/api/cache-game/{g}/log`, join lines, auto-scroll
  - [ ] Implement localStorage persist for game picklist (`proxyshop.search.game`)

- [ ] **logs.html** (new)
  - [ ] Create dedicated Logs page with full-height log viewer
  - [ ] Add game switcher (pills or select)
  - [ ] Poll `/api/cache-game/{game}/log` every 3 s while running
  - [ ] Show jobs summary strip at top

- [ ] **base.html** — nav + badge
  - [ ] Add "Logs" link in nav (or add to existing tabs)
  - [ ] Add `<span id="nav-cache-badge" class="nav-badge" hidden>` in Search link

- [ ] **app.js** — shared helpers
  - [ ] Add `wireCacheBadge()` — fetch `/api/cache-jobs` on load, re-poll every 10 s while running
  - [ ] Add `renderJobChips(container, jobs, onSelect)` helper (used by search.html + logs.html)

- [ ] **app.css** — new styles
  - [ ] `.jobs-strip` — flex row, wrap, gap
  - [ ] `.job-chip` — pill button, running variant with accent
  - [ ] `.cache-log` — monospace, max-height 16rem, overflow auto
  - [ ] `.cache-advanced` — collapsible section styling
  - [ ] `.nav-badge` — accent pill badge

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
