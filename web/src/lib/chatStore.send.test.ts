import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TEXT } from "./chatStore";
import {
  advance,
  badJson,
  flush,
  hang,
  json,
  makeHarness,
  MemPersistence,
  msg,
  netFail,
  page,
  placeholderOf,
  row,
  turn,
  TZ,
  useFakeClock,
  userIdOf,
  type Harness,
} from "./testSupport";

let h: Harness;
beforeEach(() => useFakeClock());
afterEach(() => {
  h?.store.stop();
  vi.useRealTimers();
});

const state = () => h.store.getState();
const posts = () => h.server.to("POST /v1/message");

describe("SND-1 optimistic send", () => {
  it("puts the user message and the placeholder in messages before any fetch; client_msg_id is the user id; fires the sent event", async () => {
    h = makeHarness();
    let seenAtFetch: typeof state extends () => infer S ? S : never = undefined as never;
    h.server.script("POST /v1/message", (req) => {
      seenAtFetch = state();
      return json({ turn_id: "turn-1" })(req);
    });
    expect(await h.store.send("  hello  ")).toBe(true);
    // The fetch happened synchronously inside send(), and both bubbles were
    // already there.
    expect(seenAtFetch.messages.map((m) => [m.role, m.status, m.text])).toEqual([
      ["user", "done", "hello"],
      ["assistant", "sending", ""],
    ]);
    const [user, placeholder] = state().messages;
    expect(posts()[0].body.client_msg_id).toBe(user.id);
    expect(placeholder.clientMsgId).toBe(user.id);
    expect(posts()[0].body.text).toBe("hello");
    expect(h.events[0]).toEqual({ kind: "sent", userId: user.id, placeholderId: placeholder.id });
  });

  it("ignores an empty draft", async () => {
    h = makeHarness();
    expect(await h.store.send("   ")).toBe(false);
    expect(state().messages).toEqual([]);
    expect(posts()).toHaveLength(0);
  });
});

describe("SND-2 per-attempt timeout", () => {
  it("times an attempt that never resolves out at 20s and RETRIES it, not terminal", async () => {
    h = makeHarness();
    h.server.script("POST /v1/message", hang());
    await h.store.send("hi");
    await advance(19_999);
    expect(posts()).toHaveLength(1);
    await advance(1); // 20s: the attempt is aborted as a TransportError
    expect(placeholderOf(h).status).toBe("sending");
    // 4s of backoff, then the second attempt.
    await advance(3_999);
    expect(posts()).toHaveLength(1);
    await advance(1);
    expect(posts()).toHaveLength(2);
    expect(posts()[1].at - posts()[0].at).toBe(24_000);
    expect(placeholderOf(h).status).toBe("running");
  });

  it("applies the same 20s bound to an image send", async () => {
    h = makeHarness();
    h.server.script("POST /v1/message", hang());
    await h.store.send("hi", [new Blob([new Uint8Array([1, 2, 3])])]);
    await advance(24_000);
    expect(posts()).toHaveLength(2);
  });
});

describe("SND-3 backoff and attempt cap", () => {
  it("sleeps 4s then 8s..., makes 8 attempts, stays sending throughout, and ends in error with the exhausted text (~52s)", async () => {
    h = makeHarness();
    h.server.always("POST /v1/message", netFail());
    await h.store.send("hi");
    const t0 = posts()[0].at;
    // Sit just before the last attempt: still `sending`.
    await advance(51_999);
    expect(posts()).toHaveLength(7);
    expect(placeholderOf(h).status).toBe("sending");
    await advance(1);
    expect(posts()).toHaveLength(8);
    expect(posts().map((r) => r.at - t0)).toEqual([0, 4_000, 12_000, 20_000, 28_000, 36_000, 44_000, 52_000]);
    expect(placeholderOf(h)).toMatchObject({ status: "error", text: TEXT.sendExhausted });
    // No ninth.
    await advance(120_000);
    expect(posts()).toHaveLength(8);
  });

  it("says the server failed when the last failure was a 5xx", async () => {
    h = makeHarness();
    h.server.always("POST /v1/message", json({ detail: "boom" }, 503));
    await h.store.send("hi");
    await advance(52_000);
    expect(posts()).toHaveLength(8);
    expect(placeholderOf(h).status).toBe("error");
    expect(placeholderOf(h).text).toContain("503");
  });
});

describe("SND-4 transient vs permanent", () => {
  it.each([400, 401, 413, 422])("a %i fails at once with the server's detail", async (status) => {
    h = makeHarness();
    h.server.always("POST /v1/message", json({ detail: "too big for me" }, status));
    await h.store.send("hi");
    await flush();
    expect(posts()).toHaveLength(1);
    expect(placeholderOf(h).status).toBe("error");
    expect(placeholderOf(h).text).toContain("too big for me");
    await advance(60_000);
    expect(posts()).toHaveLength(1);
  });

  it("retries a 503", async () => {
    h = makeHarness();
    h.server.script("POST /v1/message", json({ detail: "busy" }, 503));
    await h.store.send("hi");
    await advance(4_000);
    expect(posts()).toHaveLength(2);
    expect(placeholderOf(h).status).toBe("running");
  });

  it("retries a transport failure until it lands", async () => {
    h = makeHarness();
    h.server.script("POST /v1/message", netFail(), netFail());
    await h.store.send("hi");
    await advance(12_000);
    expect(posts()).toHaveLength(3);
    expect(placeholderOf(h)).toMatchObject({ status: "running", turnId: "turn-1" });
  });

  it("treats a 2xx with an undecodable body as permanent (the server did answer)", async () => {
    h = makeHarness();
    h.server.script("POST /v1/message", badJson());
    await h.store.send("hi");
    await flush();
    expect(posts()).toHaveLength(1);
    expect(placeholderOf(h).status).toBe("error");
  });
});

describe("SND-5 after the POST", () => {
  it("gives the placeholder its turn_id, marks it running, and persists it", async () => {
    h = makeHarness();
    h.server.script("POST /v1/message", json({ turn_id: "turn-9" }));
    await h.store.send("hi");
    await flush();
    expect(placeholderOf(h)).toMatchObject({ turnId: "turn-9", status: "running" });
    const saved = h.persist.messages.get(placeholderOf(h).id);
    expect(saved).toMatchObject({ turnId: "turn-9", status: "running" });
  });

  it("stop() during a backoff sleep sends no further request", async () => {
    h = makeHarness();
    h.server.always("POST /v1/message", json({ detail: "busy" }, 503));
    await h.store.send("hi");
    await advance(1_000);
    h.store.stop();
    await advance(120_000);
    expect(posts()).toHaveLength(1);
    expect(placeholderOf(h).status).toBe("sending");
  });
});

describe("SND-6 retry", () => {
  async function failedSend() {
    h = makeHarness();
    h.server.script("POST /v1/message", json({ detail: "nope" }, 400));
    await h.store.send("say it again");
    await flush();
    expect(placeholderOf(h).status).toBe("error");
  }

  it("resends the same text under the same client_msg_id", async () => {
    await failedSend();
    const ph = placeholderOf(h).id;
    const user = userIdOf(h);
    h.store.retry(ph);
    expect(placeholderOf(h)).toMatchObject({ status: "sending", text: "" });
    await flush();
    expect(posts()).toHaveLength(2);
    expect(posts()[1].body).toMatchObject({ text: "say it again", client_msg_id: user });
    expect(placeholderOf(h).status).toBe("running");
  });

  it("clears seq and answers, so the retry's reply row confirms the placeholder instead of landing as a second bubble", async () => {
    // An error row the server already confirmed: it has a seq and answers.
    const user = msg({ id: "u1", text: "ask", seq: 1, turnId: "tA" });
    const err = msg({
      id: "p1",
      role: "assistant",
      text: "boom",
      status: "error",
      seq: 2,
      turnId: "tA",
      clientMsgId: "u1",
      answers: ["tA"],
    });
    h = makeHarness({ persist: new MemPersistence({ messages: [user, err], cursor: { latestSeq: 2, hasOlderOnServer: false } }) });
    await h.store.start();
    h.server.script("POST /v1/message", json({ turn_id: "tB" }));
    h.store.retry("p1");
    expect(state().messages.find((m) => m.id === "p1")).toMatchObject({ status: "sending", seq: undefined, answers: undefined });
    await flush();
    expect(posts()[0].body).toMatchObject({ text: "ask", client_msg_id: "u1" });
    // The reply row for the fresh turn arrives on the next sync pass.
    h.server.script("GET /v1/transcript", page([row({ seq: 3, turn_id: "tB", answers: ["tA"], text: "answer" })]));
    await advance(15_000);
    const assistants = state().messages.filter((m) => m.role === "assistant");
    expect(assistants).toHaveLength(1);
    expect(assistants[0]).toMatchObject({ id: "p1", text: "answer", status: "done", seq: 3 });
  });

  it("does nothing on a placeholder that is not in error, one with no findable user message, or an unknown id", async () => {
    h = makeHarness({
      persist: new MemPersistence({
        messages: [
          msg({ id: "u1", seq: 1 }),
          msg({ id: "done1", role: "assistant", status: "done", seq: 2, clientMsgId: "u1" }),
          msg({ id: "orphan", role: "assistant", status: "error", seq: 3, clientMsgId: "gone" }),
          msg({ id: "plain", role: "assistant", status: "error", seq: 4 }),
        ],
        cursor: { latestSeq: 4, hasOlderOnServer: false },
      }),
    });
    await h.store.start();
    await flush();
    for (const id of ["done1", "orphan", "plain", "missing", "u1"]) h.store.retry(id);
    await advance(1_000);
    expect(posts()).toHaveLength(0);
    expect(state().messages.find((m) => m.id === "orphan")!.status).toBe("error");
  });

  it("does nothing while the message is still sending or running", async () => {
    h = makeHarness();
    h.server.script("POST /v1/message", hang());
    await h.store.send("hi");
    h.store.retry(placeholderOf(h).id);
    expect(posts()).toHaveLength(1);
  });

  it("resends the stored image on retry", async () => {
    h = makeHarness();
    h.server.script("POST /v1/message", json({ detail: "nope" }, 400));
    await h.store.send("look", [new Blob([new Uint8Array([9, 8, 7])], { type: "image/jpeg" })]);
    await flush();
    expect(placeholderOf(h).status).toBe("error");
    h.store.retry(placeholderOf(h).id);
    await flush();
    expect(posts()[1].body.images).toEqual([{ b64: Buffer.from([9, 8, 7]).toString("base64"), mime: "image/jpeg" }]);
    expect(posts()[1].body.images).toEqual(posts()[0].body.images);
  });
});

describe("SND-7 reconcile on start", () => {
  const start = async (messages: ReturnType<typeof msg>[]) => {
    h = makeHarness({ persist: new MemPersistence({ messages, cursor: { latestSeq: 100, hasOlderOnServer: false } }) });
    await h.store.start();
    await flush();
  };

  it("resumes a running message with a turn id as a late joiner: the first slab shows whole", async () => {
    const full = "The quick brown fox jumps over the lazy dog. ".repeat(4);
    h = makeHarness({
      persist: new MemPersistence({
        messages: [
          msg({ id: "u1" }),
          msg({ id: "p1", role: "assistant", status: "running", turnId: "tR", clientMsgId: "u1", text: "stale label text" }),
        ],
        cursor: { latestSeq: 100, hasOlderOnServer: false },
      }),
    });
    h.server.always("GET /v1/turn", turn({ turn_id: "tR", partial_text: full }));
    await h.store.start();
    await advance(500); // one burst tick
    expect(h.server.to("GET /v1/turn")[0].path).toBe("/v1/turn/tR");
    expect(h.store.getStream("p1")?.text).toBe(full);
  });

  it("contrast: a live turn types its first slab out instead of showing it whole", async () => {
    h = makeHarness();
    const full = "The quick brown fox jumps over the lazy dog. ".repeat(4);
    h.server.always("GET /v1/turn", turn({ partial_text: full }));
    await h.store.send("go");
    await advance(500);
    const shown = h.store.getStream(placeholderOf(h).id)?.text ?? "";
    expect(shown.length).toBeGreaterThan(0);
    expect(shown.length).toBeLessThan(full.length);
  });

  it("marks a running message with no turn id as interrupted", async () => {
    await start([msg({ id: "u1" }), msg({ id: "p1", role: "assistant", status: "running", clientMsgId: "u1" })]);
    expect(state().messages.find((m) => m.id === "p1")).toMatchObject({ status: "error", text: TEXT.interruptedBeforeReply });
    expect(h.server.count("GET /v1/turn")).toBe(0);
  });

  it("resends a sending message under the same client_msg_id", async () => {
    await start([msg({ id: "u1", text: "unsent words" }), msg({ id: "p1", role: "assistant", status: "sending", clientMsgId: "u1" })]);
    expect(posts()).toHaveLength(1);
    expect(posts()[0].body).toMatchObject({ client_msg_id: "u1", text: "unsent words" });
    expect(state().messages.find((m) => m.id === "p1")!.status).toBe("running");
  });

  it("resends a sending message's stored image too", async () => {
    h = makeHarness({
      persist: new MemPersistence({
        messages: [msg({ id: "u1", imageKeys: ["u1"] }), msg({ id: "p1", role: "assistant", status: "sending", clientMsgId: "u1" })],
        cursor: { latestSeq: 100, hasOlderOnServer: false },
      }),
    });
    h.persist.images.set("u1", new Blob([new Uint8Array([5, 6])]));
    await h.store.start();
    await flush();
    expect(posts()[0].body.images).toEqual([{ b64: Buffer.from([5, 6]).toString("base64"), mime: "image/jpeg" }]);
  });

  it("still resends an image saved by an earlier build under the single imageKey field", async () => {
    h = makeHarness({
      persist: new MemPersistence({
        messages: [msg({ id: "u1", imageKey: "u1" }), msg({ id: "p1", role: "assistant", status: "sending", clientMsgId: "u1" })],
        cursor: { latestSeq: 100, hasOlderOnServer: false },
      }),
    });
    h.persist.images.set("u1", new Blob([new Uint8Array([5, 6])]));
    await h.store.start();
    await flush();
    expect(posts()[0].body.images).toEqual([{ b64: Buffer.from([5, 6]).toString("base64"), mime: "image/jpeg" }]);
  });

  it("says interrupted-before-sending when the user message is gone", async () => {
    await start([msg({ id: "p1", role: "assistant", status: "sending", clientMsgId: "missing" })]);
    expect(state().messages[0]).toMatchObject({ status: "error", text: TEXT.interruptedBeforeSend });
    expect(posts()).toHaveLength(0);
  });

  it("leaves done and error messages alone", async () => {
    await start([
      msg({ id: "u1", seq: 1 }),
      msg({ id: "a1", role: "assistant", status: "done", seq: 2, text: "fine", clientMsgId: "u1" }),
      msg({ id: "u2", seq: 3 }),
      msg({ id: "a2", role: "assistant", status: "error", seq: 4, text: "broke", clientMsgId: "u2" }),
    ]);
    expect(state().messages.map((m) => [m.id, m.status, m.text])).toEqual([
      ["u1", "done", "hi"],
      ["a1", "done", "fine"],
      ["u2", "done", "hi"],
      ["a2", "error", "broke"],
    ]);
    expect(posts()).toHaveLength(0);
    expect(h.server.count("GET /v1/turn")).toBe(0);
  });

  it("resumes a stalled message with a turn id, and gives up on one without", async () => {
    await start([
      msg({ id: "u1" }),
      msg({ id: "p1", role: "assistant", status: "stalled", turnId: "tS", clientMsgId: "u1", text: TEXT.stalled }),
      msg({ id: "u2" }),
      msg({ id: "p2", role: "assistant", status: "stalled", clientMsgId: "u2" }),
    ]);
    await advance(500);
    expect(h.server.to("GET /v1/turn").map((r) => r.path)).toEqual(["/v1/turn/tS"]);
    expect(state().messages.find((m) => m.id === "p2")).toMatchObject({ status: "error", text: TEXT.interruptedBeforeReply });
  });
});

describe("CMP-6 images", () => {
  const bytes = new Uint8Array([0, 1, 2, 250, 251, 255, 128]);

  it("an image send carries images: [{b64 (base64 of the exact bytes), mime image/jpeg}]", async () => {
    h = makeHarness();
    await h.store.send("see", [new Blob([bytes], { type: "image/jpeg" })]);
    await flush();
    expect(posts()[0].body.images).toEqual([{ b64: Buffer.from(bytes).toString("base64"), mime: "image/jpeg" }]);
    expect(posts()[0].body).not.toHaveProperty("image_b64");
    expect(h.persist.images.has(userIdOf(h))).toBe(true);
    expect(state().messages[0].imageKeys).toEqual([userIdOf(h)]);
  });

  it("several images go in one request, in order, each saved for retry", async () => {
    h = makeHarness();
    const blobs = [1, 2, 3].map((n) => new Blob([new Uint8Array([n, n])], { type: "image/jpeg" }));
    expect(await h.store.send("three", blobs)).toBe(true);
    await flush();
    expect(posts()).toHaveLength(1);
    expect(posts()[0].body.images.map((i: { b64: string }) => i.b64)).toEqual(
      [1, 2, 3].map((n) => Buffer.from([n, n]).toString("base64")),
    );
    const id = userIdOf(h);
    expect(state().messages[0].imageKeys).toEqual([id, `${id}:1`, `${id}:2`]);
    expect([...h.persist.images.keys()].sort()).toEqual([id, `${id}:1`, `${id}:2`].sort());
  });

  it("a retry resends every saved image, same order, same bytes", async () => {
    h = makeHarness();
    h.server.script("POST /v1/message", json({ detail: "nope" }, 400));
    await h.store.send("two", [new Blob([new Uint8Array([1])]), new Blob([new Uint8Array([2])])]);
    await flush();
    h.store.retry(placeholderOf(h).id);
    await flush();
    expect(posts()[1].body.images).toEqual(posts()[0].body.images);
    expect(posts()[1].body.images).toHaveLength(2);
  });

  it("if any retry image is gone it sends none (never the caption with some missing) and says so", async () => {
    h = makeHarness();
    h.server.script("POST /v1/message", json({ detail: "nope" }, 400));
    await h.store.send("two", [new Blob([new Uint8Array([1])]), new Blob([new Uint8Array([2])])]);
    await flush();
    h.persist.images.delete(`${userIdOf(h)}:1`);
    h.store.retry(placeholderOf(h).id);
    await flush();
    expect(posts()[1].body).not.toHaveProperty("images");
    expect(state().notice).toBe(TEXT.photoMissing);
  });

  it("if saving any one image fails the message goes text-only, not with some images", async () => {
    h = makeHarness();
    let n = 0;
    const put = h.persist.putImage.bind(h.persist);
    h.persist.putImage = async (k: string, b: Blob) => {
      if (++n === 2) throw new Error("quota");
      return put(k, b);
    };
    await h.store.send("two", [new Blob([new Uint8Array([1])]), new Blob([new Uint8Array([2])])]);
    expect(posts()[0].body).not.toHaveProperty("images");
    expect(state().notice).toBe(TEXT.photoSaveFailed);
    expect(state().messages[0].imageKeys).toBeUndefined();
  });

  it("a text-only send carries no image fields", async () => {
    h = makeHarness();
    await h.store.send("plain");
    expect(posts()[0].body).not.toHaveProperty("images");
  });

  it("an image with no caption still sends", async () => {
    h = makeHarness();
    expect(await h.store.send("", [new Blob([bytes])])).toBe(true);
    await flush();
    expect(posts()[0].body.text).toBe("");
    expect(posts()[0].body.images).toHaveLength(1);
  });

  it("a failed image save sends text-only and shows the notice", async () => {
    h = makeHarness();
    h.persist.failPutImage = true;
    await h.store.send("see", [new Blob([bytes])]);
    expect(posts()[0].body).not.toHaveProperty("images");
    expect(state().notice).toBe(TEXT.photoSaveFailed);
    expect(state().messages[0].imageKeys).toBeUndefined();
  });

  it("a failed image save with no caption has nothing to send", async () => {
    h = makeHarness();
    h.persist.failPutImage = true;
    expect(await h.store.send("", [new Blob([bytes])])).toBe(false);
    expect(posts()).toHaveLength(0);
    expect(state().messages).toEqual([]);
  });

  it("a retry whose image is gone sends without it and shows the photo-missing notice", async () => {
    h = makeHarness();
    h.server.script("POST /v1/message", json({ detail: "nope" }, 400));
    await h.store.send("see", [new Blob([bytes])]);
    await flush();
    h.persist.images.clear();
    h.store.retry(placeholderOf(h).id);
    await flush();
    expect(posts()).toHaveLength(2);
    expect(posts()[1].body).not.toHaveProperty("images");
    expect(state().notice).toBe(TEXT.photoMissing);
  });
});

describe("CMP-7 timezone and effort", () => {
  it("every message carries the timezone and the effort", async () => {
    h = makeHarness({ store: { initialEffort: "medium" } });
    await h.store.send("hi");
    expect(posts()[0].body).toMatchObject({ timezone: TZ, effort: "medium" });
  });

  it("an untouched client asks for medium, the server's default", async () => {
    h = makeHarness();
    await h.store.send("hi");
    expect(posts()[0].body.effort).toBe("medium");
  });

  it("reads the effort per attempt: a retry after a change carries the new one", async () => {
    h = makeHarness();
    h.server.script("POST /v1/message", json({ detail: "busy" }, 503));
    await h.store.send("hi");
    expect(posts()[0].body.effort).toBe("medium");
    h.store.setEffort("high");
    await advance(4_000);
    expect(posts()[1].body.effort).toBe("high");
  });

  it("a user Retry after a permanent failure also reads the current effort", async () => {
    h = makeHarness();
    h.server.script("POST /v1/message", json({ detail: "nope" }, 400));
    await h.store.send("hi");
    await flush();
    h.store.setEffort("medium");
    h.store.retry(placeholderOf(h).id);
    await flush();
    expect(posts()[1].body.effort).toBe("medium");
  });
});

describe("message location", () => {
  const where = { latitude: 46.81, longitude: -71.21, accuracy_m: 1000 };

  it("carries location in the body when the source has one", async () => {
    h = makeHarness({ store: { location: () => where } });
    await h.store.send("hi");
    expect(posts()[0].body.location).toEqual(where);
  });

  it("has NO location key at all when there is none (absence clears the server's copy)", async () => {
    h = makeHarness({ store: { location: () => undefined } });
    await h.store.send("hi");
    expect(posts()[0].body).not.toHaveProperty("location");
    h.store.stop();
    h = makeHarness(); // no source wired
    await h.store.send("hi");
    expect(posts()[0].body).not.toHaveProperty("location");
  });

  it("reads the location per attempt: a retry sends what is current, even nothing", async () => {
    let current: typeof where | undefined = where;
    h = makeHarness({ store: { location: () => current } });
    h.server.script("POST /v1/message", json({ detail: "busy" }, 503));
    await h.store.send("hi");
    expect(posts()[0].body.location).toEqual(where);
    current = undefined;
    await advance(4_000);
    expect(posts()[1].body).not.toHaveProperty("location");
  });
});

const QUOTE = { idea_id: "dinner-invites", emoji: "🍽", title: "Draft the dinner invites" };

describe("idea_id and the quote (Do it on an idea)", () => {
  it("carries idea_id in the body when the send came from an idea", async () => {
    h = makeHarness();
    await h.store.send("Do it", [], QUOTE);
    expect(posts()[0].body).toMatchObject({ text: "Do it", idea_id: "dinner-invites" });
  });

  it("the optimistic bubble has the quote at once, before any server row", async () => {
    h = makeHarness();
    h.server.script("POST /v1/message", hang());
    await h.store.send("Do it", [], QUOTE);
    expect(state().messages[0]).toMatchObject({ role: "user", text: "Do it", quote: QUOTE });
  });

  it("has NO idea_id key and no quote on a normal send", async () => {
    h = makeHarness();
    await h.store.send("hi");
    expect(posts()[0].body).not.toHaveProperty("idea_id");
    expect(state().messages[0]).not.toHaveProperty("quote");
  });

  it("a retry after a permanent failure sends the same idea_id", async () => {
    h = makeHarness();
    h.server.script("POST /v1/message", json({ detail: "nope" }, 400));
    await h.store.send("Do it", [], QUOTE);
    await flush();
    expect(placeholderOf(h).status).toBe("error");
    h.store.retry(placeholderOf(h).id);
    await flush();
    expect(posts()).toHaveLength(2);
    expect(posts()[1].body.idea_id).toBe("dinner-invites");
  });

  it("an outbox resume after a restart sends the same idea_id", async () => {
    h = makeHarness({
      persist: new MemPersistence({
        messages: [
          msg({ id: "u1", text: "Do it", quote: QUOTE }),
          msg({ id: "p1", role: "assistant", status: "sending", clientMsgId: "u1" }),
        ],
        cursor: { latestSeq: 100, hasOlderOnServer: false },
      }),
    });
    await h.store.start();
    await flush();
    expect(posts()[0].body).toMatchObject({ client_msg_id: "u1", idea_id: "dinner-invites" });
  });
});
