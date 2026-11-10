const CACHE_NAME = 'taskforge-pwa-v1';
const STATIC_ASSETS = [
  '/',
  '/manifest.json',
  '/static/icon.svg',
  '/static/app.js'
];

self.addEventListener('install', (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS).catch(() => {}))
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET' || req.url.includes('/api/') || req.url.includes('/executions')) {
    return;
  }
  event.respondWith(
    fetch(req)
      .then((res) => {
        const copy = res.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(req, copy)).catch(() => {});
        return res;
      })
      .catch(() => caches.match(req))
  );
});

self.addEventListener('push', (event) => {
  let payload = {
    title: 'TaskForge',
    body: 'Você tem uma atualização de tarefas.',
    url: '/',
    tag: 'taskforge-notification'
  };

  if (event.data) {
    try {
      payload = Object.assign(payload, JSON.parse(event.data.text()));
    } catch (e) {
      payload.body = event.data.text();
    }
  }

  const options = {
    body: payload.body,
    icon: '/static/icon.svg',
    badge: '/static/icon.svg',
    tag: payload.tag || 'taskforge',
    renotify: true,
    vibrate: [150, 80, 150],
    data: { url: payload.url || '/' }
  };

  event.waitUntil(self.registration.showNotification(payload.title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const targetUrl = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
      for (const client of clientList) {
        if ('focus' in client) {
          return client.focus();
        }
      }
      if (self.clients.openWindow) {
        return self.clients.openWindow(targetUrl);
      }
    })
  );
});
