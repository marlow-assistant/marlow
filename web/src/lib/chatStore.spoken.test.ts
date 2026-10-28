// Voice mode's send path (docs/tasks/voice-mode-android.md), after iOS
// `VoiceModeSendTests`.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { advance, json, makeHarness, turn, useFakeClock, type Harness } from "./testSupport";

let h: Harness;
beforeEach(() => useFakeClock());
afterEach(() => {
  h?.store.stop();
  vi.useRealTimers();
});

const posts = () => h.server.to("POST /v1/message");

describe("sendSpoken", () => {
  it("marks a spoken send spoken and an ordinary one not, and names the reply's bubble", async () => {
    h = makeHarness();
    h.server.always("POST /v1/message", json({ turn_id: "turn-1" }));
    h.server.always("GET /v1/turn", turn({ status: "done", result_text: "Ok." }));
    const placeholder = h.store.sendSpoken("  what's on today ");
    expect(placeholder).not.toBeNull();
    await advance(5_000);
    await h.store.send("thanks");
    await advance(5_000);
    expect(posts().map((p) => p.body.spoken)).toEqual([true, undefined]);
    expect(posts()[0].body.text).toBe("what's on today");
    const reply = h.store.getState().messages.find((m) => m.id === placeholder);
    expect(reply?.role).toBe("assistant");
    expect(reply?.status).toBe("done");
  });

  it("sends nothing for nothing", () => {
    h = makeHarness();
    expect(h.store.sendSpoken("   ")).toBeNull();
    expect(posts()).toHaveLength(0);
  });

  it("keeps the spoken flag on an automatic retry", async () => {
    h = makeHarness();
    h.server.script("POST /v1/message", json({ detail: "busy" }, 503), json({ turn_id: "turn-1" }));
    h.server.always("GET /v1/turn", turn({ status: "done", result_text: "Ok." }));
    h.store.sendSpoken("hi");
    await advance(30_000);
    expect(posts().map((p) => p.body.spoken)).toEqual([true, true]);
  });

  it("hands on the raw stream while the reply runs", async () => {
    h = makeHarness();
    h.server.always("POST /v1/message", json({ turn_id: "turn-1" }));
    h.server.script("GET /v1/turn", turn({ partial_text: "Half a", partial_text_marks: [3] }));
    h.server.always("GET /v1/turn", turn({ status: "done", result_text: "Half a reply." }));
    const seen: [string, string, number[]][] = [];
    const off = h.store.onReplyProgress((id, text, marks) => seen.push([id, text, marks]));
    const placeholder = h.store.sendSpoken("hi");
    await advance(10_000);
    expect(seen[0]).toEqual([placeholder, "Half a", [3]]);
    off();
  });
});
