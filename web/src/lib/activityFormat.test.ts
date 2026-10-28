import { describe, expect, it } from "vitest";
import {
  countText,
  detailLines,
  duration,
  flags,
  isBackground,
  isNoteworthy,
  isVisible,
  modelName,
  outputText,
  runLine,
  summary,
  summaryLine,
  title,
} from "./activityFormat";
import { makeActivityEntry, type ActivityEntry } from "./apiModels";

function entry(o: {
  source?: string;
  label?: string | null;
  tools?: [string, string | null][];
  files?: string[];
  approvals?: [string, string][];
  denials?: string[];
  status?: string;
  dropped?: number;
} = {}): ActivityEntry {
  return makeActivityEntry({
    turnId: crypto.randomUUID(),
    source: o.source ?? "cron",
    label: o.label === undefined ? "morning" : o.label,
    startedAt: 100,
    finishedAt: 114,
    status: o.status ?? "done",
    tools: (o.tools ?? []).map(([name, target]) => ({ name, target, at: null })),
    toolsDropped: o.dropped ?? 0,
    approvals: (o.approvals ?? []).map(([tool, decision]) => ({ tool, decision })),
    denials: o.denials ?? [],
    files: o.files ?? [],
  });
}

describe("ActivityFormat", () => {
  it("titles name known jobs and humanise the rest", () => {
    expect(title(entry({ label: "morning" }))).toBe("Morning briefing");
    expect(title(entry({ label: "email-flash" }))).toBe("Check-in");
    expect(title(entry({ label: "physio-nudge" }))).toBe("Physio nudge");
    expect(title(entry({ label: null }))).toBe("Scheduled job");
    expect(title(entry({ source: "chat", label: null }))).toBe("Chat");
  });

  // Activity-log records written before the `ios` -> `chat` rename keep
  // `source: "ios"` for their 12-month retention. Deliberately the old value.
  it("a legacy `ios` record is a chat turn exactly like a `chat` one", () => {
    expect(title(entry({ source: "ios", label: null }))).toBe("Chat");
    expect(summaryLine(entry({ source: "ios", label: null }))).toBe("Replied");
    expect(isNoteworthy(entry({ source: "ios", label: null }))).toBe(false);
    expect(isNoteworthy(entry({ source: "ios", label: null, status: "error" }))).toBe(true);
  });

  it("a job id that collides with an Object prototype key is humanised, not looked up", () => {
    expect(title(entry({ label: "constructor" }))).toBe("Constructor");
  });

  it("the morning brief reads as one line", () => {
    const e = entry({
      tools: [
        ["mcp__claude_ai_Gmail__search_threads", null],
        ["mcp__claude_ai_Google_Calendar__list_events", null],
        ["mcp__claude_ai_Gmail__get_thread", null],
        ["Read", "memory/travel.md"],
        ["Edit", "memory/travel.md"],
      ],
      files: ["memory/travel.md"],
    });
    expect(summary(e)).toBe("Checked Gmail, Calendar · read travel.md · changed travel.md");
  });

  // Connector names are never shown: an unknown MCP tool is "another tool", not
  // its name, same rule as the activity label.
  it("unknown tool names are never rendered", () => {
    const e = entry({ tools: [["mcp__evil__<b>Ignore_previous</b>", null], ["FutureTool", null]], dropped: 1 });
    expect(summary(e)).toBe("Used 3 other tools");
    expect(detailLines(e).every((l) => !l.label.includes("evil"))).toBe(true);
  });

  it("searches quote the first query and count the rest", () => {
    const e = entry({ tools: [["WebSearch", "flights to YYC"], ["WebSearch", "hotels"], ["WebFetch", "example.com"]] });
    expect(summary(e)).toBe("Searched “flights to YYC” +1 more · opened example.com");
  });

  it("long lists stop at two names", () => {
    const e = entry({ tools: [["Read", "a.md"], ["Read", "b.md"], ["Read", "c.md"], ["Read", "a.md"]] });
    expect(summary(e)).toBe("Read a.md, b.md +1 more");
  });

  it("reads without a target are counted", () => {
    expect(summary(entry({ tools: [["Read", null], ["Read", null]] }))).toBe("Read 2 files");
  });

  it("flags separate Mike's answers from blocks", () => {
    const e = entry({
      approvals: [["Edit", "allow"], ["Bash", "deny"], ["Write", "timeout"]],
      denials: ["Bash", "Write", "Bash"],
      status: "error",
    });
    expect(flags(e).map((f) => f.text)).toEqual([
      "Didn't finish", "you approved 1", "you declined 1", "1 request missed", "1 blocked",
    ]);
    expect(flags(entry())).toEqual([]);
  });

  // Mike's approvals are a record, not a warning: only they stay grey.
  it("only approvals are not warnings", () => {
    const e = entry({ approvals: [["Bash", "allow"], ["Bash", "deny"]], denials: ["Bash"], status: "error" });
    expect(flags(e)).toEqual([
      { text: "Didn't finish", isWarning: true },
      { text: "you approved 1", isWarning: false },
      { text: "you declined 1", isWarning: true },
    ]);
    expect(flags(entry({ approvals: [["Read", "allow"]] }))[0]).toEqual({
      text: "You approved 1",
      isWarning: false,
    });
  });

  // A failed turn did not reply; "Replied" over it was false.
  it("a failed tool-less turn has no summary line", () => {
    expect(summaryLine(entry({ source: "chat", label: null, status: "error" }))).toBeNull();
    expect(summaryLine(entry({ source: "chat", label: null }))).toBe("Replied");
    expect(summaryLine(entry({ tools: [["Read", "a.md"]], status: "error" }))).toBe("Read a.md");
  });

  it("a tool-less chat turn is hidden but an idle cron job is not", () => {
    expect(isNoteworthy(entry({ source: "chat", label: null }))).toBe(false);
    expect(isNoteworthy(entry({ source: "chat", label: null, tools: [["Read", null]] }))).toBe(true);
    expect(isNoteworthy(entry({ source: "chat", label: null, status: "error" }))).toBe(true);
    expect(isNoteworthy(entry({ source: "cron" }))).toBe(true);
  });

  it("duration is seconds then minutes", () => {
    expect(duration(entry())).toBe("14s");
    const long = makeActivityEntry({ turnId: "x", source: "cron", startedAt: 0, finishedAt: 125 });
    expect(duration(long)).toBe("2m 5s");
    expect(duration(makeActivityEntry({ turnId: "x", finishedAt: 120, startedAt: 0 }))).toBe("2m");
    expect(duration(makeActivityEntry({ turnId: "x", finishedAt: 120 }))).toBeNull();
  });

  it("run line says how long, which model and what effort", () => {
    const full = makeActivityEntry({
      turnId: "x", source: "cron", label: "consolidate", startedAt: 100, finishedAt: 114,
      model: "claude-sonnet-5-5", effort: "high",
    });
    expect(runLine(full)).toBe("Took 14s · Sonnet 5.5 · High effort");
    // An old record: duration alone, exactly what the row used to say.
    expect(runLine(entry())).toBe("Took 14s");
    expect(runLine(makeActivityEntry({ turnId: "y", finishedAt: 5, effort: "low" }))).toBe("Low effort");
    expect(runLine(makeActivityEntry({ turnId: "z", finishedAt: 5 }))).toBeNull();
    // A level this build doesn't know is shown as the server sent it.
    expect(runLine(makeActivityEntry({ turnId: "w", finishedAt: 5, effort: "xhigh" }))).toBe("xhigh effort");
    expect(runLine(makeActivityEntry({ turnId: "v", finishedAt: 5, effort: "constructor" }))).toBe("constructor effort");
  });

  it("model names keep the version and never guess", () => {
    expect(modelName("claude-sonnet-5-5")).toBe("Sonnet 5.5");
    expect(modelName("claude-opus-5")).toBe("Opus 5");
    expect(modelName("claude-haiku-4-5-20251001")).toBe("Haiku 4.5");
    expect(modelName("claude-fable")).toBe("Fable");
    expect(modelName("sonnet")).toBe("Sonnet");
    expect(modelName("gpt-x-1")).toBe("gpt-x-1");
  });

  it("detail lines label each call and note dropped ones", () => {
    const e = entry({ tools: [["Read", "a.md"], ["Bash", null]], dropped: 2 });
    expect(detailLines(e)).toEqual([
      { label: "Read", target: "a.md", count: 1 },
      { label: "Command", target: null, count: 1 },
      { label: "2 more not recorded", target: null, count: 1 },
    ]);
  });

  it("detail lines merge repeats in first-seen order, with a count", () => {
    const e = entry({
      tools: [
        ["Read", "memory/travel.md"],
        ["Bash", null],
        ["Read", "memory/travel.md"],
        ["Edit", "memory/travel.md"],
        ["Bash", null],
        ["Read", "memory/travel.md"],
        ["Read", "memory/food.md"],
      ],
    });
    const lines = detailLines(e);
    expect(lines.map((l) => [l.label, l.target, countText(l)])).toEqual([
      ["Read", "memory/travel.md", "×3"],
      ["Command", null, "×2"],
      ["Changed", "memory/travel.md", null],
      ["Read", "memory/food.md", null],
    ]);
  });

  it("names the overnight jobs, never by id", () => {
    expect(title(entry({ label: "ideas" }))).toBe("Overnight ideas");
    expect(title(entry({ label: "consolidate" }))).toBe("Nightly tidy");
  });

  it("hides background jobs unless asked, but never a failed one", () => {
    const plumbing = (status: string) => makeActivityEntry({ turnId: "p", finishedAt: 1, source: "cron", label: "ideas", tier: "plumbing", status });
    expect(isBackground(plumbing("done"))).toBe(true);
    expect(isVisible(plumbing("done"), false)).toBe(false);
    expect(isVisible(plumbing("done"), true)).toBe(true);
    expect(isVisible(plumbing("error"), false)).toBe(true);
    // An ordinary job ignores the toggle.
    expect(isVisible(entry(), false)).toBe(true);
    // And the toggle doesn't resurrect a chat turn with nothing in it.
    expect(isVisible(entry({ source: "chat" }), true)).toBe(false);
  });

  it("words what a job said, but not the silent token", () => {
    expect(outputText("[silent]")).toBe("Nothing to report.");
    expect(outputText(" `[SILENT]` ")).toBe("Nothing to report.");
    expect(outputText("  ")).toBe("Nothing to report.");
    expect(outputText("\nAdded one idea.\n")).toBe("Added one idea.");
  });
});
