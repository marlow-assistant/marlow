import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

// Needles built by concatenation so this file does not match itself.
const NEEDLES = [
  "dangerously" + "SetInnerHTML",
  ".inner" + "HTML =",
  "insertAdjacent" + "HTML",
  "document." + "write",
];

const SRC = resolve(fileURLToPath(import.meta.url), "../..");
const SELF = fileURLToPath(import.meta.url);

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n);
    return statSync(p).isDirectory() ? walk(p) : [p];
  });
}

describe("no raw HTML sinks in web/src", () => {
  it("finds source files", () => {
    expect(walk(SRC).length).toBeGreaterThan(3);
  });
  it("contains none of the forbidden sinks", () => {
    const hits: string[] = [];
    for (const f of walk(SRC)) {
      if (f === SELF || !/\.(tsx?|jsx?|mjs|html)$/.test(f)) continue;
      const text = readFileSync(f, "utf8");
      for (const n of NEEDLES) if (text.includes(n)) hits.push(`${f}: ${n}`);
    }
    expect(hits).toEqual([]);
  });
});
