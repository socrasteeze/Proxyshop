# TODO — Proxyshop Web

Working backlog for the self-hosted web stack under `web/`. Desktop Proxyshop
work is tracked upstream.

> The previous contents of this file tracked the "Offline-Cache UX
> Improvements" project. That work shipped and has since been superseded twice
> (Search folded into the Card library, then the per-game download queue), and
> most items referenced `templates/search.html`, which no longer exists. See
> `CHANGELOG.md` for what actually landed.

## Performance

- [ ] **FTS-backed gallery search.** `cardquery.build_where` matches free text
      with `LIKE '%…%'` over a concatenated `json_extract` blob, so every search
      is a full scan that parses each row's JSON twice (COUNT + SELECT). The
      FTS5 infrastructure already exists in `carddb.py` (`cards_fts`) but is
      only wired to `search_local`, not to `list_gallery`.
- [ ] **Index the hot JSON sort fields.** `rarity`, `artist`, `type_line`,
      `usd`, `cmc`, `set_name` are read via `json_extract` on every sort.
      Promote them to stored generated columns with indexes.
- [ ] **Batch the Full-view prints lookup.** `page_gallery` calls
      `list_art_group` once per card, and that query compares against a
      computed `CASE` expression, so it can't use an index — one full scan per
      card, up to 120 per page. Needs a batched query plus a stored generated
      column for the art-group key.
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
