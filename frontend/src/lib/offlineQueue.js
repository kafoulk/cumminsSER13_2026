const DB_NAME = "cummins_service_reboot";
const STORE_NAME = "offline_requests";
const DB_VERSION = 1;
const FALLBACK_KEY = "cummins_offline_queue_fallback";

export const OFFLINE_QUEUE_EVENT = "offline-queue-updated";

function hasWindow() {
  return typeof window !== "undefined";
}

function supportsIndexedDb() {
  return hasWindow() && typeof window.indexedDB !== "undefined";
}

function nowIso() {
  return new Date().toISOString();
}

function emitQueueEvent() {
  if (!hasWindow()) return;
  window.dispatchEvent(new Event(OFFLINE_QUEUE_EVENT));
}

function readFallbackQueue() {
  if (!hasWindow()) return [];
  try {
    const raw = window.localStorage.getItem(FALLBACK_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function writeFallbackQueue(items) {
  if (!hasWindow()) return;
  window.localStorage.setItem(FALLBACK_KEY, JSON.stringify(items));
  emitQueueEvent();
}

function openDb() {
  return new Promise((resolve, reject) => {
    if (!supportsIndexedDb()) {
      reject(new Error("IndexedDB is not available in this environment."));
      return;
    }

    const request = window.indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        db.createObjectStore(STORE_NAME, { keyPath: "id", autoIncrement: true });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("Failed to open IndexedDB."));
  });
}

async function runWithStore(mode, action) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, mode);
    const store = tx.objectStore(STORE_NAME);

    let didResolve = false;
    const done = (value) => {
      if (!didResolve) {
        didResolve = true;
        resolve(value);
      }
    };

    tx.oncomplete = () => {
      db.close();
    };
    tx.onerror = () => {
      reject(tx.error || new Error("IndexedDB transaction failed."));
      db.close();
    };
    tx.onabort = () => {
      reject(tx.error || new Error("IndexedDB transaction aborted."));
      db.close();
    };

    action(store, done, reject);
  });
}

function normalizeQueuedRequest(payload) {
  return {
    path: payload.path,
    method: String(payload.method || "POST").toUpperCase(),
    body: payload.body || {},
    ts: payload.ts || nowIso(),
    reason: payload.reason || "network_error",
    retries: Number(payload.retries || 0),
    last_error: payload.last_error || "",
  };
}

export async function queueRequest(payload) {
  const request = normalizeQueuedRequest(payload);

  if (!supportsIndexedDb()) {
    const queue = readFallbackQueue();
    const id = Date.now() + Math.floor(Math.random() * 1000);
    const item = { id, ...request };
    queue.push(item);
    writeFallbackQueue(queue);
    return item;
  }

  const item = await runWithStore("readwrite", (store, done, reject) => {
    const addReq = store.add(request);
    addReq.onsuccess = () => done({ id: addReq.result, ...request });
    addReq.onerror = () => reject(addReq.error || new Error("Failed to queue request."));
  });

  emitQueueEvent();
  return item;
}

export async function getQueuedRequests() {
  if (!supportsIndexedDb()) {
    return readFallbackQueue().sort((a, b) => a.id - b.id);
  }

  const items = await runWithStore("readonly", (store, done, reject) => {
    const getReq = store.getAll();
    getReq.onsuccess = () => done(getReq.result || []);
    getReq.onerror = () => reject(getReq.error || new Error("Failed to read queue."));
  });

  return items.sort((a, b) => a.id - b.id);
}

export async function removeQueuedRequest(id) {
  if (!supportsIndexedDb()) {
    const queue = readFallbackQueue().filter((item) => item.id !== id);
    writeFallbackQueue(queue);
    return;
  }

  await runWithStore("readwrite", (store, done, reject) => {
    const deleteReq = store.delete(id);
    deleteReq.onsuccess = () => done(true);
    deleteReq.onerror = () => reject(deleteReq.error || new Error("Failed to remove queue item."));
  });

  emitQueueEvent();
}

export async function updateQueuedRequest(id, updater) {
  if (!supportsIndexedDb()) {
    const queue = readFallbackQueue();
    const idx = queue.findIndex((item) => item.id === id);
    if (idx === -1) return;
    queue[idx] = updater(queue[idx]);
    writeFallbackQueue(queue);
    return;
  }

  await runWithStore("readwrite", (store, done, reject) => {
    const getReq = store.get(id);
    getReq.onerror = () => reject(getReq.error || new Error("Failed to read queue item."));
    getReq.onsuccess = () => {
      const existing = getReq.result;
      if (!existing) {
        done(false);
        return;
      }
      const next = updater(existing);
      const putReq = store.put(next);
      putReq.onsuccess = () => done(true);
      putReq.onerror = () => reject(putReq.error || new Error("Failed to update queue item."));
    };
  });

  emitQueueEvent();
}

export async function getQueueCount() {
  const items = await getQueuedRequests();
  return items.length;
}

function parseResponseSafely(text) {
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    return { raw: text };
  }
}

async function replayOneRequest(apiBaseUrl, item, timeoutMs) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
  const url = `${apiBaseUrl}${item.path}`;

  try {
    const response = await fetch(url, {
      method: item.method,
      headers: { "Content-Type": "application/json" },
      body: item.body ? JSON.stringify(item.body) : undefined,
      signal: controller.signal,
    });

    const text = await response.text();
    const payload = parseResponseSafely(text);

    if (!response.ok) {
      const detail = payload?.detail || `Request failed with ${response.status}`;
      throw new Error(detail);
    }

    return { ok: true, payload };
  } finally {
    clearTimeout(timeoutId);
  }
}

export async function replayQueuedRequests({ apiBaseUrl, timeoutMs = 15000 }) {
  const items = await getQueuedRequests();
  let synced = 0;
  let failed = 0;
  const results = [];

  for (const item of items) {
    try {
      const replayed = await replayOneRequest(apiBaseUrl, item, timeoutMs);
      await removeQueuedRequest(item.id);
      synced += 1;
      results.push({ id: item.id, ok: true, response: replayed.payload });
    } catch (error) {
      failed += 1;
      await updateQueuedRequest(item.id, (existing) => ({
        ...existing,
        retries: Number(existing.retries || 0) + 1,
        last_error: String(error?.message || "Replay failed"),
      }));
      results.push({
        id: item.id,
        ok: false,
        error: String(error?.message || "Replay failed"),
      });
    }
  }

  const remaining = await getQueueCount();
  emitQueueEvent();
  return {
    processed: items.length,
    synced,
    failed,
    remaining,
    ts: nowIso(),
    results,
  };
}
