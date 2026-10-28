import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { applyTextSize, currentTextSize, DEFAULT_TEXT_SIZE, parseTextSize, TEXT_SIZES } from "./textSize";

afterEach(() => applyTextSize(DEFAULT_TEXT_SIZE));

describe("text size", () => {
  it("reads a stored id back, and anything unknown as the default", () => {
    expect(parseTextSize("larger").scale).toBe(1.3);
    expect(parseTextSize(undefined)).toBe(DEFAULT_TEXT_SIZE);
    expect(parseTextSize("huge")).toBe(DEFAULT_TEXT_SIZE);
    expect(parseTextSize(1.3)).toBe(DEFAULT_TEXT_SIZE);
  });

  it("scales the root font size as a percentage, and the default leaves it alone", () => {
    const root = document.createElement("html");
    applyTextSize(parseTextSize("largest"), root);
    expect(root.style.fontSize).toBe("150%");
    expect(currentTextSize().id).toBe("largest");
    applyTextSize(DEFAULT_TEXT_SIZE, root);
    expect(root.style.fontSize).toBe("");
  });

  it("only grows", () => {
    const scales = TEXT_SIZES.map((s) => s.scale);
    expect(scales[0]).toBe(1);
    expect([...scales].sort((a, b) => a - b)).toEqual(scales);
  });

  // A px font-size ignores the setting, and nothing else notices: the text just
  // stays small. Only the glyphs sized to a fixed circle may use px.
  it("every font size in the app's CSS is in rem, apart from the fixed-circle glyphs", () => {
    const allowed = new Set([".retry-arrow", ".jump", ".send", ".preview-remove", ".mic"]);
    const offenders: string[] = [];
    for (const file of ["src/app/styles.css", "src/app/views.css", "src/markdown/markdown.css"]) {
      let selector = "";
      for (const line of readFileSync(join(process.cwd(), file), "utf8").split("\n")) {
        if (line.includes("{")) selector = line.split("{")[0].trim();
        if (/font(-size)?:[^;]*\d+px/.test(line) && !allowed.has(selector)) offenders.push(`${file}: ${selector}`);
      }
    }
    for (const file of readdirSync(join(process.cwd(), "src"), { recursive: true }) as string[]) {
      if (file.endsWith(".tsx") && !file.endsWith(".test.tsx") && /fontSize\s*:/.test(readFileSync(join(process.cwd(), "src", file), "utf8"))) offenders.push(file);
    }
    expect(offenders).toEqual([]);
  });
});
