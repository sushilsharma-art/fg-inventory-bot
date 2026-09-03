/* FG Inventory Assistant — service worker */
var CACHE = 'fg-chat-v8-lite';
/* Keep the first mobile load small. The 1.6 MB social preview is not app shell. */
var SHELL = ['./', './index.html', './manifest.webmanifest', './icon-192.png', './icon-512.png', './pako_inflate.min.js'];

self.addEventListener('install', function (e) {
  e.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(SHELL); }).then(function(){ return self.skipWaiting(); }));
});

self.addEventListener('activate', function (e) {
  e.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) { return k !== CACHE; }).map(function (k) { return caches.delete(k); }));
  }).then(function(){ return self.clients.claim(); }));
});

self.addEventListener('fetch', function (e) {
  var url = new URL(e.request.url);
  if (url.pathname.endsWith('data.enc.json') || url.pathname.endsWith('index.html') || url.pathname.endsWith('/')) {
    /* Data and chat shell are network-first so everyone receives today's bot. */
    e.respondWith(
      fetch(e.request).then(function (r) {
        var copy = r.clone();
        caches.open(CACHE).then(function (c) { c.put(e.request, copy); });
        return r;
      }).catch(function () { return caches.match(e.request); })
    );
  } else {
    /* Small static assets are cache-first. */
    e.respondWith(
      caches.match(e.request).then(function (hit) {
        var net = fetch(e.request).then(function (r) {
          var copy = r.clone();
          caches.open(CACHE).then(function (c) { c.put(e.request, copy); });
          return r;
        }).catch(function () { return hit; });
        return hit || net;
      })
    );
  }
});
