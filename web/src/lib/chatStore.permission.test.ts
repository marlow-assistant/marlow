import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TEXT } from "./chatStore";
import { advance, Gate, json, makeHarness, useFakeClock, type Harness } from "./testSupport";

let h: Harness;
beforeEach(() => useFakeClock());
afterEach(() => {
  h?.store.stop();
  vi.useRealTimers();
});

const state = () => h.store.getState();
const nowS = () => Date.now() / 1000;
const req = (id: string, expiresIn: number) => ({
  request_id: id,
  tool: "Bash",
  tool_input: { command: "ls" },
  created_at: nowS(),
  expires_at: nowS() + expiresIn,
});
const list = (...r: ReturnType<typeof req>[]) => json({ permission_requests: r });

/** A turn polled in the fast phase from t=2s (no burst), so permissions are checked. */
async function inFastPhase(perms?: () => void) {
  h = makeHarness({ policy: { burstAttempts: 0 } });
  perms?.();
  await h.store.send("do it");
}

describe("PRM-1 publishing", () => {
  it("publishes the first unexpired request", async () => {
    await inFastPhase(() =>
      h.server.always("GET /v1/permissions", list(req("old", -5), req("first", 60), req("second", 60))),
    );
    await advance(2_000);
    expect(state().pendingPermission?.request_id).toBe("first");
  });

  it("never publishes an expired one", async () => {
    await inFastPhase(() => h.server.always("GET /v1/permissions", list(req("old", -1))));
    await advance(10_000);
    expect(state().pendingPermission).toBeNull();
  });

  it("clears a request that disappears from the poll", async () => {
    await inFastPhase(() => h.server.script("GET /v1/permissions", list(req("r1", 600))));
    await advance(2_000);
    expect(state().pendingPermission?.request_id).toBe("r1");
    await advance(2_000);
    expect(state().pendingPermission).toBeNull();
  });

  it("a failed permissions fetch does not disturb turn polling", async () => {
    await inFastPhase(() => {
      h.server.always("GET /v1/permissions", json({}, 500));
      h.server.always("GET /v1/turn", json({ turn_id: "turn-1", status: "done", result_text: "ok" }));
    });
    await advance(2_000);
    expect(state().messages[1]).toMatchObject({ status: "done", text: "ok" });
  });

  it("the slow phase does not check permissions", async () => {
    await inFastPhase(() => h.server.always("GET /v1/permissions", list(req("r1", 99_999))));
    await advance(2_000 * 150 + 30_000 * 3);
    const before = h.server.count("GET /v1/permissions");
    await advance(300_000);
    expect(h.server.count("GET /v1/permissions")).toBe(before);
  });
});

describe("PRM-3 answering", () => {
  it("posts the decision and clears the prompt at once", async () => {
    await inFastPhase(() => h.server.script("GET /v1/permissions", list(req("r1", 600))));
    await advance(2_000);
    const answered = h.store.answerPermission("allow");
    expect(state().pendingPermission?.request_id).toBe("r1");
    await answered;
    expect(state().pendingPermission).toBeNull();
    const post = h.server.to("POST /v1/permission")[0];
    expect(post.path).toBe("/v1/permission/r1");
    expect(post.body).toEqual({ decision: "allow" });
    expect(state().notice).toBeNull();
  });

  it("a permissions GET already in flight when the answer lands does not resurrect it (generation counter)", async () => {
    await inFastPhase(() => {
      h.server.script("GET /v1/permissions", list(req("r1", 600)));
      const stale = new Gate(list(req("r1", 600)));
      h.server.script("GET /v1/permissions", stale.handler);
      (h as unknown as { stale: Gate }).stale = stale;
    });
    await advance(2_000);
    expect(state().pendingPermission?.request_id).toBe("r1");
    await advance(2_000); // the second GET starts and is held
    const stale = (h as unknown as { stale: Gate }).stale;
    expect(stale.reached).toBe(true);
    await h.store.answerPermission("deny");
    expect(state().pendingPermission).toBeNull();
    stale.release(); // answered "r1 is pending", from before the answer
    await advance(100);
    expect(state().pendingPermission).toBeNull();
  });

  it("accepted:false shows the moved-on notice and still clears", async () => {
    await inFastPhase(() => {
      h.server.script("GET /v1/permissions", list(req("r1", 600)));
      h.server.script("POST /v1/permission", json({ accepted: false }));
    });
    await advance(2_000);
    await h.store.answerPermission("allow");
    expect(state().notice).toBe(TEXT.movedOn);
    expect(state().pendingPermission).toBeNull();
  });

  it("a failed POST leaves the prompt up", async () => {
    await inFastPhase(() => {
      h.server.script("GET /v1/permissions", list(req("r1", 600)));
      h.server.script("POST /v1/permission", json({ detail: "x" }, 500));
    });
    await advance(2_000);
    await h.store.answerPermission("allow");
    expect(state().pendingPermission?.request_id).toBe("r1");
    expect(state().notice).toBeNull();
  });

  it("an answer aimed at a request a poll has since replaced is dropped, not applied to the new one", async () => {
    await inFastPhase(() => {
      h.server.script("GET /v1/permissions", list(req("A", 60)));
      h.server.always("GET /v1/permissions", list(req("B", 60)));
    });
    await advance(2_000);
    expect(state().pendingPermission?.request_id).toBe("A");
    await advance(2_000);
    expect(state().pendingPermission?.request_id).toBe("B");
    await h.store.answerPermission("allow", "A");
    expect(h.server.to("POST /v1/permission")).toHaveLength(0);
    expect(state().pendingPermission?.request_id).toBe("B");
  });

  it("answering with nothing pending does nothing", async () => {
    h = makeHarness();
    await h.store.answerPermission("allow");
    expect(h.server.count("POST /v1/permission")).toBe(0);
  });

  it("expirePermission clears the matching prompt only", async () => {
    await inFastPhase(() => h.server.script("GET /v1/permissions", list(req("r1", 600))));
    await advance(2_000);
    h.store.expirePermission("other");
    expect(state().pendingPermission?.request_id).toBe("r1");
    h.store.expirePermission("r1");
    expect(state().pendingPermission).toBeNull();
  });
});

describe("STA-6 notices", () => {
  it("clear after 3s", async () => {
    h = makeHarness();
    h.store.showNotice("one");
    await advance(2_999);
    expect(state().notice).toBe("one");
    await advance(1);
    expect(state().notice).toBeNull();
  });

  it("a second notice is not cleared early by the first's timer", async () => {
    h = makeHarness();
    h.store.showNotice("one");
    await advance(2_000);
    h.store.showNotice("one"); // same text: identify the timer, not the text
    await advance(1_500); // the first timer's 3s mark
    expect(state().notice).toBe("one");
    await advance(1_500);
    expect(state().notice).toBeNull();
  });
});

describe("the approval summary and Allow similar", () => {
  it("publishes the server's summary with the request", async () => {
    await inFastPhase(() =>
      h.server.script(
        "GET /v1/permissions",
        json({
          permission_requests: [
            {
              ...req("r1", 600),
              summary: {
                title: "Search your notes for “Percé”",
                preview: [{ op: "text", text: "grep -rn Percé notes/" }, { op: "weird", text: "x" }],
                marlow_says: "Looking for the trip notes",
                similar: { label: "other grep searches" },
              },
            },
          ],
        }),
      ),
    );
    await advance(2_000);
    expect(state().pendingPermission?.summary).toEqual({
      title: "Search your notes for “Percé”",
      preview: [
        { op: "text", text: "grep -rn Percé notes/" },
        { op: "text", text: "x" },
      ],
      marlowSays: "Looking for the trip notes",
      similar: { label: "other grep searches" },
    });
  });

  it("an older server's request has no summary", async () => {
    await inFastPhase(() => h.server.script("GET /v1/permissions", list(req("r1", 600))));
    await advance(2_000);
    expect(state().pendingPermission?.summary).toBeNull();
  });

  it("posts allow_similar and clears at once", async () => {
    await inFastPhase(() => h.server.script("GET /v1/permissions", list(req("r1", 600))));
    await advance(2_000);
    await h.store.answerPermission("allow_similar");
    expect(h.server.to("POST /v1/permission")[0].body).toEqual({ decision: "allow_similar" });
    expect(state().pendingPermission).toBeNull();
  });
});
