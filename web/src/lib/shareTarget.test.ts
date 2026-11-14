// @vitest-environment node
// The service worker runs in a worker, not a page, so its handler is tested
// where there is no `window` either: Node's own Request, FormData and File.
import "fake-indexeddb/auto";
import { IDBFactory } from "fake-indexeddb";
import { beforeEach, describe, expect, test } from "vitest";
import { receiveShare, SHARE_FAILED, shareCaption, sweepShares, takeShare } from "./shareTarget";
import { resetDbForTests, wipeAll } from "./storage";

beforeEach(() => {
  globalThis.indexedDB = new IDBFactory();
  resetDbForTests();
});

function shareRequest(build: (fd: FormData) => void): Request {
  const fd = new FormData();
  build(fd);
  return new Request("https://marlow.test/share-target", { method: "POST", body: fd });
}

const location = (r: Response) => new URL(r.headers.get("location")!, "https://marlow.test");

describe("the share-target handler (Phase 4: form data in, IndexedDB entry, 303 out)", () => {
  test("answers 303 to /?share=<id>, so the app loads as a normal GET", async () => {
    const res = await receiveShare(shareRequest((fd) => fd.set("title", "T")), () => "abc");
    expect(res.status).toBe(303);
    expect(location(res).pathname).toBe("/");
    expect(location(res).searchParams.get("share")).toBe("abc");
  });

  test("stores the files and the text under that id", async () => {
    const res = await receiveShare(
      shareRequest((fd) => {
        fd.set("title", "Trip");
        fd.set("text", "see attached");
        fd.set("url", "https://example.com/a");
        fd.append("files", new File([new Uint8Array([1, 2, 3])], "Plan.pdf", { type: "application/pdf" }));
        fd.append("files", new File(["x"], "Budget.numbers", { type: "" }));
      }),
    );
    const id = location(res).searchParams.get("share")!;
    const entry = await takeShare(id);
    expect(entry).toMatchObject({ id, title: "Trip", text: "see attached", url: "https://example.com/a" });
    // (Multipart gives a part with no type the generic one: the client's guess
    // decides nothing, so what it is does not matter here.)
    expect(entry!.files.map((f) => [f.name, f.size])).toEqual([
      ["Plan.pdf", 3],
      ["Budget.numbers", 1],
    ]);
    expect(entry!.files[0].type).toBe("application/pdf");
    expect(new Uint8Array(await entry!.files[0].arrayBuffer())).toEqual(new Uint8Array([1, 2, 3]));
  });

  test("accepts any type: nothing is filtered", async () => {
    const res = await receiveShare(
      shareRequest((fd) => {
        fd.append("files", new File(["x"], "a.exe", { type: "application/x-msdownload" }));
        fd.append("files", new File(["x"], "b.mov", { type: "video/quicktime" }));
      }),
    );
    expect((await takeShare(location(res).searchParams.get("share")!))!.files).toHaveLength(2);
  });

  test("a text-only share (a link from a browser) has no files", async () => {
    const res = await receiveShare(shareRequest((fd) => fd.set("text", "https://example.com")));
    const entry = await takeShare(location(res).searchParams.get("share")!);
    expect(entry).toMatchObject({ text: "https://example.com", title: "", url: "", files: [] });
  });

  test("an empty file part (a form with no file chosen) is not a file", async () => {
    const res = await receiveShare(
      shareRequest((fd) => {
        fd.set("text", "hi");
        fd.append("files", new File([], "", { type: "application/octet-stream" }));
      }),
    );
    expect((await takeShare(location(res).searchParams.get("share")!))!.files).toEqual([]);
  });

  test("each share gets a fresh id of its own", async () => {
    const a = await receiveShare(shareRequest((fd) => fd.set("text", "a")));
    const b = await receiveShare(shareRequest((fd) => fd.set("text", "b")));
    const [ia, ib] = [a, b].map((r) => location(r).searchParams.get("share")!);
    expect(ia).not.toBe(ib);
    expect((await takeShare(ia))!.text).toBe("a");
    expect((await takeShare(ib))!.text).toBe("b");
  });

  test("a body that cannot be read still lands on the app, which says it could not find the share", async () => {
    const broken = {
      url: "https://marlow.test/share-target",
      formData: async () => {
        throw new TypeError("bad multipart");
      },
    };
    const res = await receiveShare(broken as unknown as Request);
    expect(res.status).toBe(303);
    expect(location(res).searchParams.get("share")).toBe(SHARE_FAILED);
    expect(await takeShare(SHARE_FAILED)).toBeNull();
  });
});

describe("taking a share", () => {
  test("reading it deletes it: a reload does not stage it twice", async () => {
    const res = await receiveShare(shareRequest((fd) => fd.set("text", "once")));
    const id = location(res).searchParams.get("share")!;
    expect(await takeShare(id)).not.toBeNull();
    expect(await takeShare(id)).toBeNull();
  });

  test("an unknown id is null, not an error", async () => {
    expect(await takeShare("nope")).toBeNull();
  });

  test("unpairing forgets a share nobody took", async () => {
    const id = location(await receiveShare(shareRequest((fd) => fd.set("text", "private")))).searchParams.get("share")!;
    await wipeAll();
    expect(await takeShare(id)).toBeNull();
  });

  test("entries older than the limit are swept, newer ones stay", async () => {
    const old = location(await receiveShare(shareRequest((fd) => fd.set("text", "old")), () => "old", () => 1_000)).searchParams.get("share")!;
    const fresh = location(await receiveShare(shareRequest((fd) => fd.set("text", "new")), () => "new", () => 90_000)).searchParams.get("share")!;
    await sweepShares(60_000, 100_000);
    expect(await takeShare(old)).toBeNull();
    expect(await takeShare(fresh)).not.toBeNull();
  });
});

describe("the caption pre-filled from title, text and url", () => {
  test("title, text, then the URL last", () => {
    expect(shareCaption({ title: "A page", text: "Worth a read", url: "https://example.com/p" })).toBe(
      "A page\nWorth a read\nhttps://example.com/p",
    );
  });

  test("the URL goes last whatever the order of the fields", () => {
    expect(shareCaption({ url: "https://example.com", title: "T", text: "X" })).toBe("T\nX\nhttps://example.com");
  });

  test("Chrome puts the link in text: it is not repeated", () => {
    expect(shareCaption({ title: "A page", text: "https://example.com/p", url: "https://example.com/p" })).toBe(
      "A page\nhttps://example.com/p",
    );
    expect(shareCaption({ title: "", text: "Look https://example.com/p", url: "https://example.com/p" })).toBe(
      "Look https://example.com/p",
    );
  });

  test("a title that is the same as the text is said once", () => {
    expect(shareCaption({ title: "Same", text: "Same", url: "" })).toBe("Same");
  });

  test("a URL alone, and nothing at all", () => {
    expect(shareCaption({ title: "", text: "", url: " https://example.com " })).toBe("https://example.com");
    expect(shareCaption({ title: " ", text: "", url: "" })).toBe("");
  });
});
