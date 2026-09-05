/* 只做一件事：让"外壳"离线可用。模型本体由 transformers.js 的 Cache Storage 自己管。
   不要在这里预缓存模型 —— 几百 MB 预缓存会让首次打开变黑屏。 */
const CACHE = "localmt-shell-v1";
const SHELL = ["./", "./index.html", "./app.js", "./manifest.webmanifest"];

addEventListener("install", (e) =>
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())));

addEventListener("activate", (e) =>
  e.waitUntil(
    caches.keys()
      .then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  ));

addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  const isShell = url.origin === location.origin || SHELL.some((s) => url.pathname.endsWith(s.slice(1)));
  if (isShell) {
    // 外壳 cache-first：离线冷启动也能打开（这才是"像 App"的关键）
    e.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then((r) => {
        const cp = r.clone();
        caches.open(CACHE).then((c) => c.put(req, cp));
        return r;
      }))
    );
    return;
  }
  // CDN / 模型权重：network-first，成功后落盘；断网时命中缓存 → 飞行模式可推理
  e.respondWith(
    fetch(req)
      .then((r) => {
        if (r.ok && r.type !== "opaque") {
          const cp = r.clone();
          caches.open(CACHE).then((c) => c.put(req, cp));
        }
        return r;
      })
      .catch(() => caches.match(req).then((hit) => hit || Response.error()))
  );
});
