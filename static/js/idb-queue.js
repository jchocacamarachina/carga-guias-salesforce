// Cola de subidas persistida en IndexedDB. Script clásico: se carga con <script src>
// en la página y con importScripts() dentro del Service Worker, por eso no usa
// `document`/`window` directamente y expone todo en el objeto global `UploadQueue`.
(function (global) {
  const DB_NAME = 'entregas_queue';
  const DB_VERSION = 1;
  const STORE = 'jobs';

  function newId() {
    if (global.crypto && global.crypto.randomUUID) return global.crypto.randomUUID();
    return 'id-' + Date.now() + '-' + Math.random().toString(16).slice(2);
  }

  function openDB() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains(STORE)) {
          db.createObjectStore(STORE, { keyPath: 'job_id' });
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  async function withStore(mode, fn) {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, mode);
      const store = tx.objectStore(STORE);
      let result;
      Promise.resolve(fn(store))
        .then((r) => { result = r; })
        .catch(reject);
      tx.oncomplete = () => resolve(result);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    });
  }

  function reqToPromise(req) {
    return new Promise((resolve, reject) => {
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  async function getDraftJob() {
    return withStore('readonly', async (store) => {
      const all = await reqToPromise(store.getAll());
      return all.find((j) => j.status === 'draft') || null;
    });
  }

  async function addPhotoToDraft(orders, blob, filename) {
    return withStore('readwrite', async (store) => {
      const all = await reqToPromise(store.getAll());
      let job = all.find((j) => j.status === 'draft');
      const now = Date.now();
      if (!job) {
        job = {
          job_id: newId(),
          status: 'draft',
          orders: orders,
          photos: [],
          created_at: now,
          updated_at: now,
          result: null,
          error: null,
        };
      }
      job.orders = orders;
      job.photos.push({ id: newId(), name: filename, type: blob.type, blob });
      job.updated_at = now;
      store.put(job);
      return job;
    });
  }

  async function updateDraftOrders(orders) {
    return withStore('readwrite', async (store) => {
      const all = await reqToPromise(store.getAll());
      const job = all.find((j) => j.status === 'draft');
      if (!job) return null;
      job.orders = orders;
      job.updated_at = Date.now();
      store.put(job);
      return job;
    });
  }

  async function removePhotoFromDraft(photoId) {
    return withStore('readwrite', async (store) => {
      const all = await reqToPromise(store.getAll());
      const job = all.find((j) => j.status === 'draft');
      if (!job) return null;
      job.photos = job.photos.filter((p) => p.id !== photoId);
      job.updated_at = Date.now();
      store.put(job);
      return job;
    });
  }

  async function clearDraft() {
    return withStore('readwrite', async (store) => {
      const all = await reqToPromise(store.getAll());
      const job = all.find((j) => j.status === 'draft');
      if (job) store.delete(job.job_id);
    });
  }

  async function finalizeJob() {
    return withStore('readwrite', async (store) => {
      const all = await reqToPromise(store.getAll());
      const job = all.find((j) => j.status === 'draft');
      if (!job || !job.photos.length || !job.orders.length) return null;
      job.status = 'queued';
      job.updated_at = Date.now();
      store.put(job);
      return job;
    });
  }

  async function getAllJobs() {
    return withStore('readonly', (store) => reqToPromise(store.getAll())).then((jobs) =>
      jobs.sort((a, b) => b.updated_at - a.updated_at)
    );
  }

  // Solo 'queued'. Los 'error' quedan fuera a propósito: son fallos permanentes
  // (orden inexistente, clientes distintos) que no se arreglan reintentando solos.
  // Los fallos transitorios vuelven a 'queued', así que sí se reintentan.
  async function getJobsToUpload() {
    const all = await getAllJobs();
    return all.filter((j) => j.status === 'queued');
  }

  // Reintento manual desde la cola: para cuando el error permanente ya se corrigió
  // (p. ej. se creó la orden en Salesforce). Sin esto un job en 'error' solo se puede
  // descartar, perdiendo las fotos.
  async function retryJob(jobId) {
    return withStore('readwrite', async (store) => {
      const job = await reqToPromise(store.get(jobId));
      if (!job || !job.photos || !job.photos.length) return null;
      job.status = 'queued';
      job.error = null;
      job.updated_at = Date.now();
      store.put(job);
      return job;
    });
  }

  // Los jobs terminados conservan los blobs de las fotos hasta que alguien los
  // descarta a mano, y así el almacenamiento del teléfono se llena. Se purgan solos.
  const DONE_TTL_MS = 3 * 24 * 60 * 60 * 1000;   // 3 días
  const ERROR_TTL_MS = 14 * 24 * 60 * 60 * 1000; // 14 días (dan margen para revisarlos)

  async function purgeOldJobs() {
    const now = Date.now();
    return withStore('readwrite', async (store) => {
      const all = await reqToPromise(store.getAll());
      let removed = 0;
      for (const job of all) {
        const age = now - (job.updated_at || 0);
        const ttl = job.status === 'done' ? DONE_TTL_MS
                  : job.status === 'error' ? ERROR_TTL_MS
                  : null;
        if (ttl !== null && age > ttl) {
          store.delete(job.job_id);
          removed++;
        }
      }
      return removed;
    });
  }

  async function updateJobStatus(jobId, status, extra = {}) {
    return withStore('readwrite', async (store) => {
      const job = await reqToPromise(store.get(jobId));
      if (!job) return null;
      job.status = status;
      job.updated_at = Date.now();
      Object.assign(job, extra);
      store.put(job);
      return job;
    });
  }

  async function dismissJob(jobId) {
    return withStore('readwrite', (store) => store.delete(jobId));
  }

  global.UploadQueue = {
    getDraftJob,
    addPhotoToDraft,
    updateDraftOrders,
    removePhotoFromDraft,
    clearDraft,
    finalizeJob,
    getAllJobs,
    getJobsToUpload,
    updateJobStatus,
    retryJob,
    purgeOldJobs,
    dismissJob,
  };
})(self);
