const CACHE = 'argus-shell-v3';
const SHELL = ['/static/hud.css', '/static/hud.js', '/static/pcm-worklet.js', '/static/icon.svg', '/static/style.css', '/static/visualizer.js', '/static/vendor/three.min.js', '/static/vendor/chart.umd.min.js', '/manifest.json'];
self.addEventListener('install', (event) => { event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL))); self.skipWaiting(); });
self.addEventListener('activate', (event) => event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((key) => key.startsWith('argus-shell-') && key !== CACHE).map((key) => caches.delete(key)))).then(() => self.clients.claim())));
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  // Never cache API responses, authentication material, audio, or documents.
  if (event.request.method !== 'GET' || url.origin !== location.origin || !SHELL.includes(url.pathname)) return;
  event.respondWith(fetch(event.request).then((response) => {
    if (response.ok) { const copy = response.clone(); caches.open(CACHE).then((cache) => cache.put(event.request, copy)); }
    return response;
  }).catch(() => caches.match(event.request)));
});
