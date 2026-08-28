// LoRa the Explorer — service worker.
//
// Goals: faster repeat loads and partial-offline for the map, without ever
// touching the live game data path. SSE (/api/events) and every /api call go
// straight to the network — the SW must not buffer or cache them.
//
// Strategy:
//   - Map tiles (cross-origin, effectively immutable) → cache-first.
//   - Same-origin /static/ assets (incl. self-hosted Leaflet + fonts) →
//     cache-first (bump CACHE_VERSION on release).
//   - Same-origin navigations (HTML) → network-first, cached copy as offline fallback.
//   - Everything else (API, auth, non-GET) → default network, untouched.

const CACHE_VERSION = 'v2';
const SHELL_CACHE = 'lora-shell-' + CACHE_VERSION;
const TILE_CACHE = 'lora-tiles-v1';

const SHELL_ASSETS = [
    // NB: style.css is intentionally omitted — the page requests it with a
    // cache-busting ?v=N query that a query-less precache entry would never
    // match, so it's cached on first load by the runtime /static/ rule below.
    '/static/pico.min.css',
    '/static/icon-192.png',
    '/static/icon-512.png',
    '/static/icon.svg',
    '/static/manifest.json',
    // Self-hosted front-end libraries/fonts (no CDN dependency) — precache so
    // the map pages render fully even on a first load with no internet. The
    // marker/layer PNGs are referenced by leaflet.css (relative url()) and by
    // Leaflet's default marker at runtime, so they must be precached too.
    '/static/vendor/leaflet/leaflet.css',
    '/static/vendor/leaflet/leaflet.js',
    '/static/vendor/leaflet/images/marker-icon.png',
    '/static/vendor/leaflet/images/marker-icon-2x.png',
    '/static/vendor/leaflet/images/marker-shadow.png',
    '/static/vendor/leaflet/images/layers.png',
    '/static/vendor/leaflet/images/layers-2x.png',
    '/static/vendor/fonts/cinzel-600.woff2',
];

// Map tiles are the only remaining cross-origin fetch; everything else is now
// same-origin /static/ (handled by the cache-first rule below).
const TILE_HOSTS = ['tile.openstreetmap.org', 'basemaps.cartocdn.com'];

self.addEventListener('install', (event) => {
    self.skipWaiting();
    // Cache each asset independently (not addAll, which is atomic) so one bad
    // path — e.g. a future renamed vendor file — can't silently abort the whole
    // precache and quietly break offline support.
    event.waitUntil(
        caches.open(SHELL_CACHE).then((cache) =>
            Promise.allSettled(SHELL_ASSETS.map((a) => cache.add(a)))
        )
    );
});

self.addEventListener('activate', (event) => {
    event.waitUntil((async () => {
        const keys = await caches.keys();
        await Promise.all(
            keys.filter((k) => k !== SHELL_CACHE && k !== TILE_CACHE).map((k) => caches.delete(k))
        );
        await self.clients.claim();
    })());
});

self.addEventListener('fetch', (event) => {
    const req = event.request;
    if (req.method !== 'GET') return;

    let url;
    try { url = new URL(req.url); } catch (e) { return; }

    const sameOrigin = url.origin === self.location.origin;

    // Never intercept the live game data path (SSE + API) or auth flows.
    if (sameOrigin && (
        url.pathname.startsWith('/api/') ||
        url.pathname.startsWith('/login') ||
        url.pathname.startsWith('/logout') ||
        url.pathname.startsWith('/auth/')
    )) {
        return; // default network
    }

    // Map tiles → cache-first in a dedicated cache.
    if (TILE_HOSTS.some((h) => url.hostname.endsWith(h))) {
        event.respondWith(cacheFirst(req, TILE_CACHE));
        return;
    }

    // Same-origin static assets → cache-first.
    if (sameOrigin && url.pathname.startsWith('/static/')) {
        event.respondWith(cacheFirst(req, SHELL_CACHE));
        return;
    }

    // Same-origin page navigations → network-first with offline fallback.
    if (sameOrigin && req.mode === 'navigate') {
        event.respondWith(networkFirst(req, SHELL_CACHE));
        return;
    }
    // Anything else: leave it to the network.
});

async function cacheFirst(req, cacheName) {
    const cache = await caches.open(cacheName);
    const hit = await cache.match(req);
    if (hit) return hit;
    try {
        const res = await fetch(req);
        // Cache successful and opaque (cross-origin CDN/tile) responses.
        if (res && (res.ok || res.type === 'opaque')) cache.put(req, res.clone());
        return res;
    } catch (e) {
        return hit || Response.error();
    }
}

async function networkFirst(req, cacheName) {
    const cache = await caches.open(cacheName);
    try {
        const res = await fetch(req);
        if (res && res.ok) cache.put(req, res.clone());
        return res;
    } catch (e) {
        const hit = await cache.match(req);
        return hit || Response.error();
    }
}
