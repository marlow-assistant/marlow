import { describe, expect, it } from "vitest";
import {
  bundledChanges,
  changeDateText,
  decodeChanges,
  mergeChanges,
  refreshState,
  type ChangeEntry,
  unseenChanges,
} from "./whatsNew";

function entry(id: number, part: "app" | "server" = "app"): ChangeEntry {
  return { id, date: "2026-10-23", part, category: "feature", text: `change ${id}` };
}

describe("merging the halves", () => {
  it("takes only each half's own part", () => {
    // An app built from a newer main than its box would otherwise announce
    // server behaviour the box isn't running yet.
    const merged = mergeChanges([entry(1), entry(5, "server")], [entry(2, "server"), entry(6)]);
    expect(merged.map((e) => e.id)).toEqual([2, 1]);
  });

  it("bundles the repo-root file's app entries", () => {
    const bundled = bundledChanges();
    expect(bundled.length).toBeGreaterThan(0);
    expect(bundled.every((e) => e.part === "app")).toBe(true);
  });

  it("decodes the endpoint and drops malformed entries", () => {
    const decoded = decodeChanges({ changes: [entry(1, "server"), { id: "2" }, null] });
    expect(decoded.map((e) => e.id)).toEqual([1]);
    expect(decodeChanges(null)).toEqual([]);
  });
});

describe("what is news", () => {
  it("shows the newest few on a first run", () => {
    const entries = [7, 6, 5, 4, 3, 2, 1].map((id) => entry(id));
    expect(unseenChanges(entries, null).map((e) => e.id)).toEqual([7, 6, 5, 4, 3]);
  });

  it("is a set, not a high-water mark", () => {
    expect(unseenChanges([entry(12), entry(11, "server")], new Set([12])).map((e) => e.id)).toEqual([11]);
  });

  it("records first-run history as seen so it does not come back", () => {
    const entries = [7, 6, 5, 4, 3, 2, 1].map((id) => entry(id));
    const first = refreshState(entries, null, false);
    expect(first.pending.map((e) => e.id)).toEqual([7, 6, 5, 4, 3]);
    expect(first.seen).toEqual([1, 2]);
  });

  it("counts everything as seen while muted", () => {
    const state = refreshState([entry(2), entry(1)], [1], true);
    expect(state.pending).toEqual([]);
    expect(state.seen).toEqual([1, 2]);
  });

  it("hands back the same seen array while muted when nothing is new, so nothing is written", () => {
    const seen = [1, 2];
    expect(refreshState([entry(2), entry(1)], seen, true).seen).toBe(seen);
  });
});

describe("before onboarding ends", () => {
  it("shows nothing and counts everything as seen, even on a first run", () => {
    const state = refreshState([entry(2), entry(1)], null, false, false);
    expect(state.pending).toEqual([]);
    expect(state.seen).toEqual([1, 2]);
  });

  it("then shows only what is new after", () => {
    const held = refreshState([entry(2), entry(1)], null, false, false);
    expect(refreshState([entry(3), entry(2), entry(1)], held.seen, false, true).pending.map((e) => e.id)).toEqual([3]);
  });
});

it("formats the date", () => {
  expect(changeDateText("2026-10-23")).toBe("Oct 23");
  expect(changeDateText("soon")).toBe("soon");
});
