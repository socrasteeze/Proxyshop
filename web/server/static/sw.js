/*
 * Service worker — deliberately conservative scope.
 *
 * This app is a thin client over a server-side SQLite card library, job
 * queue, and render pipeline: nearly every page and API call must reflect
 * live server state (download progress, job status, log tails). Caching any
 * of that would show stale progress/results, which is worse than no offline
 * support at all — the checklist's own "BLOCKER if the only update mechanism
 * is clear Safari cache" warning is about exactly this failure mode.
 *
 * So the scope here is narrow on purpose:
 *  - Precache only the handful of small, rarely-changing static assets
 *    (CSS/JS/icons/manifest) — never HTML, never /api/*, never card images.
 *  - HTML navigations are network-first with a static offline fallback, so a
 *    dropped connection to the NAS gets a friendly page instead of Safari's
 *    default error, without ever pretending the app works offline.
 *  - Precached assets are stale-while-revalidate: fast repeat loads, but the
 *    cache is refreshed in the background on every fetch so a redeploy is
 *    picked up within one extra load, not stuck until a cache-name bump.
 *  - Everything else (/api/*, card images, uploads) is left untouched —
 *    those already have correct HTTP caching (see the Cache-Control headers
 *    in app.py) and must always be able to go live.
 *
 * Total precache is a few small files, nowhere near iOS Safari's ~50MB Cache
 * Storage quota. Do not add card images or API responses to PRECACHE_URLS —
 * that would reintroduce the staleness problem this file exists to avoid.
 */

// Bump this on any change to the files below (or to this file itself) so
// `activate` deletes the previous version's cache instead of leaving it
// around unused.
const CACHE_VERSION = 'v1';
const CACHE_NAME = `proxyshop-static-${CACHE_VERSION}`;

const PRECACHE_URLS = [
  '/static/app.css',
  '/static/app.js',
  '/static/manifest.webmanifest',
  '/static/img/favicon.ico',
  '/static/img/favicon-32.png',
  '/static/img/apple-touch-icon.png',
  '/static/img/logo-96.png',
  '/static/img/icon-192.png',
  '/static/img/icon-512.png',
  '/static/offline.html',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(PRECACHE_URLS))
      .then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(
        names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n))))
      .then(() => self.clients.claim()));
});

function isPrecachedAsset(url) {
  return url.origin === self.location.origin && PRECACHE_URLS.includes(url.pathname);
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return; // never intercept writes

  const url = new URL(req.url);

  // Navigations (HTML pages): always try the network first so job status,
  // download progress, etc. are current. Only fall back to a cached page
  // when the network is genuinely unreachable.
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).catch(() =>
        caches.match('/static/offline.html').then((res) => res || Response.error())));
    return;
  }

  // Precached static assets: stale-while-revalidate.
  if (isPrecachedAsset(url)) {
    event.respondWith(
      caches.open(CACHE_NAME).then((cache) =>
        cache.match(req).then((cached) => {
          const network = fetch(req)
            .then((res) => { cache.put(req, res.clone()); return res; })
            .catch(() => cached);
          return cached || network;
        })));
    return;
  }

  // Everything else (API calls, card images, uploads, worker endpoints) is
  // left completely alone — normal network + the browser's own HTTP cache.
});
