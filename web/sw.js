const CACHE='finance-dashboard-shell-v31';const SHELL=['/','/index.html','/income-tracker.html','/income-history.html','/income-spending.html','/personal-finances.html','/offline-vault.js','/theme.css'];
self.addEventListener('install',e=>e.waitUntil(caches.open(CACHE).then(c=>c.addAll(SHELL)).then(()=>self.skipWaiting())));
self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(key=>key.startsWith('finance-dashboard-shell-')&&key!==CACHE).map(key=>caches.delete(key)))).then(()=>self.clients.claim())));
self.addEventListener('fetch',e=>{const u=new URL(e.request.url);if(e.request.method!=='GET'||u.origin!==location.origin||u.pathname.startsWith('/api/'))return;
e.respondWith(fetch(e.request).then(r=>{if(r.ok){const clone=r.clone();caches.open(CACHE).then(c=>c.put(e.request,clone));}return r}).catch(()=>caches.match(e.request).then(r=>r||caches.match('/index.html'))));});
