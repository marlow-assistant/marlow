import { describe, expect, test } from "vitest";
import { fileKind, formatSize, truncateMiddle } from "./fileInfo";

describe("fileKind: the chip's icon, from the name's extension, display only", () => {
  test.each([
    ["Plan.pdf", "pdf"],
    ["PLAN.PDF", "pdf"],
    ["Trip notes.docx", "document"],
    ["a.pages", "document"],
    ["Budget.xlsx", "sheet"],
    ["Budget.numbers", "sheet"],
    ["a.csv", "sheet"],
    ["deck.pptx", "slides"],
    ["deck.key", "slides"],
    ["notes.txt", "text"],
    ["README.md", "text"],
    ["a.zip", "archive"],
    ["a.mp3", "audio"],
    ["a.mov", "video"],
    ["a.heic", "image"],
    ["setup.exe", "other"],
    ["noextension", "other"],
    [".hidden", "other"],
    ["trailingdot.", "other"],
    ["", "other"],
  ])("%j is %s", (name, kind) => {
    expect(fileKind(name)).toBe(kind);
  });
});

describe("truncateMiddle", () => {
  test("a short name is left alone", () => {
    expect(truncateMiddle("a.pdf", 12)).toBe("a.pdf");
    expect(truncateMiddle("exactly12chr", 12)).toBe("exactly12chr");
  });

  test("a long name keeps its start and its end, so the extension survives", () => {
    const out = truncateMiddle("Quarterly budget review final.xlsx", 14);
    expect(out).toHaveLength(14);
    expect(out).toContain("…");
    expect(out.startsWith("Quar")).toBe(true);
    expect(out.endsWith("xlsx")).toBe(true);
  });

  test("never longer than the limit", () => {
    for (const max of [3, 5, 8, 20]) expect(truncateMiddle("x".repeat(100) + ".pdf", max).length).toBeLessThanOrEqual(max);
  });
});

describe("formatSize", () => {
  test.each([
    [0, "0 B"],
    [999, "999 B"],
    [1024, "1 KB"],
    [1536, "1.5 KB"],
    [250 * 1024, "250 KB"],
    [1024 * 1024, "1 MB"],
    [Math.round(2.4 * 1024 * 1024), "2.4 MB"],
    [20 * 1024 * 1024, "20 MB"],
  ])("%d is %s", (bytes, text) => {
    expect(formatSize(bytes)).toBe(text);
  });

  test("an old row with no size shows nothing", () => {
    expect(formatSize(null)).toBe("");
    expect(formatSize(undefined)).toBe("");
  });
});
