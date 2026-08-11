/* Bump this whenever the pairing protocol changes. A returning phone that
 * kept a cached app.js would otherwise keep speaking the old handshake to a
 * computer that no longer understands it. */
const CACHE = 'rainette-pwa-v5';
const SHELL = [
  './',
  './index.html',
  './styles.css',
  './app.js',
  './src/state.js',
  './src/dom.js',
  './src/bridge.js',
  './src/player.js',
  './src/sheets.js',
  './src/tracks.js',
  './src/queue.js',
  './src/nowplaying.js',
  './src/extras.js',
  './src/sync.js',
  './manifest.webmanifest',
  './icon.svg',
];

self.addEventListener('install', event => {
  // addAll is all-or-nothing: one missing module would leave the phone with no
  // offline shell at all rather than a partial one, so a failed precache falls
  // back to caching lazily through the fetch handler instead of blocking the
  // install outright.
  event.waitUntil(
    caches.open(CACHE)
      .then(cache => cache.addAll(SHELL))
      .catch(() => {})
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request)
        .then(response => {
          const copy = response.clone();
          caches.open(CACHE).then(cache => cache.put('./index.html', copy));
          return response;
        })
        .catch(() => caches.match('./index.html')),
    );
    return;
  }

  event.respondWith(
    caches.match(request).then(cached => cached || fetch(request).then(response => {
      if (response.ok) {
        const copy = response.clone();
        caches.open(CACHE).then(cache => cache.put(request, copy));
      }
      return response;
    })),
  );
});
