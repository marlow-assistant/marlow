import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { inlineType, openAttachment, REVOKE_AFTER_MS, type OpenDeps } from "./openAttachment";

// docs/tasks/share-and-files-wire.md, "Opening a file chip", web. A blob URL
// runs in the web client's origin, beside the device token, so what may be
// opened inline is a short fixed list and the Blob's type comes from the row's
// mime through that list, never from the response.

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

const INLINE: [string, string][] = [
  ["text/plain", "text/plain;charset=utf-8"],
  ["image/jpeg", "image/jpeg"],
  ["image/png", "image/png"],
  ["image/gif", "image/gif"],
  ["image/webp", "image/webp"],
];

const DOWNLOAD = [
  "application/pdf", // a blob tab inherits the page CSP, whose object-src none blocks the PDF viewer
  "text/html",
  "application/xhtml+xml",
  "image/svg+xml",
  "text/xml",
  "application/xml",
  "text/javascript",
  "application/javascript",
  "application/octet-stream",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "text/plain;charset=utf-8", // not the row's mime shape: nothing but the exact keys opens
  "TEXT/PLAIN",
  "",
  "__proto__",
  "constructor",
];

describe("the inline map", () => {
  test.each(INLINE)("%s opens inline, as %s", (mime, type) => {
    expect(inlineType(mime)).toBe(type);
  });

  test.each(DOWNLOAD)("%j downloads", (mime) => {
    expect(inlineType(mime)).toBeNull();
  });
});

class FakeTab {
  href: string | null = null;
  closed = false;
  navigate(url: string) {
    this.href = url;
  }
  close() {
    this.closed = true;
  }
}

function harness(opts: { blob?: Blob; fail?: boolean; blocked?: boolean } = {}) {
  const log: string[] = [];
  const tab = new FakeTab();
  const made: { blob: Blob; url: string }[] = [];
  const revoked: string[] = [];
  const downloads: { url: string; name: string }[] = [];
  const notices: string[] = [];
  let gate!: () => void;
  const fetched = new Promise<void>((r) => (gate = r));
  const deps: OpenDeps = {
    openTab: () => {
      log.push("openTab");
      return opts.blocked ? null : tab;
    },
    fetchBlob: async () => {
      log.push("fetch");
      await fetched;
      if (opts.fail) throw new Error("offline");
      return opts.blob ?? new Blob(["x"], { type: "text/html" }); // a hostile Content-Type
    },
    createUrl: (blob) => {
      const url = `blob:test/${made.length}`;
      made.push({ blob, url });
      return url;
    },
    revokeUrl: (url) => revoked.push(url),
    download: (url, name) => downloads.push({ url, name }),
    notify: (m) => notices.push(m),
  };
  return { deps, tab, made, revoked, downloads, notices, log, release: gate };
}

async function open(
  h: ReturnType<typeof harness>,
  file: { id?: string; mime: string; name: string | null },
) {
  const p = openAttachment({ id: "a1", ...file }, h.deps);
  h.release();
  await p;
}

describe("opening a chip", () => {
  test("a text file opens in a tab opened before the fetch, with the Blob typed from the row, not the response", async () => {
    const h = harness(); // the response says text/html
    await open(h, { mime: "text/plain", name: "notes.txt" });
    expect(h.log).toEqual(["openTab", "fetch"]); // synchronous on the tap: not popup-blocked
    expect(h.made).toHaveLength(1);
    expect(h.made[0].blob.type).toBe("text/plain;charset=utf-8");
    expect(h.tab.href).toBe(h.made[0].url);
    expect(h.downloads).toEqual([]);
  });

  test("text opens as text/plain;charset=utf-8, whatever the response says", async () => {
    const h = harness({ blob: new Blob(["<script>1</script>"], { type: "text/html" }) });
    await open(h, { mime: "text/plain", name: "notes.txt" });
    expect(h.made[0].blob.type).toBe("text/plain;charset=utf-8");
    expect(h.tab.href).toBe(h.made[0].url);
  });

  test.each(["text/html", "image/svg+xml", "application/octet-stream", "application/xml"])(
    "%s is downloaded from an octet-stream Blob, never opened, even if the response claims it is plain text",
    async (mime) => {
      const h = harness({ blob: new Blob(["<script>window.__pwned=1</script>"], { type: "text/plain" }) });
      await open(h, { mime, name: "page.html" });
      expect(h.log).toEqual(["fetch"]); // no tab: nothing to be blocked
      expect(h.made[0].blob.type).toBe("application/octet-stream");
      expect(h.downloads).toEqual([{ url: h.made[0].url, name: "page.html" }]);
      expect(h.tab.href).toBeNull();
    },
  );

  test("a docx downloads under its own name", async () => {
    const h = harness();
    await open(h, {
      mime: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      name: "Trip notes.docx",
    });
    expect(h.downloads).toEqual([{ url: h.made[0].url, name: "Trip notes.docx" }]);
  });

  test("an old row with no name downloads as 'file'", async () => {
    const h = harness();
    await open(h, { mime: "application/octet-stream", name: null });
    expect(h.downloads[0].name).toBe("file");
  });

  test("a blocked tab falls back to a download, still from an octet-stream Blob", async () => {
    const h = harness({ blocked: true });
    await open(h, { mime: "text/plain", name: "notes.txt" });
    expect(h.made[0].blob.type).toBe("application/octet-stream");
    expect(h.downloads).toEqual([{ url: h.made[0].url, name: "notes.txt" }]);
  });

  test("a failed fetch closes the tab it opened and says so", async () => {
    const h = harness({ fail: true });
    await open(h, { mime: "text/plain", name: "notes.txt" });
    expect(h.tab.closed).toBe(true);
    expect(h.made).toEqual([]);
    expect(h.notices).toEqual(["Couldn't open that file."]);
  });

  test("a failed download fetch says so too", async () => {
    const h = harness({ fail: true });
    await open(h, { mime: "application/octet-stream", name: "a.bin" });
    expect(h.notices).toEqual(["Couldn't open that file."]);
    expect(h.downloads).toEqual([]);
  });

  test("the blob URL is revoked after use: for a tab, after a delay long enough to load it", async () => {
    const h = harness();
    await open(h, { mime: "text/plain", name: "notes.txt" });
    expect(h.revoked).toEqual([]);
    vi.advanceTimersByTime(REVOKE_AFTER_MS - 1);
    expect(h.revoked).toEqual([]);
    vi.advanceTimersByTime(1);
    expect(h.revoked).toEqual([h.made[0].url]);
  });

  test("a download's blob URL is revoked too", async () => {
    const h = harness();
    await open(h, { mime: "application/octet-stream", name: "a.bin" });
    vi.advanceTimersByTime(REVOKE_AFTER_MS);
    expect(h.revoked).toEqual([h.made[0].url]);
  });
});
