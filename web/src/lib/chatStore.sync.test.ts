import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RETENTION_MS, TEXT } from "./chatStore";
import {
  advance,
  after,
  badJson,
  flush,
  Gate,
  hang,
  json,
  makeHarness,
  MemPersistence,
  msg,
  netFail,
  page,
  row,
  stallBody,
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
const tx = () => h.server.to("GET /v1/transcript");
const finished = () => h.events.filter((e) => e.kind === "finished");
const cursorOf = (latestSeq: number, hasOlderOnServer = false) => ({ latestSeq, hasOlderOnServer });
const seqs = () => state().messages.map((m) => m.seq);

describe("SYN-1 bootstrap and catch-up", () => {
  it("bootstrap: reverses the newest page to ascending, cursor = max row seq (not latest_seq), records has_more, announces nothing", async () => {
    h = makeHarness();
    h.server.script(
      "GET /v1/transcript",
      page(
        [
          row({ seq: 5, turn_id: "t3", text: "newest answer" }),
          row({ seq: 4, role: "user", turn_id: "t3", text: "q3" }),
          row({ seq: 3, turn_id: "t2", text: "older answer" }),
        ],
        { latest: 99, hasMore: true },
      ),
    );
    expect(state().awaitingFirstFetch).toBe(true);
    await h.store.start();
    await flush();
    expect(tx()[0].query.has("after")).toBe(false);
    expect(tx()[0].query.get("limit")).toBe("200");
    expect(seqs()).toEqual([3, 4, 5]);
    expect(state().hasOlderOnServer).toBe(true);
    expect(state().awaitingFirstFetch).toBe(false);
    expect(h.persist.cursor).toEqual(cursorOf(5, true));
    expect(finished()).toEqual([]);
    expect(h.events).toEqual([]);
    // The next ask is past the row cursor, not latest_seq.
    await advance(15_000);
    expect(tx()[1].query.get("after")).toBe("5");
  });

  it("an empty bootstrap page falls back to latest_seq for the cursor", async () => {
    h = makeHarness();
    h.server.script("GET /v1/transcript", page([], { latest: 42 }));
    await h.store.start();
    await flush();
    expect(h.persist.cursor?.latestSeq).toBe(42);
  });

  it("catch-up: follows has_more with after=<last row seq>", async () => {
    h = makeHarness({ persist: new MemPersistence({ cursor: cursorOf(10) }) });
    h.server.script(
      "GET /v1/transcript",
      page([row({ seq: 11 }), row({ seq: 12 })], { hasMore: true, latest: 13 }),
      page([row({ seq: 13 })], { latest: 13 }),
    );
    await h.store.start();
    await flush();
    expect(tx().map((r) => r.query.get("after"))).toEqual(["10", "12"]);
    expect(seqs()).toEqual([11, 12, 13]);
    expect(h.persist.cursor?.latestSeq).toBe(13);
    // Catch-up announces: the completed replies are queued for feedback.
    expect(finished().length).toBeGreaterThan(0);
  });

  it("a page that makes no progress stops the pass even with has_more true", async () => {
    h = makeHarness({ persist: new MemPersistence({ cursor: cursorOf(10) }) });
    h.server.always("GET /v1/transcript", page([], { hasMore: true }));
    await h.store.start();
    await advance(10_000);
    expect(tx()).toHaveLength(1);
    // A page of already-held rows is no progress either.
    h.store.stop();
    h = makeHarness({ persist: new MemPersistence({ cursor: cursorOf(10) }) });
    h.server.always("GET /v1/transcript", page([row({ seq: 10 })], { hasMore: true }));
    await h.store.start();
    await advance(10_000);
    expect(tx()).toHaveLength(1);
  });

  it("nothing is applied (and the cursor does not move) if stopped mid-fetch", async () => {
    h = makeHarness();
    const gate = new Gate(page([row({ seq: 1 })]));
    h.server.script("GET /v1/transcript", gate.handler);
    await h.store.start();
    await flush();
    expect(gate.reached).toBe(true);
    h.store.stop();
    gate.release();
    await advance(1_000);
    expect(state().messages).toEqual([]);
    expect(h.persist.cursor?.latestSeq ?? null).toBe(null);

    // The next launch asks past the same absent cursor and gets the row.
    const next = makeHarness({ persist: h.persist });
    next.server.script("GET /v1/transcript", page([row({ seq: 1 })]));
    await next.store.start();
    await flush();
    expect(next.store.getState().messages).toHaveLength(1);
    next.store.stop();
  });

  it("a sync started while another is in flight waits for it rather than being skipped", async () => {
    h = makeHarness();
    const gate = new Gate(page([]));
    h.server.script("GET /v1/transcript", gate.handler, page([row({ seq: 1, turn_id: "cron-1" })]));
    await h.store.start();
    await flush();
    expect(gate.reached).toBe(true);
    h.store.restartLoop(); // the successor
    await flush();
    expect(tx()).toHaveLength(1); // it is waiting, not racing
    gate.release();
    await advance(100);
    expect(tx()).toHaveLength(2);
    expect(state().messages).toHaveLength(1);
  });

  it("a synced row is persisted immediately", async () => {
    h = makeHarness();
    h.server.script("GET /v1/transcript", page([row({ seq: 1, id: "ROW-1" })]));
    await h.store.start();
    await flush();
    expect(h.persist.messages.get("row-1")).toMatchObject({ seq: 1 });
  });

  it("our own send is confirmed by its row, not duplicated", async () => {
    h = makeHarness({ persist: new MemPersistence({ cursor: cursorOf(1) }) });
    await h.store.start();
    h.server.script("POST /v1/message", json({ turn_id: "tU" }));
    await h.store.send("mine");
    await flush();
    const [user, ph] = state().messages;
    h.server.script(
      "GET /v1/transcript",
      page([
        row({ seq: 2, role: "user", id: user.id, client_msg_id: user.id, turn_id: "tU", text: "mine" }),
        row({ seq: 3, turn_id: "tU", text: "reply" }),
      ]),
    );
    await advance(15_000);
    expect(state().messages.map((m) => [m.id, m.seq])).toEqual([
      [user.id, 2],
      [ph.id, 3],
    ]);
    expect(state().messages[1]).toMatchObject({ status: "done", text: "reply" });
  });
});

describe("SYN-3 another device's turn", () => {
  it("a user row with no answer starts a poll for its turn; the assistant row arriving later resolves it and cancels the poll", async () => {
    h = makeHarness({ persist: new MemPersistence({ cursor: cursorOf(1) }) });
    h.server.script(
      "GET /v1/transcript",
      page([row({ seq: 2, role: "user", turn_id: "tx", text: "from my phone" })]),
      page([row({ seq: 3, turn_id: "tx", text: "answered elsewhere", answers: ["tx"] })]),
    );
    h.server.always("GET /v1/turn", turn({ turn_id: "tx" }));
    await h.store.start();
    await flush();
    expect(state().messages.map((m) => [m.role, m.status])).toEqual([
      ["user", "done"],
      ["assistant", "running"],
    ]);
    await advance(1_000);
    expect(h.server.to("GET /v1/turn").every((r) => r.path === "/v1/turn/tx")).toBe(true);
    expect(h.server.to("GET /v1/turn").length).toBeGreaterThan(0);

    await advance(14_000); // the next sync pass delivers the reply
    expect(state().messages).toHaveLength(2);
    expect(state().messages[1]).toMatchObject({ status: "done", text: "answered elsewhere", seq: 3 });
    expect(finished()).toEqual([{ kind: "finished", turnIds: ["tx"] }]);
    const n = h.server.to("GET /v1/turn").length;
    await advance(60_000);
    expect(h.server.to("GET /v1/turn")).toHaveLength(n); // poll cancelled
    expect(h.store.getStream(state().messages[1].id)).toBeUndefined();
  });

  it("a turn already answered when it is synced starts no poll", async () => {
    h = makeHarness({ persist: new MemPersistence({ cursor: cursorOf(1) }) });
    h.server.script(
      "GET /v1/transcript",
      page([row({ seq: 2, role: "user", turn_id: "tx" }), row({ seq: 3, turn_id: "tx" })]),
    );
    await h.store.start();
    await advance(5_000);
    expect(h.server.count("GET /v1/turn")).toBe(0);
    expect(state().messages).toHaveLength(2);
  });
});

describe("SYN-6 persistence", () => {
  it("writes only the messages that changed, deletes removed ids, never per typewriter tick", async () => {
    h = makeHarness({
      persist: new MemPersistence({
        messages: [msg({ id: "a", seq: 1 }), msg({ id: "b", role: "assistant", seq: 2 }), msg({ id: "c", seq: 3 })],
        cursor: cursorOf(3),
      }),
    });
    h.server.always("GET /v1/turn", turn({ partial_text: "streaming text ".repeat(60) }));
    await h.store.start();
    await flush();
    h.persist.saves.length = 0;

    await h.store.send("new");
    await flush();
    // The send wrote exactly its two new messages; nothing already stored.
    const [user, ph] = h.store.getState().messages.slice(-2);
    expect(h.persist.saves[0].put.map((m) => m.id)).toEqual([user.id, ph.id]);
    // The POST answer rewrote only the placeholder.
    expect(h.persist.saves[1].put.map((m) => m.id)).toEqual([ph.id]);
    expect(h.persist.saves[1].put[0]).toMatchObject({ status: "running", turnId: "turn-1" });

    // Typing for seconds writes nothing.
    await advance(500);
    const saves = h.persist.saves.length;
    await advance(1_500);
    expect(h.store.getStream(ph.id)?.text?.length).toBeGreaterThan(0);
    expect(h.persist.saves.length).toBe(saves);

    // A removed message is deleted from the store.
    h.server.always("GET /v1/turn", turn({ status: "merged" }));
    await advance(2_000);
    expect(h.persist.saves.some((s) => s.remove.includes(ph.id))).toBe(true);
    expect(h.persist.messages.has(ph.id)).toBe(false);
    expect(h.persist.messages.has(user.id)).toBe(true);
    expect(h.persist.putCount).toBeLessThan(8);
  });

  it("on start drops server-backed messages older than 30 days and keeps local-only ones", async () => {
    const old = Date.now() - RETENTION_MS - 60_000;
    h = makeHarness({
      persist: new MemPersistence({
        messages: [
          msg({ id: "old-server", seq: 1, createdAt: old }),
          msg({ id: "old-local", createdAt: old, text: "my only copy" }),
          msg({ id: "new-server", seq: 2, createdAt: Date.now() - 60_000 }),
        ],
        cursor: cursorOf(2),
      }),
    });
    await h.store.start();
    expect(state().messages.map((m) => m.id).sort()).toEqual(["new-server", "old-local"]);
    // And gone from storage, not re-dropped on every load while it grows.
    await h.store.idle();
    expect([...h.persist.messages.keys()].sort()).toEqual(["new-server", "old-local"]);
    // The dropped row may still be on the server: "Show earlier" can reach it.
    expect(state().hasOlderOnServer).toBe(true);
  });

  it("restores the transcript across a restart, in order, with the cursor", async () => {
    h = makeHarness();
    h.server.script("GET /v1/transcript", page([row({ seq: 2, role: "assistant", turn_id: "t1" }), row({ seq: 1, role: "user", turn_id: "t1" })]));
    await h.store.start();
    await flush();
    h.store.stop();
    const again = makeHarness({ persist: h.persist });
    await again.store.start();
    expect(again.store.getState().messages.map((m) => m.seq)).toEqual([1, 2]);
    expect(again.store.getState().awaitingFirstFetch).toBe(false);
    await flush();
    expect(again.server.to("GET /v1/transcript")[0].query.get("after")).toBe("2");
    again.store.stop();
  });

  it("an unreadable cache starts empty and bootstraps", async () => {
    const persist = new MemPersistence({ messages: [msg({ id: "x", seq: 1 })], cursor: cursorOf(7) });
    persist.failLoad = true;
    h = makeHarness({ persist });
    h.server.script("GET /v1/transcript", page([row({ seq: 1 })]));
    await h.store.start();
    await flush();
    expect(tx()[0].query.has("after")).toBe(false);
    expect(state().messages).toHaveLength(1);
    expect(state().awaitingFirstFetch).toBe(false);
  });

  it("a record that is not a plausible message is skipped, the rest of the cache survives", async () => {
    const persist = new MemPersistence({ messages: [msg({ id: "ok", seq: 1 })], cursor: cursorOf(1) });
    persist.messages.set("bad", { id: "bad", role: "user", text: "x", createdAt: 1, status: "from-the-future" } as never);
    h = makeHarness({ persist });
    await h.store.start();
    expect(state().messages.map((m) => m.id)).toEqual(["ok"]);
  });

  it("an empty cache reads as awaiting the first fetch; a cached transcript never does", async () => {
    h = makeHarness();
    h.server.script("GET /v1/transcript", after(1_000, page([row({ seq: 1 })])));
    await h.store.start();
    expect(state().awaitingFirstFetch).toBe(true);
    await advance(1_000);
    expect(state().awaitingFirstFetch).toBe(false);
    h.store.stop();

    h = makeHarness({ persist: new MemPersistence({ messages: [msg({ id: "a", seq: 1 })], cursor: cursorOf(1) }) });
    await h.store.start();
    expect(state().awaitingFirstFetch).toBe(false);
  });
});

describe("SYN-7 attachments", () => {
  const withImage = row({ seq: 2, role: "user", turn_id: "tp", attachments: [{ id: "att-1", mime: "image/jpeg" }] });
  const answer = row({ seq: 3, turn_id: "tp" });

  it("fetches every image of a multi-image row and keeps their order", async () => {
    const multi = row({
      seq: 2, role: "user", turn_id: "tp",
      attachments: [{ id: "a-1", mime: "image/jpeg" }, { id: "a-2", mime: "image/jpeg" }, { id: "a-3", mime: "image/jpeg" }],
    });
    h = makeHarness({ persist: new MemPersistence({ cursor: cursorOf(1) }) });
    h.server.script("GET /v1/transcript", page([multi, answer]));
    h.server.always("GET /v1/transcript/attachment", async () => new Response(new Uint8Array([1])));
    await h.store.start();
    await advance(1_000);
    expect(h.server.count("GET /v1/transcript/attachment")).toBe(3);
    const m = state().messages.find((x) => x.attachmentIds);
    expect(m?.attachmentIds).toEqual(["a-1", "a-2", "a-3"]);
    expect([...(m?.imageKeys ?? [])].sort()).toEqual(["att:a-1", "att:a-2", "att:a-3"]);
  });

  it("is fetched once per load even if it fails, not every 15s", async () => {
    h = makeHarness({ persist: new MemPersistence({ cursor: cursorOf(1) }) });
    h.server.script("GET /v1/transcript", page([withImage, answer]));
    h.server.always("GET /v1/transcript/attachment", json({}, 500));
    await h.store.start();
    await advance(75_000);
    expect(h.server.count("GET /v1/transcript/attachment")).toBe(1);
    expect(h.server.to("GET /v1/transcript/attachment")[0].path).toBe("/v1/transcript/attachment/att-1");
    expect(h.server.to("GET /v1/transcript/attachment")[0].headers.Authorization).toBe("Bearer tok");
    expect(state().messages.find((m) => m.attachmentIds)?.imageKeys).toBeUndefined();

    // A new load tries it once more.
    h.store.stop();
    const again = makeHarness({ persist: h.persist });
    again.server.always("GET /v1/transcript/attachment", async () => new Response(new Uint8Array([1])));
    await again.store.start();
    await advance(1_000);
    expect(again.server.count("GET /v1/transcript/attachment")).toBe(1);
    expect(again.store.getState().messages.find((m) => m.attachmentIds)?.imageKeys).toEqual(["att:att-1"]);
    expect(again.persist.images.has("att:att-1")).toBe(true);
    again.store.stop();
  });
});

describe("WIN-3 show earlier", () => {
  it("fetches before=<oldest seq>, prepends, records has_more, announces nothing", async () => {
    h = makeHarness({
      persist: new MemPersistence({
        messages: [msg({ id: "a", seq: 11 }), msg({ id: "b", role: "assistant", seq: 12 })],
        cursor: cursorOf(12, true),
      }),
    });
    await h.store.start();
    await flush();
    expect(state().hasOlderOnServer).toBe(true);
    h.server.script(
      "GET /v1/transcript",
      page([row({ seq: 10, turn_id: "t10" }), row({ seq: 9, role: "user", turn_id: "t10" })], { hasMore: true }),
    );
    h.store.showEarlier();
    expect(state().loadingEarlier).toBe(true);
    await flush();
    const ask = tx().at(-1)!;
    expect(ask.query.get("before")).toBe("11");
    expect(ask.query.has("after")).toBe(false);
    expect(seqs()).toEqual([9, 10, 11, 12]);
    expect(state().loadingEarlier).toBe(false);
    expect(state().hasOlderOnServer).toBe(true);
    expect(h.events).toEqual([]);

    h.server.script("GET /v1/transcript", page([row({ seq: 8 })], { hasMore: false }));
    h.store.showEarlier();
    await flush();
    expect(tx().at(-1)!.query.get("before")).toBe("9");
    expect(state().hasOlderOnServer).toBe(false);
    // Nothing more on the server: a tap fetches nothing.
    const n = tx().length;
    h.store.showEarlier();
    await flush();
    expect(tx()).toHaveLength(n);
  });
});

describe("POL-7 the loop", () => {
  it("syncs first, then /health, every 15s", async () => {
    h = makeHarness();
    await h.store.start();
    await advance(30_000);
    const passes = h.server.requests.filter((r) => r.path === "/v1/transcript" || r.path === "/health");
    expect(passes.map((r) => [r.path, r.at - h.server.requests[0].at])).toEqual([
      ["/v1/transcript", 0],
      ["/health", 0],
      ["/v1/transcript", 15_000],
      ["/health", 15_000],
      ["/v1/transcript", 30_000],
      ["/health", 30_000],
    ]);
  });

  it("restartLoop() cancels the loop and runs a pass at once, leaving one loop", async () => {
    h = makeHarness();
    await h.store.start();
    await advance(5_000);
    const t = h.server.requests[0].at;
    h.store.restartLoop();
    await flush();
    expect(tx().map((r) => r.at - t)).toEqual([0, 5_000]);
    await advance(30_000);
    // Passes at 5s, 20s, 35s: the replaced loop never fires its 15s pass.
    expect(tx().map((r) => r.at - t)).toEqual([0, 5_000, 20_000, 35_000]);
  });

  it("restartLoop is a no-op after stop()", async () => {
    h = makeHarness();
    await h.store.start();
    await flush();
    h.store.stop();
    h.store.restartLoop();
    await advance(60_000);
    expect(tx()).toHaveLength(1);
  });
});

describe("STA-3 health", () => {
  it("reauthNeeded = !auth_ok; the prewarn is suppressed when reauth is needed", async () => {
    h = makeHarness();
    h.server.always("GET /health", json({ status: "ok", auth_ok: false, reauth_prewarn: true }));
    await h.store.start();
    await flush();
    expect(state()).toMatchObject({ reauthNeeded: true, reauthPrewarn: false });
  });

  it("keys on auth_ok, not on the status string", async () => {
    h = makeHarness();
    h.server.always("GET /health", json({ status: "reauth_needed", auth_ok: true }));
    await h.store.start();
    await flush();
    expect(state().reauthNeeded).toBe(false);
  });

  it("raises the prewarn when auth is fine, and queue_paused", async () => {
    h = makeHarness();
    h.server.always("GET /health", json({ status: "ok", auth_ok: true, reauth_prewarn: true, queue_paused: true }));
    await h.store.start();
    await flush();
    expect(state()).toMatchObject({ reauthNeeded: false, reauthPrewarn: true, queuePaused: true });
  });

  it("model and build are only overwritten when the server reports them", async () => {
    h = makeHarness();
    h.server.script(
      "GET /health",
      json({ status: "ok", auth_ok: true, model: "opus-x", git_branch: "main", git_commit: "abc123" }),
      json({ status: "ok", auth_ok: true }),
      json({ status: "ok", auth_ok: true, model: null }),
    );
    await h.store.start();
    await flush();
    expect(state()).toMatchObject({ serverModel: "opus-x", serverBuild: "main@abc123" });
    await advance(30_000);
    expect(state()).toMatchObject({ serverModel: "opus-x", serverBuild: "main@abc123" });
  });

  it("an unreachable health endpoint leaves banners as they were", async () => {
    h = makeHarness();
    h.server.script("GET /health", json({ status: "ok", auth_ok: false, queue_paused: true }), netFail());
    await h.store.start();
    await flush();
    expect(state()).toMatchObject({ reauthNeeded: true, queuePaused: true });
    await advance(15_000);
    expect(state()).toMatchObject({ reauthNeeded: true, queuePaused: true, connection: "unreachable" });
  });
});

describe("STA-4 / STA-5 connection", () => {
  it("shows `checking` only if the first pass is still running at 600ms (not at 599ms)", async () => {
    h = makeHarness();
    h.server.script("GET /v1/transcript", after(700, page([])));
    await h.store.start();
    await advance(599);
    expect(state().connection).toBe("idle");
    await advance(1);
    expect(state().connection).toBe("checking");
    await advance(100);
    expect(state().connection).toBe("idle");
    expect(h.connections).toEqual(["idle", "checking", "idle"]);
  });

  it("a first pass that settles at 599ms never flashes it", async () => {
    h = makeHarness();
    h.server.script("GET /v1/transcript", after(599, page([])));
    await h.store.start();
    await advance(5_000);
    expect(h.connections).toEqual(["idle"]);
  });

  it("a recurring pass never shows checking", async () => {
    h = makeHarness();
    await h.store.start();
    await advance(1_000);
    h.server.script("GET /v1/transcript", after(5_000, page([])));
    await advance(30_000);
    expect(h.server.count("GET /v1/transcript")).toBe(2);
    expect(h.connections).toEqual(["idle"]);
  });

  it("an announced pass after restartLoop shows checking again", async () => {
    h = makeHarness();
    await h.store.start();
    await advance(1_000);
    h.server.script("GET /v1/transcript", after(2_000, page([])));
    h.store.restartLoop();
    await advance(700);
    expect(state().connection).toBe("checking");
    await advance(2_000);
    expect(state().connection).toBe("idle");
  });

  it("a transport failure is unreachable; it is sticky across the gap, a new pass never downgrades it to checking, and a later good pass clears it", async () => {
    h = makeHarness();
    h.server.script("GET /health", netFail(), netFail(), netFail(), netFail(), netFail());
    await h.store.start();
    await advance(4_000); // passes at 0-3s fall inside the STA-8 grace; the one at 4s reports
    expect(state().connection).toBe("unreachable");
    await advance(14_000); // the quiet gap
    expect(state().connection).toBe("unreachable");

    // The network is back but the next pass is slow: still unreachable, never checking.
    h.server.script("GET /v1/transcript", after(2_000, page([])));
    await advance(1_000); // pass begins at 19s
    await advance(1_000);
    expect(state().connection).toBe("unreachable");
    h.store.restartLoop();
    h.server.script("GET /v1/transcript", after(2_000, page([])));
    await advance(1_000);
    expect(state().connection).toBe("unreachable");
    await advance(2_000);
    expect(state().connection).toBe("idle");
    expect(h.connections).toEqual(["idle", "checking", "unreachable", "idle"]);
  });

  it("STA-8: a loop run keeps a transport failure to itself for the grace, retrying each second behind the spinner", async () => {
    h = makeHarness();
    // A wake from Doze: the tailnet is back on the third pass.
    h.server.script("GET /health", netFail(), netFail());
    await h.store.start();
    await advance(700);
    expect(state().connection).toBe("checking");
    await advance(1_000);
    expect(state().connection).toBe("checking");
    await advance(1_000);
    expect(state().connection).toBe("idle");
    expect(h.connections).toEqual(["idle", "checking", "idle"]);
    expect(h.server.to("GET /health")).toHaveLength(3);
  });

  it("STA-8: the grace is per loop run, and a failure that outlasts it is reported", async () => {
    h = makeHarness();
    await h.store.start();
    await flush();
    expect(state().connection).toBe("idle");
    h.server.always("GET /health", netFail());
    await advance(15_000); // a recurring pass, long after the run's grace
    expect(state().connection).toBe("unreachable");

    h.server.always("GET /health", json({ status: "ok", auth_ok: true }));
    await advance(15_000);
    expect(state().connection).toBe("idle");
    h.server.always("GET /health", netFail());
    h.store.restartLoop(); // foreground: a fresh run, a fresh grace
    await advance(3_000);
    expect(state().connection).toBe("checking");
    await advance(1_000);
    expect(state().connection).toBe("unreachable");
  });

  it("a transcript transport failure is unreachable too, and a timeout we raised counts", async () => {
    h = makeHarness({ persist: new MemPersistence({ cursor: cursorOf(1) }) });
    h.server.script("GET /v1/transcript", hang());
    await h.store.start();
    await advance(20_000); // the Api's own 20s bound
    expect(state().connection).toBe("unreachable");
  });

  it("a body that stalls after the headers times out too (WEB-2 covers the whole response)", async () => {
    h = makeHarness({ persist: new MemPersistence({ cursor: cursorOf(1) }) });
    h.server.script("GET /v1/transcript", stallBody());
    await h.store.start();
    await advance(20_000);
    expect(state().connection).toBe("unreachable");
  });

  it("a 500 is not unreachable: it is `trouble`", async () => {
    h = makeHarness();
    h.server.always("GET /health", json({ detail: "boom" }, 500));
    await h.store.start();
    await flush();
    expect(state().connection).toBe("trouble");
    expect(h.connections).not.toContain("unreachable");
  });

  it("a 500 from the transcript, or a body that will not decode, is `trouble`", async () => {
    h = makeHarness();
    h.server.always("GET /v1/transcript", json({ detail: "boom" }, 500));
    await h.store.start();
    await flush();
    expect(state().connection).toBe("trouble");
    h.store.stop();

    h = makeHarness();
    h.server.always("GET /health", badJson());
    await h.store.start();
    await flush();
    expect(state().connection).toBe("trouble");
  });

  it("trouble clears when the server answers properly again", async () => {
    h = makeHarness();
    h.server.script("GET /health", json({}, 500));
    await h.store.start();
    await flush();
    expect(state().connection).toBe("trouble");
    await advance(15_000);
    expect(state().connection).toBe("idle");
  });

  it("our own cancel (restartLoop mid-request) is neither unreachable nor trouble", async () => {
    h = makeHarness();
    h.server.script("GET /v1/transcript", hang());
    await h.store.start();
    await advance(1_000);
    h.store.restartLoop(); // aborts the hanging fetch
    await advance(1_000);
    h.store.restartLoop();
    await advance(1_000);
    expect(h.connections.filter((c) => c === "unreachable" || c === "trouble")).toEqual([]);
    expect(state().connection).not.toBe("unreachable");
  });

  it("our own cancel (stop mid-request) is neither", async () => {
    for (const route of ["GET /v1/transcript", "GET /health"] as const) {
      h = makeHarness();
      h.server.script(route, hang());
      await h.store.start();
      await advance(1_000);
      h.store.stop();
      await advance(60_000);
      expect(h.connections.filter((c) => c === "unreachable" || c === "trouble")).toEqual([]);
      expect(state().connection).not.toBe("trouble");
    }
  });
});

void TEXT;

describe("usage reading (the >= 90% row's feed)", () => {
  const high = {
    plan: { observed_at: 1, status: "allowed", windows: { five_hour: { utilization: 0.93, resets_at: 4_000_000_000 } } },
  };
  const usage = () => h.server.to("GET /v1/usage");

  it("reads on the first pass, after health, and not again within the interval", async () => {
    h = makeHarness();
    h.server.always("GET /v1/usage", json(high));
    await h.store.start();
    await flush();
    expect(state().usageReport?.plan?.windows.five_hour.utilization).toBe(0.93);
    const order = h.server.requests.map((r) => r.path);
    expect(order.indexOf("/health")).toBeLessThan(order.indexOf("/v1/usage"));
    await advance(60_000); // four more health passes
    expect(h.server.to("GET /health").length).toBeGreaterThanOrEqual(5);
    expect(usage()).toHaveLength(1);
  });

  it("reads again once the interval has passed", async () => {
    h = makeHarness();
    await h.store.start();
    await flush();
    await advance(300_000);
    expect(usage()).toHaveLength(2);
  });

  it("retries a failed first read on the next pass, not after the interval", async () => {
    h = makeHarness();
    h.server.script("GET /v1/usage", json({ detail: "boom" }, 500));
    h.server.always("GET /v1/usage", json(high));
    await h.store.start();
    await flush();
    expect(state().usageReport).toBeNull();
    await advance(15_000);
    expect(state().usageReport?.plan?.windows.five_hour.utilization).toBe(0.93);
  });

  it("keeps the old reading on failure, and a failure is not unreachable", async () => {
    h = makeHarness({ policy: { usageInterval: 0 } });
    h.server.always("GET /v1/usage", json(high));
    await h.store.start();
    await flush();
    h.server.always("GET /v1/usage", netFail());
    await advance(30_000);
    expect(usage().length).toBeGreaterThanOrEqual(3);
    expect(state().usageReport?.plan?.windows.five_hour.utilization).toBe(0.93);
    expect(state().connection).toBe("idle");
  });

  it("each loop run reads on its first pass", async () => {
    h = makeHarness();
    await h.store.start();
    await flush();
    expect(usage()).toHaveLength(1);
    h.store.restartLoop();
    await flush();
    expect(usage()).toHaveLength(2);
  });
});
