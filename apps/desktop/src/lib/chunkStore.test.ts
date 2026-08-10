// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  LEGACY_RECORDING_ID,
  clearRecordingChunks,
  isChunkStoreAvailable,
  listRecoverableRecordings,
  loadRecordingChunks,
  persistRecordingChunk,
} from "./chunkStore";

type AnyHandler = (() => void) | undefined;

interface StoredRecord {
  recordingId: string;
  index: number;
  blob: Blob;
  createdAt: number;
}

function compareValues(a: unknown, b: unknown): number {
  if (Array.isArray(a) && Array.isArray(b)) {
    const length = Math.max(a.length, b.length);
    for (let i = 0; i < length; i += 1) {
      if (i >= a.length) return -1;
      if (i >= b.length) return 1;
      const step = compareValues(a[i], b[i]);
      if (step !== 0) return step;
    }
    return 0;
  }
  if (typeof a === "number" && typeof b === "number") return a - b;
  if (typeof a === "string" && typeof b === "string") {
    return a < b ? -1 : a > b ? 1 : 0;
  }
  if (Array.isArray(a)) return 1;
  if (Array.isArray(b)) return -1;
  return 0;
}

function inRange(
  key: unknown[],
  range: { lower: unknown[]; upper: unknown[] } | null,
): boolean {
  if (!range) return true;
  return compareValues(key, range.lower) >= 0 && compareValues(key, range.upper) <= 0;
}

class FakeRequest<T> {
  result: T | undefined;
  onsuccess: AnyHandler;
  onerror: AnyHandler;

  constructor(result?: T) {
    this.result = result;
    queueMicrotask(() => this.onsuccess?.());
  }
}

class FakeObjectStore {
  constructor(
    private records: Map<string, StoredRecord>,
    private keyPath: string | string[],
  ) {}

  private keyOf(record: StoredRecord): unknown[] {
    const paths = Array.isArray(this.keyPath) ? this.keyPath : [this.keyPath];
    return paths.map((path) => (record as unknown as Record<string, unknown>)[path]);
  }

  put(record: StoredRecord) {
    this.records.set(JSON.stringify(this.keyOf(record)), record);
    return new FakeRequest(undefined);
  }

  getAll(range: { lower: unknown[]; upper: unknown[] } | null = null) {
    const values = [...this.records.values()].filter((record) =>
      inRange(this.keyOf(record) as unknown[], range),
    );
    return new FakeRequest(values) as unknown as IDBRequest;
  }

  delete(range: { lower: unknown[]; upper: unknown[] } | null = null) {
    for (const [key, record] of [...this.records.entries()]) {
      if (inRange(this.keyOf(record) as unknown[], range)) {
        this.records.delete(key);
      }
    }
    return new FakeRequest(undefined);
  }

  clear() {
    this.records.clear();
    return new FakeRequest(undefined);
  }
}

class FakeTransaction {
  oncomplete: AnyHandler;
  onerror: AnyHandler;
  onabort: AnyHandler;

  constructor(private stores: Map<string, FakeObjectStore>) {
    queueMicrotask(() => this.oncomplete?.());
  }

  objectStore(name: string) {
    const store = this.stores.get(name);
    if (!store) throw new Error(`missing object store ${name}`);
    return store;
  }
}

class FakeDb {
  constructor(
    private stores: Map<string, FakeObjectStore>,
    private storeNames: string[],
  ) {}

  get objectStoreNames() {
    const names = this.storeNames;
    return { contains: (name: string) => names.includes(name) };
  }

  createObjectStore(name: string, _options: unknown) {
    this.stores.set(name, new FakeObjectStore(new Map(), ["recordingId", "index"]));
    this.storeNames.push(name);
  }

  transaction(_name: string, _mode?: string) {
    return new FakeTransaction(this.stores);
  }

  close() {}
}

function installFakeIndexedDB(seedLegacy: { index: number; blob: Blob }[] = []) {
  const v2 = new Map<string, StoredRecord>();
  const legacy = new Map<string, StoredRecord>();
  for (const record of seedLegacy) {
    legacy.set(String(record.index), {
      recordingId: "",
      index: record.index,
      blob: record.blob,
      createdAt: 0,
    });
  }
  const state = {
    version: seedLegacy.length > 0 ? 1 : 0,
    stores: new Map<string, FakeObjectStore>(
      seedLegacy.length > 0
        ? [["chunks", new FakeObjectStore(legacy, "index")]]
        : [],
    ),
    storeNames: seedLegacy.length > 0 ? ["chunks"] : [],
  };

  const fake = {
    open(_name: string, version?: number) {
      const request: {
        result: FakeDb | undefined;
        transaction: FakeTransaction | null;
        onupgradeneeded: AnyHandler;
        onsuccess: AnyHandler;
        onerror: AnyHandler;
      } = {
        result: undefined,
        transaction: null,
        onupgradeneeded: undefined,
        onsuccess: undefined,
        onerror: undefined,
      };
      queueMicrotask(() => {
        if ((version ?? 1) > state.version) {
          if (!state.stores.has("chunks_v2")) {
            state.stores.set(
              "chunks_v2",
              new FakeObjectStore(v2, ["recordingId", "index"]),
            );
            state.storeNames.push("chunks_v2");
          }
          const db = new FakeDb(state.stores, state.storeNames);
          const tx = new FakeTransaction(state.stores);
          request.result = db;
          request.transaction = tx;
          request.onupgradeneeded?.();
          queueMicrotask(() => {
            state.version = version ?? 1;
            tx.oncomplete?.();
            request.result = db;
            request.transaction = null;
            request.onsuccess?.();
          });
          return;
        }
        request.result = new FakeDb(state.stores, state.storeNames);
        request.onsuccess?.();
      });
      return request;
    },
  };
  (globalThis as { indexedDB?: unknown }).indexedDB = fake;
  (globalThis as { IDBKeyRange?: unknown }).IDBKeyRange = {
    bound: (lower: unknown, upper: unknown) => ({ lower, upper }),
  };
  return { v2, legacy };
}

describe("chunkStore (namespaced per recording)", () => {
  beforeEach(() => {
    installFakeIndexedDB();
  });

  afterEach(() => {
    delete (globalThis as { indexedDB?: unknown }).indexedDB;
    delete (globalThis as { IDBKeyRange?: unknown }).IDBKeyRange;
  });

  it("keeps two interrupted recordings separate and lists both", async () => {
    await persistRecordingChunk("rec-a", 0, new Blob(["aaaa"]));
    await persistRecordingChunk("rec-a", 1, new Blob(["bb"]));
    await persistRecordingChunk("rec-b", 0, new Blob(["cccccc"]));

    const listed = await listRecoverableRecordings();
    expect(listed.map((item) => item.recordingId).sort()).toEqual(["rec-a", "rec-b"]);
    const recA = listed.find((item) => item.recordingId === "rec-a");
    const recB = listed.find((item) => item.recordingId === "rec-b");
    expect(recA?.chunkCount).toBe(2);
    expect(recA?.totalBytes).toBe(6);
    expect(recB?.chunkCount).toBe(1);
    expect(recB?.totalBytes).toBe(6);

    const chunksA = await loadRecordingChunks("rec-a");
    expect(chunksA.map((chunk) => chunk.size)).toEqual([4, 2]);
    const chunksB = await loadRecordingChunks("rec-b");
    expect(chunksB.map((chunk) => chunk.size)).toEqual([6]);
  });

  it("never lets a new recording overwrite or mix an older one (stale-tail prevention)", async () => {
    await persistRecordingChunk("rec-old", 0, new Blob(["old-head"]));
    await persistRecordingChunk("rec-old", 1, new Blob(["old-tail"]));
    // A newer recording reuses the same indices.
    await persistRecordingChunk("rec-new", 0, new Blob(["new"]));

    const oldChunks = await loadRecordingChunks("rec-old");
    expect(oldChunks).toHaveLength(2);
    expect(oldChunks[0].size).toBe(8);
    expect(oldChunks[1].size).toBe(8);
    const newChunks = await loadRecordingChunks("rec-new");
    expect(newChunks).toHaveLength(1);
    expect(newChunks[0].size).toBe(3);

    await clearRecordingChunks("rec-new");
    expect(await loadRecordingChunks("rec-old")).toHaveLength(2);
    expect(await loadRecordingChunks("rec-new")).toHaveLength(0);
    expect(
      (await listRecoverableRecordings()).map((item) => item.recordingId),
    ).toEqual(["rec-old"]);
  });

  it("migrates the legacy index-only store into a single legacy recording", async () => {
    installFakeIndexedDB([
      { index: 0, blob: new Blob(["l0"]) },
      { index: 1, blob: new Blob(["l1"]) },
    ]);

    const listed = await listRecoverableRecordings();
    expect(listed).toHaveLength(1);
    expect(listed[0].recordingId).toBe(LEGACY_RECORDING_ID);
    expect(listed[0].chunkCount).toBe(2);

    const chunks = await loadRecordingChunks(LEGACY_RECORDING_ID);
    expect(chunks.map((chunk) => chunk.size)).toEqual([2, 2]);

    await clearRecordingChunks(LEGACY_RECORDING_ID);
    expect(await listRecoverableRecordings()).toHaveLength(0);
  });

  it("rejects invalid recording ids and degrades without IndexedDB", async () => {
    await expect(
      persistRecordingChunk("bad id!", 0, new Blob(["x"])),
    ).rejects.toThrow("invalid recording id");
    expect(await loadRecordingChunks("bad id!")).toHaveLength(0);

    delete (globalThis as { indexedDB?: unknown }).indexedDB;
    expect(isChunkStoreAvailable()).toBe(false);
    await expect(
      persistRecordingChunk("rec-x", 0, new Blob(["x"])),
    ).resolves.toBeUndefined();
    expect(await listRecoverableRecordings()).toHaveLength(0);
    expect(await loadRecordingChunks("rec-x")).toHaveLength(0);
    await expect(clearRecordingChunks("rec-x")).resolves.toBeUndefined();
  });
});
