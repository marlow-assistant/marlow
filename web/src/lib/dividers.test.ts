import { describe, expect, test } from "vitest";
import { dividerLabel, dividerLabels } from "./dividers";
import type { ChatMessage } from "./types";

const tz = "America/Toronto";
const opts = { locale: "en-US", timeZone: tz };
// Toronto is UTC-4 on these dates (EDT).
const at = (iso: string) => new Date(iso + "-04:00").getTime();
const NOW = new Date("2026-10-01T15:00:00-04:00");

function msg(id: string, ms: number): ChatMessage {
  return { id, role: "assistant", text: "", createdAt: ms, status: "done" };
}

describe("divider labels", () => {
  test("today reads as the time", () => {
    expect(dividerLabel(at("2026-10-01T09:05:00"), NOW.getTime(), opts)).toBe("9:05 AM");
  });

  test("yesterday says so", () => {
    expect(dividerLabel(at("2026-09-30T22:10:00"), NOW.getTime(), opts)).toBe("Yesterday, 10:10 PM");
  });

  test("within a week reads as the weekday", () => {
    // 2026-09-27 is a Sunday; 4 days ago.
    expect(dividerLabel(at("2026-09-27T08:00:00"), NOW.getTime(), opts)).toBe("Sunday, 8:00 AM");
  });

  test("six days ago is still a weekday, seven is a date", () => {
    expect(dividerLabel(at("2026-09-25T16:00:00"), NOW.getTime(), opts)).toBe("Friday, 4:00 PM");
    expect(dividerLabel(at("2026-09-24T09:00:00"), NOW.getTime(), opts)).toBe("Sep 24, 9:00 AM");
  });

  test("older reads as month and day with no year", () => {
    expect(dividerLabel(at("2026-08-02T07:30:00"), NOW.getTime(), opts)).toBe("Aug 2, 7:30 AM");
  });

  test("the day boundary follows the injected timezone, not the machine's", () => {
    // 2026-10-01T02:00Z is still Sept 30 evening in Toronto: "yesterday".
    const ms = Date.UTC(2026, 9, 1, 2, 0);
    expect(dividerLabel(ms, NOW.getTime(), opts)).toBe("Yesterday, 10:00 PM");
    expect(dividerLabel(ms, NOW.getTime(), { locale: "en-US", timeZone: "UTC" })).toBe("2:00 AM");
  });
});

describe("divider placement", () => {
  test("the first message always gets a divider", () => {
    const m = msg("a", at("2026-10-01T09:00:00"));
    expect([...dividerLabels([m], NOW, opts).keys()]).toEqual(["a"]);
  });

  test("a burst within 30 minutes gets one divider", () => {
    const base = at("2026-10-01T09:00:00");
    const ms = [msg("a", base), msg("b", base + 5 * 60_000), msg("c", base + 35 * 60_000)];
    expect([...dividerLabels(ms, NOW, opts).keys()]).toEqual(["a"]);
  });

  test("a gap of more than 30 minutes gets a divider; exactly 30 does not", () => {
    const base = at("2026-10-01T09:00:00");
    const ms = [msg("a", base), msg("b", base + 30 * 60_000), msg("c", base + 30 * 60_000 + 30 * 60_000 + 1)];
    expect([...dividerLabels(ms, NOW, opts).keys()]).toEqual(["a", "c"]);
  });

  test("labels are carried per message", () => {
    const ms = [msg("a", at("2026-09-30T22:00:00")), msg("b", at("2026-10-01T09:00:00"))];
    expect(dividerLabels(ms, NOW, opts)).toEqual(
      new Map([
        ["a", "Yesterday, 10:00 PM"],
        ["b", "9:00 AM"],
      ]),
    );
  });

  test("an empty window has no dividers", () => {
    expect(dividerLabels([], NOW, opts).size).toBe(0);
  });
});
