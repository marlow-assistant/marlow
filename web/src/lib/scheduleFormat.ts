// Grouping and wording for the Schedule panel. Port of ios ScheduleFormat.swift.
// The server sends structure (`Recurrence`); every word on screen is made here so
// a wording tweak never needs a box deploy.
//
// Time zone and clock are injected (`timeZone` is an IANA name, `now` is epoch
// ms) so the output never depends on the machine's zone. The zone that matters
// is the schedule STORE's, which is the zone its "HH:MM" wall-clock times are in
// (see `calendarZone` in identityFormat.ts).

import type { Recurrence, ScheduleEntry } from "./apiModels";

export type Section = "Reminders" | "Daily" | "Weekly" | "Monthly" | "One-off jobs" | "Other";

/** Fixed section order. */
export const SECTION_ORDER: readonly Section[] = [
  "Reminders",
  "Daily",
  "Weekly",
  "Monthly",
  "One-off jobs",
  "Other",
];

export interface Group {
  section: Section;
  entries: ScheduleEntry[];
}

// MARK: Grouping

export function section(entry: ScheduleEntry): Section {
  if (entry.notify !== null) return "Reminders";
  const r = entry.recurrence;
  switch (r.type) {
    case "daily":
      return "Daily";
    case "weekly":
      return new Set(r.weekdays).size === 7 ? "Daily" : "Weekly";
    case "monthly":
      return "Monthly";
    case "once":
      return "One-off jobs";
    case "other":
      return "Other";
  }
}

/** Fixed section order, empty sections omitted. Sorting is stable, so ties (and
 * the whole of "Other") keep store order. */
export function sections(entries: readonly ScheduleEntry[]): Group[] {
  const groups: Group[] = [];
  for (const sec of SECTION_ORDER) {
    const members = entries.filter((e) => section(e) === sec);
    if (members.length === 0) continue;
    groups.push({ section: sec, entries: sorted(members, sec) });
  }
  return groups;
}

function sorted(entries: ScheduleEntry[], sec: Section): ScheduleEntry[] {
  const key = (e: ScheduleEntry): number[] => {
    switch (sec) {
      case "Reminders":
      case "One-off jobs":
        return [e.nextFireAt ?? Infinity];
      case "Daily":
        return [firstMinute(e.recurrence)];
      case "Weekly":
        return [firstWeekdayRank(e.recurrence), firstMinute(e.recurrence)];
      case "Monthly":
        return [firstDay(e.recurrence), firstMinute(e.recurrence)];
      case "Other":
        return [];
    }
  };
  return entries
    .map((element, offset) => ({ element, offset, k: key(element) }))
    .sort((a, b) => {
      for (let i = 0; i < Math.min(a.k.length, b.k.length); i++) {
        if (a.k[i] !== b.k[i]) return a.k[i] < b.k[i] ? -1 : 1;
      }
      return a.offset - b.offset;
    })
    .map((x) => x.element);
}

function timesOf(r: Recurrence): string[] {
  switch (r.type) {
    case "daily":
    case "weekly":
    case "monthly":
      return r.times;
    default:
      return [];
  }
}

function firstMinute(r: Recurrence): number {
  const first = timesOf(r)[0];
  const parsed = first === undefined ? null : parseClock(first);
  return parsed ? parsed[0] * 60 + parsed[1] : Infinity;
}

/** Monday-first: Mon 0 ... Sat 5, Sun 6. */
const MONDAY_FIRST = [1, 2, 3, 4, 5, 6, 0];

function firstWeekdayRank(r: Recurrence): number {
  if (r.type !== "weekly") return Infinity;
  const ranks = r.weekdays.map((d) => MONDAY_FIRST.indexOf(d)).filter((i) => i >= 0);
  return ranks.length === 0 ? 99 : Math.min(...ranks);
}

function firstDay(r: Recurrence): number {
  if (r.type !== "monthly" || r.days.length === 0) return Infinity;
  return Math.min(...r.days);
}

// MARK: Wording

/** Only a past `once` is overdue. `nextFireAt` is epoch seconds, `now` epoch ms. */
export function isOverdue(entry: ScheduleEntry, now: number): boolean {
  if (entry.recurrence.type !== "once" || entry.nextFireAt === null) return false;
  return entry.nextFireAt <= now / 1000;
}

/** The short right-hand text of a row. */
export function whenText(
  entry: ScheduleEntry,
  opts: { timeZone: string; now: number },
): string {
  const reminder = entry.notify !== null;
  const r = entry.recurrence;
  switch (r.type) {
    case "daily": {
      const text = timesText(r.times);
      return reminder ? join("Daily", text) : text;
    }
    case "weekly": {
      if (new Set(r.weekdays).size === 7) {
        const text = timesText(r.times);
        return reminder ? join("Daily", text) : text;
      }
      return join(weekdaysText(r.weekdays), timesText(r.times));
    }
    case "monthly": {
      const text = join(r.days.map(ordinal).join(", "), timesText(r.times));
      return reminder ? join("Monthly", text) : text;
    }
    case "once":
      if (entry.nextFireAt === null) return entry.at ?? "";
      return dateText(entry.nextFireAt * 1000, opts.timeZone, opts.now);
    case "other":
      return entry.cron ?? entry.at ?? "";
  }
}

function join(a: string, b: string): string {
  return [a, b].filter((s) => s !== "").join(" ");
}

function parseClock(time: string): [number, number] | null {
  const parts = time.split(":");
  if (parts.length !== 2) return null;
  if (!/^[+-]?\d+$/.test(parts[0]) || !/^[+-]?\d+$/.test(parts[1])) return null;
  return [parseInt(parts[0], 10), parseInt(parts[1], 10)];
}

/** "08:03" -> "8:03"; anything unparseable is shown as sent. */
function clock(time: string): string {
  const p = parseClock(time);
  if (!p) return time;
  return `${p[0]}:${String(p[1]).padStart(2, "0")}`;
}

function timesText(times: string[]): string {
  if (times.length > 3) return `${times.length}× a day`;
  return times.map(clock).join(", ");
}

const DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

function weekdaysText(days: number[]): string {
  const set = new Set(days);
  const same = (want: number[]) => set.size === want.length && want.every((d) => set.has(d));
  if (same([1, 2, 3, 4, 5])) return "Weekdays";
  if (same([0, 6])) return "Weekends";
  return MONDAY_FIRST.filter((d) => set.has(d))
    .map((d) => DAY_NAMES[d])
    .join(", ");
}

function ordinal(n: number): string {
  let suffix: string;
  const mod100 = n % 100;
  if (mod100 >= 11 && mod100 <= 13) {
    suffix = "th";
  } else {
    switch (n % 10) {
      case 1:
        suffix = "st";
        break;
      case 2:
        suffix = "nd";
        break;
      case 3:
        suffix = "rd";
        break;
      default:
        suffix = "th";
    }
  }
  return `${n}${suffix}`;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

interface Wall {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
}

/** Wall-clock parts of an instant in an IANA zone. */
function wallClock(ms: number, timeZone: string): Wall {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    year: "numeric",
    month: "numeric",
    day: "numeric",
    hour: "numeric",
    minute: "numeric",
    hourCycle: "h23",
  }).formatToParts(new Date(ms));
  const get = (t: string) => Number(parts.find((p) => p.type === t)?.value ?? 0);
  return { year: get("year"), month: get("month"), day: get("day"), hour: get("hour") % 24, minute: get("minute") };
}

/** "Oct 20, 8:15"; "Jan 5 2027, 8:15" when the year isn't the current one. */
function dateText(ms: number, timeZone: string, now: number): string {
  const d = wallClock(ms, timeZone);
  const sameYear = d.year === wallClock(now, timeZone).year;
  const time = `${d.hour}:${String(d.minute).padStart(2, "0")}`;
  const date = `${MONTHS[d.month - 1]} ${d.day}`;
  return sameYear ? `${date}, ${time}` : `${date} ${d.year}, ${time}`;
}
