import { isValidShareUnicode } from './share-draft';

export type ShareFields = { shared_title: string; shared_text: string; shared_url: string };
export const SHARE_INTENT_DATABASE = 'shunshilu-share-intents';
export const SHARE_INTENT_STORE = 'intents';
export const SHARE_INTENT_TTL_MS = 5 * 60 * 1000;
const VERSION = 1;
const CAPACITY = 3;
const FIELD_NAMES = ['shared_title', 'shared_text', 'shared_url'] as const;
type Intent = ShareFields & { version: number; id: string; created_at: number; expires_at: number };
type ErrorCode = 'SHARE_FIELDS_INVALID' | 'SHARE_STORAGE_UNAVAILABLE' | 'SHARE_INTENT_VERSION' | 'SHARE_INTENT_CAPACITY';

export class ShareIntentError extends Error {
  constructor(readonly code: ErrorCode) {
    super('本机暂时无法接收分享，请回到素材库手动粘贴。');
    this.name = 'ShareIntentError';
  }
}

export function isShareIntentToken(value: string): boolean {
  return /^[a-f0-9]{32}$/.test(value);
}

export function validateShareFields(value: unknown): value is ShareFields {
  if (!value || typeof value !== 'object') return false;
  const record = value as Record<string, unknown>;
  if (Object.keys(record).length !== FIELD_NAMES.length || Object.keys(record).some((name) => !FIELD_NAMES.includes(name as typeof FIELD_NAMES[number]))) return false;
  let bytes = 0;
  for (const name of FIELD_NAMES) {
    const field = record[name];
    if (typeof field !== 'string' || !isValidShareUnicode(field) || [...field].length > 10000) return false;
    bytes += new TextEncoder().encode(field).byteLength;
  }
  return bytes <= 65536;
}

const fieldsOf = (record: Intent): ShareFields => ({ shared_title: record.shared_title, shared_text: record.shared_text, shared_url: record.shared_url });
function validRecord(record: Intent): boolean {
  return record !== null && typeof record === 'object' && record.version === VERSION &&
    typeof record.id === 'string' && isShareIntentToken(record.id) &&
    Number.isSafeInteger(record.created_at) && Number.isSafeInteger(record.expires_at) &&
    record.expires_at - record.created_at === SHARE_INTENT_TTL_MS && validateShareFields(fieldsOf(record));
}

type StoreOptions = {
  factory?: IDBFactory | null;
  now?: () => number;
  randomBytes?: () => Uint8Array;
};

/** The injected factory is also used by independent-client concurrency tests.
 * Production always defaults to the browser/worker's real IDB and CSPRNG. */
export function createShareIntentStore(options: StoreOptions = {}) {
  const now = options.now ?? Date.now;
  const randomBytes = options.randomBytes ?? (() => globalThis.crypto.getRandomValues(new Uint8Array(16)));

  function open(): Promise<IDBDatabase> {
    return new Promise((resolve, reject) => {
      let request: IDBOpenDBRequest;
      let rejected = false;
      const fail = (code: ErrorCode) => { rejected = true; reject(new ShareIntentError(code)); };
      try {
        const factory = options.factory === undefined ? globalThis.indexedDB : options.factory;
        if (!factory) { fail('SHARE_STORAGE_UNAVAILABLE'); return; }
        request = factory.open(SHARE_INTENT_DATABASE, VERSION);
      } catch { fail('SHARE_STORAGE_UNAVAILABLE'); return; }
      request.onblocked = () => fail('SHARE_STORAGE_UNAVAILABLE');
      request.onerror = () => fail(request.error?.name === 'VersionError' ? 'SHARE_INTENT_VERSION' : 'SHARE_STORAGE_UNAVAILABLE');
      request.onupgradeneeded = (event) => {
        if (rejected || event.oldVersion !== 0) { request.transaction?.abort(); return; }
        request.result.createObjectStore(SHARE_INTENT_STORE, { keyPath: 'id' });
      };
      request.onsuccess = () => {
        const db = request.result;
        if (rejected) { db.close(); return; }
        if (db.version !== VERSION || db.objectStoreNames.length !== 1 || !db.objectStoreNames.contains(SHARE_INTENT_STORE)) {
          db.close(); fail('SHARE_INTENT_VERSION'); return;
        }
        db.onversionchange = () => db.close();
        resolve(db);
      };
    });
  }

  async function transaction<T>(operation: (store: IDBObjectStore, live: Intent[], timestamp: number) => T): Promise<T> {
    const db = await open();
    return new Promise<T>((resolve, reject) => {
      let tx: IDBTransaction;
      let result: T;
      let failure: ShareIntentError | undefined;
      try { tx = db.transaction(SHARE_INTENT_STORE, 'readwrite'); }
      catch { db.close(); reject(new ShareIntentError('SHARE_STORAGE_UNAVAILABLE')); return; }
      tx.onabort = () => { db.close(); reject(new ShareIntentError('SHARE_STORAGE_UNAVAILABLE')); };
      tx.oncomplete = () => {
        db.close();
        if (failure) reject(failure);
        else resolve(result);
      };
      const store = tx.objectStore(SHARE_INTENT_STORE);
      if (store.keyPath !== 'id' || store.autoIncrement || store.indexNames.length) {
        failure = new ShareIntentError('SHARE_INTENT_VERSION');
        return;
      }
      // Capacity is three by construction. The fourth row is only a corruption
      // sentinel: never scan an unbounded or unknown store and never delete it.
      const request = store.getAll(undefined, CAPACITY + 1);
      request.onsuccess = () => {
        try {
          const timestamp = now();
          if (!Number.isSafeInteger(timestamp)) throw new ShareIntentError('SHARE_STORAGE_UNAVAILABLE');
          const records = request.result as Intent[];
          if (records.length > CAPACITY) throw new ShareIntentError('SHARE_INTENT_VERSION');
          const live: Intent[] = [];
          for (const record of records) {
            if (!validRecord(record)) {
              // Known records with incompatible payloads are never migrated.
              if (typeof record?.id === 'string') store.delete(record.id);
              failure = new ShareIntentError('SHARE_INTENT_VERSION');
            } else if (record.expires_at <= timestamp || record.created_at > timestamp) store.delete(record.id);
            else live.push(record);
          }
          if (!failure) result = operation(store, live, timestamp);
        } catch (error) {
          failure = error instanceof ShareIntentError ? error : new ShareIntentError('SHARE_STORAGE_UNAVAILABLE');
        }
      };
    });
  }

  return {
    async createShareIntent(fields: ShareFields): Promise<string> {
      if (!validateShareFields(fields)) throw new ShareIntentError('SHARE_FIELDS_INVALID');
      // Copy before the asynchronous open so the caller cannot mutate the stored
      // payload between validation and the write boundary.
      const snapshot = fieldsOf(fields as Intent);
      let id: string;
      try {
        const bytes = randomBytes();
        if (bytes.length !== 16) throw new Error();
        id = [...bytes].map((byte) => byte.toString(16).padStart(2, '0')).join('');
      } catch { throw new ShareIntentError('SHARE_STORAGE_UNAVAILABLE'); }
      return transaction((store, live, timestamp) => {
        if (live.length >= CAPACITY) throw new ShareIntentError('SHARE_INTENT_CAPACITY');
        store.add({ version: VERSION, id, ...snapshot, created_at: timestamp, expires_at: timestamp + SHARE_INTENT_TTL_MS } satisfies Intent);
        return id;
      });
    },
    async claimShareIntent(token: string): Promise<ShareFields | null> {
      return transaction((store, live) => {
        if (!isShareIntentToken(token)) return null;
        const record = live.find((item) => item.id === token);
        if (!record) return null;
        store.delete(token);
        return fieldsOf(record);
      });
    },
    async cleanupShareIntents(): Promise<void> {
      await transaction(() => undefined);
    },
  };
}

const defaultStore = createShareIntentStore();
export const { createShareIntent, claimShareIntent, cleanupShareIntents } = defaultStore;
