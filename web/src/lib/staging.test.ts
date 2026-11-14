import { describe, expect, test } from "vitest";
import { ImageUnsupportedError } from "./imageDownscale";
import {
  MAX_FILE_BYTES,
  MAX_ITEMS,
  MAX_TOTAL_BYTES,
  stageBatch,
  type StagedItem,
} from "./staging";

// docs/tasks/share-and-files.md, "The type rule" and Phase 2: the client checks
// count and size, never type; anything it can decode as an image is a photo,
// everything else is a file chip.

const MB = 1024 * 1024;

function file(name: string, type: string, size = 10): File {
  return new File([new Uint8Array(size)], name, { type });
}

/** A File that reports `size` without holding that many bytes. */
function big(name: string, type: string, size: number): File {
  const f = file(name, type, 1);
  Object.defineProperty(f, "size", { value: size });
  return f;
}

/** A decoder that reads PNG and JPEG and nothing else, like a desktop browser
 * handed a HEIC. The result is a small "downscaled" blob. */
async function fakeDownscale(blob: Blob): Promise<Blob> {
  if (blob.type === "image/png" || blob.type === "image/jpeg") {
    return new Blob([new Uint8Array(5)], { type: "image/jpeg" });
  }
  throw new ImageUnsupportedError();
}

async function stage(files: File[], held: StagedItem[] = []) {
  const items = [...held];
  let n = 0;
  const notice = await stageBatch(
    files,
    () => items,
    (item) => items.push(item),
    fakeDownscale,
    () => `id-${n++}`,
  );
  return { items, added: items.slice(held.length), notice };
}

const held = (count: number, kind: "photo" | "file" = "photo"): StagedItem[] =>
  Array.from({ length: count }, (_, i) => ({
    id: `held-${i}`,
    kind,
    blob: new Blob([new Uint8Array(5)]),
    name: `held-${i}`,
  }));

describe("what is a photo and what is a file", () => {
  test("anything decodable goes the photo way, whichever input picked it", async () => {
    const { added, notice } = await stage([file("a.png", "image/png"), file("b.jpg", "image/jpeg")]);
    expect(added.map((i) => i.kind)).toEqual(["photo", "photo"]);
    expect(added[0].blob.type).toBe("image/jpeg"); // the downscaled copy
    expect(notice).toBeNull();
  });

  test("a PDF, a docx and a .numbers file are chips, in the order picked", async () => {
    const { added, notice } = await stage([
      file("Plan.pdf", "application/pdf"),
      file("Trip notes.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
      file("Budget.numbers", ""),
    ]);
    expect(added.map((i) => [i.kind, i.name])).toEqual([
      ["file", "Plan.pdf"],
      ["file", "Trip notes.docx"],
      ["file", "Budget.numbers"],
    ]);
    expect(notice).toBeNull();
  });

  test("an unfamiliar type is staged without a notice: no client carries a type list", async () => {
    const { added, notice } = await stage([
      file("Budget.numbers", ""),
      file("setup.exe", "application/x-msdownload"),
      file("clip.mov", "video/quicktime"),
    ]);
    expect(added).toHaveLength(3);
    expect(added.every((i) => i.kind === "file")).toBe(true);
    expect(notice).toBeNull();
  });

  test("an image the browser cannot decode (a HEIC on desktop) becomes a file, not a refusal", async () => {
    const { added, notice } = await stage([file("IMG_1.heic", "image/heic")]);
    expect(added.map((i) => [i.kind, i.name])).toEqual([["file", "IMG_1.heic"]]);
    expect(notice).toBeNull();
  });

  test("the file keeps its own bytes and type for the send", async () => {
    const pdf = file("Plan.pdf", "application/pdf", 7);
    const { added } = await stage([pdf]);
    expect(added[0].blob).toBe(pdf);
    expect(added[0].blob.type).toBe("application/pdf");
  });
});

describe("the six-item cap counts photos and files together", () => {
  test("mixed items fill the room that is left, in order, and a notice says the rest were left out", async () => {
    const { added, notice } = await stage(
      [
        file("a.png", "image/png"),
        file("b.pdf", "application/pdf"),
        file("c.png", "image/png"),
        file("d.docx", ""),
      ],
      [...held(2, "photo"), ...held(2, "file")],
    );
    expect(added.map((i) => i.name)).toEqual(["a.png", "b.pdf"]);
    expect(notice).toBe(`Only the first 2 added: ${MAX_ITEMS} items at most.`);
  });

  test("a full composer refuses everything and says so", async () => {
    const { added, notice } = await stage([file("a.pdf", "application/pdf")], [...held(3), ...held(3, "file")]);
    expect(added).toEqual([]);
    expect(notice).toBe(`${MAX_ITEMS} items at most.`);
  });

  test("six of mixed kinds fit exactly, with no notice", async () => {
    const { items, notice } = await stage([
      file("1.png", "image/png"),
      file("2.pdf", "application/pdf"),
      file("3.png", "image/png"),
      file("4.txt", "text/plain"),
      file("5.numbers", ""),
      file("6.jpg", "image/jpeg"),
    ]);
    expect(items).toHaveLength(MAX_ITEMS);
    expect(notice).toBeNull();
  });
});

describe("size checks, before staging", () => {
  test("the limits are the server's", () => {
    expect(MAX_ITEMS).toBe(6);
    expect(MAX_FILE_BYTES).toBe(20 * MB);
    expect(MAX_TOTAL_BYTES).toBe(25 * MB);
  });

  test("a file over 20MB is refused with a notice; the others still stage", async () => {
    const { added, notice } = await stage([
      big("huge.pdf", "application/pdf", 20 * MB + 1),
      file("ok.pdf", "application/pdf"),
    ]);
    expect(added.map((i) => i.name)).toEqual(["ok.pdf"]);
    expect(notice).toBe("That file is over 20MB.");
  });

  test("exactly 20MB is allowed", async () => {
    const { added, notice } = await stage([big("edge.pdf", "application/pdf", 20 * MB)]);
    expect(added).toHaveLength(1);
    expect(notice).toBeNull();
  });

  test("the message total is capped at 25MB across photos and files", async () => {
    const { added, notice } = await stage([
      big("a.pdf", "application/pdf", 15 * MB),
      big("b.pdf", "application/pdf", 11 * MB), // 26MB together
      file("c.txt", "text/plain"),
    ]);
    expect(added.map((i) => i.name)).toEqual(["a.pdf", "c.txt"]);
    expect(notice).toBe("That would put the message over 25MB.");
  });

  test("what is already staged counts toward the total", async () => {
    const staged: StagedItem = { id: "h", kind: "file", blob: big("x", "", 20 * MB), name: "x" };
    const { added, notice } = await stage([big("b.pdf", "application/pdf", 6 * MB)], [staged]);
    expect(added).toEqual([]);
    expect(notice).toBe("That would put the message over 25MB.");
  });

  test("a photo counts at its downscaled size, and is not held to the 20MB file limit", async () => {
    const { added, notice } = await stage([big("raw.jpg", "image/jpeg", 30 * MB)]);
    expect(added.map((i) => i.kind)).toEqual(["photo"]);
    expect(notice).toBeNull();
  });

  test("an undecodable file over 20MB is a file, so the file limit applies", async () => {
    const { added, notice } = await stage([big("scan.heic", "image/heic", 21 * MB)]);
    expect(added).toEqual([]);
    expect(notice).toBe("That file is over 20MB.");
  });

  test("the cap note and a size refusal are both shown", async () => {
    const { added, notice } = await stage(
      [big("huge.pdf", "application/pdf", 21 * MB), file("a.png", "image/png"), file("b.png", "image/png")],
      held(5),
    );
    expect(added.map((i) => i.name)).toEqual([]); // the one room left went to the oversized pick
    expect(notice).toContain("That file is over 20MB.");
    expect(notice).toContain(`${MAX_ITEMS} items at most.`);
  });
});
