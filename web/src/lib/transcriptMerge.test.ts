import { describe, expect, test } from "vitest";
import { applyRows } from "./transcriptMerge";
import { decodePage } from "./types";
import type { ChatMessage, MessageStatus, Role, TranscriptRow } from "./types";

// `applyRows` is pure, so every rule in docs/tasks/ios-server-transcript.md is
// pinned here against plain values, with no store, task or clock in the way.

const uuid = () => crypto.randomUUID();

function localUser(text = "hello", opts: { id?: string; imageKeys?: string[] } = {}): ChatMessage {
  return {
    id: opts.id ?? uuid(),
    role: "user",
    text,
    createdAt: Date.now(),
    status: "done",
    imageKeys: opts.imageKeys,
  };
}

function placeholder(
  turn: string | undefined,
  opts: { status?: MessageStatus; text?: string; pairedWith?: ChatMessage } = {},
): ChatMessage {
  return {
    id: uuid(),
    role: "assistant",
    text: opts.text ?? "",
    createdAt: Date.now(),
    turnId: turn,
    status: opts.status ?? "running",
    clientMsgId: opts.pairedWith?.id,
  };
}

function serverMessage(seq: number, role: Role = "assistant", turn?: string): ChatMessage {
  return {
    id: uuid(),
    role,
    text: `row ${seq}`,
    createdAt: seq * 1000,
    turnId: turn,
    status: "done",
    seq,
  };
}

function userRow(
  seq: number,
  turn: string,
  opts: { clientMsgId?: string | null; text?: string; attachments?: { id: string; mime: string }[] } = {},
): TranscriptRow {
  return {
    seq,
    id: uuid(),
    role: "user",
    source: "ios",
    turn_id: turn,
    client_msg_id: opts.clientMsgId ?? null,
    text: opts.text ?? "from elsewhere",
    status: null,
    created_at: 1000 + seq,
    answers: [],
    attachments: opts.attachments ?? [],
  };
}

/** `answers` defaults to the row's own turn, which is what the server writes for
 * a client-sent turn; a cron or system row passes `[]`. */
function assistantRow(
  seq: number,
  turn: string,
  opts: { text?: string; status?: "done" | "error"; answers?: string[]; source?: string } = {},
): TranscriptRow {
  return {
    seq,
    id: uuid(),
    role: "assistant",
    source: opts.source ?? "ios",
    turn_id: turn,
    client_msg_id: null,
    text: opts.text ?? "reply",
    status: opts.status ?? "done",
    created_at: 1000 + seq,
    answers: opts.answers ?? [turn],
    attachments: [],
  };
}

describe("user rows", () => {
  test("an own send's row is folded into the bubble already on screen", () => {
    const user = localUser("with a photo", { imageKeys: ["local.jpg"] });
    const pending = placeholder("t1", { pairedWith: user });
    const row = userRow(1, "t1", {
      clientMsgId: user.id,
      text: "with a photo",
      attachments: [{ id: uuid(), mime: "image/jpeg" }],
    });

    const result = applyRows([row], [user, pending], true);

    expect(result.messages).toHaveLength(2);
    expect(result.messages[0].id).toBe(user.id); // local identity wins: the id never changes
    expect(result.messages[0].seq).toBe(1);
    expect(result.messages[0].imageKeys).toEqual(["local.jpg"]); // the photo this client sent is not downloaded back
    expect(result.messages[0].attachmentIds).toBeUndefined();
    expect(result.messages[1]).toEqual(pending); // the placeholder is untouched by its user row
    expect(result.effects.fetchAttachments).toEqual([]);
    expect(result.effects.startPolls).toEqual([]); // this client already polls its own turn
  });

  test("an own send's row matches whatever the case of its client_msg_id", () => {
    const user = localUser();
    const upper = applyRows([userRow(1, "t1", { clientMsgId: user.id.toUpperCase() })], [user], true);
    expect(upper.messages).toHaveLength(1);
    expect(upper.messages[0].seq).toBe(1);
    expect(upper.messages[0].id).toBe(user.id);
  });

  test("a foreign user row gets a placeholder and a poll", () => {
    const row = userRow(5, "tX", { text: "asked from the desktop" });

    const result = applyRows([row], [], true);

    expect(result.messages).toHaveLength(2);
    const [user, ph] = result.messages;
    expect(user.role).toBe("user");
    expect(user.id).toBe(row.id); // a row with no local counterpart takes the row's id
    expect(user.text).toBe("asked from the desktop");
    expect(user.status).toBe("done");
    expect(user.turnId).toBe("tX");
    expect(user.seq).toBe(5);
    expect(user.createdAt).toBe(row.created_at * 1000);
    expect(ph.role).toBe("assistant"); // the placeholder sits right after the user message
    expect(ph.status).toBe("running");
    expect(ph.turnId).toBe("tX");
    expect(ph.seq).toBeUndefined();
    expect(ph.clientMsgId).toBeUndefined();
    expect(result.effects.startPolls).toEqual([{ messageId: ph.id, turnId: "tX" }]);
  });

  test("a foreign user row answered in the same batch gets no placeholder", () => {
    const result = applyRows([userRow(5, "tX"), assistantRow(6, "tX")], [], true);
    expect(result.messages.map((m) => m.role)).toEqual(["user", "assistant"]);
    expect(result.messages[1].seq).toBe(6);
    expect(result.effects.startPolls).toEqual([]);
  });

  // The match is on `answers`, not the reply's own turn: a retry's reply
  // finishes a fresh turn B but resolves the original row's turn A.
  test("a foreign user row answered through answers gets no placeholder", () => {
    const result = applyRows([userRow(5, "A"), assistantRow(6, "B", { answers: ["A"] })], [], true);
    expect(result.messages.map((m) => m.role)).toEqual(["user", "assistant"]);
    expect(result.effects.startPolls).toEqual([]);
  });

  test("a foreign user row whose reply is already in the cache gets no placeholder", () => {
    // The reply arrived in an earlier fetch, the question in a later page.
    const reply: ChatMessage = { ...serverMessage(6, "assistant", "B"), answers: ["A"] };

    const result = applyRows([userRow(5, "A")], [reply], false);

    expect(result.messages.map((m) => m.role)).toEqual(["user", "assistant"]);
    expect(result.effects.startPolls).toEqual([]);
  });

  test("a foreign user row with a placeholder already polling it gets no second", () => {
    const polling = placeholder("tX");
    const result = applyRows([userRow(5, "tX")], [polling], true);
    expect(result.messages.filter((m) => m.role === "assistant")).toHaveLength(1);
    expect(result.effects.startPolls).toEqual([]);
  });

  test("an image attachment on a foreign user row emits a fetch", () => {
    const image = uuid();
    const row = userRow(5, "tX", {
      attachments: [
        { id: uuid(), mime: "application/pdf" },
        { id: image, mime: "image/png" },
        { id: uuid(), mime: "image/jpeg" },
      ],
    });

    const result = applyRows([row], [], true);

    const second = row.attachments[2].id;
    expect(result.messages[0].attachmentIds).toEqual([image, second]); // every image, in order, no pdf
    expect(result.messages[0].imageKeys).toBeUndefined(); // set by the store once they are downloaded
    expect(result.effects.fetchAttachments).toEqual([
      { messageId: result.messages[0].id, attachmentId: image },
      { messageId: result.messages[0].id, attachmentId: second },
    ]);
  });

  test("a row with no image attachment emits no fetch", () => {
    const row = userRow(5, "tX", { attachments: [{ id: uuid(), mime: "application/pdf" }] });
    const result = applyRows([row], [], true);
    expect(result.messages[0].attachmentIds).toBeUndefined();
    expect(result.effects.fetchAttachments).toEqual([]);
  });
});

describe("assistant rows", () => {
  test("an assistant row resolves the placeholder for its turn", () => {
    const user = localUser();
    const pending = placeholder("t1", { pairedWith: user });

    const result = applyRows([assistantRow(2, "t1", { text: "The answer." })], [user, pending], true);

    expect(result.messages).toHaveLength(2);
    const m = result.messages[1];
    expect(m.id).toBe(pending.id); // the placeholder keeps its id: a teardown would break follow-scroll
    expect(m.seq).toBe(2);
    expect(m.text).toBe("The answer.");
    expect(m.status).toBe("done");
    expect(m.answers).toEqual(["t1"]);
    expect(result.effects.cancelPolls).toEqual([pending.id]);
    expect(result.effects.completedTurnIds).toEqual(["t1"]);
  });

  test("an assistant row resolves the placeholder for a turn it answers", () => {
    // The placeholder still carries the original turn A; the reply finishes a
    // fresh turn B and says it answers A.
    const pending = placeholder("A");

    const result = applyRows(
      [assistantRow(2, "B", { text: "Second time lucky.", answers: ["A"] })],
      [pending],
      true,
    );

    expect(result.messages).toHaveLength(1);
    expect(result.messages[0].id).toBe(pending.id);
    expect(result.messages[0].text).toBe("Second time lucky.");
    expect(result.effects.cancelPolls).toEqual([pending.id]);
  });

  // After a failed turn the user taps Retry: the same placeholder is sent again
  // and takes the new turn id B, while the server's reply row names B as its
  // own turn and the original A as the one it answers.
  test("a retry after an error resolves the placeholder carrying the new turn", () => {
    const pending = placeholder("B", { status: "running" });

    const result = applyRows([assistantRow(4, "B", { text: "Fixed.", answers: ["A"] })], [pending], true);

    expect(result.messages).toHaveLength(1);
    expect(result.messages[0].id).toBe(pending.id);
    expect(result.messages[0].status).toBe("done");
    expect(result.messages[0].text).toBe("Fixed.");
    expect(result.effects.completedTurnIds).toEqual(["B"]);
  });

  test("a merged sibling placeholder is removed", () => {
    // Two sends coalesced: turn t1 merged into t2, and one reply answers both.
    const u1 = localUser("first");
    const p1 = placeholder("t1", { pairedWith: u1 });
    const u2 = localUser("second");
    const p2 = placeholder("t2", { pairedWith: u2 });

    const result = applyRows([assistantRow(3, "t2", { answers: ["t1", "t2"] })], [u1, p1, u2, p2], true);

    // one reply must not sit under two bubbles
    expect(result.messages.map((m) => m.id)).toEqual([u1.id, u2.id, p2.id]);
    expect(result.messages[2].seq).toBe(3);
    expect(new Set(result.effects.cancelPolls)).toEqual(new Set([p1.id, p2.id]));
    expect(result.effects.completedTurnIds).toEqual(["t2"]); // one sound for one reply
  });

  test("a reply row never removes a placeholder it does not answer", () => {
    const other = placeholder("unrelated");
    const mine = placeholder("t2");

    const result = applyRows([assistantRow(3, "t2")], [other, mine], true);

    expect(result.messages).toHaveLength(2);
    expect(result.messages[0]).toEqual(other);
  });

  test("a row with no placeholder is inserted with the row's own id", () => {
    const row = assistantRow(7, "cron-1", { text: "**Morning.**", answers: [], source: "cron" });

    const result = applyRows([row], [], true);

    expect(result.messages).toHaveLength(1);
    const m = result.messages[0];
    expect(m.id).toBe(row.id);
    expect(m.role).toBe("assistant");
    expect(m.text).toBe("**Morning.**");
    expect(m.status).toBe("done");
    expect(m.turnId).toBe("cron-1");
    expect(m.seq).toBe(7);
    expect(result.effects.completedTurnIds).toEqual(["cron-1"]);
    expect(result.effects.cancelPolls).toEqual([]);
  });

  test("an error row with no text says something went wrong", () => {
    const result = applyRows(
      [assistantRow(2, "t1", { text: "", status: "error" })],
      [placeholder("t1")],
      true,
    );

    expect(result.messages).toHaveLength(1);
    expect(result.messages[0].status).toBe("error");
    expect(result.messages[0].text).toBe("Something went wrong.");
    expect(result.effects.completedTurnIds).toEqual([]); // the receive sound is for a reply that landed
  });

  test("an error row keeps the text the server gave it", () => {
    const result = applyRows([assistantRow(2, "t1", { text: "claude exited 1", status: "error" })], [], true);
    expect(result.messages[0].status).toBe("error");
    expect(result.messages[0].text).toBe("claude exited 1");
  });
});

describe("announcing", () => {
  test("announce false emits no completions", () => {
    const pending = placeholder("t1");

    const result = applyRows([assistantRow(1, "old"), assistantRow(2, "t1")], [pending], false);

    expect(result.messages).toHaveLength(2);
    // a fresh install must not play 100 receive sounds
    expect(result.effects.completedTurnIds).toEqual([]);
    // silence is about sounds, not about stopping polls
    expect(result.effects.cancelPolls).toEqual([pending.id]);
  });

  // The placeholder's own poll got to `done` first and already sounded. The row
  // that arrives afterwards must only confirm it.
  test("a placeholder already done does not complete again", () => {
    const done = placeholder("t1", { status: "done", text: "Hi." });

    const result = applyRows([assistantRow(2, "t1", { text: "Hi." })], [done], true);

    expect(result.messages).toHaveLength(1);
    expect(result.messages[0].id).toBe(done.id);
    expect(result.messages[0].seq).toBe(2);
    // the receive sound would play twice for one reply
    expect(result.effects.completedTurnIds).toEqual([]);
  });

  test("a placeholder that gave up with an error still announces the real reply", () => {
    const gaveUp = placeholder("t1", { status: "error", text: "Marlow lost track of this message." });

    const result = applyRows([assistantRow(2, "t1", { text: "Here it is." })], [gaveUp], true);

    expect(result.messages[0].status).toBe("done");
    expect(result.messages[0].text).toBe("Here it is.");
    expect(result.effects.completedTurnIds).toEqual(["t1"]); // the user never heard this one
  });
});

describe("placement and idempotence", () => {
  test("a cron row lands above this client's still-pending send", () => {
    const earlier = serverMessage(3);
    const user = localUser("what's the weather");
    const pending = placeholder(undefined, { status: "sending", pairedWith: user });

    const result = applyRows(
      [assistantRow(4, "cron-1", { answers: [], source: "cron" })],
      [earlier, user, pending],
      true,
    );

    expect(result.messages).toHaveLength(4);
    expect(result.messages[0].id).toBe(earlier.id);
    expect(result.messages[1].turnId).toBe("cron-1"); // in server order, above the unconfirmed send
    expect(result.messages[2].id).toBe(user.id);
    expect(result.messages[3].id).toBe(pending.id);
  });

  // Follow-scroll treats a user bubble and the reply streaming under it as an
  // adjacent pair. A row that lands mid-stream goes below the live placeholder,
  // not between the two.
  test.each<MessageStatus>(["sending", "running", "stalled"])(
    "a live row does not land between a confirmed send and its streaming reply (%s)",
    (status) => {
      const user: ChatMessage = { ...localUser("question"), seq: 10 };
      const streaming = placeholder("t1", { status, pairedWith: user });

      const result = applyRows(
        [assistantRow(11, "cron-1", { answers: [], source: "cron" })],
        [user, streaming],
        true,
      );

      expect(result.messages).toHaveLength(3);
      expect(result.messages[0].id).toBe(user.id);
      expect(result.messages[1].id).toBe(streaming.id); // the reply stays directly under its confirmed send
      expect(result.messages[2].turnId).toBe("cron-1");
    },
  );

  test("a row still lands above an unconfirmed send whose reply is streaming", () => {
    // The unconfirmed user message (no seq) is not a confirmed send: the row
    // goes above it, as in server order.
    const earlier = serverMessage(3);
    const user = localUser("question");
    const streaming = placeholder("t1", { pairedWith: user });

    const result = applyRows(
      [assistantRow(4, "cron-1", { answers: [], source: "cron" })],
      [earlier, user, streaming],
      true,
    );

    expect(result.messages.map((m) => m.id)).toEqual([earlier.id, result.messages[1].id, user.id, streaming.id]);
    expect(result.messages[1].turnId).toBe("cron-1");
  });

  test("a finished unconfirmed reply does not hold a row back", () => {
    // Only a reply still in flight keeps its place; one that already finished
    // (awaiting only its row) is not streaming under anything.
    const user: ChatMessage = { ...localUser("question"), seq: 10 };
    const finished = placeholder("t1", { status: "done", text: "Hi.", pairedWith: user });

    const result = applyRows(
      [assistantRow(11, "cron-1", { answers: [], source: "cron" })],
      [user, finished],
      true,
    );

    expect(result.messages.map((m) => m.turnId)).toEqual([undefined, "cron-1", "t1"]);
  });

  test("a cron row is above a local send when nothing else is confirmed", () => {
    const user = localUser();
    const pending = placeholder(undefined, { status: "sending", pairedWith: user });

    const result = applyRows(
      [assistantRow(1, "cron-1", { answers: [], source: "cron" })],
      [user, pending],
      true,
    );

    expect(result.messages).toHaveLength(3);
    expect(result.messages[0].turnId).toBe("cron-1");
  });

  test("rows land in server order whichever order they arrive in", () => {
    const newer = serverMessage(12);
    const older = serverMessage(10);

    const result = applyRows(
      [assistantRow(11, "mid", { answers: [] }), assistantRow(5, "ancient", { answers: [] })],
      [older, newer],
      false,
    );

    expect(result.messages.map((m) => m.seq)).toEqual([5, 10, 11, 12]);
  });

  test("rows are applied in ascending seq even if handed over descending", () => {
    // The bootstrap page arrives newest-first; the store reverses it, but the
    // merge must not depend on that for its placeholders to pair up.
    const result = applyRows([assistantRow(6, "tX"), userRow(5, "tX")], [], false);

    expect(result.messages.map((m) => m.role)).toEqual(["user", "assistant"]);
    expect(result.effects.startPolls).toEqual([]);
  });

  test("reapplying a page changes nothing", () => {
    const rows = [userRow(1, "tX"), assistantRow(2, "tX"), assistantRow(3, "c", { answers: [] })];
    const first = applyRows(rows, [], true);

    const second = applyRows(rows, first.messages, true);

    expect(second.messages).toEqual(first.messages);
    expect(second.effects.completedTurnIds).toEqual([]);
    expect(second.effects.startPolls).toEqual([]);
    expect(second.effects.cancelPolls).toEqual([]);
    expect(second.effects.fetchAttachments).toEqual([]);
  });

  test("never mutates its inputs", () => {
    const user = localUser();
    const pending = placeholder("t1", { pairedWith: user });
    const messages = [user, pending];
    const snapshot = structuredClone(messages);
    const rows = [userRow(1, "t1", { clientMsgId: user.id }), assistantRow(2, "t1")];
    const rowsSnapshot = structuredClone(rows);

    applyRows(rows, messages, true);

    expect(messages).toEqual(snapshot);
    expect(rows).toEqual(rowsSnapshot);
  });

  test("an unknown role is skipped but does not disturb the rest", () => {
    const odd: TranscriptRow = { ...assistantRow(1, "t0"), role: "other" };
    const result = applyRows([odd, assistantRow(2, "t1")], [], true);
    expect(result.messages.map((m) => m.seq)).toEqual([2]);
    expect(result.effects.completedTurnIds).toEqual(["t1"]);
  });
});

describe("decoding", () => {
  test("an unknown role, status or key does not fail the page", () => {
    const page = decodePage({
      rows: [
        { seq: 1, id: uuid(), role: "tool", source: "ios", turn_id: "t0", client_msg_id: null, text: "?",
          status: null, created_at: 1.0, answers: [], attachments: [], a_future_key: { nested: true } },
        { seq: 2, id: uuid(), role: "assistant", source: "ios", turn_id: "t1", client_msg_id: null, text: "ok",
          status: "half-done", created_at: 2.0, answers: ["t1"],
          attachments: [{ id: "a1", mime: "image/png", extra: 1 }] },
        { seq: 3, id: uuid(), role: "user", source: "ios", turn_id: "t1", client_msg_id: "c", text: "hi",
          status: null, created_at: 3.0, answers: [], attachments: [] },
      ],
      latest_seq: 3,
      has_more: true,
      another_future_key: 1,
    });

    expect(page.rows.map((r) => r.role)).toEqual(["other", "assistant", "user"]);
    expect(page.rows[1].status).toBe("error"); // an unknown status is an error
    expect(page.rows[1].attachments).toEqual([{ id: "a1", mime: "image/png" }]);
    expect(page.latest_seq).toBe(3);
    expect(page.has_more).toBe(true);

    const merged = applyRows(page.rows, [], false);
    expect(merged.messages.some((m) => m.seq === 1)).toBe(false); // skipped, not rendered as a bubble
    expect(merged.messages.some((m) => m.seq === 2)).toBe(true);
  });

  test("a row with only the required keys still decodes", () => {
    const page = decodePage({ rows: [{ seq: 4, id: "x", role: "user" }], latest_seq: 4 });
    expect(page.rows[0].answers).toEqual([]);
    expect(page.rows[0].attachments).toEqual([]);
    expect(page.rows[0].status).toBeNull();
    expect(page.has_more).toBe(false);
  });
});

describe("quote on a Do it row", () => {
  const Q = { idea_id: "dinner-invites", emoji: "🍽", title: "Draft the dinner invites" };
  const wire = (extra: Record<string, unknown>) => ({
    seq: 1, id: uuid(), role: "user", source: "web", turn_id: "t1", client_msg_id: null, text: "Do it",
    status: null, created_at: 1.0, answers: [], attachments: [], ...extra,
  });

  test("decodes present, null and absent; a malformed quote is no quote", () => {
    const rows = decodePage({
      rows: [
        wire({ seq: 1, quote: Q }),
        wire({ seq: 2, quote: null }),
        wire({ seq: 3 }),
        wire({ seq: 4, quote: { idea_id: "x" } }),
        wire({ seq: 5, quote: "Draft" }),
        wire({ seq: 6, quote: { idea_id: "y", title: "No emoji" } }),
      ],
      latest_seq: 6,
      has_more: false,
    }).rows;
    expect(rows.map((r) => r.quote)).toEqual([
      Q,
      null,
      null,
      null,
      null,
      { idea_id: "y", emoji: "", title: "No emoji" },
    ]);
  });

  test("the server row keeps the quote the optimistic bubble already had", () => {
    const user: ChatMessage = { ...localUser("Do it"), quote: Q };
    const pending = placeholder("t1", { pairedWith: user });
    const row = { ...userRow(1, "t1", { clientMsgId: user.id, text: "Do it" }), quote: { ...Q, title: "Server's reading" } };
    const out = applyRows([row], [user, pending], false).messages;
    expect(out[0]).toMatchObject({ id: user.id, seq: 1, quote: Q });
  });

  test("the server row fills the quote when the local bubble had none", () => {
    const user = localUser("Do it");
    const pending = placeholder("t1", { pairedWith: user });
    const row = { ...userRow(1, "t1", { clientMsgId: user.id, text: "Do it" }), quote: Q };
    expect(applyRows([row], [user, pending], false).messages[0].quote).toEqual(Q);
  });

  test("a Do it from another device arrives with its quote", () => {
    const row = { ...userRow(1, "t1", { text: "Do it" }), quote: Q };
    expect(applyRows([row], [], false).messages[0]).toMatchObject({ role: "user", text: "Do it", quote: Q });
  });

  test("no quote key at all for a plain message", () => {
    const row = { ...userRow(1, "t1"), quote: null };
    expect(applyRows([row], [], false).messages[0]).not.toHaveProperty("quote");
  });
});
