// Timestamp dividers (contract RND-2). Shown above a message when it lands more
// than 30 minutes after the one before it, and above the first rendered one: a
// burst of replies a few minutes apart doesn't need one between each, but a
// message opened cold the next morning should say when it actually arrived.
//
// Labels: today, the time; yesterday, "Yesterday, <time>"; within 7 days,
// "<Weekday>, <time>"; older, "<Mon> <day>, <time>", no year.

import type { ChatMessage } from "./types";

export const DIVIDER_GAP_MS = 30 * 60 * 1000;

export interface DividerOptions {
  locale?: string;
  /** IANA zone; defaults to the runtime's. Injectable so tests do not depend on
   * the machine's TZ. */
  timeZone?: string;
}

/** Calendar day number in `timeZone`, comparable by subtraction. */
function dayNumber(ms: number, timeZone?: string): number {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    year: "numeric",
    month: "numeric",
    day: "numeric",
  }).formatToParts(new Date(ms));
  const get = (t: string) => Number(parts.find((p) => p.type === t)?.value);
  return Math.round(Date.UTC(get("year"), get("month") - 1, get("day")) / 86_400_000);
}

export function dividerLabel(ms: number, nowMs: number, opts: DividerOptions = {}): string {
  const { locale, timeZone } = opts;
  const time = new Intl.DateTimeFormat(locale, { timeZone, hour: "numeric", minute: "2-digit" }).format(ms);
  const dayDiff = dayNumber(nowMs, timeZone) - dayNumber(ms, timeZone);
  if (dayDiff <= 0) return time;
  if (dayDiff === 1) return `Yesterday, ${time}`;
  // iOS: whole 24h periods elapsed (`dateComponents([.day], from: date, to: now)`).
  const daysAgo = Math.floor((nowMs - ms) / 86_400_000);
  if (daysAgo < 7) {
    return `${new Intl.DateTimeFormat(locale, { timeZone, weekday: "long" }).format(ms)}, ${time}`;
  }
  return `${new Intl.DateTimeFormat(locale, { timeZone, month: "short", day: "numeric" }).format(ms)}, ${time}`;
}

/** message id -> divider label, for the messages that get a divider above them.
 * One O(n) pass over the rendered window. */
export function dividerLabels(
  messages: ChatMessage[],
  now: Date,
  opts: DividerOptions = {},
): Map<string, string> {
  const result = new Map<string, string>();
  let previous: ChatMessage | undefined;
  for (const m of messages) {
    if (!previous || m.createdAt - previous.createdAt > DIVIDER_GAP_MS) {
      result.set(m.id, dividerLabel(m.createdAt, now.getTime(), opts));
    }
    previous = m;
  }
  return result;
}
