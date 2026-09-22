const CACHE = 'saferoad-v3';
const SHELL = ['/static/app.js?v=3', '/static/icons/icon-192.png', '/static/icons/icon-512.png', '/manifest.webmanifest'];
self.addEventListener('install', (e) => { e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())); });
self.addEventListener('activate', (e) => { e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim())); });
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.pathname.startsWith('/events') || url.pathname.startsWith('/api')) return;
  if (url.pathname.startsWith('/static') || url.pathname.startsWith('/media')) {
    e.respondWith(caches.match(e.request).then((hit) => hit || fetch(e.request).then((res) => { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); return res; })));
    return;
  }
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request).then((hit) => hit || new Response('<h1 style="font-family:sans-serif;padding:2rem">You are offline. SafeRoad needs a connection to check alerts.</h1>', { headers: { 'Content-Type': 'text/html' } }))));
});
self.addEventListener('push', (e) => {
  let d = {}; try { d = e.data ? e.data.json() : {}; } catch (_) { d = { title: 'SafeRoad', body: e.data ? e.data.text() : '' }; }
  e.waitUntil(self.registration.showNotification(d.title || 'SafeRoad', {
    body: d.body || '', icon: '/static/icons/icon-192.png', badge: '/static/icons/badge-96.png', tag: d.tag, renotify: true,
    vibrate: [200, 100, 200, 100, 400], data: { url: d.url || '/app' }, requireInteraction: !!d.tag && d.tag.startsWith('alert-'),
  }));
});
self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/app';
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((cs) => { for (const c of cs) { if ('focus' in c) { c.navigate(url); return c.focus(); } } return self.clients.openWindow(url); }));
});
