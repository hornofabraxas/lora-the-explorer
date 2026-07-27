// LoRa the Explorer — service worker.
//
// Goals: faster repeat loads and partial-offline for the map, without ever
// touching the live game data path. SSE (/api/events) and every /api call go
// straight to the network — the SW must not buffer or cache them.
//
// Strategy:
//   - Map tiles + CDN libs/fonts (cross-origin, effectively immutable) → cache-first.
//   - Same-origin /static/ assets → cache-first (bump CACHE_VERSION on release).
//   - Same-origin navigations (HTML) → network-first, cached copy as offline fallback.
//   - Everything else (API, auth, non-GET) → default network, untouched.

const CACHE_VERSION = 'v1';
const SHELL_CACHE = 'lora-shell-' + CACHE_VERSION;
const TILE_CACHE = 'lora-tiles-v1';

const SHELL_ASSETS = [
    '/static/style.css',
    '/static/pico.min.css',
    '/static/icon-192.png',
    '/static/icon-512.png',
    '/static/icon.svg',
    '/static/manifest.json',
];

const TILE_HOSTS = ['tile.openstreetmap.org', 'basemaps.cartocdn.com'];
const CDN_HOSTS = ['unpkg.com', 'cdn.jsdelivr.net', 'fonts.googleapis.com', 'fonts.gstatic.com'];

self.addEventListener('install', (event) => {
    self.skipWaiting();
    event.waitUntil(
        caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_ASSETS).catch(() => {}))
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

    // CDN libraries + fonts → cache-first in the shell cache.
    if (CDN_HOSTS.some((h) => url.hostname.endsWith(h))) {
        event.respondWith(cacheFirst(req, SHELL_CACHE));
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
