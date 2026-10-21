// Офлайн-режим: страница и котировки сохраняются на телефоне.
const SHELL = 'replay-shell-v4';
const DATA = 'replay-data';
const FILES = ['./', 'index.html', 'manifest.webmanifest', 'icons/icon-180.png', 'icons/icon-192.png', 'icons/icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(FILES)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== SHELL && k !== DATA).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin) return;
  // годы истории хранятся в самом приложении, второй копией их не держим
  if (url.pathname.includes('/data/hist/') && !url.pathname.endsWith('index.json')) return;
  const isData = url.pathname.includes('/data/');
  const isPage = req.mode === 'navigate' || url.pathname.endsWith('/') || url.pathname.endsWith('index.html');
  if (isData || isPage) {
    // Сначала сеть (с проверкой изменений по ETag), без сети — сохранённая копия.
    e.respondWith(
      fetch(req).then(res => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(isData ? DATA : SHELL).then(c => c.put(url.pathname, copy));
        }
        return res;
      }).catch(() => caches.match(url.pathname).then(m => m || caches.match('index.html')))
    );
    return;
  }
  e.respondWith(caches.match(req).then(m => m || fetch(req)));
});
