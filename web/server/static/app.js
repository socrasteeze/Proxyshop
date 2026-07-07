/* Minimal helpers: form-to-fetch submission + status polling. No dependencies. */

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
    btn.disabled = true;
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
      btn.disabled = false;
    }
  });
}

/* Give every submit form a fresh idempotency key per page load. */
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('input[name="idempotency_key"]').forEach(inp => {
    if (!inp.value) inp.value = crypto.randomUUID();
  });
});
