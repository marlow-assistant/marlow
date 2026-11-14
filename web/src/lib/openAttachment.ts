// Opening a file chip (docs/tasks/share-and-files.md, Phase 2; the rule is
// pinned in share-and-files-wire.md, "Opening a file chip").
//
// WHY THIS IS SO STRICT. A blob: URL has the origin of the page that made it.
// This page's origin holds the device token (IndexedDB) and can call the box
// with it, so a file opened as text/html or image/svg+xml in a tab from a blob
// URL is script running with the token. The server already stores such files
// as text/plain, and serves attachments with nosniff; this is the second wall,
// and it does not trust the first:
//
//   * what may open inline is the short fixed list below, keyed by the row's
//     mime, and
//   * the Blob's type comes from that list, never from the response's
//     Content-Type, so a wrong or hostile header changes nothing, and
//   * everything else is saved to disk from an application/octet-stream Blob
//     under an `<a download>`, which no browser renders.
//
// The list is of what is safe to SHOW, not of what Marlow accepts, so it does
// not grow with new file types.

/** Row mime -> the type the Blob is built with, for the types that open in a
 * tab. A Map so a mime like "__proto__" is simply absent.
 *
 * Not PDF: a blob: document inherits the page's CSP, and the box serves
 * `object-src 'none'` (server/web_static.py), which can block the browser's PDF
 * viewer, so the tab would open blank. Android Chrome downloads PDFs anyway.
 * A PDF downloads like any other file (2026-11-14). */
const INLINE_TYPES = new Map<string, string>([
  ["text/plain", "text/plain;charset=utf-8"],
  ["image/jpeg", "image/jpeg"],
  ["image/png", "image/png"],
  ["image/gif", "image/gif"],
  ["image/webp", "image/webp"],
]);

/** The Blob type to open `mime` with inline, or null: download it. */
export function inlineType(mime: string): string | null {
  return INLINE_TYPES.get(mime) ?? null;
}

/** A tab opened on the tap. Its address is set once the bytes are here. */
export interface Tab {
  navigate(url: string): void;
  close(): void;
}

export interface OpenDeps {
  /** Opens a blank tab, or null if the browser refuses. Called synchronously
   * on the tap, before any await: a window opened after a fetch is no longer
   * a user gesture and is popup-blocked. */
  openTab(): Tab | null;
  fetchBlob(id: string): Promise<Blob>;
  createUrl(blob: Blob): string;
  revokeUrl(url: string): void;
  /** Clicks an `<a download="name" href=url>`. */
  download(url: string, name: string): void;
  notify(message: string): void;
}

/** How long a blob URL lives after it is used. A tab needs it until its
 * viewer has loaded the document (and may read it again on a reload), and a
 * download needs it until the browser has started saving. */
export const REVOKE_AFTER_MS = 60_000;

export const OPEN_FAILED = "Couldn't open that file.";

const OCTET_STREAM = "application/octet-stream";

/** The chip's tap. `file.mime` is the row's stored mime (the server's, not
 * the client's guess), `file.name` null on an old row. */
export async function openAttachment(
  file: { id: string; mime: string; name: string | null },
  deps: OpenDeps,
): Promise<void> {
  const type = inlineType(file.mime);
  const tab = type ? deps.openTab() : null;
  let blob: Blob;
  try {
    // Rebuilt with a type of our choosing: the fetched Blob's own type is the
    // response's Content-Type, which is not trusted.
    const bytes = await deps.fetchBlob(file.id);
    blob = new Blob([bytes], { type: tab && type ? type : OCTET_STREAM });
  } catch {
    tab?.close();
    deps.notify(OPEN_FAILED);
    return;
  }
  const url = deps.createUrl(blob);
  if (tab) tab.navigate(url);
  else deps.download(url, file.name ?? "file");
  setTimeout(() => deps.revokeUrl(url), REVOKE_AFTER_MS);
}

/** The real browser, for the bubble. */
export function browserOpenDeps(fetchBlob: (id: string) => Promise<Blob>, notify: (message: string) => void): OpenDeps {
  return {
    openTab() {
      const w = window.open("", "_blank");
      if (!w) return null;
      try {
        // The tab shows a PDF, text or an image, none of which run script,
        // but it has no business reaching back into this page.
        w.opener = null;
      } catch {
        // Leave it.
      }
      return {
        navigate: (url) => {
          w.location.href = url;
        },
        close: () => w.close(),
      };
    },
    fetchBlob,
    createUrl: (blob) => URL.createObjectURL(blob),
    revokeUrl: (url) => URL.revokeObjectURL(url),
    download(url, name) {
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      a.hidden = true;
      document.body.appendChild(a);
      a.click();
      a.remove();
    },
    notify,
  };
}
