// Every word, threshold and grouping behind the Usage panel and the >= 90%
// status row (docs/tasks/usage-visibility.md). Port of ios UsageFormat.swift.
// The server sends fractions, epochs and counts; this is where they become
// sentences, so a wording tweak never needs a box deploy.
//
// Two things are deliberately absent. There is no percentage of the PLAN for
// Marlow's own part: the plan's accounting is not public, so the server cannot
// measure it and the client does not pretend (design doc, section 2). And
// nothing here is phrased as a warning except the one row Mike asked for: he
// wants fewer excuses not to use Marlow, so the rest stays quiet.
//
// Time zone and clock are injected (`timeZone` is an IANA name, `now` is epoch
// ms), like scheduleFormat.ts, so tests pin them.

import type { UsageBuckets, UsageConversation, UsagePlan, UsageReport } from "./apiModels";

export interface UsageClock {
  timeZone: string;
  /** Epoch ms. */
  now: number;
}

/** At or above this utilization a window earns the yellow status row. Lives
 * here, not on the server: the 90 is a display decision. */
export const HIGH_USAGE_THRESHOLD = 0.9;

/** What the plan is called when the server doesn't say, or says something this
 * build has no name for. */
export const FALLBACK_PLAN_NAME = "Your Claude plan";

export const NO_TURNS = "No turns in this window.";
export const NO_READING = "No reading yet — appears after Marlow's next turn.";

// MARK: Windows

/** The windows the panel has wording for, in display order. A window the
 * server passes through under any other name is ignored: its length is
 * unknown, so it can't be titled honestly. */
export const WINDOW_NAMES = ["five_hour", "seven_day"] as const;

export function windowTitle(name: string): string | null {
  switch (name) {
    case "five_hour":
      return "This 5 hours";
    case "seven_day":
      return "This week";
    default:
      return null;
  }
}

/** 14 for 0.14. Whole percent, floored at 0. */
export function percent(utilization: number): number {
  return Math.round(Math.max(0, utilization) * 100);
}

// MARK: Plan

/** "Claude Pro" for "pro", "Claude Max" for "max" (any case, trimmed); anything
 * else, including null, is the fallback. */
export function planName(subscriptionType: string | null | undefined): string {
  switch (subscriptionType?.trim().toLowerCase()) {
    case "pro":
      return "Claude Pro";
    case "max":
      return "Claude Max";
    default:
      return FALLBACK_PLAN_NAME;
  }
}

/** The caveat under the bars. Plan usage is shared with everything else the
 * user does on Claude, which is why the bars can move without Marlow. */
export function planFootnote(name: string): string {
  if (name === FALLBACK_PLAN_NAME) {
    return "Marlow shares your Claude plan's limits with everything else you use Claude for.";
  }
  return `Marlow shares your ${name} limits with everything else you use Claude for.`;
}

export interface PlanRow {
  id: string;
  title: string;
  /** 0...1, for the bar. */
  fraction: number;
  /** "14% used · resets 8:59 PM" */
  detail: string;
  /** Tints the bar yellow: at the threshold and not yet reset. */
  isHigh: boolean;
}

export function planRows(plan: UsagePlan, clock: UsageClock): PlanRow[] {
  const rows: PlanRow[] = [];
  for (const name of WINDOW_NAMES) {
    const window = plan.windows[name];
    const title = windowTitle(name);
    if (!window || !title) continue;
    const percentText = `${percent(window.utilization)}%`;
    const resetsMs = window.resetsAt === null ? null : window.resetsAt * 1000;
    const live = resetsMs === null || resetsMs > clock.now;
    let detail: string;
    if (resetsMs !== null && live) {
      detail = `${percentText} used · resets ${dayTime(resetsMs, clock)}`;
    } else if (resetsMs !== null) {
      // The reading is of a window that has since ended; the live one is
      // unknown until Marlow next runs.
      detail = `${percentText} at last reading · has since reset`;
    } else {
      detail = `${percentText} used`;
    }
    rows.push({
      id: name,
      title,
      fraction: Math.min(1, Math.max(0, window.utilization)),
      detail,
      isHigh: live && window.utilization >= HIGH_USAGE_THRESHOLD,
    });
  }
  return rows;
}

/** "7:44 PM", with the weekday when it was not today: when the reading was
 * taken, for the right of the section header. Null without a timestamp. No
 * staleness styling: the footnote carries the caveat. */
export function asOfTime(plan: UsagePlan, clock: UsageClock): string | null {
  return plan.observedAt === null ? null : dayTime(plan.observedAt * 1000, clock);
}

// MARK: Conversation

export interface ConversationSummary {
  /** "37K tokens"; null when unmeasured or retired. */
  size: string | null;
  /** Size over the threshold, 0...1, for the bar; null without both. */
  fraction: number | null;
  /** "Marlow tidies up around 110K" */
  thresholdText: string | null;
  /** The sentence that stands in for the size, when there is none. */
  notice: string | null;
  /** "Started 6:00 AM" / "Started yesterday 9:12 PM" */
  started: string | null;
  /** "Tidied up once" / "Tidied up 3 times" */
  tidiedUp: string | null;
}

/** Start and tidy-ups on one line: "Started 6:00 AM · tidied up once". Two
 * short facts were two rows (Mike, 2026-10-05: wasted space). */
export function history(s: ConversationSummary): string | null {
  if (s.started !== null && s.tidiedUp !== null) return `${s.started} · ${s.tidiedUp.toLowerCase()}`;
  return s.started ?? s.tidiedUp;
}

export function conversation(c: UsageConversation, clock: UsageClock): ConversationSummary {
  // A retired session's numbers describe yesterday's conversation: say only
  // that a fresh one is coming, and show nothing else of it.
  if (c.retired) {
    return {
      size: null,
      fraction: null,
      thresholdText: null,
      notice: "A fresh conversation starts with your next message.",
      started: null,
      tidiedUp: null,
    };
  }
  const threshold = c.compactionThresholdTokens !== null && c.compactionThresholdTokens > 0 ? c.compactionThresholdTokens : null;
  let size: string | null = null;
  let fraction: number | null = null;
  let notice: string | null = null;
  if (c.contextTokens !== null) {
    size = `${tokenCount(c.contextTokens)} tokens`;
    fraction = threshold === null ? null : Math.min(1, Math.max(0, c.contextTokens / threshold));
  } else {
    // After a tidy-up the size is unknown until the next turn measures the
    // new, smaller context; say so, or it reads as a broken reading.
    notice = c.compactions > 0 ? "Tidied up · new size after your next message" : "Size shows after your next message";
  }
  return {
    size,
    fraction,
    thresholdText: threshold === null ? null : `Marlow tidies up around ${tokenCount(threshold)}`,
    notice,
    started: c.startedAt === null ? null : startedText(c.startedAt * 1000, clock),
    // The notice already says it was tidied up; the count line would repeat
    // it right underneath.
    tidiedUp: c.contextTokens === null ? null : tidiedUpText(c.compactions),
  };
}

/** 36,938 -> "37K"; under 1,000 the number itself. */
export function tokenCount(tokens: number): string {
  return tokens < 1000 ? String(tokens) : `${Math.floor((tokens + 500) / 1000)}K`;
}

function tidiedUpText(count: number): string | null {
  if (count < 1) return null;
  return count === 1 ? "Tidied up once" : `Tidied up ${count} times`;
}

function startedText(ms: number, clock: UsageClock): string {
  if (dayKey(ms, clock.timeZone) === dayKey(clock.now - 86_400_000, clock.timeZone)) {
    return `Started yesterday ${clockText(ms, clock.timeZone)}`;
  }
  return `Started ${dayTime(ms, clock)}`;
}

// MARK: Marlow's turns

export interface MarlowPart {
  id: string;
  title: string;
  /** "12 chat · 6 scheduled"; null when there were no turns. */
  counts: string | null;
}

export function marlowParts(marlow: Record<string, UsageBuckets>): MarlowPart[] {
  const parts: MarlowPart[] = [];
  for (const name of WINDOW_NAMES) {
    const buckets = marlow[name];
    const title = windowTitle(name);
    if (buckets && title) parts.push({ id: name, title, counts: counts(buckets) });
  }
  return parts;
}

export function counts(b: UsageBuckets): string | null {
  const parts: string[] = [];
  if (b.chat.turns > 0) parts.push(`${b.chat.turns} chat`);
  if (b.scheduled.turns > 0) parts.push(`${b.scheduled.turns} scheduled`);
  if (b.compaction.turns > 0) parts.push(b.compaction.turns === 1 ? "1 tidy-up" : `${b.compaction.turns} tidy-ups`);
  return parts.length === 0 ? null : parts.join(" · ");
}

// MARK: High usage

export interface HighUsage {
  windowTitle: string;
  percent: number;
  /** "8:59 PM" or "Fri 4:59 AM" — without the word "resets". */
  resetText: string;
}

/** The most-used window at or above the threshold whose reset is still ahead,
 * else null. A window already past its reset is a stale reading of a window
 * that no longer exists: showing it would keep the row up after the problem
 * has gone. */
export function highUsage(report: UsageReport | null, clock: UsageClock): HighUsage | null {
  const plan = report?.plan;
  if (!plan) return null;
  let best: { name: string; utilization: number; resetsAt: number } | null = null;
  for (const name of WINDOW_NAMES) {
    const w = plan.windows[name];
    if (!w || w.utilization < HIGH_USAGE_THRESHOLD || w.resetsAt === null || w.resetsAt * 1000 <= clock.now) continue;
    if (best === null || w.utilization > best.utilization) best = { name, utilization: w.utilization, resetsAt: w.resetsAt };
  }
  const title = best && windowTitle(best.name);
  if (!best || !title) return null;
  return { windowTitle: title, percent: percent(best.utilization), resetText: dayTime(best.resetsAt * 1000, clock) };
}

/** "93% of this 5 hours used — resets 8:59 PM." */
export function highUsageText(high: HighUsage): string {
  return `${high.percent}% of ${high.windowTitle.toLowerCase()} used — resets ${high.resetText}.`;
}

// MARK: Dates

/** "8:59 PM" today, "Fri 4:59 AM" on any other day. */
export function dayTime(ms: number, clock: UsageClock): string {
  if (dayKey(ms, clock.timeZone) === dayKey(clock.now, clock.timeZone)) return clockText(ms, clock.timeZone);
  const weekday = new Intl.DateTimeFormat("en-US", { timeZone: clock.timeZone, weekday: "short" }).format(new Date(ms));
  return `${weekday} ${clockText(ms, clock.timeZone)}`;
}

function clockText(ms: number, timeZone: string): string {
  return (
    new Intl.DateTimeFormat("en-US", { timeZone, hour: "numeric", minute: "2-digit" })
      .format(new Date(ms))
      // Recent ICU puts a narrow no-break space before "PM"; a plain space
      // reads the same and keeps the strings stable across engines.
      .replace(/ /g, " ")
  );
}

function dayKey(ms: number, timeZone: string): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }).format(
    new Date(ms),
  );
}
