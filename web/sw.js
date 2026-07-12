/**
 * Jarvis Service Worker — app shell always-fresh, static assets cached.
 * Does NOT intercept WebSocket (/ws) or non-GET requests.
 *
 * Strategy:
 *   App shell (/, /index.html, /app.js, /orb.js, /style.css, /sw.js,
 *              /manifest.webmanifest, any navigation) → ALWAYS network,
 *              no cache read, no cache write.  If truly offline, falls
 *              back to cache as last resort but does NOT store the response.
 *   Static assets (/vendor/*, /icons/*)                → network-first,
 *              cache the 200 response for offline fallback.
 */

const CACHE_NAME = 'jarvis-v3';

// Only cache rarely-changing static assets at install time.
const STATIC_PRECACHE = [
  '/vendor/three.min.js',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
];

// Pathnames that must ALWAYS come fresh from the network.
const APP_SHELL_PATHS = new Set([
  '/',
  '/index.html',
  '/app.js',
  '/orb.js',
  '/style.css',
  '/sw.js',
  '/manifest.webmanifest',
]);

// ── Install: skip waiting + optionally precache static assets ────────────────
self.addEventListener('install', (event) => {
  // Activate immediately — don't wait for old tabs to close.
  self.skipWaiting();

  event.waitUntil(
    (async () => {
      const cache = await caches.open(CACHE_NAME);
      // Wrap each asset individually so one missing file can't abort install.
      await Promise.all(
        STATIC_PRECACHE.map((url) =>
          cache.add(url).catch((err) => {
            console.warn('[SW] precache skip:', url, err.message);
          })
        )
      );
    })()
  );
});

// ── Activate: purge ALL old caches, then claim clients ───────────────────────
self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(
        keys
          .filter((key) => key !== CACHE_NAME)
          .map((key) => {
            console.log('[SW] deleting old cache:', key);
            return caches.delete(key);
          })
      );
      await self.clients.claim();
    })()
  );
});

// ── Fetch: GET http(s) only ───────────────────────────────────────────────────
self.addEventListener('fetch', (event) => {
  const { request } = event;

  // Only intercept GET over http(s).
  if (request.method !== 'GET') return;
  if (!request.url.startsWith('http')) return;

  const url = new URL(request.url);

  // Never intercept the /ws endpoint (belt-and-suspenders).
  if (url.pathname === '/ws' || url.pathname.startsWith('/ws/')) return;

  // ── App shell: always network, never cache ──────────────────────────────
  const isAppShell =
    request.mode === 'navigate' || APP_SHELL_PATHS.has(url.pathname);

  if (isAppShell) {
    event.respondWith(
      fetch(request).catch(() => {
        // Truly offline last resort — read from cache but do NOT re-store.
        return caches.match(request);
      })
    );
    return;
  }

  // ── Static assets: network-first, cache fallback, cache 200 responses ───
  event.respondWith(
    fetch(request)
      .then((networkResponse) => {
        if (networkResponse && networkResponse.status === 200) {
          const cloned = networkResponse.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, cloned));
        }
        return networkResponse;
      })
      .catch(() => caches.match(request))
  );
});
