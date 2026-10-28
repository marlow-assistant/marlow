import { describe, expect, it } from "vitest";
import { makeScheduleEntry, type Recurrence, type ScheduleEntry } from "./apiModels";
import { calendarZone } from "./identityFormat";
import { isOverdue, sections, whenText } from "./scheduleFormat";

const TORONTO = "America/Toronto";

/** Epoch ms of a wall-clock time in an IANA zone (independent of the machine's). */
function atIn(zone: string, y: number, mo: number, d: number, h: number, mi: number): number {
  let guess = Date.UTC(y, mo - 1, d, h, mi);
  for (let i = 0; i < 3; i++) {
    const p = new Intl.DateTimeFormat("en-US", {
      timeZone: zone, year: "numeric", month: "numeric", day: "numeric",
      hour: "numeric", minute: "numeric", hourCycle: "h23",
    }).formatToParts(new Date(guess));
    const g = (t: string) => Number(p.find((x) => x.type === t)!.value);
    const shown = Date.UTC(g("year"), g("month") - 1, g("day"), g("hour") % 24, g("minute"));
    guess += Date.UTC(y, mo - 1, d, h, mi) - shown;
  }
  return guess;
}
const at = (y: number, mo: number, d: number, h: number, mi: number) => atIn(TORONTO, y, mo, d, h, mi);

function entry(
  id: string,
  recurrence: Recurrence,
  o: { notify?: string; next?: number; cron?: string; at?: string } = {},
): ScheduleEntry {
  return makeScheduleEntry({
    id,
    cron: o.cron ?? null,
    at: o.at ?? null,
    recurrence,
    nextFireAt: o.next === undefined ? null : o.next / 1000,
    notify: o.notify ?? null,
    job: o.notify === undefined ? id : null,
  });
}
const daily = (times: string[]): Recurrence => ({ type: "daily", times });
const weekly = (weekdays: number[], times: string[]): Recurrence => ({ type: "weekly", weekdays, times });
const monthly = (days: number[], times: string[]): Recurrence => ({ type: "monthly", days, times });
const once: Recurrence = { type: "once" };
const other: Recurrence = { type: "other" };

const NOW = at(2026, 10, 14, 9, 0);
const when = (e: ScheduleEntry) => whenText(e, { timeZone: TORONTO, now: NOW });
const ids = (es: ScheduleEntry[]) => es.map((e) => e.id);

describe("sections", () => {
  it("follow a fixed order and omit empty ones", () => {
    const entries = [
      entry("o", other, { cron: "0 8 1 6 *" }),
      entry("m", monthly([1], ["08:00"])),
      entry("w", weekly([1], ["08:00"])),
      entry("d", daily(["08:00"])),
      entry("r", once, { notify: "Do it", next: at(2026, 10, 20, 8, 15) }),
      entry("j", once, { next: at(2026, 10, 21, 8, 15) }),
    ];
    const groups = sections(entries);
    expect(groups.map((g) => g.section)).toEqual(["Reminders", "Daily", "Weekly", "Monthly", "One-off jobs", "Other"]);
    expect(groups.map((g) => ids(g.entries))).toEqual([["r"], ["d"], ["w"], ["m"], ["j"], ["o"]]);
    expect(sections([entry("d", daily(["08:00"]))]).map((g) => g.section)).toEqual(["Daily"]);
    expect(sections([])).toEqual([]);
  });

  it("a notify entry is a reminder whatever its recurrence", () => {
    const a = entry("a", daily(["08:00"]), { notify: "x" });
    const b = entry("b", weekly([1], ["08:00"]), { notify: "x" });
    expect(sections([a, b]).map((g) => g.section)).toEqual(["Reminders"]);
  });

  it("a weekly entry on all seven days is daily", () => {
    const all = entry("all", weekly([0, 1, 2, 3, 4, 5, 6], ["09:00"]));
    const six = entry("six", weekly([1, 2, 3, 4, 5, 6], ["09:00"]));
    expect(sections([all, six]).map((g) => g.section)).toEqual(["Daily", "Weekly"]);
    expect(when(all)).toBe("9:00");
  });

  it("reminders sort by soonest with no time last", () => {
    const late = entry("late", once, { notify: "x", next: at(2026, 11, 1, 8, 0) });
    const none = entry("none", once, { notify: "x" });
    const soon = entry("soon", once, { notify: "x", next: at(2026, 10, 16, 8, 0) });
    expect(ids(sections([late, none, soon])[0].entries)).toEqual(["soon", "late", "none"]);
  });

  it("daily, weekly and monthly sort within their sections", () => {
    const d = [
      entry("evening", daily(["20:57"])),
      entry("night", daily(["01:00"])),
      entry("morning", daily(["08:00", "20:00"])),
    ];
    expect(ids(sections(d)[0].entries)).toEqual(["night", "morning", "evening"]);

    // Monday-first: Sunday sorts last, then by time.
    const w = [
      entry("sun", weekly([0], ["18:00"])),
      entry("mon-late", weekly([1], ["09:00"])),
      entry("mon-early", weekly([1], ["08:03"])),
      entry("fri", weekly([5], ["07:00"])),
    ];
    expect(ids(sections(w)[0].entries)).toEqual(["mon-early", "mon-late", "fri", "sun"]);

    const m = [
      entry("fifteenth", monthly([15], ["08:00"])),
      entry("first-late", monthly([1, 15], ["09:00"])),
      entry("first", monthly([1], ["08:00"])),
    ];
    expect(ids(sections(m)[0].entries)).toEqual(["first", "first-late", "fifteenth"]);
  });

  it("one-off jobs sort by time and other keeps store order", () => {
    const later = entry("later", once, { next: at(2026, 12, 1, 8, 0) });
    const sooner = entry("sooner", once, { next: at(2026, 10, 20, 8, 0) });
    expect(ids(sections([later, sooner])[0].entries)).toEqual(["sooner", "later"]);
    const b = entry("b", other, { cron: "* * * 6 *" });
    const a = entry("a", other, { cron: "* * * 7 *" });
    expect(ids(sections([b, a])[0].entries)).toEqual(["b", "a"]);
  });
});

describe("wording", () => {
  it("daily shows its times and collapses past three", () => {
    expect(when(entry("a", daily(["08:00"])))).toBe("8:00");
    expect(when(entry("a", daily(["08:00", "20:00"])))).toBe("8:00, 20:00");
    expect(when(entry("a", daily(["01:00", "02:00", "03:00"])))).toBe("1:00, 2:00, 3:00");
    const many = Array.from({ length: 18 }, (_, i) =>
      `${String(8 + Math.floor(i / 2)).padStart(2, "0")}:${String((i % 2) * 30).padStart(2, "0")}`);
    expect(when(entry("a", daily(many)))).toBe("18× a day");
  });

  it("weekly names days Monday-first", () => {
    expect(when(entry("a", weekly([1], ["08:03"])))).toBe("Mon 8:03");
    expect(when(entry("a", weekly([0], ["18:00"])))).toBe("Sun 18:00");
    expect(when(entry("a", weekly([1, 2, 3, 4, 5], ["08:00"])))).toBe("Weekdays 8:00");
    expect(when(entry("a", weekly([0, 6], ["08:00"])))).toBe("Weekends 8:00");
    expect(when(entry("a", weekly([0, 5, 3, 1], ["08:00"])))).toBe("Mon, Wed, Fri, Sun 8:00");
    expect(when(entry("a", weekly([5, 3, 1], ["08:00"])))).toBe("Mon, Wed, Fri 8:00");
    expect(when(entry("a", weekly([1], ["01:00", "02:00", "03:00", "04:00"])))).toBe("Mon 4× a day");
  });

  it("monthly uses ordinals", () => {
    expect(when(entry("a", monthly([1], ["08:00"])))).toBe("1st 8:00");
    expect(when(entry("a", monthly([1, 15], ["08:00"])))).toBe("1st, 15th 8:00");
    const days = [2, 3, 4, 11, 12, 13, 21, 22, 23, 31];
    expect(when(entry("a", monthly(days, ["08:00"])))).toBe(
      "2nd, 3rd, 4th, 11th, 12th, 13th, 21st, 22nd, 23rd, 31st 8:00",
    );
  });

  it("once reads the date in the store zone with the year only if not current", () => {
    const thisYear = entry("a", once, { next: at(2026, 10, 20, 8, 15) });
    expect(when(thisYear)).toBe("Oct 20, 8:15");
    const nextYear = entry("a", once, { next: at(2027, 1, 5, 8, 15) });
    expect(when(nextYear)).toBe("Jan 5 2027, 8:15");
    // Store zone, not the browser's: 8:15 Toronto stays 8:15 from Edmonton.
    const store = calendarZone(TORONTO, "America/Edmonton");
    expect(whenText(thisYear, { timeZone: store, now: NOW })).toBe("Oct 20, 8:15");
    // And reading it in Edmonton instead would be an hour earlier: the zone is what decides.
    expect(whenText(thisYear, { timeZone: "America/Edmonton", now: NOW })).toBe("Oct 20, 6:15");
  });

  it("a once with no instant shows its raw at", () => {
    expect(when(entry("a", once, { at: "2026-10-20T08:15-0400" }))).toBe("2026-10-20T08:15-0400");
  });

  it("other shows the raw cron", () => {
    expect(when(entry("a", other, { cron: "0 8 1 6 *" }))).toBe("0 8 1 6 *");
  });

  it("recurring reminders carry their cadence", () => {
    expect(when(entry("a", daily(["08:00"]), { notify: "x" }))).toBe("Daily 8:00");
    expect(when(entry("a", weekly([1], ["08:03"]), { notify: "x" }))).toBe("Mon 8:03");
    expect(when(entry("a", monthly([1], ["08:00"]), { notify: "x" }))).toBe("Monthly 1st 8:00");
    expect(when(entry("a", once, { notify: "x", next: at(2026, 10, 20, 8, 15) }))).toBe("Oct 20, 8:15");
  });

  it("midnight reads as 0:xx, not 24:xx", () => {
    expect(when(entry("a", once, { next: at(2026, 10, 20, 0, 5) }))).toBe("Oct 20, 0:05");
  });
});

describe("overdue", () => {
  it("only a past once is overdue", () => {
    const now = at(2026, 10, 14, 9, 0);
    expect(isOverdue(entry("a", once, { next: at(2026, 10, 14, 8, 0) }), now)).toBe(true);
    expect(isOverdue(entry("a", once, { next: now }), now)).toBe(true);
    expect(isOverdue(entry("a", once, { next: at(2026, 10, 15, 8, 0) }), now)).toBe(false);
    expect(isOverdue(entry("a", once), now)).toBe(false);
    expect(isOverdue(entry("a", daily(["08:00"]), { next: at(2026, 10, 14, 8, 0) }), now)).toBe(false);
  });
});
