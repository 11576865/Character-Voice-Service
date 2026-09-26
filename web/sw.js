const CACHE = "cvs-reader-shell-v2";
const SHELL = ["/", "/reader-assets/js/reader.js?v=2", "/reader-assets/js/sources.js",
  "/reader-assets/js/epub_source.js", "/reader-assets/js/segmenter.js",
  "/reader-assets/js/player.js", "/reader-assets/js/queue.js",
  "/reader-assets/js/progress.js", "/reader-assets/js/navigation.js",
  "/reader-assets/js/variants.js", "/reader-assets/js/offline.js"];

self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)));
  self.skipWaiting();
});
self.addEventListener("activate", event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(
    keys.filter(key => key !== CACHE).map(key => caches.delete(key)))));
  self.clients.claim();
});
self.addEventListener("fetch", event => {
  if (event.request.method !== "GET") return;
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin || !SHELL.includes(url.pathname + url.search)) return;
  event.respondWith(fetch(event.request).then(response => {
    if (response.ok) {
      const cachedResponse = response.clone();
      event.waitUntil(caches.open(CACHE).then(cache => cache.put(event.request, cachedResponse))
        .catch(() => {}));
    }
    return response;
  }).catch(() => caches.match(event.request)));
});
