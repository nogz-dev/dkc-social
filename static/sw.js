/* DKC Social — service worker: push + cache leve do app shell */
const CACHE = "dkc-social-v6";
const SHELL = ["/", "/static/app.css", "/static/app.js", "/manifest.webmanifest", "/static/icons/icon-192.png", "/static/logo-dkc.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
/* rede primeiro para o shell (sempre a versão nova), cache só sem internet; API e mídia nunca em cache */
self.addEventListener("fetch", (e) => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET" || u.origin !== location.origin) return;
  if (u.pathname.startsWith("/api/") || u.pathname.startsWith("/m/")) return;
  e.respondWith(fetch(e.request).then((r) => {
    if (r.ok && SHELL.includes(u.pathname)) { const cp = r.clone(); caches.open(CACHE).then((c) => c.put(e.request, cp)); }
    return r;
  }).catch(() => caches.match(e.request).then((r) => r || caches.match("/"))));
});

self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = { title: "DKC Social", body: e.data && e.data.text() }; }
  e.waitUntil(self.registration.showNotification(d.title || "DKC Social", {
    body: d.body || "", icon: "/static/icons/icon-192.png", badge: "/static/icons/badge-96.png",
    tag: d.tag || undefined, renotify: !!d.tag, data: { url: d.url || "/" },
  }));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || "/";
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((cs) => {
    for (const c of cs) {
      if (new URL(c.url).origin === location.origin) { c.postMessage({ tipo: "abrir", url }); return c.focus(); }
    }
    return self.clients.openWindow(url);
  }));
});
