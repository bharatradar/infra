const CACHE_NAME = 'bharatradar-v1';
const PRECACHE_URLS = [
  '/static/css/common.css',
  '/static/js/app.js',
  '/static/js/tar1090-markers.js',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/manifest.json',
];

// Install: precache app shell
self.addEventListener('install', function(event) {
  event.waitUntil(
    caches.open(CACHE_NAME).then(function(cache) {
      return cache.addAll(PRECACHE_URLS);
    }).then(function() {
      return self.skipWaiting();
    })
  );
});

// Activate: cleanup old caches
self.addEventListener('activate', function(event) {
  event.waitUntil(
    caches.keys().then(function(cacheNames) {
      return Promise.all(
        cacheNames.map(function(name) {
          if (name !== CACHE_NAME) {
            return caches.delete(name);
          }
        })
      );
    }).then(function() {
      return self.clients.claim();
    })
  );
});

// Fetch: cache-first for static, network-first for API
self.addEventListener('fetch', function(event) {
  const url = new URL(event.request.url);

  // API calls: network-first
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(
      fetch(event.request).catch(function() {
        return caches.match(event.request);
      })
    );
    return;
  }

  // Static assets: cache-first
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(event.request).then(function(cached) {
        return cached || fetch(event.request).then(function(response) {
          return caches.open(CACHE_NAME).then(function(cache) {
            cache.put(event.request, response.clone());
            return response;
          });
        });
      })
    );
    return;
  }

  // Everything else: network-first
  event.respondWith(
    fetch(event.request).catch(function() {
      return caches.match(event.request);
    })
  );
});

// Keep existing VAPID push handlers
self.addEventListener('push', function(event) {
  if (event.data) {
    try {
      const payload = event.data.json();
      event.waitUntil(
        self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(windowClients) {
          for (let client of windowClients) {
            client.postMessage({
              type: 'INCOMING_ALERT',
              message: payload.body
            });
          }
        })
      );
      const options = {
        body: payload.body,
        icon: payload.icon || 'https://cdn-icons-png.flaticon.com/512/785/785116.png',
        badge: 'https://cdn-icons-png.flaticon.com/512/785/785116.png',
        vibrate: [200, 100, 200],
        requireInteraction: true,
        data: { dateOfArrival: Date.now(), primaryKey: 1 }
      };
      event.waitUntil(
        self.registration.showNotification(payload.title || 'Raga Radar Alert', options)
      );
    } catch (e) {
      console.error("Error parsing push payload", e);
    }
  }
});

self.addEventListener('notificationclick', function(event) {
  event.notification.close();
  event.waitUntil(
    clients.matchAll({ type: 'window' }).then(function(windowClients) {
      for (let client of windowClients) {
        if (client.url.includes('/command_center') && 'focus' in client) {
          return client.focus();
        }
      }
      if (clients.openWindow) {
        return clients.openWindow('/command_center/dashboard');
      }
    })
  );
});
