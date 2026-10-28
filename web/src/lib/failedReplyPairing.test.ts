import { describe, expect, test } from "vitest";
import { failedReplies } from "./failedReplyPairing";
import type { ChatMessage, MessageStatus } from "./types";

// The failed-send layout moves Retry onto the user's bubble. The failure worth
// guarding is Retry vanishing: a failed reply that pairs with nothing on screen
// must keep its own button, which it does by being absent here.

function user(): ChatMessage {
  return { id: crypto.randomUUID(), role: "user", text: "hi", createdAt: Date.now(), status: "done" };
}

function reply(to: ChatMessage | undefined, status: MessageStatus): ChatMessage {
  return {
    id: crypto.randomUUID(),
    role: "assistant",
    text: "Something went wrong.",
    createdAt: Date.now(),
    status,
    clientMsgId: to?.id,
  };
}

describe("failed reply pairing", () => {
  test("a failed reply pairs with its message", () => {
    const mine = user();
    const failed = reply(mine, "error");
    expect(failedReplies([mine, failed])).toEqual(new Map([[mine.id, failed.id]]));
  });

  test("a reply that did not fail pairs with nothing", () => {
    const mine = user();
    expect(failedReplies([mine, reply(mine, "done")]).size).toBe(0);
  });

  test("a failed reply whose message scrolled out keeps its own retry", () => {
    expect(failedReplies([reply(user(), "error")]).size).toBe(0);
  });

  test("a failed cron turn pairs with nothing", () => {
    expect(failedReplies([user(), reply(undefined, "error")]).size).toBe(0);
  });

  test("the client_msg_id matches whatever its case", () => {
    const mine = user();
    const failed = { ...reply(mine, "error"), clientMsgId: mine.id.toUpperCase() };
    expect(failedReplies([mine, failed])).toEqual(new Map([[mine.id, failed.id]]));
  });

  test("a client_msg_id naming an assistant message does not pair", () => {
    const a = reply(undefined, "done");
    const failed = reply(undefined, "error");
    failed.clientMsgId = a.id;
    expect(failedReplies([a, failed]).size).toBe(0);
  });
});
