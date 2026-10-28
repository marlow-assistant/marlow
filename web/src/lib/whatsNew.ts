// What's new (docs/tasks/whats-new.md). Port of iOS `WhatsNew`: the same
// rules, so the two clients agree on what counts as news.
//
// Both halves come from the repo-root changes.json. `app` entries are bundled
// at build time (below); `server` entries come from `GET /v1/changes`, so each
// half announces only what is actually running. On a box, update.sh builds
// this client from the same checkout it deploys, so the two always match;
// keeping the split anyway means a client served from elsewhere stays honest.

import bundledFile from "../../../changes.json";

export interface ChangeEntry {
  id: number;
  date: string;
  part: "app" | "server";
  category: string;
  text: string;
}

/** A device that has never shown the card gets the newest few, not the
 * whole history. Same as iOS `WhatsNew.firstRunCount`. */
export const FIRST_RUN_COUNT = 5;

const ALWAYS_TELL = new Set(["access", "unattended", "data", "personality", "removed"]);

export function decodeChanges(raw: unknown): ChangeEntry[] {
  const list = (raw as { changes?: unknown; entries?: unknown } | null)?.changes ?? (raw as { entries?: unknown } | null)?.entries;
  if (!Array.isArray(list)) return [];
  return list.flatMap((e: unknown): ChangeEntry[] => {
    const r = e as Partial<ChangeEntry> | null;
    if (
      typeof r?.id !== "number" ||
      typeof r.date !== "string" ||
      (r.part !== "app" && r.part !== "server") ||
      typeof r.category !== "string" ||
      typeof r.text !== "string"
    )
      return [];
    return [{ id: r.id, date: r.date, part: r.part, category: r.category, text: r.text }];
  });
}

/** This build's own half. */
export function bundledChanges(): ChangeEntry[] {
  return decodeChanges(bundledFile).filter((e) => e.part === "app");
}

/** Each half contributes only its own part, whatever it was handed. Newest
 * first. */
export function mergeChanges(app: ChangeEntry[], server: ChangeEntry[]): ChangeEntry[] {
  const byId = new Map<number, ChangeEntry>();
  for (const e of app) if (e.part === "app") byId.set(e.id, e);
  for (const e of server) if (e.part === "server") byId.set(e.id, e);
  return [...byId.values()].sort((a, b) => b.id - a.id);
}

/** A set rather than a high-water mark: the halves update at different times,
 * so app entry 12 can be shown before the box deploys server entry 11. Null
 * means this device has never shown the card. */
export function unseenChanges(entries: ChangeEntry[], seen: ReadonlySet<number> | null): ChangeEntry[] {
  if (seen === null) return entries.slice(0, FIRST_RUN_COUNT);
  return entries.filter((e) => !seen.has(e.id));
}

export interface WhatsNewState {
  seen: number[];
  pending: ChangeEntry[];
}

/** One refresh, as iOS `WhatsNewStore.refresh()`: while muted, or before onboarding
 * ends, everything counts as seen (turning the card back on brings only what is new from then
 * on); on a first run everything older than what the card shows is history. */
export function refreshState(
  entries: ChangeEntry[],
  seen: number[] | null,
  muted: boolean,
  onboarded = true,
): WhatsNewState {
  const all = entries.map((e) => e.id);
  // Before onboarding ends, as when muted: nothing shows and what would have
  // is history, so finishing onboarding doesn't open on a backlog.
  if (muted || !onboarded) {
    // The same array when nothing is added, so the caller skips the write.
    const merged = union(seen ?? [], all);
    return { seen: seen && merged.length === seen.length ? seen : merged, pending: [] };
  }
  const pending = unseenChanges(entries, seen === null ? null : new Set(seen));
  if (seen === null) {
    const shown = new Set(pending.map((e) => e.id));
    return { seen: union([], all.filter((id) => !shown.has(id))), pending };
  }
  return { seen, pending };
}

export function union(a: number[], b: number[]): number[] {
  return [...new Set([...a, ...b])].sort((x, y) => x - y);
}

/** The policy's must-tell categories, drawn in the accent. */
export function isAlwaysTell(category: string): boolean {
  return ALWAYS_TELL.has(category);
}

export function categoryLabel(category: string): string {
  switch (category) {
    case "access":
      return "What Marlow can reach";
    case "unattended":
      return "Runs on its own";
    case "data":
      return "Your data";
    case "personality":
      return "How Marlow behaves";
    case "removed":
      return "Removed";
    case "fix":
      return "Fixed";
    default:
      return "New";
  }
}

/** "Oct 23" from "2026-10-23"; the raw string if it won't parse. */
export function changeDateText(date: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date);
  if (!m) return date;
  const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])));
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
}
