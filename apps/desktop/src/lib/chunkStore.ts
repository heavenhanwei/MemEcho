/**
 * Best-effort persistence of browser MediaRecorder chunks in IndexedDB.
 *
 * Chunks are namespaced by a stable recording id plus index, so an
 * interrupted recording can never be overwritten or mixed with a newer
 * one. Live recording still lives in page memory; this mirror survives
 * reloads and crashes so interrupted recordings can be listed, downloaded,
 * or discarded individually. Nothing here uploads automatically — recovery
 * is always an explicit user action.
 */

const DB_NAME = "memecho-recorder";
const DB_VERSION = 2;
const LEGACY_STORE = "chunks";
const STORE_NAME = "chunks_v2";
export const LEGACY_RECORDING_ID = "legacy";

const RECORDING_ID_RE = /^[A-Za-z0-9._:-]{1,128}$/;

interface StoredChunk {
  recordingId: string;
  index: number;
  blob: Blob;
  createdAt: number;
}

interface LegacyStoredChunk {
  index: number;
  blob: Blob;
}

export interface RecoverableRecording {
  recordingId: string;
  chunkCount: number;
  totalBytes: number;
  createdAt: number;
}

export function isChunkStoreAvailable(): boolean {
  return typeof indexedDB !== "undefined";
}

export function isValidRecordingId(recordingId: string): boolean {
  return RECORDING_ID_RE.test(recordingId);
}

function requestToPromise<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () =>
      reject(request.error ?? new Error("IndexedDB request failed"));
  });
}

function txDone(tx: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error ?? new Error("IndexedDB write failed"));
    tx.onabort = () => reject(tx.error ?? new Error("IndexedDB write aborted"));
  });
}

function migrateLegacy(db: IDBDatabase, tx: IDBTransaction) {
  if (!db.objectStoreNames.contains(LEGACY_STORE)) return;
  const legacy = tx.objectStore(LEGACY_STORE);
  const target = tx.objectStore(STORE_NAME);
  const all = legacy.getAll() as IDBRequest<LegacyStoredChunk[]>;
  all.onsuccess = () => {
    for (const record of all.result ?? []) {
      if (!(record?.blob instanceof Blob) || typeof record.index !== "number") {
        continue;
      }
      target.put({
        recordingId: LEGACY_RECORDING_ID,
        index: record.index,
        blob: record.blob,
        createdAt: 0,
      } satisfies StoredChunk);
    }
    legacy.clear();
  };
}

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        db.createObjectStore(STORE_NAME, { keyPath: ["recordingId", "index"] });
      }
      if (request.transaction) {
        migrateLegacy(db, request.transaction);
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () =>
      reject(request.error ?? new Error("IndexedDB open failed"));
  });
}

export async function persistRecordingChunk(
  recordingId: string,
  index: number,
  blob: Blob,
): Promise<void> {
  if (!isChunkStoreAvailable()) return;
  if (!isValidRecordingId(recordingId)) {
    throw new Error("invalid recording id");
  }
  const db = await openDb();
  try {
    const tx = db.transaction(STORE_NAME, "readwrite");
    tx.objectStore(STORE_NAME).put({
      recordingId,
      index,
      blob,
      createdAt: Date.now(),
    } satisfies StoredChunk);
    await txDone(tx);
  } finally {
    db.close();
  }
}

export async function listRecoverableRecordings(): Promise<RecoverableRecording[]> {
  if (!isChunkStoreAvailable()) return [];
  const db = await openDb();
  try {
    const store = db.transaction(STORE_NAME, "readonly").objectStore(STORE_NAME);
    const records = await requestToPromise(
      store.getAll() as IDBRequest<StoredChunk[]>,
    );
    const byRecording = new Map<string, RecoverableRecording>();
    for (const record of records) {
      if (!(record?.blob instanceof Blob)) continue;
      const summary = byRecording.get(record.recordingId) ?? {
        recordingId: record.recordingId,
        chunkCount: 0,
        totalBytes: 0,
        createdAt: Number.MAX_SAFE_INTEGER,
      };
      summary.chunkCount += 1;
      summary.totalBytes += record.blob.size;
      summary.createdAt = Math.min(summary.createdAt, record.createdAt || 0);
      byRecording.set(record.recordingId, summary);
    }
    return [...byRecording.values()].sort((a, b) => a.createdAt - b.createdAt);
  } finally {
    db.close();
  }
}

export async function loadRecordingChunks(recordingId: string): Promise<Blob[]> {
  if (!isChunkStoreAvailable()) return [];
  if (!isValidRecordingId(recordingId)) return [];
  const db = await openDb();
  try {
    const store = db.transaction(STORE_NAME, "readonly").objectStore(STORE_NAME);
    const records = await requestToPromise(
      store.getAll(IDBKeyRange.bound([recordingId, 0], [recordingId, []])) as IDBRequest<
        StoredChunk[]
      >,
    );
    return records
      .filter((record) => record.blob instanceof Blob)
      .sort((left, right) => left.index - right.index)
      .map((record) => record.blob);
  } finally {
    db.close();
  }
}

export async function clearRecordingChunks(recordingId: string): Promise<void> {
  if (!isChunkStoreAvailable()) return;
  if (!isValidRecordingId(recordingId)) return;
  const db = await openDb();
  try {
    const tx = db.transaction(STORE_NAME, "readwrite");
    tx.objectStore(STORE_NAME).delete(
      IDBKeyRange.bound([recordingId, 0], [recordingId, []]),
    );
    await txDone(tx);
  } finally {
    db.close();
  }
}
