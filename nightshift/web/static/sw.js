// Minimal service worker: makes nightshift installable and shows a friendly
// screen when you're offline. Job data is always fetched live.
const CACHE = "nightshift-shell-v1";
const SHELL = ["/static/app.css", "/static/app.js", "/static/icon-192.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/static/")) {
    e.respondWith(caches.match(e.request).then((hit) => hit || fetch(e.request)));
    return;
  }
  if (e.request.mode === "navigate") {
    e.respondWith(fetch(e.request).catch(() => new Response(
      '<!doctype html><meta name=viewport content="width=device-width,initial-scale=1"><body style="margin:0;font:16px system-ui;background:#1B1F3B;color:#F2F3FA;display:grid;place-items:center;min-height:100vh;text-align:center;padding:24px"><div><h1 style="font-size:24px">You\'re offline</h1><p style="color:#B9BDD8">Your jobs will be here when you reconnect.</p></div>',
      { headers: { "content-type": "text/html" } })));
  }
});
