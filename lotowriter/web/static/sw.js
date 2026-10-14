// Service worker: lets the app open without a connection and keeps photos available offline.
// Data itself is handled by data.js (IndexedDB); API calls are never cached here except photos.
const SHELL_CACHE = "lw-shell-v4";
const PHOTO_CACHE = "lw-photos";
const MAX_PHOTOS = 800;
const SHELL = [
  "/",
  "/static/app.js",
  "/static/app.css",
  "/static/data.js",
  "/static/idb.js",
  "/static/annotate.js",
  "/static/manifest.webmanifest",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL_CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) {
      if (key !== SHELL_CACHE && key !== PHOTO_CACHE) await caches.delete(key);
    }
    await self.clients.claim();
  })());
});

async function networkFirst(request, cacheKey) {
  const cache = await caches.open(SHELL_CACHE);
  try {
    const res = await fetch(request);
    if (res.ok) cache.put(cacheKey || request, res.clone());
    return res;
  } catch (err) {
    const hit = await cache.match(cacheKey || request, { ignoreSearch: true });
    if (hit) return hit;
    throw err;
  }
}

async function trimPhotos(cache) {
  const keys = await cache.keys();
  for (let i = 0; i < keys.length - MAX_PHOTOS; i += 1) await cache.delete(keys[i]);
}

async function photoCacheFirst(request) {
  const cache = await caches.open(PHOTO_CACHE);
  const hit = await cache.match(request);
  if (hit) return hit;
  const res = await fetch(request);
  if (res.ok) {
    await cache.put(request, res.clone());
    trimPhotos(cache);
  }
  return res;
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/photo-files/")) {
    event.respondWith(photoCacheFirst(req));  // photo files never change
  } else if (url.pathname.startsWith("/api/")) {
    // Live data: network only (data.js falls back to the device copy).
  } else if (req.mode === "navigate") {
    event.respondWith(networkFirst(req, "/"));
  } else if (url.pathname.startsWith("/static/")) {
    event.respondWith(networkFirst(req));
  }
});
