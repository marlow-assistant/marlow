// The to-dos pane's rules (docs/tasks/todos-and-reminders.md, part 2), pure so
// they are tested without a screen. The box sends facts (a state, a due that is
// a day or an instant); the sections, the order, "overdue", the badge and every
// word are decided here, on the phone's own clock, which each function takes as
// `now` so tests pin it.
//
// A due is `YYYY-MM-DD` (a day: due until that day ends) or an ISO instant with
// an offset (a time). The phone's zone is the one that reads both.

import type { Todo } from "./apiModels";
import { addDays, localToday } from "./ideas";

const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

interface ParsedDue {
  /** A day's start in the phone's zone, or the instant itself. */
  at: Date;
  /** A bare day: no time of day to show, and overdue only once it has ended. */
  dayOnly: boolean;
  /** The phone-local `YYYY-MM-DD` it falls on. */
  day: string;
}

function parseDue(due: string | null): ParsedDue | null {
  if (!due) return null;
  if (ISO_DAY.test(due)) {
    const [y, m, d] = due.split("-").map(Number);
    return { at: new Date(y, m - 1, d), dayOnly: true, day: due };
  }
  const at = new Date(due);
  if (Number.isNaN(at.getTime())) return null;
  return { at, dayOnly: false, day: localToday(at) };
}

/** An open item whose due has passed: a time before now, or a day before
 * today. A day is not overdue on the day itself. */
export function isOverdue(todo: Todo, now: Date): boolean {
  if (todo.state !== "open") return false;
  const due = parseDue(todo.due);
  if (!due) return false;
  return due.dayOnly ? due.day < localToday(now) : due.at.getTime() < now.getTime();
}

/** What the icon's badge counts: overdue open items plus proposed ones. */
export function badgeCount(todos: Todo[], now: Date): number {
  return todos.filter((t) => t.state === "proposed" || isOverdue(t, now)).length;
}

export type TodoSectionId = "todo" | "suggested" | "done";

export interface TodoSection {
  id: TodoSectionId;
  title: string;
  todos: Todo[];
}

function ms(iso: string | null): number {
  const t = iso ? Date.parse(iso) : NaN;
  return Number.isNaN(t) ? 0 : t;
}

/** To do (by due, a day as its start, then the undated by created), Marlow
 * suggested (newest first), Done this week (newest closed first). Empty
 * sections are left out, and a dropped item is in none. `sort` is stable, so
 * equal keys keep the box's list order. */
export function sectionsOf(todos: Todo[]): TodoSection[] {
  const open = todos
    .filter((t) => t.state === "open")
    .sort((a, b) => {
      const da = parseDue(a.due);
      const db = parseDue(b.due);
      if (da && db) return da.at.getTime() - db.at.getTime();
      if (da) return -1;
      if (db) return 1;
      return ms(a.created) - ms(b.created);
    });
  const suggested = todos.filter((t) => t.state === "proposed").sort((a, b) => ms(b.created) - ms(a.created));
  const done = todos.filter((t) => t.state === "done").sort((a, b) => ms(b.closed) - ms(a.closed));
  const sections: TodoSection[] = [
    { id: "todo", title: "To do", todos: open },
    { id: "suggested", title: "Marlow suggested", todos: suggested },
    { id: "done", title: "Done this week", todos: done },
  ];
  return sections.filter((s) => s.todos.length > 0);
}

const p2 = (n: number) => String(n).padStart(2, "0");

/** "Today", "Tomorrow", else "Fri 6 Nov"; a time adds " 09:00". Fixed English
 * wording rather than `Intl`, so it reads the same in every locale. */
function friendly(at: Date, dayOnly: boolean, now: Date): string {
  const today = localToday(now);
  const day = localToday(at);
  const label =
    day === today
      ? "Today"
      : day === addDays(today, 1)
        ? "Tomorrow"
        : `${WEEKDAYS[at.getDay()]} ${at.getDate()} ${MONTHS[at.getMonth()]}`;
  return dayOnly ? label : `${label} ${p2(at.getHours())}:${p2(at.getMinutes())}`;
}

/** The due, in words, with "Overdue · " ahead of an open item that is late. */
export function dueLine(todo: Todo, now: Date): string | null {
  const due = parseDue(todo.due);
  if (!due) return null;
  const text = friendly(due.at, due.dayOnly, now);
  return isOverdue(todo, now) ? `Overdue · ${text}` : text;
}

export function snoozedLine(todo: Todo, now: Date): string | null {
  if (!todo.snoozedUntil) return null;
  const at = new Date(todo.snoozedUntil);
  if (Number.isNaN(at.getTime())) return null;
  return `Snoozed until ${friendly(at, false, now)}`;
}

/** An ISO instant with the phone's offset at that moment, the one shape the
 * box takes for a snooze (a bare local time is a 422). */
export function snoozeIso(date: Date): string {
  const off = -date.getTimezoneOffset();
  const sign = off < 0 ? "-" : "+";
  const abs = Math.abs(off);
  return (
    `${String(date.getFullYear()).padStart(4, "0")}-${p2(date.getMonth() + 1)}-${p2(date.getDate())}` +
    `T${p2(date.getHours())}:${p2(date.getMinutes())}:${p2(date.getSeconds())}` +
    `${sign}${p2(Math.trunc(abs / 60))}:${p2(abs % 60)}`
  );
}

export interface SnoozePreset {
  id: "two-hours" | "evening" | "tomorrow";
  label: string;
  until: string;
}

/** In 2 hours, This evening 18:00 (only before 17:00, when it is still a
 * stretch away), Tomorrow 09:00. */
export function snoozePresets(now: Date): SnoozePreset[] {
  const minute = new Date(now.getFullYear(), now.getMonth(), now.getDate(), now.getHours(), now.getMinutes());
  const presets: SnoozePreset[] = [
    { id: "two-hours", label: "In 2 hours", until: snoozeIso(new Date(minute.getTime() + 2 * 3_600_000)) },
  ];
  if (now.getHours() < 17) {
    presets.push({
      id: "evening",
      label: "This evening 18:00",
      until: snoozeIso(new Date(now.getFullYear(), now.getMonth(), now.getDate(), 18)),
    });
  }
  presets.push({
    id: "tomorrow",
    label: "Tomorrow 09:00",
    until: snoozeIso(new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1, 9)),
  });
  return presets;
}

/** A year less a day: the box takes a snooze up to a year ahead. */
const MAX_SNOOZE_MS = 364 * 86_400_000;

/** A `datetime-local` value (`YYYY-MM-DDTHH:mm`, the phone's zone) as the
 * instant to send, or null when it is empty, not a time, in the past, or too
 * far ahead for the box to take. */
export function localInputToIso(value: string, now: Date): string | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value);
  if (!m) return null;
  const [y, mo, d, h, mi] = m.slice(1).map(Number);
  const at = new Date(y, mo - 1, d, h, mi);
  // A date the calendar rolled over (31 Nov) is not what was typed.
  if (at.getMonth() !== mo - 1 || at.getDate() !== d) return null;
  const ahead = at.getTime() - now.getTime();
  if (ahead <= 0 || ahead > MAX_SNOOZE_MS) return null;
  return snoozeIso(at);
}
