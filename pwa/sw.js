/* Bump this whenever the pairing protocol changes, or whenever a module is
 * added or its behaviour changes. Requests below are served cache-first, so a
 * returning phone that kept a cached app.js would otherwise keep running the
 * old client — speaking a handshake the computer no longer understands, or
 * simply never seeing the new screens — until it was uninstalled. */
const CACHE = 'rainette-pwa-v14';
const SHELL = [
  './',
  './index.html',
  './styles.css',
  './app.js',
  './src/state.js',
  './src/connection.js',
  './src/sessions.js',
  './src/target.js',
  './src/prefsync.js',
  './src/codecs.js',
  './src/dom.js',
  './src/bridge.js',
  './src/player.js',
  './src/audio.js',
  './src/sheets.js',
  './src/motion.js',
  './src/gesture.js',
  './src/slider.js',
  './src/tracks.js',
  './src/queue.js',
  './src/nowplaying.js',
  './src/extras.js',
  './src/sync.js',
  './src/collections.js',
  './src/scanner.js',
  './src/qr.js',
  './src/catalog.js',
  './src/artists.js',
  './src/sorting.js',
  './src/eq.js',
  './src/prefs.js',
  './src/local.js',
  './src/playlists.js',
  './src/backup.js',
  './src/import.js',
  './src/settings.js',
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

  // Stale-while-revalidate rather than cache-first.
  //
  // Cache-first meant that forgetting to bump CACHE above shipped a *new*
  // index.html to a phone still running the *old* modules — new markup wired by
  // old JavaScript, which is an app whose buttons are all there and none of
  // which do anything. That failure was permanent until the app was
  // reinstalled. Here a missed bump costs one stale load and then heals itself,
  // because every hit also refreshes the copy in the background.
  event.respondWith(
    caches.open(CACHE).then(cache => cache.match(request).then(cached => {
      const network = fetch(request)
        .then(response => {
          if (response.ok) cache.put(request, response.clone());
          return response;
        })
        .catch(() => cached);   // offline: whatever we have is the best answer
      return cached || network;
    })),
  );
});
