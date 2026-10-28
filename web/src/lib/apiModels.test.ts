import { describe, expect, it } from "vitest";
import {
  decodeActivityPage,
  decodeIdentityFile,
  decodeIdentityIndex,
  decodeRecurrence,
  decodeScheduleIndex,
} from "./apiModels";

describe("activity wire format", () => {
  it("decodes the server's record and tolerates gaps", () => {
    const page = decodeActivityPage({
      entries: [
        {
          v: 1, turn_id: "t1", source: "cron", label: "morning", merged: [],
          created_at: 1.0, started_at: 2.0, finished_at: 16.5, status: "done",
          tools: [{ name: "Read", target: "memory/a.md", at: 3.0 }], tools_dropped: 0,
          approvals: [{ tool: "Edit", decision: "allow" }], denials: ["Bash"],
          files: ["memory/a.md"], commit: "abc1234", some_future_field: 7,
          model: "claude-sonnet-5-5", effort: "high",
        },
        { turn_id: "t2", finished_at: 10.0, tools: "garbage" },
      ],
      next_before: 10.0,
    });
    expect(page.nextBefore).toBe(10.0);
    expect(page.entries).toHaveLength(2);
    const first = page.entries[0];
    expect(first.label).toBe("morning");
    expect(first.tools).toEqual([{ name: "Read", target: "memory/a.md", at: 3.0 }]);
    expect(first.approvals).toEqual([{ tool: "Edit", decision: "allow" }]);
    expect(first.files).toEqual(["memory/a.md"]);
    expect(page.entries[1].tools).toEqual([]);
    expect(page.entries[1].toolsDropped).toBe(0);
    expect([first.model, first.effort]).toEqual(["claude-sonnet-5-5", "high"]);
    expect([page.entries[1].model, page.entries[1].effort]).toEqual([null, null]);
  });

  it("skips an entry with no id or time instead of failing the page", () => {
    const page = decodeActivityPage({
      entries: [{ finished_at: 1 }, { turn_id: "x" }, { turn_id: "ok", finished_at: 2 }, null, 7],
      next_before: null,
    });
    expect(page.entries.map((e) => e.turnId)).toEqual(["ok"]);
    expect(page.nextBefore).toBeNull();
  });

  it("garbage in, empty page out", () => {
    expect(decodeActivityPage(null)).toEqual({ entries: [], nextBefore: null });
    expect(decodeActivityPage({ entries: "no" })).toEqual({ entries: [], nextBefore: null });
  });

  it("wrongly typed optional fields fall back instead of throwing", () => {
    const [e] = decodeActivityPage({
      entries: [{ turn_id: "t", finished_at: 1, source: 5, label: {}, status: [], tools_dropped: "x", denials: [1, "a"], approvals: [{ tool: 1 }] }],
    }).entries;
    expect(e.source).toBeNull();
    expect(e.label).toBeNull();
    expect(e.toolsDropped).toBe(0);
    expect(e.denials).toEqual(["a"]);
    expect(e.approvals).toEqual([]);
  });
});

describe("identity and schedule wire format", () => {
  it("decodes the index, files and schedules", () => {
    const idx = decodeIdentityIndex({
      files: [
        { id: "SOUL.md", kind: "soul", modified_at: 5.5, size: 10, description: null },
        { id: "memory/a.md", kind: "memory", modified_at: 6, size: 20, description: "About a" },
        { id: "x.md", kind: "future_kind", modified_at: 1, size: 1 },
        { kind: "soul" },
      ],
      schedules: {
        timezone: "America/Toronto", rejected: null,
        entries: [
          { id: "morning", cron: "0 8 * * *", at: null, recurrence: { type: "daily", times: ["08:00"] },
            next_fire_at: 1790000000.5, notify: null, prompt: null, job: "morning", job_file: "cron/morning.md", extra: 1 },
          { id: "nore", cron: "* * * 6 *" },
          { cron: "no id" },
        ],
      },
    });
    expect(idx.files.map((f) => f.id)).toEqual(["SOUL.md", "memory/a.md", "x.md"]);
    expect(idx.files[1].description).toBe("About a");
    expect(idx.files[2].kind).toBe("unknown"); // an unknown kind decodes, it does not throw
    expect(idx.schedules.timezone).toBe("America/Toronto");
    expect(idx.schedules.entries).toHaveLength(2);
    expect(idx.schedules.entries[0].recurrence).toEqual({ type: "daily", times: ["08:00"] });
    expect(idx.schedules.entries[0].jobFile).toBe("cron/morning.md");
    expect(idx.schedules.entries[1].recurrence).toEqual({ type: "other" }); // missing recurrence
  });

  it("a missing schedules block still shows the files", () => {
    const idx = decodeIdentityIndex({ files: [{ id: "SOUL.md", kind: "soul", modified_at: 1, size: 1 }] });
    expect(idx.files).toHaveLength(1);
    expect(idx.schedules).toEqual({ timezone: null, rejected: null, entries: [] });
    expect(decodeScheduleIndex("garbage").entries).toEqual([]);
  });

  it("recurrence types, with an unknown one reading as other", () => {
    expect(decodeRecurrence({ type: "weekly", weekdays: [1, 2], times: ["08:00"] }))
      .toEqual({ type: "weekly", weekdays: [1, 2], times: ["08:00"] });
    expect(decodeRecurrence({ type: "monthly", days: [1, 15], times: ["08:00"] }))
      .toEqual({ type: "monthly", days: [1, 15], times: ["08:00"] });
    expect(decodeRecurrence({ type: "once" })).toEqual({ type: "once" });
    expect(decodeRecurrence({ type: "yearly" })).toEqual({ type: "other" });
    expect(decodeRecurrence({ type: "daily", times: "bad" })).toEqual({ type: "daily", times: [] });
    expect(decodeRecurrence(null)).toEqual({ type: "other" });
  });

  it("decodes an identity file and its truncation flag", () => {
    expect(decodeIdentityFile({ id: "SOUL.md", kind: "soul", modified_at: 3, text: "hi", truncated: true }))
      .toEqual({ id: "SOUL.md", kind: "soul", modifiedAt: 3, text: "hi", truncated: true });
    expect(decodeIdentityFile({ id: "a", kind: 9 })?.kind).toBe("unknown");
    expect(decodeIdentityFile({})).toBeNull();
  });
});
