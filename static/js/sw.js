// Service Worker: dos trabajos.
//   1. Cachear el "app shell" para que la app abra sin señal. Sin esto todo el diseño
//      offline (fotos en IndexedDB + Background Sync) era inútil: el repartidor sin
//      cobertura no podía ni cargar la página para tomar la foto.
//   2. Drenar la cola de IndexedDB cuando el navegador dispara 'sync'.
importScripts('/static/js/idb-queue.js');

// IMPORTANTE: subir este número al cambiar app.css, index.html o los JS. Los estáticos
// se sirven desde caché primero y se refrescan en segundo plano, así que sin bump el
// repartidor sigue viendo la versión anterior hasta la siguiente apertura de la app.
const CACHE = 'entregas-shell-v3';

// Todo same-origin: si un recurso viniera de un CDN, install fallaría sin red y el
// SW nunca se activaría.
const SHELL = [
  '/',
  '/static/css/app.css',
  '/static/js/idb-queue.js',
  '/static/js/compress.js',
  '/static/img/icon.svg',
  '/manifest.webmanifest',
];

// Nunca cachear: siempre tienen que ir a la red.
const NO_CACHE = ['/start_upload', '/get_order_info', '/api/', '/admin/'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)));
    await self.clients.claim();
    // Los jobs terminados guardan los blobs de las fotos; sin purga el
    // almacenamiento del teléfono crece hasta llenarse.
    try { await UploadQueue.purgeOldJobs(); } catch (e) { /* no es crítico */ }
  })());
});

// ---------- Caché ----------
function isShellRequest(url) {
  return SHELL.includes(url.pathname);
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (NO_CACHE.some((p) => url.pathname.startsWith(p))) return;

  // Navegación: red primero (para que los cambios lleguen), caché como respaldo.
  // Solo se responde desde caché la raíz — servir la app de entregas cuando se pidió
  // /historial sería engañoso.
  if (req.mode === 'navigate') {
    event.respondWith((async () => {
      try {
        const fresh = await fetch(req);
        if (url.pathname === '/' && fresh.ok) {
          const cache = await caches.open(CACHE);
          cache.put('/', fresh.clone());
        }
        return fresh;
      } catch (e) {
        const cached = await caches.match(url.pathname === '/' ? '/' : req);
        if (cached) return cached;
        throw e;
      }
    })());
    return;
  }

  // Estáticos del shell: caché primero (arranque instantáneo), refresco en segundo plano.
  if (isShellRequest(url)) {
    event.respondWith((async () => {
      const cache = await caches.open(CACHE);
      const cached = await cache.match(req);
      if (cached) {
        fetch(req)
          .then((fresh) => { if (fresh.ok) cache.put(req, fresh); })
          .catch(() => {});
        return cached;
      }
      const fresh = await fetch(req);
      if (fresh.ok) cache.put(req, fresh.clone());
      return fresh;
    })());
  }
});

// ---------- Cola de subidas ----------
async function broadcast(msg) {
  const clientsList = await self.clients.matchAll({ includeUncontrolled: true, type: 'window' });
  clientsList.forEach((c) => c.postMessage(msg));
}

async function setStatus(jobId, status, extra = {}) {
  await UploadQueue.updateJobStatus(jobId, status, extra);
  await broadcast({ type: 'job-status', job_id: jobId, status, ...extra });
}

function buildFormData(job) {
  const fd = new FormData();
  fd.append('job_id', job.job_id);
  fd.append('order_numbers_json', JSON.stringify(job.orders.map((o) => o.raw)));
  job.photos.forEach((p) => fd.append('photos', p.blob, p.name));
  return fd;
}

async function uploadJob(job) {
  if (!job.photos || !job.photos.length) {
    // Job ya subido cuyas fotos se liberaron: nada que reenviar.
    await setStatus(job.job_id, 'done', { error: null });
    return;
  }

  await setStatus(job.job_id, 'uploading');

  let res, data;
  try {
    res = await fetch('/start_upload', { method: 'POST', body: buildFormData(job) });
  } catch (networkErr) {
    await setStatus(job.job_id, 'queued');
    throw networkErr; // sin red: que el navegador reprograme el reintento
  }

  try {
    data = await res.json();
  } catch (e) {
    data = null; // respuesta no-JSON (proxy, error de plataforma): se decide por el status
  }

  if (res.ok && data && data.success) {
    await setStatus(job.job_id, 'done', {
      result: data.result || null,
      error: null,
      photo_count: job.photos.length,
      photos: [], // libera los blobs del teléfono; el job queda como comprobante
    });
    return;
  }

  const msg = (data && data.error) || ('HTTP ' + res.status);

  // 409 = el servidor ya está procesando este mismo job: transitorio, esperar.
  // Otro 4xx (400 validación, 413 muy grande, 422 orden inexistente / clientes
  // distintos) = no se arregla reintentando, mostrarlo como error al repartidor.
  // 5xx = transitorio (Salesforce caído, worker reiniciado): reintentar.
  const esPermanente = res.status !== 409 && res.status >= 400 && res.status < 500;

  if (esPermanente) {
    await setStatus(job.job_id, 'error', { error: msg, photo_count: job.photos.length });
    return;
  }

  await setStatus(job.job_id, 'queued', { error: msg });
  throw new Error(msg); // le indica al navegador que reprograme Background Sync
}

async function drainQueue() {
  const pending = await UploadQueue.getJobsToUpload();
  let lastError = null;
  for (const job of pending) {
    try {
      await uploadJob(job);
    } catch (err) {
      lastError = err;
    }
  }
  if (lastError) throw lastError; // le indica al navegador que reprograme Background Sync
}

self.addEventListener('sync', (event) => {
  if (event.tag === 'upload-queue') {
    event.waitUntil(drainQueue());
  }
});

// Respaldo: si el navegador no soporta Background Sync, la página pide un intento inmediato.
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'try-upload') {
    event.waitUntil(drainQueue().catch(() => {}));
  }
});
