// Wire types for the activity log and the Marlow sheet (identity files and
// schedules). Port of the relevant parts of ios APIModels.swift. The server
// shapes are in server/activity_log.py, server/transparency.py, server/app.py.
//
// Every decoder is lenient: the activity record is a file on the box that
// outlives client versions, and an entry the client half understands is worth
// more than a page that fails to decode. An unknown key or value never throws;
// a malformed entry is skipped and a malformed optional field falls back.
//
// Models are camelCase (as in Swift); the decoders map from the snake_case wire.

import type { PermissionPreviewLine, PermissionSummary } from "./types";

type Obj = Record<string, unknown>;

function obj(v: unknown): Obj | null {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Obj) : null;
}
function optStr(v: unknown): string | null {
  return typeof v === "string" ? v : null;
}
function optNum(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}
function optInt(v: unknown): number | null {
  return typeof v === "number" && Number.isInteger(v) ? v : null;
}
function strList(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
}
function intList(v: unknown): number[] {
  return Array.isArray(v) ? v.filter((x): x is number => typeof x === "number" && Number.isInteger(x)) : [];
}
function list(v: unknown): unknown[] {
  return Array.isArray(v) ? v : [];
}

// MARK: Activity log

export interface ActivityToolCall {
  name: string;
  target: string | null;
  at: number | null;
}

export interface ActivityApproval {
  tool: string;
  /** "allow" | "deny" | "timeout" */
  decision: string;
}

/** One turn's record, as the server wrote it at turn end. Every field but the
 * id and time is optional, and decoding never depends on `v`. */
export interface ActivityEntry {
  turnId: string;
  source: string | null;
  label: string | null;
  createdAt: number | null;
  startedAt: number | null;
  /** Server epoch seconds. */
  finishedAt: number;
  status: string | null;
  tools: ActivityToolCall[];
  toolsDropped: number;
  approvals: ActivityApproval[];
  denials: string[];
  files: string[];
  /** The model id the API answered with ("claude-sonnet-5-5") and the effort
   * the server passed. Raw facts; `runLine` words them. null on records
   * written before 2026-10-18. */
  model: string | null;
  effort: string | null;
  /** "plumbing" for the harness's own upkeep (nightly tidy, overnight ideas),
   * which the panel files under "background jobs"; null for everything else
   * and for every record written before 2026-10-21. */
  tier: string | null;
  /** The server kept what this turn said because it never reached the chat;
   * fetch it with `Api.activityOutput` when the row is expanded. */
  hasOutput: boolean;
}

/** Defaults as the Swift memberwise init: status "done", nothing else. */
export function makeActivityEntry(
  p: Pick<ActivityEntry, "turnId" | "finishedAt"> & Partial<ActivityEntry>,
): ActivityEntry {
  return {
    source: null,
    label: null,
    createdAt: null,
    startedAt: null,
    status: "done",
    tools: [],
    toolsDropped: 0,
    approvals: [],
    denials: [],
    files: [],
    model: null,
    effort: null,
    tier: null,
    hasOutput: false,
    ...p,
  };
}

export interface ActivityPage {
  entries: ActivityEntry[];
  /** null on the last page: the server only sets it when the page came back full. */
  nextBefore: number | null;
}

function decodeToolCall(raw: unknown): ActivityToolCall | null {
  const o = obj(raw);
  if (!o || typeof o.name !== "string") return null;
  return { name: o.name, target: optStr(o.target), at: optNum(o.at) };
}

function decodeApproval(raw: unknown): ActivityApproval | null {
  const o = obj(raw);
  if (!o || typeof o.tool !== "string" || typeof o.decision !== "string") return null;
  return { tool: o.tool, decision: o.decision };
}

/** null only when the entry has no usable `turn_id` or `finished_at`. */
export function decodeActivityEntry(raw: unknown): ActivityEntry | null {
  const o = obj(raw);
  if (!o || typeof o.turn_id !== "string") return null;
  const finishedAt = optNum(o.finished_at);
  if (finishedAt === null) return null;
  return {
    turnId: o.turn_id,
    source: optStr(o.source),
    label: optStr(o.label),
    createdAt: optNum(o.created_at),
    startedAt: optNum(o.started_at),
    finishedAt,
    status: optStr(o.status),
    tools: list(o.tools).map(decodeToolCall).filter((x): x is ActivityToolCall => x !== null),
    toolsDropped: optInt(o.tools_dropped) ?? 0,
    approvals: list(o.approvals).map(decodeApproval).filter((x): x is ActivityApproval => x !== null),
    denials: strList(o.denials),
    files: strList(o.files),
    model: optStr(o.model),
    effort: optStr(o.effort),
    tier: optStr(o.tier),
    hasOutput: o.has_output === true,
  };
}

/** The text of `GET /v1/activity/{turn_id}/output`, or null if the body has none. */
export function decodeActivityOutput(raw: unknown): string | null {
  const o = obj(raw);
  return o && typeof o.text === "string" ? o.text : null;
}

export function decodeActivityPage(raw: unknown): ActivityPage {
  const o = obj(raw) ?? {};
  return {
    entries: list(o.entries).map(decodeActivityEntry).filter((x): x is ActivityEntry => x !== null),
    nextBefore: optNum(o.next_before),
  };
}

// MARK: Identity files

/** What a file is for; picks the "About this file" sentence. An unknown value
 * decodes to "unknown" so a newer server can add kinds without breaking the sheet. */
export type IdentityKind = "soul" | "user" | "instructions" | "memory" | "job" | "unknown";

const KINDS: readonly string[] = ["soul", "user", "instructions", "memory", "job"];

export function decodeIdentityKind(v: unknown): IdentityKind {
  return typeof v === "string" && KINDS.includes(v) ? (v as IdentityKind) : "unknown";
}

/** One entry of `GET /v1/identity`'s `files`. `description` is only set for
 * memory files (the index line Marlow's prompt carries). */
export interface IdentityFileInfo {
  id: string;
  kind: IdentityKind;
  modifiedAt: number;
  size: number;
  description: string | null;
}

export function decodeIdentityFileInfo(raw: unknown): IdentityFileInfo | null {
  const o = obj(raw);
  if (!o || typeof o.id !== "string") return null;
  return {
    id: o.id,
    kind: decodeIdentityKind(o.kind),
    modifiedAt: optNum(o.modified_at) ?? 0,
    size: optInt(o.size) ?? 0,
    description: optStr(o.description),
  };
}

/** `GET /v1/identity/file`. `truncated` means the server cut the text at its cap. */
export interface IdentityFile {
  id: string;
  kind: IdentityKind;
  modifiedAt: number;
  text: string;
  truncated: boolean;
}

export function decodeIdentityFile(raw: unknown): IdentityFile | null {
  const o = obj(raw);
  if (!o || typeof o.id !== "string") return null;
  return {
    id: o.id,
    kind: decodeIdentityKind(o.kind),
    modifiedAt: optNum(o.modified_at) ?? 0,
    text: typeof o.text === "string" ? o.text : "",
    truncated: o.truncated === true,
  };
}

// MARK: Schedules

/** Structure only; the server sends no wording. Times are "HH:MM" store
 * wall-clock, weekdays 0-6 with Sunday 0. A type this build doesn't know decodes
 * as "other". */
export type Recurrence =
  | { type: "daily"; times: string[] }
  | { type: "weekly"; weekdays: number[]; times: string[] }
  | { type: "monthly"; days: number[]; times: string[] }
  | { type: "once" }
  | { type: "other" };

export function decodeRecurrence(raw: unknown): Recurrence {
  const o = obj(raw);
  if (!o) return { type: "other" };
  const times = strList(o.times);
  switch (o.type) {
    case "daily":
      return { type: "daily", times };
    case "weekly":
      return { type: "weekly", weekdays: intList(o.weekdays), times };
    case "monthly":
      return { type: "monthly", days: intList(o.days), times };
    case "once":
      return { type: "once" };
    default:
      return { type: "other" };
  }
}

export interface ScheduleEntry {
  id: string;
  cron: string | null;
  at: string | null;
  recurrence: Recurrence;
  /** Server epoch seconds. */
  nextFireAt: number | null;
  notify: string | null;
  prompt: string | null;
  job: string | null;
  jobFile: string | null;
}

/** Defaults as the Swift memberwise init would need (everything optional null). */
export function makeScheduleEntry(
  p: Pick<ScheduleEntry, "id"> & Partial<ScheduleEntry>,
): ScheduleEntry {
  return {
    cron: null,
    at: null,
    recurrence: { type: "other" },
    nextFireAt: null,
    notify: null,
    prompt: null,
    job: null,
    jobFile: null,
    ...p,
  };
}

export function decodeScheduleEntry(raw: unknown): ScheduleEntry | null {
  const o = obj(raw);
  if (!o || typeof o.id !== "string") return null;
  return {
    id: o.id,
    cron: optStr(o.cron),
    at: optStr(o.at),
    // A missing `recurrence` reads as "other" (the raw cron is shown).
    recurrence: decodeRecurrence(o.recurrence),
    nextFireAt: optNum(o.next_fire_at),
    notify: optStr(o.notify),
    prompt: optStr(o.prompt),
    job: optStr(o.job),
    jobFile: optStr(o.job_file),
  };
}

export interface ScheduleIndex {
  timezone: string | null;
  /** Why the schedule file was refused; the last good version keeps running. */
  rejected: string | null;
  entries: ScheduleEntry[];
}

export const EMPTY_SCHEDULE_INDEX: ScheduleIndex = { timezone: null, rejected: null, entries: [] };

export function decodeScheduleIndex(raw: unknown): ScheduleIndex {
  const o = obj(raw);
  if (!o) return { ...EMPTY_SCHEDULE_INDEX };
  return {
    timezone: optStr(o.timezone),
    rejected: optStr(o.rejected),
    entries: list(o.entries).map(decodeScheduleEntry).filter((x): x is ScheduleEntry => x !== null),
  };
}

/** `GET /v1/identity`. Tolerant of a missing `schedules`: the files are still
 * worth showing. */
export interface IdentityIndex {
  files: IdentityFileInfo[];
  schedules: ScheduleIndex;
}

export function decodeIdentityIndex(raw: unknown): IdentityIndex {
  const o = obj(raw) ?? {};
  return {
    files: list(o.files).map(decodeIdentityFileInfo).filter((x): x is IdentityFileInfo => x !== null),
    schedules: decodeScheduleIndex(o.schedules),
  };
}

// MARK: Approval summary (server/approval_summary.py)

export function decodePermissionSummary(raw: unknown): PermissionSummary | null {
  const o = obj(raw);
  const title = o ? optStr(o.title) : null;
  if (!o || title === null) return null;
  const preview = list(o.preview).flatMap((l): PermissionPreviewLine[] => {
    const line = obj(l);
    const text = line ? optStr(line.text) : null;
    if (!line || text === null) return [];
    // An op the server grows later renders as plain text.
    return [{ op: line.op === "remove" || line.op === "add" ? line.op : "text", text }];
  });
  const similarLabel = optStr(obj(o.similar)?.label);
  const says = optStr(o.marlow_says);
  return {
    title,
    preview,
    marlowSays: says === "" ? null : says,
    similar: similarLabel === null ? null : { label: similarLabel },
  };
}

// MARK: Usage (docs/tasks/usage-visibility.md)

/** One rate-limit window: `utilization` is a fraction, 0...1. */
export interface UsageWindow {
  utilization: number;
  resetsAt: number | null;
}

/** The subscription's windows, as of the last time Marlow ran. Every window the
 * server passed through; the panel only shows the names it has wording for. */
export interface UsagePlan {
  observedAt: number | null;
  status: string | null;
  windows: Record<string, UsageWindow>;
}

/** The attended conversation's size and history. */
export interface UsageConversation {
  contextTokens: number | null;
  observedAt: number | null;
  compactionThresholdTokens: number | null;
  startedAt: number | null;
  compactions: number;
  /** Absent on a server that predates the field, which reads as not retired. */
  retired: boolean;
}

export interface UsageBucket {
  turns: number;
  /** A relative proxy for work done, not a cost and not a share of the plan. */
  weightedTokens: number;
}

/** Marlow's turns in one window, by what started them. */
export interface UsageBuckets {
  chat: UsageBucket;
  scheduled: UsageBucket;
  compaction: UsageBucket;
}

/** `GET /v1/usage`: facts only, no wording. Every block can be null (nothing
 * observed yet), and a block that fails to decode reads as null rather than
 * failing the whole response: a half-empty panel beats an error for one odd
 * field, and the server's shape is allowed to grow. */
export interface UsageReport {
  plan: UsagePlan | null;
  conversation: UsageConversation | null;
  /** Window name ("five_hour", "seven_day") -> Marlow's own activity in it. */
  marlow: Record<string, UsageBuckets> | null;
  /** The raw plan tier ("pro", "max"); the panel words it (`planName`). */
  subscriptionType: string | null;
}

export const EMPTY_USAGE_REPORT: UsageReport = { plan: null, conversation: null, marlow: null, subscriptionType: null };

/** A whole number the server may have written as `61000` or `61000.0`. */
function lenientInt(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? Math.trunc(v) : null;
}

function decodeUsageWindow(raw: unknown): UsageWindow | null {
  const o = obj(raw);
  const utilization = o ? optNum(o.utilization) : null;
  if (!o || utilization === null) return null;
  return { utilization, resetsAt: optNum(o.resets_at) };
}

function decodeUsagePlan(raw: unknown): UsagePlan | null {
  const o = obj(raw);
  if (!o) return null;
  const windows: Record<string, UsageWindow> = {};
  for (const [name, w] of Object.entries(obj(o.windows) ?? {})) {
    const decoded = decodeUsageWindow(w);
    if (decoded) windows[name] = decoded;
  }
  return { observedAt: optNum(o.observed_at), status: optStr(o.status), windows };
}

function decodeUsageConversation(raw: unknown): UsageConversation | null {
  const o = obj(raw);
  if (!o) return null;
  return {
    contextTokens: lenientInt(o.context_tokens),
    observedAt: optNum(o.observed_at),
    compactionThresholdTokens: lenientInt(o.compaction_threshold_tokens),
    startedAt: optNum(o.started_at),
    compactions: lenientInt(o.compactions) ?? 0,
    retired: o.retired === true,
  };
}

function decodeUsageBucket(raw: unknown): UsageBucket {
  const o = obj(raw);
  return { turns: lenientInt(o?.turns) ?? 0, weightedTokens: lenientInt(o?.weighted_tokens) ?? 0 };
}

export function makeUsageBuckets(b: Partial<UsageBuckets> = {}): UsageBuckets {
  const zero = { turns: 0, weightedTokens: 0 };
  return { chat: b.chat ?? zero, scheduled: b.scheduled ?? zero, compaction: b.compaction ?? zero };
}

export function decodeUsageReport(raw: unknown): UsageReport {
  const o = obj(raw);
  if (!o) return { ...EMPTY_USAGE_REPORT };
  let marlow: Record<string, UsageBuckets> | null = null;
  const m = obj(o.marlow);
  if (m) {
    marlow = {};
    for (const [name, b] of Object.entries(m)) {
      const bo = obj(b);
      // One bad entry drops alone.
      if (bo) {
        marlow[name] = {
          chat: decodeUsageBucket(bo.chat),
          scheduled: decodeUsageBucket(bo.scheduled),
          compaction: decodeUsageBucket(bo.compaction),
        };
      }
    }
  }
  return {
    plan: decodeUsagePlan(o.plan),
    conversation: decodeUsageConversation(o.conversation),
    marlow,
    subscriptionType: optStr(o.subscription_type),
  };
}

// MARK: Ideas

export type IdeaSection = "now" | "standing";

/** One open idea from `GET /v1/ideas` (docs/tasks/ideas-panel.md). Dates are
 * the store's `YYYY-MM-DD` strings, kept as strings: they are calendar days,
 * not instants, and comparing them as text is exact. */
export interface Idea {
  id: string;
  section: IdeaSection;
  emoji: string;
  title: string;
  plan: string;
  category: string;
  source: string | null;
  until: string | null;
  added: string;
  spotlight: boolean;
}

const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;

function optDay(v: unknown): string | null {
  return typeof v === "string" && ISO_DAY.test(v) ? v : null;
}

/** Lenient like the rest: an idea this build cannot show (no id or title, a
 * section it does not know) is skipped alone, and the store's order is kept.
 * A missing `added` decodes as "" so it never counts as new. */
export function decodeIdea(raw: unknown): Idea | null {
  const o = obj(raw);
  if (!o) return null;
  const id = optStr(o.id);
  const title = optStr(o.title);
  if (!id || !title) return null;
  if (o.section !== "now" && o.section !== "standing") return null;
  return {
    id,
    section: o.section,
    emoji: optStr(o.emoji) ?? "",
    title,
    plan: optStr(o.plan) ?? "",
    category: optStr(o.category) ?? "",
    source: optStr(o.source),
    until: optDay(o.until),
    added: optDay(o.added) ?? "",
    spotlight: o.spotlight === true,
  };
}

export function decodeIdeas(raw: unknown): Idea[] {
  return list(obj(raw)?.ideas)
    .map(decodeIdea)
    .filter((i): i is Idea => i !== null);
}
