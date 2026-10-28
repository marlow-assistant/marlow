// How an activity-log record reads on screen. Port of ios ActivityFormat.swift.
//
// Pure, and separate from the view, because the row IS this text and the
// wording is expected to change after Mike has lived with it for a few days.
//
// **Tool names select wording; they are never shown.** Same rule as the activity
// label: MCP connectors choose those names, so an unknown one is semi-trusted
// text, not copy. A known service gets its name ("Gmail"), anything else counts
// as "another tool". Targets (file paths, search queries) ARE shown: they are
// Marlow's own tool input, filtered by the server's allowlist, and must be
// rendered as plain text, never markdown or HTML.

import type { ActivityEntry } from "./apiModels";
import { capitalizedModelName } from "./modelName";

/** The server calls a turn a person sent from a paired device `chat`. It used
 * to call it `ios`, and records stored before the rename (the activity log
 * keeps 12 months) still say so, so both are accepted for good. */
export function isChatSource(value: string | null): boolean {
  return value === "chat" || value === "ios";
}

// MARK: Title

/** Cron job ids with a better name than their id. Anything else is the id
 * humanised, which covers schedules entries Marlow names itself. */
const JOB_TITLES: Record<string, string> = {
  morning: "Morning briefing",
  consolidate: "Nightly tidy",
  ideas: "Overnight ideas",
  // The inbox check. "Email flash" is our name for it, not the user's
  // (Taylor's first run, docs/tasks/onboarding-v2.md).
  "email-flash": "Check-in",
  "weekly-digest": "Weekly digest",
};

export function title(entry: ActivityEntry): string {
  if (isChatSource(entry.source)) return "Chat";
  if (!entry.label) return "Scheduled job";
  return jobTitle(entry.label);
}

/** A schedule id as a title. Shared with the Schedule panel and job files so a
 * job reads the same everywhere. */
export function jobTitle(id: string): string {
  return Object.prototype.hasOwnProperty.call(JOB_TITLES, id) ? JOB_TITLES[id] : humanise(id);
}

/** "physio-nudge" -> "Physio nudge": separators to spaces, first letter up. */
export function humanise(id: string): string {
  const words = id.replaceAll("-", " ").replaceAll("_", " ");
  const [first, ...rest] = Array.from(words);
  return first === undefined ? "" : first.toUpperCase() + rest.join("");
}

// MARK: Services

/** A connector's service name, from its tool name. Substring matches because the
 * prefix is the connector's and the service word is what stays stable. */
export function service(toolName: string): string | null {
  const n = toolName.toLowerCase();
  if (!n.startsWith("mcp__")) return null;
  if (n.includes("gmail")) return "Gmail";
  if (n.includes("calendar")) return "Calendar";
  if (n.includes("trello")) return "Trello";
  if (n.includes("drive")) return "Drive";
  return null;
}

const READ_TOOLS = new Set(["Read"]);
const WRITE_TOOLS = new Set(["Edit", "Write", "NotebookEdit"]);
const FILE_SEARCH_TOOLS = new Set(["Grep", "Glob"]);

// MARK: Summary

/** "Checked Gmail, Calendar · read travel.md · changed travel.md". Empty for a
 * turn that used no tools and changed nothing. */
export function summary(entry: ActivityEntry): string {
  const services: string[] = [];
  const reads: (string | null)[] = [];
  const searches: (string | null)[] = [];
  const fetches: string[] = [];
  const changed: string[] = [];
  let fileSearches = 0;
  let commands = 0;
  let other = entry.toolsDropped;

  for (const call of entry.tools) {
    const svc = service(call.name);
    if (svc) {
      appendUnique(svc, services);
    } else if (READ_TOOLS.has(call.name)) {
      reads.push(call.target === null ? null : fileName(call.target));
    } else if (WRITE_TOOLS.has(call.name)) {
      if (call.target !== null) appendUnique(fileName(call.target), changed);
    } else if (call.name === "WebSearch") {
      searches.push(call.target);
    } else if (call.name === "WebFetch") {
      if (call.target !== null) appendUnique(call.target, fetches);
      else other += 1;
    } else if (FILE_SEARCH_TOOLS.has(call.name)) {
      fileSearches += 1;
    } else if (call.name === "Bash") {
      commands += 1;
    } else {
      other += 1;
    }
  }
  // The commit is the authority on what changed in the identity dir; Edit
  // targets add files outside it, which never commit.
  for (const file of entry.files) appendUnique(fileName(file), changed);

  const phrases: string[] = [];
  if (services.length > 0) phrases.push("checked " + services.join(", "));
  if (reads.length > 0) phrases.push(readPhrase(reads));
  if (searches.length > 0) phrases.push(searchPhrase(searches));
  if (fetches.length > 0) phrases.push("opened " + listText(fetches));
  if (fileSearches > 0) phrases.push("searched files");
  if (commands > 0) phrases.push(commands === 1 ? "ran a command" : `ran ${commands} commands`);
  if (changed.length > 0) phrases.push("changed " + listText(changed));
  if (other > 0) phrases.push(other === 1 ? "used another tool" : `used ${other} other tools`);

  if (phrases.length === 0) return "";
  phrases[0] = capitalise(phrases[0]);
  return phrases.join(" · ");
}

/** The row's second line. `summary`, or for a turn that used no tools, "Replied",
 * unless it failed, when it did not reply at all and the flags line's "Didn't
 * finish" says so alone (null: no line). Showing "Replied" over a turn the API
 * killed was the first device pass's finding, 2026-09-28. */
export function summaryLine(entry: ActivityEntry): string | null {
  const s = summary(entry);
  if (s !== "") return s;
  return entry.status === "error" ? null : "Replied";
}

/** One part of the flags line. `isWarning` parts render orange; Mike's own
 * approvals are a record, not a problem, and render grey: the whole line going
 * orange made "you approved 4" read as a warning (device pass, 2026-09-28). */
export interface Flag {
  text: string;
  isWarning: boolean;
}

/** What went wrong or needed Mike, in one line; empty when nothing did. */
export function flags(entry: ActivityEntry): Flag[] {
  const parts: Flag[] = [];
  if (entry.status === "error") parts.push({ text: "Didn't finish", isWarning: true });
  const approved = entry.approvals.filter((a) => a.decision === "allow").length;
  const declined = entry.approvals.filter((a) => a.decision === "deny").length;
  const missed = entry.approvals.filter((a) => a.decision === "timeout").length;
  if (approved > 0) parts.push({ text: `you approved ${approved}`, isWarning: false });
  if (declined > 0) parts.push({ text: `you declined ${declined}`, isWarning: true });
  if (missed > 0) {
    parts.push({ text: missed === 1 ? "1 request missed" : `${missed} requests missed`, isWarning: true });
  }
  // Denials include the ones Mike declined above; count the rest.
  const blocked = Math.max(0, entry.denials.length - declined - missed);
  if (blocked > 0) parts.push({ text: `${blocked} blocked`, isWarning: true });
  if (parts.length > 0) parts[0] = { text: capitalise(parts[0].text), isWarning: parts[0].isWarning };
  return parts;
}

/** Whether a record earns a row. A chat turn that used no tools is just the
 * conversation, already on screen in the transcript; every scheduled turn shows,
 * even an idle one, because "did the morning job run" is half of what this log
 * is for. */
export function isNoteworthy(entry: ActivityEntry): boolean {
  return (
    !isChatSource(entry.source) ||
    entry.tools.length > 0 ||
    entry.toolsDropped > 0 ||
    entry.files.length > 0 ||
    entry.denials.length > 0 ||
    entry.approvals.length > 0 ||
    entry.status === "error"
  );
}

// MARK: Background jobs

/** The harness's own upkeep (docs/tasks/default-infrastructure.md, tier 1):
 * the nightly tidy and overnight ideas. Shown unless the user turns off "Show
 * background jobs". */
export function isBackground(entry: ActivityEntry): boolean {
  return entry.tier === "plumbing";
}

/** Whether a record gets a row, given this device's background toggle. A
 * background job that didn't finish shows regardless: a failed tidy means
 * yesterday's conversation carried into today, and the toggle must never hide
 * that. */
export function isVisible(entry: ActivityEntry, showBackground: boolean): boolean {
  if (!isNoteworthy(entry)) return false;
  return showBackground || !isBackground(entry) || entry.status !== "done";
}

/** What a job said, for the expanded row. A job with nothing to say replies
 * with the server's silent token, which is a mechanism, not something to show. */
export function outputText(text: string): string {
  const bare = text.trim().replace(/^`+|`+$/g, "").trim().toLowerCase();
  return bare === "" || bare === "[silent]" ? "Nothing to report." : text.trim();
}

// MARK: Detail

export interface DetailLine {
  label: string;
  target: string | null;
  /** How many calls this line stands for; 1 for most. */
  count: number;
}

/** One line per distinct tool call for the expanded row: a plain label, plus
 * the target when there is one. Repeats of the same label and target merge
 * into one line with a count, in the order they first appeared, so reading
 * travel.md three times is "Read travel.md ×3", not three identical lines
 * (Mike, 2026-10-21). Merged here, not on the server: the record is a true log
 * of every call, and a year of stored records already has the repeats. */
export function detailLines(entry: ActivityEntry): DetailLine[] {
  const lines: DetailLine[] = [];
  const index = new Map<string, DetailLine>();
  for (const call of entry.tools) {
    const label = toolLabel(call.name);
    const key = JSON.stringify([label, call.target]);
    const seen = index.get(key);
    if (seen) {
      seen.count += 1;
      continue;
    }
    const line = { label, target: call.target, count: 1 };
    index.set(key, line);
    lines.push(line);
  }
  if (entry.toolsDropped > 0) {
    lines.push({ label: `${entry.toolsDropped} more not recorded`, target: null, count: 1 });
  }
  return lines;
}

/** "×3" after a merged line; null for a single call. */
export function countText(line: DetailLine): string | null {
  return line.count > 1 ? `×${line.count}` : null;
}

export function toolLabel(name: string): string {
  const svc = service(name);
  if (svc) return svc;
  switch (name) {
    case "Read":
      return "Read";
    case "Edit":
    case "Write":
    case "NotebookEdit":
      return "Changed";
    case "WebSearch":
      return "Web search";
    case "WebFetch":
      return "Opened";
    case "Grep":
    case "Glob":
      return "Searched files";
    case "Bash":
      return "Command";
    default:
      return "Other tool";
  }
}

/** "14s", "2m 5s"; null without a start time. */
export function duration(entry: ActivityEntry): string | null {
  if (entry.startedAt === null) return null;
  const seconds = Math.max(0, Math.round(entry.finishedAt - entry.startedAt));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return rest === 0 ? `${minutes}m` : `${minutes}m ${rest}s`;
}

const EFFORT_WORDS: Record<string, string> = { low: "Low", medium: "Medium", high: "High" };

/** The expanded row's last line: "Took 14s · Sonnet 5.5 · High effort".
 * Whatever is known, in that order; null when nothing is (an old record with no
 * start time). */
export function runLine(entry: ActivityEntry): string | null {
  const parts: string[] = [];
  const took = duration(entry);
  if (took !== null) parts.push(`Took ${took}`);
  if (entry.model) parts.push(modelName(entry.model));
  if (entry.effort) {
    const word = Object.prototype.hasOwnProperty.call(EFFORT_WORDS, entry.effort)
      ? EFFORT_WORDS[entry.effort]
      : entry.effort;
    parts.push(`${word} effort`);
  }
  return parts.length === 0 ? null : parts.join(" · ");
}

/** "claude-sonnet-5-5" -> "Sonnet 5.5", "claude-opus-5" -> "Opus 5". The
 * version is the point here, unlike Settings' tier-only name: this says what
 * actually ran, and an alias quietly moving to a new model is one of the things
 * it should show. Version parts are short numbers, so a date suffix stops the
 * version rather than joining it. Anything else falls back to
 * `capitalizedModelName`, which leaves an unfamiliar id as is. */
export function modelName(id: string): string {
  const parts = id.split("-").filter((p) => p !== "");
  if (parts.length < 2 || parts[0].toLowerCase() !== "claude") return capitalizedModelName(id);
  const version: string[] = [];
  for (const p of parts.slice(2)) {
    if (!/^[0-9]{1,2}$/.test(p)) break;
    version.push(p);
  }
  const tier = capitalizedModelName(id);
  return version.length === 0 ? tier : `${tier} ${version.join(".")}`;
}

// MARK: Helpers

function readPhrase(reads: (string | null)[]): string {
  const named: string[] = [];
  for (const r of reads) if (r !== null) appendUnique(r, named);
  if (named.length === 0) return reads.length === 1 ? "read a file" : `read ${reads.length} files`;
  return "read " + listText(named);
}

function searchPhrase(searches: (string | null)[]): string {
  const first = searches.find((s): s is string => s !== null);
  if (first === undefined) return "searched the web";
  const more = searches.length - 1;
  return `searched “${first}”` + (more > 0 ? ` +${more} more` : "");
}

/** Two names, then a count: a row is one glance, not an inventory. */
function listText(items: string[]): string {
  if (items.length <= 2) return items.join(", ");
  return items.slice(0, 2).join(", ") + ` +${items.length - 2} more`;
}

/** Last path component, as NSString.lastPathComponent: trailing slashes ignored. */
export function lastPathComponent(path: string): string {
  const parts = path.split("/").filter((p) => p !== "");
  return parts.length === 0 ? (path.startsWith("/") ? "/" : "") : parts[parts.length - 1];
}

function fileName(path: string): string {
  return lastPathComponent(path);
}

function appendUnique(item: string, items: string[]): void {
  if (!items.includes(item)) items.push(item);
}

function capitalise(s: string): string {
  const [first, ...rest] = Array.from(s);
  return first === undefined ? "" : first.toUpperCase() + rest.join("");
}
