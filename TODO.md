# TODO — Proxyshop Web

Working backlog for the self-hosted web stack under `web/`. Desktop Proxyshop
work is tracked upstream.

> The previous contents of this file tracked the "Offline-Cache UX
> Improvements" project. That work shipped and has since been superseded twice
> (Search folded into the Card library, then the per-game download queue), and
> most items referenced `templates/search.html`, which no longer exists. See
> `CHANGELOG.md` for what actually landed.

## Performance

- [x] **Index-backed gallery search.** Free text now goes through a trigram
      FTS5 index over a `card_search` shadow table instead of `LIKE '%…%'` over
      a `json_extract` blob. Trigram keeps exact substring semantics (a search
      for `bolt` still finds `Thunderbolt`), so results are unchanged.
      ~426 ms → ~40 ms at 40k cards.
- [x] **Index the hot sorts.** SQLite can't add STORED generated columns via
      ALTER TABLE, so these are expression indexes instead — which works, but
      only if the index reproduces the whole ORDER BY: leading `game`, then the
      `(expr) IS NULL` NULLs-last guard, then the expression, then every
      tiebreak column. Miss any term and SQLite silently re-sorts the whole set.
      ~179 ms → ~2 ms. Curated subset only (each index is write cost).
- [x] **Fix the Full-view prints lookup.** Indexed the art-group expression and
      added the `game` filter the composite index needs. 22.6 ms → 0.15 ms per
      card; a 60-card Full page went 1,356 ms → 9 ms. No batching needed.
- [ ] **Combine-arts view is still ~230 ms** (was ~420 ms). The two window
      functions sort the whole filtered set, and the ROW_NUMBER ordering
      (released_at, fetched_at, id) doesn't match the art-group index, so the
      sort can't be skipped. Would need a materialized "newest printing per
      group" table. Non-default view, so lower priority.
- [x] **Field filters** now read per-field columns on the `card_search` shadow
      table instead of `json_extract` per row. ~157 ms → ~40 ms. The COUNT half
      of a page dominated, since it can't early-exit on LIMIT.
- [ ] Consider a service worker for offline-first image browsing.

## Providers

- [ ] **Weiß Schwarz upscaling pass.** No source publishes print-grade scans;
      sheets come out soft. An upscale step (or a documented external workflow)
      would close the gap.
- [ ] Riftbound / Union Arena / Weiß Schwarz scrapes are HTML-shaped and will
      break when those sites redesign. `probe-game` exists to diagnose this —
      consider a scheduled canary run.

## Infrastructure

- [ ] **No CI.** `.github/` holds only issue templates; `.pre-commit-config.yaml`
      does not run the tests. `python -m pytest web/tests` is fully offline and
      would run fine in Actions.
- [ ] Multi-worker is functional (atomic claims, per-worker capabilities) but
      the UI merges template lists from all workers — treat as experimental
      until the UI is per-worker aware.

## Verification checklist (manual)

Run: `python -m uvicorn web.server.app:app --port 8000`

- [ ] Start a small Riftbound download → `/logs` shows timestamped events
- [ ] Job chips appear while running; clicking one switches the game picker
- [ ] Nav badge on Card library appears while running, clears when idle
- [ ] Stop/resume works; errors surface in both status line and log
- [ ] Queue: stack two MTG filter sets, confirm they run in sequence
- [ ] "Start new download with these filters" warns before discarding progress
