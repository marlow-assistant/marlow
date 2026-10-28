// Pure view-model logic for the Activity and Identity views: grouping, ordering
// and which rows show. No React, no clock of its own (time and zone injected).

import { isVisible } from "../../lib/activityFormat";
import type { ActivityEntry, IdentityFileInfo, IdentityKind } from "../../lib/apiModels";
import * as IdentityFormat from "../../lib/identityFormat";

// MARK: Activity

export interface DaySection {
  title: string;
  entries: ActivityEntry[];
}

/** Appends a page, dropping turns already held (a refetch can overlap). */
export function mergeEntries(held: ActivityEntry[], page: ActivityEntry[]): ActivityEntry[] {
  const seen = new Set(held.map((e) => e.turnId));
  return [...held, ...page.filter((e) => !seen.has(e.turnId))];
}

/** Noteworthy rows only (as iOS), newest first, grouped by calendar day. */
export function daySections(
  entries: readonly ActivityEntry[],
  opts: { timeZone: string; now: number; showBackground?: boolean },
): DaySection[] {
  const showBackground = opts.showBackground ?? false;
  const visible = entries
    .filter((e) => isVisible(e, showBackground))
    .sort((a, b) => b.finishedAt - a.finishedAt);
  const sections: DaySection[] = [];
  for (const entry of visible) {
    const title = dayTitle(entry.finishedAt * 1000, opts);
    const last = sections[sections.length - 1];
    if (last && last.title === title) last.entries.push(entry);
    else sections.push({ title, entries: [entry] });
  }
  return sections;
}

function dayKey(ms: number, timeZone: string): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }).format(
    new Date(ms),
  );
}

export function dayTitle(ms: number, opts: { timeZone: string; now: number }): string {
  const key = dayKey(ms, opts.timeZone);
  if (key === dayKey(opts.now, opts.timeZone)) return "Today";
  // 24h back from now lands on yesterday's date in every zone except across a
  // DST jump within the first/last hour; use 26h-safe check via two probes.
  const yesterday = dayKey(opts.now - 86400_000, opts.timeZone);
  if (key === yesterday) return "Yesterday";
  return new Intl.DateTimeFormat("en-US", {
    timeZone: opts.timeZone,
    weekday: "short",
    day: "numeric",
    month: "short",
  }).format(new Date(ms));
}

export function timeText(seconds: number, timeZone: string): string {
  return new Intl.DateTimeFormat("en-US", { timeZone, hour: "numeric", minute: "2-digit" }).format(
    new Date(seconds * 1000),
  );
}

// MARK: Identity

/** Memory files, newest first (the Memory panel). */
export function memoryFiles(files: readonly IdentityFileInfo[]): IdentityFileInfo[] {
  return files.filter((f) => f.kind === "memory").sort((a, b) => b.modifiedAt - a.modifiedAt);
}

/** The one file of a kind (USER.md, SOUL.md, CLAUDE.md), if the box has it. */
export function fileOfKind(files: readonly IdentityFileInfo[], kind: IdentityKind): IdentityFileInfo | null {
  return files.find((f) => f.kind === kind) ?? null;
}

/** Whether iOS strips frontmatter for this kind: it does for every kind, and the
 * web does the same. Kept as one function so a change is one edit. */
export function displayText(text: string): string {
  return IdentityFormat.stripFrontmatter(text);
}

/** Markdown files render as rich text; anything else is shown verbatim. */
export function isMarkdownId(id: string): boolean {
  return id.toLowerCase().endsWith(".md");
}
