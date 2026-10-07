import { IDBFactory, IDBObjectStore } from 'fake-indexeddb';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createShareIntentStore, SHARE_INTENT_DATABASE, SHARE_INTENT_STORE, SHARE_INTENT_TTL_MS } from './share-intent';

const fields = { shared_title: ' 标题😀 ', shared_text: 'e\u0301 文案', shared_url: 'https://example.com/' };
afterEach(() => vi.restoreAllMocks());
function setup() {
  const factory = new IDBFactory();
  let timestamp = 1000000;
  return { factory, tick: (ms: number) => { timestamp += ms; }, ...createShareIntentStore({ factory, now: () => timestamp }) };
}
function open(factory: IDBFactory, name = SHARE_INTENT_DATABASE, version = 1): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = factory.open(name, version);
    request.onupgradeneeded = () => request.result.createObjectStore(SHARE_INTENT_STORE, { keyPath: 'id' });
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}
async function rows(factory: IDBFactory) {
  const db = await open(factory);
  return new Promise<unknown[]>((resolve, reject) => {
    const tx = db.transaction(SHARE_INTENT_STORE, 'readonly');
    const request = tx.objectStore(SHARE_INTENT_STORE).getAll();
    tx.oncomplete = () => { db.close(); resolve(request.result); };
    tx.onabort = () => { db.close(); reject(tx.error); };
  });
}

async function seed(factory: IDBFactory, records: unknown[]) {
  const db = await open(factory);
  await new Promise<void>((resolve, reject) => {
    const tx = db.transaction(SHARE_INTENT_STORE, 'readwrite');
    for (const record of records) tx.objectStore(SHARE_INTENT_STORE).put(record);
    tx.oncomplete = () => resolve();
    tx.onabort = () => reject(tx.error);
  });
  db.close();
}

describe('one-time share intent public IndexedDB seam', () => {
  it('creates 128-bit opaque tokens and atomically consumes exactly once across independent clients', async () => {
    const store = setup();
    const token = await store.createShareIntent(fields);
    expect(token).toMatch(/^[a-f0-9]{32}$/);
    const other = createShareIntentStore({ factory: store.factory, now: () => 1000000 });
    const claimed = await Promise.all([store.claimShareIntent(token), other.claimShareIntent(token)]);
    expect(claimed.filter(Boolean)).toEqual([fields]);
    expect(await rows(store.factory)).toEqual([]);
    expect(await store.claimShareIntent(token)).toBeNull();
  });

  it('serializes concurrent capacity checks, preserves three live shares, and cleans expired slots', async () => {
    const store = setup();
    const created = await Promise.allSettled(Array.from({ length: 5 }, () => store.createShareIntent(fields)));
    expect(created.filter((result) => result.status === 'fulfilled')).toHaveLength(3);
    expect(created.filter((result) => result.status === 'rejected').map((result) => (result as PromiseRejectedResult).reason.code)).toEqual(['SHARE_INTENT_CAPACITY', 'SHARE_INTENT_CAPACITY']);
    const tokens = created.flatMap((result) => result.status === 'fulfilled' ? [result.value] : []);
    store.tick(SHARE_INTENT_TTL_MS);
    const fresh = await store.createShareIntent(fields);
    expect(await store.claimShareIntent(tokens[0])).toBeNull();
    expect(await store.claimShareIntent(fresh)).toEqual(fields);
  });

  it('cleans expired records on cleanup and claim, including invalid token calls', async () => {
    const store = setup();
    await store.createShareIntent(fields);
    store.tick(SHARE_INTENT_TTL_MS);
    expect(await store.claimShareIntent('invalid')).toBeNull();
    expect(await rows(store.factory)).toEqual([]);
    await store.createShareIntent(fields);
    store.tick(SHARE_INTENT_TTL_MS);
    await store.cleanupShareIntents();
    expect(await rows(store.factory)).toEqual([]);
  });

  it('validates Unicode code-point and UTF-8 limits before any write', async () => {
    const store = setup();
    const valid = { ...fields, shared_title: '😀'.repeat(10000) };
    const token = await store.createShareIntent(valid);
    expect(await store.claimShareIntent(token)).toEqual(valid);
    const byteBoundary = { shared_title: '😀'.repeat(10000), shared_text: '😀'.repeat(6384), shared_url: '' };
    expect(await store.claimShareIntent(await store.createShareIntent(byteBoundary))).toEqual(byteBoundary);
    await expect(store.createShareIntent({ ...byteBoundary, shared_url: 'a' })).rejects.toMatchObject({ code: 'SHARE_FIELDS_INVALID' });
    for (const invalid of [
      { ...fields, shared_title: '😀'.repeat(10001) },
      { ...fields, shared_title: '\ud800' },
      { ...fields, shared_text: '😀'.repeat(10000), shared_title: '😀'.repeat(10000) },
      { ...fields, extra: 'unknown' },
    ]) await expect(store.createShareIntent(invalid)).rejects.toMatchObject({ code: 'SHARE_FIELDS_INVALID' });
    expect(await rows(store.factory)).toEqual([]);
  });

  it('fails closed on missing or denied storage and redacts the underlying error', async () => {
    const missing = createShareIntentStore({ factory: null });
    await expect(missing.createShareIntent(fields)).rejects.toMatchObject({ code: 'SHARE_STORAGE_UNAVAILABLE' });
    const factory = new IDBFactory();
    vi.spyOn(factory, 'open').mockImplementation(() => { throw new Error('private payload, token, URL'); });
    const denied = createShareIntentStore({ factory });
    await expect(denied.createShareIntent(fields)).rejects.toMatchObject({ message: '本机暂时无法接收分享，请回到素材库手动粘贴。', code: 'SHARE_STORAGE_UNAVAILABLE' });
  });

  it('rejects quota errors without retaining payload and snapshots before asynchronous storage', async () => {
    const store = setup();
    const add = vi.spyOn(IDBObjectStore.prototype, 'add').mockImplementation(() => {
      throw new DOMException('private URL and token', 'QuotaExceededError');
    });
    await expect(store.createShareIntent(fields)).rejects.toMatchObject({ code: 'SHARE_STORAGE_UNAVAILABLE' });
    add.mockRestore();
    expect(await rows(store.factory)).toEqual([]);
    const changing = { ...fields };
    const creating = store.createShareIntent(changing);
    changing.shared_text = 'later mutation';
    expect(await store.claimShareIntent(await creating)).toEqual(fields);
  });

  it('fails closed if CSPRNG is unavailable and never overwrites a token collision', async () => {
    const factory = new IDBFactory();
    const failed = createShareIntentStore({ factory, randomBytes: () => { throw new Error('unavailable'); } });
    await expect(failed.createShareIntent(fields)).rejects.toMatchObject({ code: 'SHARE_STORAGE_UNAVAILABLE' });
    const collision = createShareIntentStore({ factory, randomBytes: () => new Uint8Array(16) });
    const token = await collision.createShareIntent(fields);
    await expect(collision.createShareIntent({ ...fields, shared_text: 'second share' })).rejects.toMatchObject({ code: 'SHARE_STORAGE_UNAVAILABLE' });
    expect(await collision.claimShareIntent(token)).toEqual(fields);
  });

  it('does not expose a claim before commit and returns no payload after transaction abort', async () => {
    const store = setup();
    const token = await store.createShareIntent(fields);
    const factoryOpen = store.factory.open.bind(store.factory);
    const spy = vi.spyOn(store.factory, 'open').mockImplementation((...args) => {
      const request = factoryOpen(...args);
      request.addEventListener('success', () => {
        const db = request.result;
        const transaction = db.transaction.bind(db);
        vi.spyOn(db, 'transaction').mockImplementation((...params) => {
          const tx = transaction(...params);
          tx.addEventListener('error', () => {});
          const objectStore = tx.objectStore(SHARE_INTENT_STORE);
          const remove = objectStore.delete.bind(objectStore);
          vi.spyOn(objectStore, 'delete').mockImplementation((key) => {
            const deletion = remove(key);
            deletion.addEventListener('success', () => tx.abort());
            return deletion;
          });
          return tx;
        });
      });
      return request;
    });
    await expect(store.claimShareIntent(token)).rejects.toMatchObject({ code: 'SHARE_STORAGE_UNAVAILABLE' });
    spy.mockRestore();
    expect(await store.claimShareIntent(token)).toEqual(fields);
  });

  it('rejects newer schema without migrating or deleting another database', async () => {
    const store = setup();
    const future = await open(store.factory, SHARE_INTENT_DATABASE, 2);
    future.close();
    const other = await open(store.factory, 'unrelated-user-data');
    other.close();
    await expect(store.cleanupShareIntents()).rejects.toMatchObject({ code: 'SHARE_INTENT_VERSION' });
    expect((await store.factory.databases()).map((db) => db.name)).toContain('unrelated-user-data');
    expect((await store.factory.databases()).find((db) => db.name === SHARE_INTENT_DATABASE)?.version).toBe(2);
  });

  it('cleans an incompatible own payload without exposing or migrating it', async () => {
    const store = setup();
    const id = '1'.repeat(32);
    await seed(store.factory, [{ ...fields, id, version: 2, created_at: 1000000, expires_at: 1300000 }]);
    await expect(store.claimShareIntent(id)).rejects.toMatchObject({ code: 'SHARE_INTENT_VERSION' });
    expect(await rows(store.factory)).toEqual([]);
    expect(await store.claimShareIntent(id)).toBeNull();
  });

  it('bounds corrupted stores to four inspected records and does not scan or clear excess data', async () => {
    const store = setup();
    const records = Array.from({ length: 8 }, (_, index) => ({ ...fields, id: index.toString(16).padStart(32, '0'), version: 1, created_at: 1000000, expires_at: 1300000 }));
    await seed(store.factory, records);
    const read = vi.spyOn(IDBObjectStore.prototype, 'getAll');
    await expect(store.cleanupShareIntents()).rejects.toMatchObject({ code: 'SHARE_INTENT_VERSION' });
    expect(read).toHaveBeenCalledExactlyOnceWith(undefined, 4);
    read.mockRestore();
    expect(await rows(store.factory)).toEqual(records);
  });
});
