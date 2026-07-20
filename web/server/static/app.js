/* Shared UI helpers: typeahead, forms, job poll/delete. No dependencies. */

function escapeHtml(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/* Poll an element's data-poll-url every data-poll-ms, swap JSON into renderer fn. */
function pollJob(jobId, el) {
  const tick = async () => {
    try {
      const res = await fetch(`/api/jobs/${jobId}`);
      if (!res.ok) return;
      const job = await res.json();
      el.querySelectorAll('[data-field="status"]').forEach(n => {
        n.textContent = job.status;
        n.className = `status ${job.status}`;
      });
      const err = el.querySelector('[data-field="error"]');
      if (err) err.textContent = job.error || '';
      const log = el.querySelector('[data-field="log"]');
      if (log) log.textContent = job.log || '';
      if (job.status === 'done' || job.status === 'failed') {
        clearInterval(timer);
        if (job.status === 'done') location.reload();
      }
    } catch (e) { /* transient network error: keep polling */ }
  };
  const timer = setInterval(tick, 2000);
  tick();
}

/* Submit a form via fetch as multipart, show the JSON response or error. */
function wireAsyncForm(form, onSuccess) {
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const btn = form.querySelector('button[type="submit"]');
    const msg = form.querySelector('[data-field="message"]');
    if (btn) btn.disabled = true;
    if (msg) { msg.textContent = 'Working…'; }
    try {
      const res = await fetch(form.action, { method: 'POST', body: new FormData(form) });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        const detail = data.detail
          ? (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail))
          : `HTTP ${res.status}`;
        if (msg) msg.textContent = `Error: ${detail}`;
        return;
      }
      if (msg) msg.textContent = '';
      onSuccess(data, form);
    } catch (e) {
      if (msg) msg.textContent = 'Network error, try again.';
    } finally {
      if (btn) btn.disabled = false;
    }
  });
}

/**
 * Thumbnail typeahead for card printings.
 * opts: { getGame, onSelect, minChars=2, limit=12, navigateOnSelect=false }
 * onSelect(card) receives {id,name,set,set_name,collector_number,thumb,game,...}
 */
function wireCardTypeahead(input, opts = {}) {
  if (!input) return;
  const getGame = opts.getGame || (() => 'mtg');
  const onSelect = opts.onSelect || (() => {});
  // Touch devices: wait longer and require more characters before fetching, so
  // the dropdown doesn't pop up mid-word while you're still typing on a phone.
  const isTouch = window.matchMedia && window.matchMedia('(pointer: coarse)').matches;
  const minChars = opts.minChars ?? (isTouch ? 3 : 2);
  const debounceMs = isTouch ? 550 : 220;
  const limit = opts.limit ?? 12;

  const wrap = document.createElement('div');
  wrap.className = 'typeahead';
  input.parentNode.insertBefore(wrap, input);
  wrap.appendChild(input);

  const drop = document.createElement('div');
  drop.className = 'typeahead-drop';
  drop.hidden = true;
  wrap.appendChild(drop);

  let timer = null;
  let lastKey = '';
  let items = [];
  let active = -1;
  let abort = null;

  function close() {
    drop.hidden = true;
    drop.innerHTML = '';
    items = [];
    active = -1;
  }

  function setActive(i) {
    active = i;
    drop.querySelectorAll('.typeahead-item').forEach((el, idx) => {
      el.classList.toggle('is-active', idx === active);
      if (idx === active) el.scrollIntoView({ block: 'nearest' });
    });
  }

  function select(card) {
    close();
    input.value = card.name || '';
    onSelect(card);
  }

  function render(cards) {
    items = cards;
    active = cards.length ? 0 : -1;
    if (!cards.length) {
      drop.innerHTML = '<div class="typeahead-empty muted">No matches</div>';
      drop.hidden = false;
      return;
    }
    drop.innerHTML = cards.map((c, i) => {
      const set = (c.set || '').toUpperCase();
      const num = c.collector_number || '';
      const meta = [set, num ? `#${num}` : ''].filter(Boolean).join(' ');
      const thumb = c.thumb
        ? `<img class="typeahead-thumb" src="${escapeHtml(c.thumb)}" alt="" loading="lazy">`
        : '<div class="typeahead-thumb typeahead-thumb--empty"></div>';
      return `<button type="button" class="typeahead-item${i === 0 ? ' is-active' : ''}" data-i="${i}">
        ${thumb}
        <span class="typeahead-meta">
          <span class="typeahead-name">${escapeHtml(c.name || '')}</span>
          <span class="muted">${escapeHtml(meta)}</span>
        </span>
      </button>`;
    }).join('');
    drop.hidden = false;
    drop.querySelectorAll('.typeahead-item').forEach(btn => {
      btn.addEventListener('mousedown', (ev) => {
        ev.preventDefault();
        select(items[Number(btn.dataset.i)]);
      });
    });
  }

  input.addEventListener('input', () => {
    const q = input.value.trim();
    const game = getGame() || 'mtg';
    const key = `${game}:${q}`;
    clearTimeout(timer);
    if (q.length < minChars) {
      lastKey = '';
      close();
      return;
    }
    if (key === lastKey) return;
    timer = setTimeout(async () => {
      lastKey = key;
      if (abort) abort.abort();
      abort = new AbortController();
      try {
        const res = await fetch(
          `/api/cards/search?q=${encodeURIComponent(q)}&limit=${limit}&game=${encodeURIComponent(game)}`,
          { signal: abort.signal });
        if (!res.ok) return;
        const data = await res.json();
        if (input.value.trim() !== q) return;
        render(data.cards || []);
      } catch (e) {
        if (e.name !== 'AbortError') { /* best-effort */ }
      }
    }, debounceMs);
  });

  input.addEventListener('keydown', (ev) => {
    if (drop.hidden || !items.length) {
      if (ev.key === 'Escape') close();
      return;
    }
    if (ev.key === 'ArrowDown') {
      ev.preventDefault();
      setActive(Math.min(active + 1, items.length - 1));
    } else if (ev.key === 'ArrowUp') {
      ev.preventDefault();
      setActive(Math.max(active - 1, 0));
    } else if (ev.key === 'Enter' && active >= 0) {
      ev.preventDefault();
      select(items[active]);
    } else if (ev.key === 'Escape') {
      ev.preventDefault();
      close();
    }
  });

  input.addEventListener('blur', () => {
    setTimeout(close, 120);
  });
}

/**
 * Local set picker (replaces native <datalist> for large catalogs).
 * opts: { getItems: () => [{id,name,card_count?,released_at?,series?}],
 *         minChars=1, limit=12, onSelect }
 * Stores the set id/code in the input; shows a muted label under it.
 */
function wireSetPicker(input, opts = {}) {
  if (!input) return null;
  const getItems = opts.getItems || (() => []);
  const onSelect = opts.onSelect || (() => {});
  const minChars = opts.minChars ?? 0;
  const limit = opts.limit ?? 12;

  // Outer holds hint; inner is the positioned typeahead shell so the dropdown
  // sits under the input even when the hint is visible.
  const outer = document.createElement('div');
  outer.className = 'set-picker';
  const wrap = document.createElement('div');
  wrap.className = 'typeahead set-picker-shell';
  input.parentNode.insertBefore(outer, input);
  outer.appendChild(wrap);
  wrap.appendChild(input);
  input.removeAttribute('list');
  input.setAttribute('autocomplete', 'off');
  input.setAttribute('spellcheck', 'false');

  const drop = document.createElement('div');
  drop.className = 'typeahead-drop set-picker-drop';
  drop.hidden = true;
  wrap.appendChild(drop);

  const hint = document.createElement('div');
  hint.className = 'set-picker-hint muted';
  hint.hidden = true;
  outer.appendChild(hint);

  let items = [];
  let active = -1;

  function close() {
    drop.hidden = true;
    drop.innerHTML = '';
    items = [];
    active = -1;
  }

  function setActive(i) {
    active = i;
    drop.querySelectorAll('.typeahead-item').forEach((el, idx) => {
      el.classList.toggle('is-active', idx === active);
      if (idx === active) el.scrollIntoView({ block: 'nearest' });
    });
  }

  function showHint(row) {
    if (!row) {
      hint.hidden = true;
      hint.textContent = '';
      return;
    }
    const bits = [row.name || row.id];
    if (row.card_count != null) bits.push(`${row.card_count} cards`);
    if (row.released_at) bits.push(String(row.released_at).slice(0, 10));
    if (row.series) bits.push(row.series);
    hint.textContent = bits.filter(Boolean).join(' · ');
    hint.hidden = false;
  }

  function select(row) {
    input.value = row.id || '';
    showHint(row);
    close();
    onSelect(row);
  }

  function rank(q, row) {
    const id = String(row.id || '').toLowerCase();
    const name = String(row.name || '').toLowerCase();
    if (id === q) return 0;
    if (id.startsWith(q)) return 1;
    if (name.startsWith(q)) return 2;
    if (id.includes(q)) return 3;
    if (name.includes(q)) return 4;
    return 9;
  }

  function filter(q) {
    const needle = q.trim().toLowerCase();
    const pool = getItems() || [];
    if (!needle) return pool.slice(0, limit);
    return pool
      .map((row) => ({ row, score: rank(needle, row) }))
      .filter((x) => x.score < 9)
      .sort((a, b) => a.score - b.score || String(a.row.id).localeCompare(String(b.row.id)))
      .slice(0, limit)
      .map((x) => x.row);
  }

  function render(rows) {
    items = rows;
    active = rows.length ? 0 : -1;
    const pool = getItems() || [];
    if (!rows.length) {
      const msg = pool.length
        ? 'No matching sets'
        : 'No sets loaded — type a set code';
      drop.innerHTML = `<div class="typeahead-empty muted">${msg}</div>`;
      drop.hidden = false;
      return;
    }
    drop.innerHTML = rows.map((row, i) => {
      const code = escapeHtml(String(row.id || '').toUpperCase());
      const name = escapeHtml(row.name || row.id || '');
      const meta = [];
      if (row.card_count != null) meta.push(`${row.card_count} cards`);
      if (row.released_at) meta.push(String(row.released_at).slice(0, 4));
      return `<button type="button" class="typeahead-item${i === 0 ? ' is-active' : ''}" data-i="${i}">
        <span class="set-picker-code">${code}</span>
        <span class="typeahead-meta">
          <span class="typeahead-name">${name}</span>
          <span class="muted">${escapeHtml(meta.join(' · '))}</span>
        </span>
      </button>`;
    }).join('');
    drop.hidden = false;
    drop.querySelectorAll('.typeahead-item').forEach((btn) => {
      btn.addEventListener('mousedown', (ev) => {
        ev.preventDefault();
        select(items[Number(btn.dataset.i)]);
      });
    });
  }

  function openForQuery() {
    const q = input.value.trim();
    if (minChars > 0 && q.length > 0 && q.length < minChars) {
      close();
      return;
    }
    render(filter(q));
  }

  function syncHintFromValue() {
    const q = input.value.trim().toLowerCase();
    if (!q) {
      showHint(null);
      return;
    }
    const hit = (getItems() || []).find((row) => String(row.id || '').toLowerCase() === q);
    showHint(hit || null);
  }

  function refresh() {
    syncHintFromValue();
    if (document.activeElement === input || !drop.hidden) openForQuery();
  }

  input.addEventListener('focus', openForQuery);
  input.addEventListener('click', openForQuery);
  input.addEventListener('input', () => {
    syncHintFromValue();
    openForQuery();
  });
  input.addEventListener('keydown', (ev) => {
    if (drop.hidden || !items.length) {
      if (ev.key === 'Escape') close();
      return;
    }
    if (ev.key === 'ArrowDown') {
      ev.preventDefault();
      setActive(Math.min(active + 1, items.length - 1));
    } else if (ev.key === 'ArrowUp') {
      ev.preventDefault();
      setActive(Math.max(active - 1, 0));
    } else if (ev.key === 'Enter' && active >= 0) {
      ev.preventDefault();
      select(items[active]);
    } else if (ev.key === 'Escape') {
      ev.preventDefault();
      close();
    }
  });
  input.addEventListener('blur', () => {
    setTimeout(() => {
      close();
      syncHintFromValue();
    }, 150);
  });

  return { syncHintFromValue, openForQuery, refresh };
}

/** Delete a job; returns true on success. Optionally remove a DOM row. */
async function deleteJob(jobId, rowEl) {
  const res = await fetch(`/api/jobs/${jobId}`, { method: 'DELETE' });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || `HTTP ${res.status}`);
  }
  if (rowEl) {
    rowEl.classList.add('is-removing');
    setTimeout(() => rowEl.remove(), 220);
  }
  return true;
}

function wireJobDeleteButtons(root = document) {
  root.querySelectorAll('[data-delete-job]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const id = btn.getAttribute('data-delete-job');
      if (!id) return;
      if (!confirm('Delete this render job?')) return;
      const row = btn.closest('tr') || btn.closest('[data-job-row]');
      btn.disabled = true;
      try {
        await deleteJob(id, row);
        if (btn.dataset.redirect === 'home') location.href = '/';
      } catch (e) {
        alert(e.message || 'Delete failed');
        btn.disabled = false;
      }
    });
  });
}

const GAME_LABELS_FALLBACK = {
  mtg: 'Magic: The Gathering',
  pokemon: 'Pokémon',
  'union-arena': 'Union Arena',
  riftbound: 'Riftbound',
};

// Friendly wording for a run state.
const CACHE_STATE_TEXT = {
  running: 'downloading',
  stopped: 'paused',
  done: 'complete',
  idle: 'idle',
};

/**
 * Human-readable status for a cache run. Returns {main, detail, cls}:
 *  - main:   headline, e.g. "Downloading 348 of 950 cards — now: Pikachu ex"
 *  - detail: muted secondary, e.g. "348 images saved · 49 already had"
 *  - cls:    state class (running/stopped/done/error)
 */
function formatCacheStatus(body) {
  if (!body || (!body.status && !body.running) || body.status === 'idle') {
    return { main: 'No download yet for this game.', detail: '', cls: 'idle' };
  }
  const stored = body.stored ?? 0;
  const total = body.total_hint;
  // Only show "X of Y" when Y is a plausible card total (a superset of what's
  // stored). Some providers page by product/series (Union Arena), so total_hint
  // is a page/series count, not a card count — showing "11773 of 157" is wrong.
  const prog = (total && total >= stored) ? `${stored} of ${total}` : `${stored}`;
  const ok = body.images_ok ?? 0;
  const skip = body.images_skip ?? 0;
  const fail = body.images_fail ?? 0;
  const detailBits = [`${ok} images saved`];
  if (skip) detailBits.push(`${skip} already had`);
  if (fail) detailBits.push(`${fail} failed`);
  const detail = detailBits.join(' · ');

  if (body.running) {
    let main = `Downloading ${prog} cards`;
    if (body.current) main += ` — now: ${body.current}`;
    return { main, detail, cls: 'running' };
  }
  if (body.status === 'stopped') {
    if (body.error) return { main: `Stopped: ${body.error}`, detail: '', cls: 'error' };
    return { main: `Paused at ${prog} cards — click Resume to continue`, detail, cls: 'stopped' };
  }
  if (body.status === 'done') {
    return { main: `Complete — ${body.db_count ?? stored} cards cached`, detail, cls: 'done' };
  }
  if (body.error) return { main: `Error: ${body.error}`, detail: '', cls: 'error' };
  return { main: body.status || 'Idle', detail, cls: '' };
}

function renderJobChips(container, jobs, onSelect, activeGame) {
  if (!container) return;
  const entries = Object.entries(jobs || {}).filter(([, st]) => {
    if (!st) return false;
    if (st.running) return true;
    const s = st.status;
    return s && s !== 'idle';
  });
  if (!entries.length) {
    container.hidden = true;
    container.innerHTML = '';
    return;
  }
  container.hidden = false;
  container.innerHTML = '';
  entries.forEach(([game, st]) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'job-chip';
    if (st.running) btn.classList.add('is-running');
    if (activeGame && game === activeGame) btn.classList.add('is-active');
    const gameName = GAME_LABELS_FALLBACK[game] || game;
    const scope = st.label || st.query_label || 'Full catalog';
    const state = st.running ? 'downloading' : (CACHE_STATE_TEXT[st.status] || st.status || '');
    const dot = st.running ? '<span class="chip-dot"></span>' : '';
    btn.innerHTML =
      `${dot}<span class="chip-game">${escapeHtml(gameName)}</span>`
      + `<span class="chip-scope">${escapeHtml(String(scope).slice(0, 32))}</span>`
      + `<span class="chip-state">${escapeHtml(state)}</span>`;
    btn.title = st.running && st.current
      ? `${gameName} · ${scope} · downloading ${st.current}`
      : `${gameName} · ${scope} · ${state}`;
    btn.addEventListener('click', () => {
      if (typeof onSelect === 'function') onSelect(game, st);
    });
    container.appendChild(btn);
  });
}

function wireCacheBadge() {
  const badge = document.getElementById('nav-cache-badge');
  if (!badge) return;
  let timer = null;

  async function refresh() {
    try {
      const res = await fetch('/api/cache-jobs');
      if (!res.ok) return;
      const body = await res.json();
      const running = Object.values(body.jobs || {}).filter((j) => j && j.running);
      if (running.length) {
        badge.hidden = false;
        badge.textContent = String(running.length);
        badge.setAttribute('aria-label', `${running.length} downloads in progress`);
      } else {
        badge.hidden = true;
        badge.textContent = '';
        badge.removeAttribute('aria-label');
      }
      if (body.any_running) {
        if (!timer) timer = setInterval(refresh, 10000);
      } else if (timer) {
        clearInterval(timer);
        timer = null;
      }
    } catch (e) { /* badge is best-effort */ }
  }

  refresh();
}

/**
 * In-place card popover: intercept .js-card-preview clicks, fetch detail JSON,
 * keep scroll position when closed. Falls back to full page navigation if JS
 * or the API fails. Modal lives on <body> (base.html) so fixed positioning
 * isn't trapped by <main>'s transform animation.
 */
function wireCardPopover(root = document) {
  const modal = document.getElementById('card-modal');
  const body = document.getElementById('card-modal-body');
  const title = document.getElementById('card-modal-title');
  const closeBtn = modal && modal.querySelector('.card-modal-close');
  if (!modal || !body || !title) return;
  if (modal.dataset.wired === '1') return;
  modal.dataset.wired = '1';

  let lastFocus = null;
  let abort = null;
  let openedAt = 0;

  function close(force) {
    // Ignore accidental backdrop clicks from the same gesture that opened us.
    if (!force && Date.now() - openedAt < 280) return;
    modal.hidden = true;
    document.body.classList.remove('modal-open');
    if (abort) { abort.abort(); abort = null; }
    if (lastFocus && typeof lastFocus.focus === 'function') {
      try { lastFocus.focus(); } catch (e) { /* ignore */ }
    }
    lastFocus = null;
  }

  function money(n) {
    if (n == null || Number.isNaN(Number(n))) return null;
    return `$${Number(n).toFixed(2)}`;
  }

  function render(data) {
    title.textContent = data.name || 'Card';
    const details = (data.details || []).map((d) =>
      `<tr><th style="width:7rem">${escapeHtml(d.label)}</th>`
      + `<td>${escapeHtml(String(d.value))}</td></tr>`).join('');
    let priceRow = '';
    if (data.price) {
      const bits = [];
      const usd = money(data.price.usd);
      const foil = money(data.price.usd_foil);
      const eur = data.price.eur != null ? `€${Number(data.price.eur).toFixed(2)}` : null;
      if (usd) bits.push(usd);
      if (foil) bits.push(`foil ${foil}`);
      if (eur) bits.push(eur);
      if (bits.length) {
        priceRow = `<tr><th>Price</th><td>${escapeHtml(bits.join(' · '))}`
          + ` <span class="muted">(${escapeHtml(data.price.source || '')})</span></td></tr>`;
      }
    }
    const actions = [];
    if (data.editor_url) {
      actions.push(`<a class="btn" href="${escapeHtml(data.editor_url)}">Open in editor</a>`);
    }
    if (data.image_png) {
      actions.push(`<a class="btn btn-ghost" href="${escapeHtml(data.image_png)}" download>Download high-quality scan</a>`);
    }
    if (data.image_art_crop) {
      actions.push(`<a class="btn btn-ghost" href="${escapeHtml(data.image_art_crop)}" download>Download art crop</a>`);
    }
    if (data.page_url) {
      actions.push(`<a class="btn btn-ghost" href="${escapeHtml(data.page_url)}">Open full page</a>`);
    }
    const pill = data.game_label
      ? ` <span class="pill">${escapeHtml(data.game_label)}</span>` : '';
    // Scryfall-style "Prints" list — every other printing/art of this card.
    const prints = data.prints || [];
    let printsBlock = '';
    if (prints.length > 1) {
      const items = prints.map((p) => {
        const meta = [(p.set || '').toUpperCase(),
                      p.collector_number ? `#${p.collector_number}` : '',
                      (p.lang && p.lang !== 'en') ? p.lang.toUpperCase() : '']
          .filter(Boolean).join(' ');
        return `<button type="button" class="print-tile${p.current ? ' is-current' : ''}"
                  data-print-id="${escapeHtml(p.id)}" title="${escapeHtml(p.set_name || meta)}">
          <img loading="lazy" src="${escapeHtml(p.thumb)}" alt="">
          <span class="muted">${escapeHtml(meta)}</span>
        </button>`;
      }).join('');
      printsBlock = `
        <div class="prints-block">
          <h3 class="prints-title">Prints · ${prints.length}</h3>
          <div class="prints-grid">${items}</div>
        </div>`;
    }
    body.innerHTML = `
      <div class="card-frame">
        <img src="${escapeHtml(data.image_png || data.image_large || '')}"
             alt="${escapeHtml(data.name || '')}" width="745" height="1040">
      </div>
      <div>
        <p style="margin:0 0 .7rem">${escapeHtml(data.name || '')}${pill}</p>
        <table><tbody>${details}${priceRow}</tbody></table>
        <p class="btn-row" style="margin-top:1rem">${actions.join('')}</p>
        ${printsBlock}
      </div>`;
    // Clicking a print swaps the modal to that printing — in place, without a
    // loading flash (detail is a fast local lookup). Mark the tapped tile
    // active immediately so it feels responsive.
    body.querySelectorAll('.print-tile').forEach((btn) => {
      btn.addEventListener('click', () => {
        const id = btn.dataset.printId;
        if (!id || id === data.id) return;
        body.querySelectorAll('.print-tile').forEach((t) => t.classList.remove('is-current'));
        btn.classList.add('is-current');
        load(id, { silent: true });
      });
    });
  }

  // Fetch a card's detail and render it. When `silent`, keep the current
  // content visible until the new data arrives (used for print-to-print swaps
  // so the modal doesn't flash "Loading…").
  async function load(cardId, { silent = false } = {}) {
    if (!silent) {
      title.textContent = 'Loading…';
      body.innerHTML = '<p class="muted">Loading…</p>';
    }
    if (abort) abort.abort();
    abort = new AbortController();
    try {
      const res = await fetch(`/api/cards/${encodeURIComponent(cardId)}/detail`, {
        signal: abort.signal,
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      render(await res.json());
    } catch (e) {
      if (e.name === 'AbortError') return;
      body.innerHTML = `<p class="muted">Could not load card.`
        + ` <a href="/card/${encodeURIComponent(cardId)}">Open full page</a>.</p>`;
    }
  }

  async function open(cardId, trigger) {
    lastFocus = trigger || document.activeElement;
    openedAt = Date.now();
    modal.hidden = false;
    document.body.classList.add('modal-open');
    if (closeBtn) {
      try { closeBtn.focus(); } catch (e) { /* ignore */ }
    }
    return load(cardId, { silent: false });
  }

  const scope = root || document;
  scope.addEventListener('click', (ev) => {
    const link = ev.target.closest('.js-card-preview');
    if (!link) return;
    const id = link.getAttribute('data-card-id');
    if (!id) return;
    // Allow modified clicks (new tab) to navigate normally.
    if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey || ev.button !== 0) return;
    ev.preventDefault();
    open(id, link);
  });

  // Backdrop closes the modal; Close button always works; dialog content does not.
  const backdrop = modal.querySelector('.card-modal-backdrop');
  if (backdrop) {
    backdrop.addEventListener('click', () => close(false));
  }
  if (closeBtn) {
    closeBtn.addEventListener('click', (ev) => {
      ev.preventDefault();
      close(true);
    });
  }
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && !modal.hidden) {
      ev.preventDefault();
      close(true);
    }
  });
}

/* Give every submit form a fresh idempotency key per page load. */
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('input[name="idempotency_key"]').forEach(inp => {
    if (!inp.value) inp.value = crypto.randomUUID();
  });
  wireJobDeleteButtons();
  wireCacheBadge();
  wireCardPopover(document);
});
