// Port of ios MarlowTests/UsageTests.swift (UsageFormatTests, UsageDecodingTests).
import { describe, expect, it } from "vitest";
import {
  decodeUsageReport,
  EMPTY_USAGE_REPORT,
  makeUsageBuckets,
  type UsageConversation,
  type UsagePlan,
  type UsageReport,
  type UsageWindow,
} from "./apiModels";
import * as UsageFormat from "./usageFormat";

// Sunday 2026-10-04 in UTC, so the strings are the same on every machine.
const at = (day: number, hour: number, minute = 0) => Date.UTC(2026, 9, day, hour, minute);
const epoch = (day: number, hour: number, minute = 0) => at(day, hour, minute) / 1000;
const clock: UsageFormat.UsageClock = { timeZone: "UTC", now: at(4, 19, 50) };

function report(o: { five?: number; fiveResets?: number; seven?: number; sevenResets?: number }): UsageReport {
  const windows: Record<string, UsageWindow> = {};
  if (o.five !== undefined) windows.five_hour = { utilization: o.five, resetsAt: o.fiveResets ?? epoch(4, 20, 59) };
  if (o.seven !== undefined) windows.seven_day = { utilization: o.seven, resetsAt: o.sevenResets ?? epoch(9, 4, 59) };
  return { ...EMPTY_USAGE_REPORT, plan: { observedAt: epoch(4, 19, 44), status: "allowed", windows } };
}

function conv(c: Partial<UsageConversation>): UsageConversation {
  return {
    contextTokens: null,
    observedAt: null,
    compactionThresholdTokens: null,
    startedAt: null,
    compactions: 0,
    retired: false,
    ...c,
  };
}

const bucket = (turns: number, weightedTokens = 0) => ({ turns, weightedTokens });
const plan = (windows: Record<string, UsageWindow>, observedAt: number | null = null): UsagePlan => ({
  observedAt,
  status: "allowed",
  windows,
});

describe("usage wording", () => {
  it("titles the known windows only", () => {
    expect(UsageFormat.windowTitle("five_hour")).toBe("This 5 hours");
    expect(UsageFormat.windowTitle("seven_day")).toBe("This week");
    expect(UsageFormat.windowTitle("thirty_day")).toBeNull();
  });

  it("shows percent and the reset, today or with the weekday", () => {
    const rows = UsageFormat.planRows(report({ five: 0.14, seven: 0.08 }).plan!, clock);
    expect(rows.map((r) => r.title)).toEqual(["This 5 hours", "This week"]);
    expect(rows[0].detail).toBe("14% used · resets 8:59 PM");
    expect(rows[1].detail).toBe("8% used · resets Fri 4:59 AM");
    expect(rows[0].fraction).toBe(0.14);
    expect(rows.every((r) => !r.isHigh)).toBe(true);
  });

  it("ignores unknown windows and keeps a fixed order", () => {
    const rows = UsageFormat.planRows(
      plan({
        seven_day: { utilization: 0.5, resetsAt: null },
        thirty_day: { utilization: 0.9, resetsAt: null },
        five_hour: { utilization: 0.1, resetsAt: null },
      }),
      clock,
    );
    expect(rows.map((r) => r.id)).toEqual(["five_hour", "seven_day"]);
    expect(rows[0].detail).toBe("10% used");
  });

  it("tints a high window, but not one that has reset", () => {
    expect(UsageFormat.planRows(report({ five: 0.93 }).plan!, clock)[0].isHigh).toBe(true);
    const reset = UsageFormat.planRows(report({ five: 0.93, fiveResets: epoch(4, 19, 0) }).plan!, clock);
    expect(reset[0].isHigh).toBe(false);
    expect(reset[0].detail).toBe("93% at last reading · has since reset");
  });

  it("gives the as-of time bare today and with the weekday otherwise", () => {
    expect(UsageFormat.asOfTime(plan({}, epoch(4, 19, 44)), clock)).toBe("7:44 PM");
    expect(UsageFormat.asOfTime(plan({}, epoch(2, 9, 5)), clock)).toBe("Fri 9:05 AM");
    expect(UsageFormat.asOfTime(plan({}), clock)).toBeNull();
  });

  it("names known plans and falls back for the rest", () => {
    expect(UsageFormat.planName("pro")).toBe("Claude Pro");
    expect(UsageFormat.planName("max")).toBe("Claude Max");
    expect(UsageFormat.planName("PRO")).toBe("Claude Pro");
    expect(UsageFormat.planName("  Max\n")).toBe("Claude Max");
    expect(UsageFormat.planName(null)).toBe("Your Claude plan");
    expect(UsageFormat.planName("")).toBe("Your Claude plan");
    expect(UsageFormat.planName("team")).toBe("Your Claude plan");
  });

  it("words the footnote with the plan's name or generically", () => {
    expect(UsageFormat.planFootnote(UsageFormat.planName("pro"))).toBe(
      "Marlow shares your Claude Pro limits with everything else you use Claude for.",
    );
    expect(UsageFormat.planFootnote(UsageFormat.planName(null))).toBe(
      "Marlow shares your Claude plan's limits with everything else you use Claude for.",
    );
  });

  it("rounds token counts to K and shows small numbers as is", () => {
    expect(UsageFormat.tokenCount(36_938)).toBe("37K");
    expect(UsageFormat.tokenCount(110_000)).toBe("110K");
    expect(UsageFormat.tokenCount(999)).toBe("999");
    expect(UsageFormat.tokenCount(0)).toBe("0");
    expect(UsageFormat.tokenCount(1_000)).toBe("1K");
    expect(UsageFormat.tokenCount(1_499)).toBe("1K");
    expect(UsageFormat.tokenCount(1_500)).toBe("2K");
  });

  it("measures the conversation against the threshold", () => {
    const s = UsageFormat.conversation(
      conv({ contextTokens: 36_938, compactionThresholdTokens: 110_000, startedAt: epoch(4, 6, 0) }),
      clock,
    );
    expect(s.size).toBe("37K tokens");
    expect(s.fraction).toBeCloseTo(36_938 / 110_000, 9);
    expect(s.thresholdText).toBe("Marlow tidies up around 110K");
    expect(s.notice).toBeNull();
    expect(s.started).toBe("Started 6:00 AM");
    expect(s.tidiedUp).toBeNull();
  });

  it("fills the bar no further past the threshold", () => {
    expect(UsageFormat.conversation(conv({ contextTokens: 150_000, compactionThresholdTokens: 110_000 }), clock).fraction).toBe(1);
  });

  it("shows a small size as the number", () => {
    expect(UsageFormat.conversation(conv({ contextTokens: 640, compactionThresholdTokens: 110_000 }), clock).size).toBe(
      "640 tokens",
    );
  });

  it("says when an unmeasured size will show", () => {
    const s = UsageFormat.conversation(conv({ compactionThresholdTokens: 110_000, startedAt: epoch(4, 6, 0) }), clock);
    expect(s.size).toBeNull();
    expect(s.fraction).toBeNull();
    expect(s.notice).toBe("Size shows after your next message");
    expect(s.started).toBe("Started 6:00 AM");
  });

  it("puts start and tidy-ups on one line", () => {
    const h = (contextTokens: number | null, startedAt: number | null, compactions: number) =>
      UsageFormat.history(
        UsageFormat.conversation(conv({ contextTokens, compactionThresholdTokens: 110_000, startedAt, compactions }), clock),
      );
    expect(h(40_000, epoch(4, 6, 0), 1)).toBe("Started 6:00 AM · tidied up once");
    expect(h(40_000, epoch(4, 6, 0), 3)).toBe("Started 6:00 AM · tidied up 3 times");
    expect(h(40_000, epoch(4, 6, 0), 0)).toBe("Started 6:00 AM");
    expect(h(40_000, null, 2)).toBe("Tidied up 2 times");
    expect(h(40_000, null, 0)).toBeNull();
  });

  it("explains an unmeasured size after a tidy-up and does not repeat the count", () => {
    const s = UsageFormat.conversation(
      conv({ compactionThresholdTokens: 110_000, startedAt: epoch(4, 6, 0), compactions: 2 }),
      clock,
    );
    expect(s.size).toBeNull();
    expect(s.notice).toBe("Tidied up · new size after your next message");
    expect(s.tidiedUp).toBeNull();
    expect(s.started).toBe("Started 6:00 AM");
  });

  it("says only that a fresh conversation is coming once retired", () => {
    const s = UsageFormat.conversation(
      conv({ compactionThresholdTokens: 110_000, startedAt: epoch(3, 21, 12), compactions: 2, retired: true }),
      clock,
    );
    expect(s).toEqual({
      size: null,
      fraction: null,
      thresholdText: null,
      notice: "A fresh conversation starts with your next message.",
      started: null,
      tidiedUp: null,
    });
    const measured = UsageFormat.conversation(
      conv({ contextTokens: 50_000, compactionThresholdTokens: 110_000, retired: true }),
      clock,
    );
    expect(measured.size).toBeNull();
  });

  it("reads the start as today, yesterday or with the weekday", () => {
    const started = (ms: number) => UsageFormat.conversation(conv({ contextTokens: 1, startedAt: ms / 1000 }), clock).started;
    expect(started(at(4, 6, 0))).toBe("Started 6:00 AM");
    expect(started(at(3, 21, 12))).toBe("Started yesterday 9:12 PM");
    expect(started(at(2, 9, 5))).toBe("Started Fri 9:05 AM");
  });

  it("drops the bar, not the size, without a threshold", () => {
    const s = UsageFormat.conversation(conv({ contextTokens: 61_000 }), clock);
    expect(s.size).toBe("61K tokens");
    expect(s.fraction).toBeNull();
    expect(s.thresholdText).toBeNull();
  });

  it("omits zero buckets and names tidy-ups only when there are any", () => {
    expect(UsageFormat.counts(makeUsageBuckets({ chat: bucket(12), scheduled: bucket(6) }))).toBe("12 chat · 6 scheduled");
    expect(UsageFormat.counts(makeUsageBuckets({ scheduled: bucket(4) }))).toBe("4 scheduled");
    expect(UsageFormat.counts(makeUsageBuckets({ chat: bucket(2), compaction: bucket(1) }))).toBe("2 chat · 1 tidy-up");
    expect(UsageFormat.counts(makeUsageBuckets({ compaction: bucket(3) }))).toBe("3 tidy-ups");
    expect(UsageFormat.counts(makeUsageBuckets())).toBeNull();
  });

  it("orders Marlow's parts by window and skips unknown windows", () => {
    const parts = UsageFormat.marlowParts({
      seven_day: makeUsageBuckets({ chat: bucket(5, 50) }),
      five_hour: makeUsageBuckets(),
      thirty_day: makeUsageBuckets({ chat: bucket(1, 1) }),
    });
    expect(parts.map((p) => p.title)).toEqual(["This 5 hours", "This week"]);
    expect(parts[0].counts).toBeNull();
    expect(parts[1].counts).toBe("5 chat");
  });
});

describe("high usage row", () => {
  it("is inclusive at ninety percent", () => {
    expect(UsageFormat.HIGH_USAGE_THRESHOLD).toBe(0.9);
    expect(UsageFormat.highUsage(report({ five: 0.899 }), clock)).toBeNull();
    expect(UsageFormat.highUsage(report({ five: 0.9 }), clock)).not.toBeNull();
  });

  it("picks the most used window and words it", () => {
    const high = UsageFormat.highUsage(report({ five: 0.92, seven: 0.97 }), clock);
    expect(high).toEqual({ windowTitle: "This week", percent: 97, resetText: "Fri 4:59 AM" });
    expect(UsageFormat.highUsageText(high!)).toBe("97% of this week used — resets Fri 4:59 AM.");
    expect(UsageFormat.highUsageText(UsageFormat.highUsage(report({ five: 0.93 }), clock)!)).toBe(
      "93% of this 5 hours used — resets 8:59 PM.",
    );
  });

  it("ignores a window that has reset, but the other still counts", () => {
    expect(UsageFormat.highUsage(report({ five: 0.95, fiveResets: epoch(4, 19, 0) }), clock)).toBeNull();
    expect(
      UsageFormat.highUsage(report({ five: 0.95, fiveResets: epoch(4, 19, 0), seven: 0.91 }), clock)?.windowTitle,
    ).toBe("This week");
  });

  it("ignores a window with no reset time, no report, and unknown windows", () => {
    const noReset = { ...EMPTY_USAGE_REPORT, plan: plan({ five_hour: { utilization: 0.99, resetsAt: null } }) };
    expect(UsageFormat.highUsage(noReset, clock)).toBeNull();
    expect(UsageFormat.highUsage(null, clock)).toBeNull();
    expect(UsageFormat.highUsage(EMPTY_USAGE_REPORT, clock)).toBeNull();
    const unknown = { ...EMPTY_USAGE_REPORT, plan: plan({ thirty_day: { utilization: 0.99, resetsAt: epoch(9, 1) } }) };
    expect(UsageFormat.highUsage(unknown, clock)).toBeNull();
  });
});

describe("usage wire format", () => {
  it("decodes the server's example payload", () => {
    const r = decodeUsageReport({
      plan: {
        observed_at: 1791163586.47,
        status: "allowed",
        windows: {
          five_hour: { utilization: 0.14, resets_at: 1791167246 },
          seven_day: { utilization: 0.08, resets_at: 1791250046 },
        },
      },
      conversation: {
        session_id: "abc", context_tokens: 61000, observed_at: 1791163616.47,
        compaction_threshold_tokens: 110000, started_at: 1791140000.0, compactions: 1, retired: false,
      },
      marlow: {
        five_hour: {
          chat: { turns: 2, weighted_tokens: 400 },
          scheduled: { turns: 2, weighted_tokens: 17 },
          compaction: { turns: 1, weighted_tokens: 1000 },
        },
      },
      subscription_type: "max",
    });
    expect(r.plan?.observedAt).toBe(1791163586.47);
    expect(r.plan?.windows.five_hour).toEqual({ utilization: 0.14, resetsAt: 1791167246 });
    expect(r.conversation).toEqual({
      contextTokens: 61000, observedAt: 1791163616.47, compactionThresholdTokens: 110000,
      startedAt: 1791140000.0, compactions: 1, retired: false,
    });
    expect(r.marlow?.five_hour).toEqual(
      makeUsageBuckets({ chat: bucket(2, 400), scheduled: bucket(2, 17), compaction: bucket(1, 1000) }),
    );
    expect(r.subscriptionType).toBe("max");
  });

  it("reads null blocks, an empty object and a non-object as empty", () => {
    expect(decodeUsageReport({ plan: null, conversation: null, marlow: null })).toEqual(EMPTY_USAGE_REPORT);
    expect(decodeUsageReport({})).toEqual(EMPTY_USAGE_REPORT);
    expect(decodeUsageReport("nope")).toEqual(EMPTY_USAGE_REPORT);
  });

  it("reads a missing retired key as not retired and a wrong-typed tier as null", () => {
    const r = decodeUsageReport({ conversation: { context_tokens: 5 }, subscription_type: 7 });
    expect(r.conversation?.retired).toBe(false);
    expect(r.subscriptionType).toBeNull();
  });

  it("lets one odd field drop alone", () => {
    const r = decodeUsageReport({
      plan: {
        observed_at: "yesterday",
        status: 7,
        windows: { five_hour: { utilization: "lots" }, seven_day: { utilization: 0.3, resets_at: null } },
      },
      conversation: "garbage",
      marlow: { five_hour: { chat: { turns: "x" }, scheduled: null }, seven_day: 4 },
    });
    expect(r.plan?.observedAt).toBeNull();
    expect(r.plan?.status).toBeNull();
    expect(Object.keys(r.plan!.windows)).toEqual(["seven_day"]);
    expect(r.plan?.windows.seven_day.resetsAt).toBeNull();
    expect(r.conversation).toBeNull();
    expect(Object.keys(r.marlow!)).toEqual(["five_hour"]);
    expect(r.marlow?.five_hour).toEqual(makeUsageBuckets());
  });

  it("still reads whole numbers written as floats", () => {
    const r = decodeUsageReport({ conversation: { context_tokens: 61000.0, compaction_threshold_tokens: 110000.0 } });
    expect(r.conversation?.contextTokens).toBe(61000);
    expect(r.conversation?.compactionThresholdTokens).toBe(110000);
  });
});
