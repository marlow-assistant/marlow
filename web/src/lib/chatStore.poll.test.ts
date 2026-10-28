import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_POLICY, TEXT } from "./chatStore";
import {
  advance,
  after,
  flush,
  json,
  makeHarness,
  netFail,
  placeholderOf,
  turn,
  useFakeClock,
  type Harness,
} from "./testSupport";

let h: Harness;
beforeEach(() => useFakeClock());
afterEach(() => {
  h?.store.stop();
  vi.useRealTimers();
});

const state = () => h.store.getState();
const turns = () => h.server.to("GET /v1/turn");
const perms = () => h.server.to("GET /v1/permissions");
/** Offsets from the first request, for readable timing assertions. */
const at = (reqs: { at: number }[], t0: number) => reqs.map((r) => r.at - t0);

describe("POL-1 phases", () => {
  it("bursts at 500ms for 6s with no permission GET, then polls fast (2s) with ONE permissions GET before each turn GET; the deadline is checked after the sleep", async () => {
    h = makeHarness();
    await h.store.send("hi");
    const t0 = h.server.requests[0].at;
    await advance(5_999);
    // 500..5500 is eleven burst requests, and not a permission among them.
    expect(at(turns(), t0)).toEqual([500, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000, 5500]);
    expect(perms()).toHaveLength(0);
    // The burst's sleep ends at 6000: past the deadline, so no request fires
    // then; the next request is the fast phase's, permissions first.
    await advance(1);
    await advance(1_999);
    expect(h.server.requests.filter((r) => r.at > t0 + 5_500)).toHaveLength(0);
    await advance(1);
    const after6s = h.server.requests.filter((r) => r.at > t0 + 5_500);
    expect(after6s.map((r) => [r.path, r.at - t0])).toEqual([
      ["/v1/permissions", 8_000],
      ["/v1/turn/turn-1", 8_000],
    ]);
    // Then every 2s: one permissions GET per turn GET, in that order.
    await advance(10_000);
    const fast = h.server.requests.filter((r) => r.at >= t0 + 8_000);
    expect(fast.map((r) => r.path)).toEqual(
      Array.from({ length: 6 }, () => ["/v1/permissions", "/v1/turn/turn-1"]).flat(),
    );
  });

  it("the burst is also capped at 16 attempts", async () => {
    h = makeHarness({ policy: { burstDuration: 60_000 } });
    await h.store.send("hi");
    const t0 = h.server.requests[0].at;
    await advance(8_000);
    expect(turns()).toHaveLength(16);
    expect(perms()).toHaveLength(0);
    await advance(1_999);
    expect(turns()).toHaveLength(16);
    await advance(1);
    expect(perms()).toHaveLength(1);
    expect(turns()[16].at - t0).toBe(10_000);
  });

  it("exhausting the fast phase stalls with its text and clears the stream; the slow phase polls every 30s with no permission GETs and, exhausted, ends in the never-answered error", async () => {
    h = makeHarness();
    h.server.always("GET /v1/turn", turn({ partial_text: "some words so far" }));
    await h.store.send("hi");
    const t0 = h.server.requests[0].at;
    await advance(1_000);
    expect(h.store.getStream(placeholderOf(h).id)?.text).toBeTruthy();
    // 6s burst + 150 fast attempts of 2s.
    await advance(6_000 + 150 * 2_000 - 1_000 - 1); // t = 305_999
    expect(perms()).toHaveLength(149);
    expect(placeholderOf(h).status).toBe("running");
    // The fast phase's 150th request (t = 306_000) is the last; the stall follows it.
    await advance(1);
    expect(perms()).toHaveLength(150);
    expect(placeholderOf(h)).toMatchObject({ status: "stalled", text: TEXT.stalled });
    expect(h.store.getStream(placeholderOf(h).id)).toBeUndefined();
    const before = turns().length;
    await advance(30_000);
    expect(turns()).toHaveLength(before + 1);
    expect(turns().at(-1)!.at - t0).toBe(6_000 + 150 * 2_000 + 30_000);
    // 119 more slow polls and then the error.
    await advance(30_000 * 118);
    expect(placeholderOf(h).status).toBe("stalled");
    await advance(30_000);
    expect(placeholderOf(h)).toMatchObject({ status: "error", text: TEXT.neverAnswered });
    expect(perms()).toHaveLength(150); // none in the slow phase
    const n = h.server.requests.length;
    await advance(300_000);
    expect(h.server.requests.length).toBe(n);
  });
});

describe("POL-2 one GET per turn in flight", () => {
  it("a slow GET delays the next instead of overlapping it", async () => {
    h = makeHarness();
    h.server.always("GET /v1/turn", after(5_000, turn()));
    await h.store.send("hi");
    const t0 = h.server.requests[0].at;
    await advance(30_000);
    expect(h.server.maxInFlight.get("GET /v1/turn")).toBe(1);
    // The first GET is in flight until 5500; the burst deadline (6000) then
    // expires, so the next GET is a fast-phase one 2s after that, and the
    // one after waits out its predecessor's 5s plus its own 2s sleep.
    expect(at(turns(), t0).slice(0, 3)).toEqual([500, 8_000, 15_000]);
  });
});

describe("POL-3 terminal statuses", () => {
  it("done: text is result_text, status done, a finished event with the turn id", async () => {
    h = makeHarness();
    h.server.script("GET /v1/turn", turn(), turn({ status: "done", result_text: "all done" }));
    await h.store.send("hi");
    await advance(1_000);
    expect(placeholderOf(h)).toMatchObject({ status: "done", text: "all done" });
    expect(h.events.filter((e) => e.kind === "finished")).toEqual([{ kind: "finished", turnIds: ["turn-1"] }]);
    await advance(10_000);
    expect(turns()).toHaveLength(2);
  });

  it("done while backgrounded fires no finished event", async () => {
    h = makeHarness({ isForeground: false });
    h.server.always("GET /v1/turn", turn({ status: "done", result_text: "quiet" }));
    await h.store.send("hi");
    await advance(500);
    expect(placeholderOf(h).text).toBe("quiet");
    expect(h.events.filter((e) => e.kind === "finished")).toEqual([]);
  });

  it("error: shows the server's error text, or 'Something went wrong.'", async () => {
    h = makeHarness();
    h.server.always("GET /v1/turn", turn({ status: "error", error: "the tool exploded" }));
    await h.store.send("hi");
    await advance(500);
    expect(placeholderOf(h)).toMatchObject({ status: "error", text: "the tool exploded" });
    h.store.stop();

    h = makeHarness();
    h.server.always("GET /v1/turn", turn({ status: "error" }));
    await h.store.send("hi");
    await advance(500);
    expect(placeholderOf(h)).toMatchObject({ status: "error", text: "Something went wrong." });
    expect(h.events.filter((e) => e.kind === "finished")).toEqual([]);
  });

  it("merged: removes the placeholder only, fires no finished event, and stops polling", async () => {
    h = makeHarness();
    h.server.always("GET /v1/turn", turn({ status: "merged", merged_into: "turn-0" }));
    await h.store.send("hi");
    await advance(500);
    expect(state().messages.map((m) => m.role)).toEqual(["user"]);
    expect(h.events.filter((e) => e.kind === "finished")).toEqual([]);
    await advance(20_000);
    expect(turns()).toHaveLength(1);
  });

  it("an unknown status keeps polling", async () => {
    h = makeHarness();
    h.server.script("GET /v1/turn", turn({ status: "quantum" }), turn({ status: "quantum" }), turn({ status: "done", result_text: "ok" }));
    await h.store.send("hi");
    await advance(1_500);
    expect(turns()).toHaveLength(3);
    expect(placeholderOf(h)).toMatchObject({ status: "done", text: "ok" });
  });
});

describe("POL-4 / POL-5 failures", () => {
  it("POL-4: a 404 is the lost-track error and polling stops", async () => {
    h = makeHarness();
    h.server.always("GET /v1/turn", json({ detail: "unknown turn" }, 404));
    await h.store.send("hi");
    await advance(500);
    expect(placeholderOf(h)).toMatchObject({ status: "error", text: TEXT.lostTrack });
    await advance(60_000);
    expect(turns()).toHaveLength(1);
  });

  it("POL-5: a 500 keeps polling", async () => {
    h = makeHarness();
    h.server.script("GET /v1/turn", json({}, 500), json({}, 502), turn({ status: "done", result_text: "late" }));
    await h.store.send("hi");
    await advance(1_500);
    expect(placeholderOf(h)).toMatchObject({ status: "done", text: "late" });
  });

  it("POL-5: a transport failure keeps polling", async () => {
    h = makeHarness();
    h.server.script("GET /v1/turn", netFail(), netFail(), turn({ status: "done", result_text: "back" }));
    await h.store.send("hi");
    await advance(1_500);
    expect(placeholderOf(h)).toMatchObject({ status: "done", text: "back" });
  });
});

describe("POL-6 cancellation", () => {
  it("stop() ends polling", async () => {
    h = makeHarness();
    await h.store.send("hi");
    await advance(1_200);
    const n = turns().length;
    h.store.stop();
    await advance(60_000);
    expect(turns()).toHaveLength(n);
  });

  it("an answer that lands after stop() mutates nothing", async () => {
    h = makeHarness();
    h.server.always("GET /v1/turn", after(1_000, turn({ status: "done", result_text: "too late" })));
    await h.store.send("hi");
    await advance(600);
    h.store.stop();
    await advance(5_000);
    expect(placeholderOf(h).status).toBe("running");
  });
});

describe("STR streaming", () => {
  it("STR-7: partial_text and activities are ingested only while running, not while stalled", async () => {
    h = makeHarness({ policy: { burstAttempts: 0, fastAttempts: 1 } });
    h.server.script("GET /v1/turn", turn()); // fast attempt 1: nothing yet
    h.server.always(
      "GET /v1/turn",
      turn({ partial_text: "late words", activities: [{ kind: "tool_start", tool: "Read", at: 1 }] }),
    );
    await h.store.send("hi");
    await advance(2_000);
    expect(placeholderOf(h).status).toBe("stalled");
    await advance(30_000 * 3);
    expect(turns().length).toBeGreaterThan(2);
    expect(h.store.getStream(placeholderOf(h).id)).toBeUndefined();
    expect(placeholderOf(h).status).toBe("stalled");
  });

  it("STR-5: at terminal the stream state is cleared and getStream is undefined", async () => {
    h = makeHarness();
    h.server.script(
      "GET /v1/turn",
      turn({ partial_text: "halfway there", activities: [{ kind: "tool_start", tool: "Read", at: 1 }] }),
      turn({ status: "done", result_text: "halfway there, done" }),
    );
    await h.store.send("hi");
    const id = placeholderOf(h).id;
    await advance(500);
    expect(h.store.getStream(id)).toMatchObject({ label: "Reading a file…" });
    expect(h.store.getStream(id)?.text).toBeTruthy();
    await advance(500);
    expect(placeholderOf(h).status).toBe("done");
    expect(h.store.getStream(id)).toBeUndefined();
  });

  it("STR-5: an error and a lost-track 404 clear it as well", async () => {
    for (const second of [turn({ status: "error", error: "x" }), json({}, 404)]) {
      h = makeHarness();
      h.server.script("GET /v1/turn", turn({ partial_text: "typing" }), second);
      await h.store.send("hi");
      const id = placeholderOf(h).id;
      await advance(500);
      expect(h.store.getStream(id)).toBeDefined();
      await advance(500);
      expect(placeholderOf(h).status).toBe("error");
      expect(h.store.getStream(id)).toBeUndefined();
      h.store.stop();
    }
  });

  it("STR-2: a long reply is revealed one stride per 50ms tick, not per poll and not by a save", async () => {
    h = makeHarness();
    const long = "word ".repeat(200); // 1000 code points, under the jump threshold
    h.server.always("GET /v1/turn", turn({ partial_text: long }));
    await h.store.send("hi");
    const id = placeholderOf(h).id;
    await advance(500);
    const first = h.store.getStream(id)!.text!.length;
    expect(first).toBeLessThan(long.length);
    await advance(50);
    expect(h.store.getStream(id)!.text!.length).toBeGreaterThan(first);
    // SYN-6: ticking never writes the cache.
    const saves = h.persist.saves.length;
    await advance(400);
    expect(h.persist.saves.length).toBe(saves);
    await advance(2_000);
    expect(h.store.getStream(id)!.text).toBe(long);
    expect(h.persist.saves.length).toBe(saves);
  });

  it("STR-9: activity labels are held for the dwell", async () => {
    h = makeHarness();
    h.server.always(
      "GET /v1/turn",
      turn({
        activities: [
          { kind: "tool_start", tool: "Read", at: 1 },
          { kind: "tool_start", tool: "Bash", at: 2 },
        ],
      }),
    );
    await h.store.send("hi");
    const id = placeholderOf(h).id;
    await advance(500);
    expect(h.store.getStream(id)?.label).toBe("Reading a file…");
    await advance(699);
    expect(h.store.getStream(id)?.label).toBe("Reading a file…");
    await advance(1);
    expect(h.store.getStream(id)?.label).toBe("Running a command…");
  });

  it("FBK-1: started fires once per placeholder however many ticks and polls", async () => {
    h = makeHarness();
    let n = 0;
    h.server.always("GET /v1/turn", (req) => turn({ partial_text: "grows ".repeat(++n * 10) })(req));
    await h.store.send("hi");
    await advance(5_000);
    expect(n).toBeGreaterThan(5);
    const started = h.events.filter((e) => e.kind === "started");
    expect(started).toEqual([{ kind: "started", placeholderIds: [placeholderOf(h).id] }]);
  });
});

describe("policy sanity", () => {
  it("the default policy is the contract's", () => {
    expect(DEFAULT_POLICY).toMatchObject({
      burstInterval: 500,
      burstDuration: 6_000,
      burstAttempts: 16,
      fastInterval: 2_000,
      fastAttempts: 150,
      slowInterval: 30_000,
      slowAttempts: 120,
      healthInterval: 15_000,
      connectionNoticeDelay: 600,
      sendMaxAttempts: 8,
      noticeDuration: 3_000,
    });
  });
});

void flush;
