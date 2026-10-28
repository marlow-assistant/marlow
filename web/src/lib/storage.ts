// IndexedDB, not localStorage, because the service worker must read the
// device token too (it fetches /v1/turn/{ref} for a notification's text) and a
// worker has no localStorage (docs/tasks/portable-web-client.md).
//
// Three stores:
//   kv        the device token, the transcript cursor and small preferences
//   messages  the transcript cache plus outbox, one record per message, so a
//             change writes the messages that changed, not the whole array
//             (SYN-6; iOS rewrites the whole file per event)
//   images    JPEG blobs: this client's own sends (for retry, CMP-6) and
//             fetched attachments (SYN-7)
//
// The service worker imports this file too, so it must not touch `window`.

import type { ChatMessage } from "./types";

export const DB_NAME = "marlow";
const DB_VERSION = 1;

export const KV_TOKEN = "device_token";
export const KV_CURSOR = "transcript_cursor";
export const KV_EFFORT = "effort";
export const KV_LOCATION = "locationSharing";
export const KV_IDEAS_OPENED = "ideasPanelOpened";
export const KV_FACE = "marlowFace";
export const KV_TEXT_SIZE = "textSize";
export const KV_VOICE = "speechVoice";
export const KV_SHOW_BACKGROUND = "activityShowBackground";
export const KV_WHATS_NEW_SEEN = "whatsNewSeenIds";
export const KV_WHATS_NEW_MUTED = "whatsNewMuted";

function promisify<T>(req: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function done(tx: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error ?? new Error("transaction aborted"));
  });
}

let dbPromise: Promise<IDBDatabase> | null = null;

export function openDb(): Promise<IDBDatabase> {
  if (dbPromise) return dbPromise;
  dbPromise = new Promise<IDBDatabase>((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains("kv")) db.createObjectStore("kv");
      if (!db.objectStoreNames.contains("messages")) db.createObjectStore("messages", { keyPath: "id" });
      if (!db.objectStoreNames.contains("images")) db.createObjectStore("images");
    };
    req.onsuccess = () => {
      const db = req.result;
      // Another tab upgrading the schema must not be blocked by this one.
      db.onversionchange = () => {
        db.close();
        dbPromise = null;
      };
      resolve(db);
    };
    req.onerror = () => {
      dbPromise = null;
      reject(req.error);
    };
  });
  return dbPromise;
}

/** Test support: forget the cached connection (fake-indexeddb resets). */
export function resetDbForTests(): void {
  dbPromise = null;
}

export async function kvGet<T>(key: string): Promise<T | undefined> {
  const db = await openDb();
  return (await promisify(db.transaction("kv").objectStore("kv").get(key))) as T | undefined;
}

export async function kvSet(key: string, value: unknown): Promise<void> {
  const db = await openDb();
  const tx = db.transaction("kv", "readwrite");
  tx.objectStore("kv").put(value, key);
  await done(tx);
}

export async function kvDelete(key: string): Promise<void> {
  const db = await openDb();
  const tx = db.transaction("kv", "readwrite");
  tx.objectStore("kv").delete(key);
  await done(tx);
}

export const getToken = () => kvGet<string>(KV_TOKEN);
export const setToken = (token: string) => kvSet(KV_TOKEN, token);

export interface CursorRecord {
  latestSeq: number | null;
  hasOlderOnServer: boolean;
}

/** The persistence the chat store needs, as an interface so its tests can
 * run against memory and the app against IndexedDB. */
export interface ChatPersistence {
  load(): Promise<{ cursor: CursorRecord | undefined; messages: ChatMessage[] }>;
  /** Writes `put`, deletes `remove`, and records the cursor, in one
   * transaction: a cursor saved without the rows it covers would skip them
   * on the next start. */
  save(put: ChatMessage[], remove: string[], cursor: CursorRecord): Promise<void>;
  putImage(key: string, blob: Blob): Promise<void>;
  getImage(key: string): Promise<Blob | undefined>;
  deleteImages(keep: (key: string) => boolean): Promise<void>;
}

export const idbPersistence: ChatPersistence = {
  async load() {
    const db = await openDb();
    const tx = db.transaction(["kv", "messages"]);
    const cursor = (await promisify(tx.objectStore("kv").get(KV_CURSOR))) as CursorRecord | undefined;
    const messages = (await promisify(tx.objectStore("messages").getAll())) as ChatMessage[];
    return { cursor, messages };
  },
  async save(put, remove, cursor) {
    const db = await openDb();
    const tx = db.transaction(["kv", "messages"], "readwrite");
    const store = tx.objectStore("messages");
    for (const m of put) store.put(m);
    for (const id of remove) store.delete(id);
    tx.objectStore("kv").put(cursor, KV_CURSOR);
    await done(tx);
  },
  async putImage(key, blob) {
    const db = await openDb();
    const tx = db.transaction("images", "readwrite");
    tx.objectStore("images").put(blob, key);
    await done(tx);
  },
  async getImage(key) {
    const db = await openDb();
    return (await promisify(db.transaction("images").objectStore("images").get(key))) as Blob | undefined;
  },
  async deleteImages(keep) {
    const db = await openDb();
    const tx = db.transaction("images", "readwrite");
    const store = tx.objectStore("images");
    const keys = await promisify(store.getAllKeys());
    for (const k of keys) if (typeof k === "string" && !keep(k)) store.delete(k);
    await done(tx);
  },
};

/** Unpairing (or a 401) forgets everything this browser holds for the box:
 * the token, the transcript cache, the outbox and the images. The cache was
 * only readable with the token, and a re-pair bootstraps from the server, so
 * keeping it would only leave a copy of the conversation on a browser that is
 * no longer trusted. */
export async function wipeAll(): Promise<void> {
  const db = await openDb();
  const tx = db.transaction(["kv", "messages", "images"], "readwrite");
  tx.objectStore("kv").clear();
  tx.objectStore("messages").clear();
  tx.objectStore("images").clear();
  await done(tx);
}
