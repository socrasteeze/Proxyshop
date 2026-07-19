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
  const minChars = opts.minChars ?? 2;
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
    }, 220);
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
    const label = (st.query_label && String(st.query_label).slice(0, 40))
      || GAME_LABELS_FALLBACK[game]
      || game;
    const state = st.running ? 'running' : (st.status || '');
    btn.textContent = `${label} · ${state}`;
    btn.title = st.query_label || game;
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
      } else {
        badge.hidden = true;
        badge.textContent = '';
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

/* Give every submit form a fresh idempotency key per page load. */
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('input[name="idempotency_key"]').forEach(inp => {
    if (!inp.value) inp.value = crypto.randomUUID();
  });
  wireJobDeleteButtons();
  wireCacheBadge();
});
