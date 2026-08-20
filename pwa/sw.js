/* The suffix is a digest of every file in SHELL, and it is not maintained by
 * hand: `tests/test_output_and_phone_sync.py` recomputes it and fails when it
 * has drifted, printing the value to paste back.
 *
 * It exists because three separate fixes to the sheet drag (#19, #22, #23) all
 * shipped without touching this line. Stale-while-revalidate meant each one
 * still cost a load running the previous JavaScript before it healed — so a
 * gesture fix could be tested on a phone, appear not to work, and be "fixed"
 * again. Tying the cache name to the bytes it holds ends that: any change to a
 * client file is a new cache, and CI will not let the two disagree. */
const CACHE = 'rainette-pwa-v16-a1fc264f';
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
  './src/downloads.js',
  './src/downloadmenu.js',
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
