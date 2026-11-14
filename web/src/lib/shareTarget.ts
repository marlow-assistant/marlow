// Android's Web Share Target (docs/tasks/share-and-files.md, Phase 4). Chrome
// lists an installed PWA in the system share sheet when the manifest has a
// `share_target`; choosing it POSTs the shared files and text to
// /share-target as multipart/form-data. A page can't receive a POST, so the
// service worker (sw.ts) answers it: it keeps what arrived and redirects to
// `/?share=<id>`, and the app, loading as an ordinary GET, takes the entry and
// stages it in the composer.
//
// This runs in the service worker too, so it must not touch `window`.
//
// The entries live in their own small database, not the app's. Adding a store
// to the app's database means a version bump, and a tab still running the
// older build would then fail to reopen it (a version error, not an upgrade)
// until it reloads; the app's image store is also swept at startup of
// everything no message references, which is exactly what a waiting share is.
// This one is only touched by a share arriving, the app taking it, and the
// sweep of anything that was never taken.

export const SHARE_DB = "marlow-share";
const STORE = "shares";
const DB_VERSION = 1;

/** The id the redirect carries when the share could not be kept, so the app
 * says so instead of showing an empty composer. */
export const SHARE_FAILED = "failed";

/** An entry nobody took (the app was never opened, or the user unpaired) is
 * dropped after this long. */
export const SHARE_MAX_AGE_MS = 24 * 60 * 60 * 1000;

export interface SharedEntry {
  id: string;
  createdAt: number;
  title: string;
  text: string;
  url: string;
  files: File[];
}

function openShareDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(SHARE_DB, DB_VERSION);
    req.onupgradeneeded = () => {
      if (!req.result.objectStoreNames.contains(STORE)) req.result.createObjectStore(STORE, { keyPath: "id" });
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function withStore<T>(mode: IDBTransactionMode, run: (store: IDBObjectStore) => IDBRequest<T> | void): Promise<T | undefined> {
  const db = await openShareDb();
  try {
    return await new Promise<T | undefined>((resolve, reject) => {
      const tx = db.transaction(STORE, mode);
      const req = run(tx.objectStore(STORE));
      tx.oncomplete = () => resolve(req ? req.result : undefined);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error ?? new Error("transaction aborted"));
    });
  } finally {
    db.close();
  }
}

const field = (data: FormData, name: string): string => {
  const v = data.get(name);
  return typeof v === "string" ? v : "";
};

const redirect = (request: Pick<Request, "url">, id: string): Response =>
  new Response(null, {
    status: 303,
    headers: { Location: new URL(`/?share=${encodeURIComponent(id)}`, request.url).href },
  });

/** The service worker's answer to `POST /share-target`: keep what was shared
 * and send the browser on to the app with a 303, so the app opens as a GET
 * (a reload then does not resubmit the share). Nothing is filtered by type.
 * A body that can't be read or stored still redirects, to `SHARE_FAILED`. */
export async function receiveShare(
  request: Pick<Request, "url" | "formData">,
  newId: () => string = () => crypto.randomUUID(),
  now: () => number = Date.now,
): Promise<Response> {
  try {
    const data = await request.formData();
    // An empty file input arrives as a zero-byte part with no name.
    const files = data
      .getAll("files")
      .filter((f): f is File => typeof f !== "string" && (f.size > 0 || f.name !== ""));
    const entry: SharedEntry = {
      id: newId(),
      createdAt: now(),
      title: field(data, "title"),
      text: field(data, "text"),
      url: field(data, "url"),
      files,
    };
    await withStore("readwrite", (s) => {
      s.put(entry);
    });
    return redirect(request, entry.id);
  } catch {
    return redirect(request, SHARE_FAILED);
  }
}

/** The entry for `id`, deleted as it is read so a reload doesn't stage it a
 * second time. Null if there is none, or the store can't be read. */
export async function takeShare(id: string): Promise<SharedEntry | null> {
  try {
    const db = await openShareDb();
    try {
      return await new Promise<SharedEntry | null>((resolve, reject) => {
        const tx = db.transaction(STORE, "readwrite");
        const store = tx.objectStore(STORE);
        const got = store.get(id);
        got.onsuccess = () => {
          if (got.result) store.delete(id);
        };
        tx.oncomplete = () => resolve((got.result as SharedEntry | undefined) ?? null);
        tx.onerror = () => reject(tx.error);
        tx.onabort = () => reject(tx.error ?? new Error("transaction aborted"));
      });
    } finally {
      db.close();
    }
  } catch {
    return null;
  }
}

/** Drops entries older than `maxAgeMs`. Best effort. */
export async function sweepShares(maxAgeMs: number = SHARE_MAX_AGE_MS, now: number = Date.now()): Promise<void> {
  try {
    await withStore("readwrite", (store) => {
      const cursorReq = store.openCursor();
      cursorReq.onsuccess = () => {
        const cursor = cursorReq.result;
        if (!cursor) return;
        if (now - (cursor.value as SharedEntry).createdAt > maxAgeMs) cursor.delete();
        cursor.continue();
      };
    });
  } catch {
    // Nothing to sweep, or no storage.
  }
}

/** Forgets every waiting share (unpairing: nothing of the old pairing is
 * kept). */
export async function wipeShares(): Promise<void> {
  try {
    await withStore("readwrite", (s) => {
      s.clear();
    });
  } catch {
    // As above.
  }
}

/** The composer's caption for a share: the title, the text, then the link
 * last, one to a line. Chrome often puts the link in `text` as well as `url`,
 * so a part that another part already contains is left out. */
export function shareCaption(share: { title: string; text: string; url: string }): string {
  const parts = [share.title, share.text, share.url].map((p) => p.trim()).filter(Boolean);
  // A part goes if a longer one contains it, or an earlier one is identical.
  const kept = parts.filter(
    (p, i) => !parts.some((q, j) => j !== i && q.includes(p) && (q.length > p.length || j < i)),
  );
  return kept.join("\n");
}
