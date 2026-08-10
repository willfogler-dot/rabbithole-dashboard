/* The Rabbit Hole — service worker
   ═══════════════════════════════════════════════════════════════
   Goal: the dashboard itself (this app shell) opens even with no
   signal. Your actual data — Supabase, Garmin sync, the Jarvis API
   call — still needs a real connection; this only makes sure the
   page isn't blank when you don't have one.

   Bump CACHE_VERSION whenever you want to force every device to pick
   up a fresh shell right away instead of waiting for the natural
   network-first refresh below.
   ═══════════════════════════════════════════════════════════════ */
const CACHE_VERSION = 'v34';
const SHELL_CACHE = 'rabbithole-shell-' + CACHE_VERSION;
const RUNTIME_CACHE = 'rabbithole-runtime-' + CACHE_VERSION;

/* Live data, not app shell — never serve these from cache. Stale data
   pretending to be fresh (a "readiness score" from three hours ago,
   served silently) is worse than a clear network error. */
const NEVER_CACHE_HOSTS = [
  'supabase.co',
  'generativelanguage.googleapis.com',
];

self.addEventListener('install', function(event){
  self.skipWaiting();
});

self.addEventListener('activate', function(event){
  event.waitUntil(
    caches.keys()
      .then(function(keys){
        return Promise.all(
          keys.filter(function(k){ return k !== SHELL_CACHE && k !== RUNTIME_CACHE; })
              .map(function(k){ return caches.delete(k); })
        );
      })
      .then(function(){ return self.clients.claim(); })
  );
});

function isNeverCache(url){
  return NEVER_CACHE_HOSTS.some(function(host){ return url.indexOf(host) >= 0; });
}

self.addEventListener('fetch', function(event){
  const req = event.request;
  if (req.method !== 'GET') return;               // never intercept writes
  if (isNeverCache(req.url)) return;                // let live data pass straight through

  /* The page itself: network-first, so an edit you push shows up the next
     time you open the app while online. Cache is only the fallback for
     when there's genuinely no connection. */
  if (req.mode === 'navigate'){
    event.respondWith(
      fetch(req)
        .then(function(res){
          const copy = res.clone();
          caches.open(SHELL_CACHE).then(function(c){ c.put(req, copy); });
          return res;
        })
        .catch(function(){
          return caches.match(req).then(function(cached){
            return cached || caches.match('./');
          });
        })
    );
    return;
  }

  /* Everything else — the icon, Google Fonts, the Leaflet map bundle for
     Routes: stale-while-revalidate. Show whatever's cached instantly,
     quietly refresh it in the background for next time. */
  event.respondWith(
    caches.open(RUNTIME_CACHE).then(function(cache){
      return cache.match(req).then(function(cached){
        const network = fetch(req)
          .then(function(res){
            if (res && res.status === 200) cache.put(req, res.clone());
            return res;
          })
          .catch(function(){ return cached; });
        return cached || network;
      });
    })
  );
});
