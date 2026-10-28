// How the Marlow sheet's files and schedules read on screen. Port of ios
// IdentityFormat.swift. Pure, and separate from the views, so the wording and the
// frontmatter rule are testable without rendering.

import { humanise, jobTitle, lastPathComponent } from "./activityFormat";
import type { IdentityKind } from "./apiModels";

// MARK: Names

function baseName(id: string): string {
  const name = lastPathComponent(id);
  return name.endsWith(".md") ? name.slice(0, -3) : name;
}

/** "memory/marlow-features.md" -> "Marlow features". Filenames are Marlow's own,
 * so the humaniser is the activity one: one rule for every id. */
export function memoryName(id: string): string {
  return humanise(baseName(id));
}

/** The screen title for a file. The three fixed files get the names the icon
 * strip uses ("Soul", "You", "Instructions"), not their filenames. */
export function title(id: string, kind: IdentityKind): string {
  switch (kind) {
    case "soul":
      return "Soul";
    case "user":
      return "You";
    case "instructions":
      return "Instructions";
    case "job":
      return jobTitle(baseName(id));
    case "memory":
    case "unknown":
      return memoryName(id);
  }
}

// MARK: About this file

/** One fixed sentence per kind, written in the app and never part of the file.
 * Says what the file is and how Marlow uses it. */
export function aboutNote(kind: IdentityKind): string {
  switch (kind) {
    case "soul":
      return "Marlow's personality and values. Marlow reads this at the start of every conversation.";
    case "user":
      return "What Marlow knows about you. Marlow reads this at the start of every conversation.";
    case "instructions":
      return "Marlow's operating instructions — how it works, what tools it uses, and its rules.";
    case "memory":
      return "One of Marlow's memory files. Marlow sees its one-line description every turn and reads the full file when it's relevant. It can be out of date.";
    case "job":
      return "The instructions Marlow follows when this scheduled job runs.";
    case "unknown":
      return "A file Marlow keeps.";
  }
}

// MARK: Frontmatter

// \r\n as one newline, then the single-character ones (Swift's Character.isNewline).
const NEWLINE = new RegExp("\\r\\n|[\\n\\r\\u000B\\u000C\\u0085\\u2028\\u2029]");

/** Drops a leading YAML frontmatter block: a first line of `---` through the next
 * line of `---`. A file that opens with `---` as a horizontal rule and never
 * closes it is left whole: losing the body to a fence that was never a fence
 * would be worse than showing a stray rule. */
export function stripFrontmatter(text: string): string {
  // Split on any newline, "\r\n" as ONE (the Swift reads it as one Character).
  const lines = text.split(NEWLINE);
  const isFence = (line: string) => line.trim() === "---";
  if (!isFence(lines[0])) return text;
  let close = -1;
  for (let i = 1; i < lines.length; i++) {
    if (isFence(lines[i])) {
      close = i;
      break;
    }
  }
  if (close < 0) return text;
  const body = lines.slice(close + 1);
  let start = 0;
  while (start < body.length && body[start].trim() === "") start++;
  return body.slice(start).join("\n");
}

// MARK: Dates

function isValidZone(zone: string): boolean {
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: zone });
    return true;
  } catch {
    return false;
  }
}

/** The zone schedule times are read in: the schedule store's, which is the zone
 * the server's wall-clock times are in. `baseZone` (the browser's own, unless
 * injected) when the store names none or one this runtime doesn't know. */
export function calendarZone(
  storeZone: string | null | undefined,
  baseZone: string = Intl.DateTimeFormat().resolvedOptions().timeZone,
): string {
  return storeZone && isValidZone(storeZone) ? storeZone : baseZone;
}

/** "Changed 3 days ago" for a memory row; "Changed just now" inside a minute,
 * because the relative formatter says "in 0 seconds" for those. `timestamp` is
 * server epoch SECONDS, `now` epoch ms. */
export function changed(timestamp: number, now: number, locale: string = "en-US"): string {
  const diffSeconds = timestamp - now / 1000; // negative: in the past
  if (Math.abs(diffSeconds) < 60) return "Changed just now";
  const rtf = new Intl.RelativeTimeFormat(locale, { numeric: "always", style: "long" });
  const abs = Math.abs(diffSeconds);
  const sign = diffSeconds < 0 ? -1 : 1;
  // The largest unit that fits, rounding down, as the system formatter does.
  const steps: [Intl.RelativeTimeFormatUnit, number][] = [
    ["year", 365 * 86400],
    ["month", 30 * 86400],
    ["week", 7 * 86400],
    ["day", 86400],
    ["hour", 3600],
    ["minute", 60],
  ];
  for (const [unit, size] of steps) {
    if (abs >= size) return "Changed " + rtf.format(sign * Math.floor(abs / size), unit);
  }
  return "Changed just now";
}
