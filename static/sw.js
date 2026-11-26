const CACHE_NAME = 'readout-v7';
const STATIC_ASSETS = [
  '/',
  '/manifest.json',
  '/static/css/style.css',
  '/static/js/app.js',
  '/static/manifest.json',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png'
];

// Install: Cache critical static assets
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      console.log('[SW] Pre-caching static assets');
      return cache.addAll(STATIC_ASSETS);
    }).then(() => self.skipWaiting())
  );
});

// Activate: Clean up old caches
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.map((key) => {
          if (key !== CACHE_NAME) {
            console.log('[SW] Deleting old cache:', key);
            return caches.delete(key);
          }
        })
      );
    }).then(() => self.clients.claim())
  );
});

// Helper: Handle HTTP Range requests for cached audio files
async function handleAudioRangeRequest(request, cache) {
  // Check if we have the full file in cache
  const cleanUrl = request.url.split('#')[0];
  const cachedResponse = await cache.match(cleanUrl);

  if (cachedResponse) {
    const rangeHeader = request.headers.get('range');
    if (!rangeHeader) {
      return cachedResponse;
    }

    const arrayBuffer = await cachedResponse.arrayBuffer();
    const totalBytes = arrayBuffer.byteLength;
    const parts = rangeHeader.replace(/bytes=/, '').split('-');
    const start = parseInt(parts[0], 10);
    const end = parts[1] ? parseInt(parts[1], 10) : totalBytes - 1;

    if (start >= totalBytes || end >= totalBytes) {
      return new Response('', {
        status: 416,
        statusText: 'Requested Range Not Satisfiable',
        headers: { 'Content-Range': `bytes */${totalBytes}` }
      });
    }

    const slicedBuffer = arrayBuffer.slice(start, end + 1);
    return new Response(slicedBuffer, {
      status: 206,
      statusText: 'Partial Content',
      headers: {
        'Content-Type': 'audio/mpeg',
        'Content-Range': `bytes ${start}-${end}/${totalBytes}`,
        'Content-Length': slicedBuffer.byteLength.toString(),
        'Accept-Ranges': 'bytes'
      }
    });
  }

  // Not in cache, try network and cache for next time
  try {
    const networkResponse = await fetch(request);
    // If it's a full response (200), store a clone in cache
    if (networkResponse.status === 200) {
      cache.put(cleanUrl, networkResponse.clone());
    }
    return networkResponse;
  } catch (err) {
    console.error('[SW] Network fetch failed for audio and not in cache:', request.url, err);
    throw err;
  }
}

// Fetch routing
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // 1. Audio file requests (/audio/*)
  if (url.pathname.startsWith('/audio/')) {
    event.respondWith(
      caches.open(CACHE_NAME).then((cache) => handleAudioRangeRequest(event.request, cache))
    );
    return;
  }

  // 2. API requests: Never cache non-GET or Curator/Feeds/Articles APIs
  if (event.request.method !== 'GET') {
    return;
  }

  if (url.pathname.startsWith('/api/queue')) {
    event.respondWith(
      fetch(event.request)
        .then((networkResponse) => {
          if (networkResponse.ok) {
            const clone = networkResponse.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
          }
          return networkResponse;
        })
        .catch(() => {
          // Return cached queue when offline
          return caches.match(event.request);
        })
    );
    return;
  }

  if (url.pathname.startsWith('/api/')) {
    event.respondWith(fetch(event.request));
    return;
  }

  // 3. Static assets and HTML: Cache First, fallback to Network
  event.respondWith(
    caches.match(event.request).then((cachedResponse) => {
      if (cachedResponse) {
        // Fetch in background to update cache for next time (Stale-While-Revalidate)
        fetch(event.request)
          .then((networkResponse) => {
            if (networkResponse.ok) {
              caches.open(CACHE_NAME).then((cache) => cache.put(event.request, networkResponse));
            }
          })
          .catch(() => {});
        return cachedResponse;
      }
      return fetch(event.request);
    })
  );
});

// Listen for messages from client (e.g. pre-cache audio command)
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'CACHE_AUDIO') {
    const audioUrl = event.data.url;
    caches.open(CACHE_NAME).then(async (cache) => {
      const match = await cache.match(audioUrl);
      if (!match) {
        console.log('[SW] Pre-caching audio:', audioUrl);
        try {
          const resp = await fetch(audioUrl);
          if (resp.ok) {
            await cache.put(audioUrl, resp);
            // Notify client that caching succeeded
            if (event.source) {
              event.source.postMessage({ type: 'AUDIO_CACHED', url: audioUrl });
            }
          }
        } catch (err) {
          console.warn('[SW] Failed to pre-cache audio:', audioUrl, err);
        }
      }
    });
  }
});
