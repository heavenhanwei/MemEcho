// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  clearPendingChunks,
  isChunkStoreAvailable,
  loadPendingChunks,
  persistRecordingChunk,
} from "./chunkStore";

type AnyHandler = (() => void) | undefined;

class FakeRequest<T> {
  result: T;
  onsuccess: AnyHandler;
  onerror: AnyHandler;

  constructor(result: T) {
    this.result = result;
    queueMicrotask(() => this.onsuccess?.());
  }
}

class FakeObjectStore {
  constructor(private records: Map<number, { index: number; blob: Blob }>) {}

  put(record: { index: number; blob: Blob }) {
    this.records.set(record.index, record);
    return new FakeRequest(undefined);
  }

  getAll() {
    return new FakeRequest([...this.records.values()]) as unknown as IDBRequest;
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

  constructor(private store: FakeObjectStore) {
    queueMicrotask(() => this.oncomplete?.());
  }

  objectStore(_name: string) {
    return this.store;
  }
}

class FakeDb {
  objectStoreNames = { contains: () => true };

  constructor(private store: FakeObjectStore) {}

  createObjectStore(_name: string, _options: unknown) {
    return {};
  }

  transaction(_name: string, _mode?: string) {
    return new FakeTransaction(this.store);
  }

  close() {}
}

function installFakeIndexedDB() {
  const records = new Map<number, { index: number; blob: Blob }>();
  const fake = {
    open(_name: string, _version?: number) {
      const request: {
        result: FakeDb;
        onupgradeneeded: AnyHandler;
        onsuccess: AnyHandler;
        onerror: AnyHandler;
      } = {
        result: new FakeDb(new FakeObjectStore(records)),
        onupgradeneeded: undefined,
        onsuccess: undefined,
        onerror: undefined,
      };
      queueMicrotask(() => request.onsuccess?.());
      return request;
    },
  };
  (globalThis as { indexedDB?: unknown }).indexedDB = fake;
  return records;
}

describe("chunkStore", () => {
  let records: Map<number, { index: number; blob: Blob }>;

  beforeEach(() => {
    records = installFakeIndexedDB();
  });

  afterEach(() => {
    delete (globalThis as { indexedDB?: unknown }).indexedDB;
  });

  it("persists chunks and returns them ordered by index", async () => {
    const second = new Blob(["second"], { type: "audio/webm" });
    const first = new Blob(["first"], { type: "audio/webm" });
    await persistRecordingChunk(1, second);
    await persistRecordingChunk(0, first);

    const loaded = await loadPendingChunks();

    expect(loaded).toHaveLength(2);
    expect(loaded[0].size).toBe(first.size);
    expect(loaded[0].type).toBe("audio/webm");
    expect(loaded[1].size).toBe(second.size);
  });

  it("clears all pending chunks", async () => {
    await persistRecordingChunk(0, new Blob(["data"]));
    await clearPendingChunks();

    expect(await loadPendingChunks()).toHaveLength(0);
    expect(records.size).toBe(0);
  });

  it("degrades to no-ops when IndexedDB is unavailable", async () => {
    delete (globalThis as { indexedDB?: unknown }).indexedDB;

    expect(isChunkStoreAvailable()).toBe(false);
    await expect(
      persistRecordingChunk(0, new Blob(["data"])),
    ).resolves.toBeUndefined();
    expect(await loadPendingChunks()).toHaveLength(0);
    await expect(clearPendingChunks()).resolves.toBeUndefined();
  });
});
