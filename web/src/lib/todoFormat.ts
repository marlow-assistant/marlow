// The to-dos pane's rules (docs/tasks/todos-and-reminders.md, part 2), pure so
// they are tested without a screen. The box sends facts (a state, a due that is
// a day or an instant); the sections, "overdue", the badge and every word are
// decided here, on the phone's own clock, which each function takes as `now` so
// tests pin it. To do's order is the exception: it's the user's, kept on the box
// so every client shows the same one (server/todos.py).
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

/** To do (the box's order, which is the user's), Marlow suggested (newest
 * first), Done this week (newest closed first). Empty sections are left out,
 * and a dropped item is in none. `sort` is stable, so equal keys keep the box's
 * list order. */
/** `placedAs` puts an item where an earlier copy of it would go: the panel
 * passes the items ticked or unticked since it opened, so they stay put and
 * show their new state there (Mike, 2026-11-04). */
export function sectionsOf(todos: Todo[], placedAs: ReadonlyMap<string, Todo> = new Map()): TodoSection[] {
  const current = new Map(todos.map((t) => [t.id, t]));
  const keys = todos.map((t) => placedAs.get(t.id) ?? t);
  const show = (list: Todo[]) => list.map((k) => current.get(k.id) ?? k);
  const open = keys.filter((t) => t.state === "open");
  const suggested = keys.filter((t) => t.state === "proposed").sort((a, b) => ms(b.created) - ms(a.created));
  const done = keys.filter((t) => t.state === "done").sort((a, b) => ms(b.closed) - ms(a.closed));
  const sections: TodoSection[] = [
    { id: "todo", title: "To do", todos: show(open) },
    { id: "suggested", title: "Marlow suggested", todos: show(suggested) },
    { id: "done", title: "Done this week", todos: show(done) },
  ];
  return sections.filter((s) => s.todos.length > 0);
}

/** The anchor that lands `id` at index `to` of `ids`: whatever then sits just
 * below it, null for the bottom. */
export function anchorAt(ids: readonly string[], id: string, to: number): string | null {
  return ids.filter((x) => x !== id)[to] ?? null;
}

/** The list as the box will have it after a move, for the moment before it
 * answers. Same rule as `TodoStore.move`: above the anchor, or after the last
 * open item for null. */
export function reordered(todos: readonly Todo[], id: string, before: string | null): Todo[] {
  const moved = todos.find((t) => t.id === id);
  if (!moved) return [...todos];
  const rest = todos.filter((t) => t.id !== id);
  const at =
    before === null ? rest.findLastIndex((t) => t.state === "open") + 1 : rest.findIndex((t) => t.id === before);
  return at < 0 ? [...todos] : [...rest.slice(0, at), moved, ...rest.slice(at)];
}

/** `todos` in the order of `order`, for undoing a move without undoing whatever
 * else changed since: rows `order` hadn't seen go last. */
export function inOrderOf(todos: readonly Todo[], order: readonly Todo[]): Todo[] {
  const current = new Map(todos.map((t) => [t.id, t]));
  const known = new Set(order.map((t) => t.id));
  return [...order.flatMap((t) => current.get(t.id) ?? []), ...todos.filter((t) => !known.has(t.id))];
}

export interface MoveChoice {
  label: string;
  to: number;
}

/** The menu's moves for the row at `index` of `count`, the way to reorder
 * without a drag. Top and bottom only when they differ from up and down. */
export function moveChoices(index: number, count: number): MoveChoice[] {
  const choices: MoveChoice[] = [];
  if (index > 1) choices.push({ label: "Move to top", to: 0 });
  if (index > 0) choices.push({ label: "Move up", to: index - 1 });
  if (index < count - 1) choices.push({ label: "Move down", to: index + 1 });
  if (index < count - 2) choices.push({ label: "Move to bottom", to: count - 1 });
  return choices;
}

export interface RowBox {
  top: number;
  height: number;
}

/** Where a dragged row would land: past every other row whose middle its own
 * middle has crossed. `offset` is how far it has travelled from `from`. */
export function dropIndex(rows: readonly RowBox[], from: number, offset: number): number {
  const middle = (r: RowBox) => r.top + r.height / 2;
  const at = middle(rows[from]) + offset;
  return rows.filter((r, i) => i !== from && middle(r) < at).length;
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
