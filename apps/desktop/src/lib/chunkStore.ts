/**
 * Best-effort persistence of browser MediaRecorder chunks in IndexedDB.
 *
 * Live recording still lives in page memory; this mirror survives page
 * reloads and crashes so an interrupted recording can be downloaded or
 * discarded instead of silently disappearing. Never uploads anything by
 * itself — recovery is always an explicit user action.
 */

const DB_NAME = "memecho-recorder";
const STORE_NAME = "chunks";
const DB_VERSION = 1;

interface StoredChunk {
  index: number;
  blob: Blob;
}

export function isChunkStoreAvailable(): boolean {
  return typeof indexedDB !== "undefined";
}

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        db.createObjectStore(STORE_NAME, { keyPath: "index" });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("IndexedDB open failed"));
  });
}

function requestToPromise<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("IndexedDB request failed"));
  });
}

export async function persistRecordingChunk(index: number, blob: Blob): Promise<void> {
  if (!isChunkStoreAvailable()) return;
  const db = await openDb();
  try {
    const tx = db.transaction(STORE_NAME, "readwrite");
    tx.objectStore(STORE_NAME).put({ index, blob } satisfies StoredChunk);
    await new Promise<void>((resolve, reject) => {
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error ?? new Error("IndexedDB write failed"));
      tx.onabort = () => reject(tx.error ?? new Error("IndexedDB write aborted"));
    });
  } finally {
    db.close();
  }
}

export async function loadPendingChunks(): Promise<Blob[]> {
  if (!isChunkStoreAvailable()) return [];
  const db = await openDb();
  try {
    const store = db.transaction(STORE_NAME, "readonly").objectStore(STORE_NAME);
    const records = await requestToPromise(store.getAll() as IDBRequest<StoredChunk[]>);
    return records
      .filter((record) => record.blob instanceof Blob)
      .sort((left, right) => left.index - right.index)
      .map((record) => record.blob);
  } finally {
    db.close();
  }
}

export async function clearPendingChunks(): Promise<void> {
  if (!isChunkStoreAvailable()) return;
  const db = await openDb();
  try {
    const tx = db.transaction(STORE_NAME, "readwrite");
    tx.objectStore(STORE_NAME).clear();
    await new Promise<void>((resolve, reject) => {
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error ?? new Error("IndexedDB clear failed"));
      tx.onabort = () => reject(tx.error ?? new Error("IndexedDB clear aborted"));
    });
  } finally {
    db.close();
  }
}
