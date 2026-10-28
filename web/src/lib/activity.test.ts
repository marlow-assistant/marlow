import { describe, expect, it } from "vitest";
import {
  ACTIVITY_DWELL_MS,
  activityLabel,
  createActivityPlaybackState,
  describesUnseenWork,
  ingestActivities,
  nextActivityAt,
  nextLabel,
  playActivity,
  runningLabel,
  type ActivityPlaybackState,
} from "./activity";
import type { TurnActivity } from "./types";

const act = (kind: string, tool: string | null = null, at = 0): TurnActivity => ({ kind, tool, at });
const ingest = (s: ActivityPlaybackState, a: TurnActivity[]) => ingestActivities(s, a);

describe("activity labels (STR-10)", () => {
  const cases: [string, string][] = [
    ["Read", "Reading a file…"],
    ["Write", "Writing a file…"],
    ["Edit", "Writing a file…"],
    ["Bash", "Running a command…"],
    ["Grep", "Searching…"],
    ["Glob", "Searching…"],
    ["WebSearch", "Searching the web…"],
    ["WebFetch", "Searching the web…"],
    ["mcp__brave__web_search", "Searching the web…"],
    ["mcp__exa__web_fetch", "Searching the web…"],
    ["mcp__webflow__list_sites", "Using a tool…"],
    ["mcp__google_calendar__list_events", "Checking your calendar…"],
    ["mcp__GMAIL__search_messages", "Checking your mail…"],
    ["mcp__weather__forecast", "Checking the weather…"],
    ["mcp__weather__route_forecast", "Planning the route…"],
    ["UnknownTool", "Using a tool…"],
  ];
  for (const [tool, expected] of cases) {
    it(`tool activity uses friendly labels: ${tool}`, () => {
      expect(activityLabel(act("tool_start", tool))).toBe(expected);
    });
  }

  it("untrusted tool names never become visible copy", () => {
    const tool = "<script>alert('x')</script>" + "x".repeat(10_000);
    const label = activityLabel(act("tool_start", tool));
    expect(label).toBe("Using a tool…");
    expect(label.includes(tool)).toBe(false);
  });

  it("a text event says Writing not Thinking", () => {
    expect(activityLabel(act("text"))).toBe("Writing…");
  });

  it("every other kind still says Thinking", () => {
    for (const kind of ["thinking", "tool_end", "future_cli_event", ""]) {
      expect(activityLabel(act(kind))).toBe("Thinking…");
    }
  });

  it("an unknown kind with a tool is still Thinking, and the tool never shows", () => {
    expect(activityLabel(act("future_cli_event", "untrusted"))).toBe("Thinking…");
  });

  it("tool start without a name uses the generic tool label", () => {
    expect(activityLabel(act("tool_start", null))).toBe("Using a tool…");
  });
});

describe("describesUnseenWork / typing hides generic labels (STR-11)", () => {
  it("generic labels and absent are unseen work; tool labels are not", () => {
    expect(describesUnseenWork("Thinking…")).toBe(true);
    expect(describesUnseenWork("Writing…")).toBe(true);
    expect(describesUnseenWork(null)).toBe(true);
    expect(describesUnseenWork(undefined)).toBe(true);
    expect(describesUnseenWork("Reading a file…")).toBe(false);
    expect(describesUnseenWork("Using a tool…")).toBe(false);
    expect(describesUnseenWork("Checking your calendar…")).toBe(false);
  });

  it("once text has streamed, generic or absent hides and a tool label stays", () => {
    expect(runningLabel("Thinking…", true)).toBeNull();
    expect(runningLabel("Writing…", true)).toBeNull();
    expect(runningLabel(null, true)).toBeNull();
    expect(runningLabel("Searching the web…", true)).toBe("Searching the web…");
  });

  it("before any text, absent falls back to Thinking and labels show", () => {
    expect(runningLabel(null, false)).toBe("Thinking…");
    expect(runningLabel("Writing…", false)).toBe("Writing…");
    expect(runningLabel("Running a command…", false)).toBe("Running a command…");
  });
});

describe("ActivityPlaybackState", () => {
  it("cumulative activity only queues new events and coalesces labels", () => {
    let s = createActivityPlaybackState();
    const first = [act("tool_start", "Read"), act("tool_end"), act("text")];

    // `text` carries its own label ("Writing…") as of reply streaming, so it no
    // longer coalesces into the tool_end "Thinking…" ahead of it.
    s = ingest(s, first);
    expect(s.pendingLabels).toEqual(["Reading a file…", "Thinking…", "Writing…"]);

    s = ingest(s, first);
    expect(s.pendingLabels).toEqual(["Reading a file…", "Thinking…", "Writing…"]); // a repeated poll adds nothing

    // Four labels now exceed the three-deep skip-ahead cap, so the oldest drops.
    s = ingest(s, [...first, act("tool_start", "Bash")]);
    expect(s.pendingLabels).toEqual(["Thinking…", "Writing…", "Running a command…"]);
  });

  it("backlog skips ahead to the last three labels", () => {
    const s = ingest(createActivityPlaybackState(), [
      act("tool_start", "Read"),
      act("tool_end"),
      act("tool_start", "Bash"),
      act("tool_end"),
      act("tool_start", "WebSearch"),
    ]);
    expect(s.pendingLabels).toEqual(["Running a command…", "Thinking…", "Searching the web…"]);
  });

  it("the cursor continues when the server's capped window slides", () => {
    let s = createActivityPlaybackState();
    const initial = Array.from({ length: 64 }, (_, i) =>
      act("tool_start", i % 2 === 0 ? "Read" : "Bash", i),
    );
    s = ingest(s, initial);
    while (nextLabel(s).label !== null) s = nextLabel(s).state;

    const newest = act("tool_end", null, 64);
    s = ingest(s, [...initial.slice(1), newest]);

    expect(s.pendingLabels).toEqual(["Thinking…"]); // the event appended after rollover is not lost
  });

  it("overlap equality includes `at`: repeated identical Reads still yield the new one", () => {
    // Without `at` in equality, a window of identical Reads over-matches (every
    // suffix equals every prefix) and the genuinely new Read is swallowed.
    // Distinct labels around them so the dedupe cannot hide the difference.
    let s = createActivityPlaybackState();
    const w1 = [act("tool_start", "Read", 1), act("tool_start", "Read", 2), act("tool_start", "Read", 3)];
    s = ingest(s, w1);
    s = { ...s, pendingLabels: [], lastQueuedLabel: null };
    // Slide by one: drop at=1, append another Read at=4 (a different label would
    // be easier; here the new event is a Bash so the swallowed case is visible).
    const w2 = [act("tool_start", "Read", 2), act("tool_start", "Read", 3), act("tool_start", "Bash", 4)];
    s = ingest(s, w2);
    expect(s.pendingLabels).toEqual(["Running a command…"]);

    // Same kind/tool, different `at`, so only `at` tells them apart: a window
    // that is entirely NEW events with the same kind/tool must all be new.
    let t = ingest(createActivityPlaybackState(), [act("tool_end", null, 1), act("tool_end", null, 2)]);
    t = { ...t, pendingLabels: [], lastQueuedLabel: null };
    t = ingest(t, [act("tool_end", null, 3), act("tool_end", null, 4), act("tool_start", "Bash", 5)]);
    expect(t.pendingLabels).toEqual(["Thinking…", "Running a command…"]);
  });

  it("dedupes consecutive identical labels, across drains too", () => {
    let s = ingest(createActivityPlaybackState(), [act("tool_start", "Read", 1)]);
    s = nextLabel(s).state; // drained
    s = ingest(s, [act("tool_start", "Read", 1), act("tool_start", "Read", 2)]);
    expect(s.pendingLabels).toEqual([]); // same label as the last queued
    s = ingest(s, [act("tool_start", "Read", 1), act("tool_start", "Read", 2), act("tool_start", "Edit", 3)]);
    expect(s.pendingLabels).toEqual(["Writing a file…"]);
  });

  it("an empty window (terminal) leaves nothing queued", () => {
    const s = ingest(createActivityPlaybackState(), []);
    expect(s.pendingLabels).toEqual([]);
  });

  it("production labels dwell for seven hundred milliseconds", () => {
    expect(ACTIVITY_DWELL_MS).toBe(700);
  });
});

describe("dwell playback (STR-9)", () => {
  const queued = () =>
    ingest(createActivityPlaybackState(), [
      act("tool_start", "Read", 1),
      act("tool_end", null, 2),
      act("tool_start", "Bash", 3),
    ]);

  it("shows the first label at once, then each after its dwell", () => {
    let s = queued();
    s = playActivity(s, 1000);
    expect(s.shownLabel).toBe("Reading a file…");
    expect(nextActivityAt(s)).toBe(1700);

    expect(playActivity(s, 1699)).toBe(s); // not yet
    s = playActivity(s, 1700);
    expect(s.shownLabel).toBe("Thinking…");
    s = playActivity(s, 2400);
    expect(s.shownLabel).toBe("Running a command…");
  });

  it("the last label stays until a newer one arrives", () => {
    let s = playActivity(ingest(createActivityPlaybackState(), [act("tool_start", "Read", 1)]), 0);
    s = playActivity(s, 60_000);
    expect(s.shownLabel).toBe("Reading a file…");
    expect(nextActivityAt(s)).toBeNull();
    s = ingest(s, [act("tool_start", "Read", 1), act("tool_start", "Edit", 2)]);
    s = playActivity(s, 60_001); // dwell long over: newer shows immediately
    expect(s.shownLabel).toBe("Writing a file…");
  });

  it("a newer label arriving mid-dwell waits for the dwell", () => {
    let s = playActivity(ingest(createActivityPlaybackState(), [act("tool_start", "Read", 1)]), 1000);
    s = ingest(s, [act("tool_start", "Read", 1), act("tool_start", "Edit", 2)]);
    expect(playActivity(s, 1300)).toBe(s);
    expect(playActivity(s, 1700).shownLabel).toBe("Writing a file…");
  });

  it("terminal clears everything by starting from a fresh state", () => {
    const s = createActivityPlaybackState();
    expect(s.shownLabel).toBeNull();
    expect(s.pendingLabels).toEqual([]);
    expect(s.previousWindow).toEqual([]);
  });
});
