// The flood after a computer wakes (raised 2026-11-10): the push service
// delivers everything it held while the machine slept, and each reply used to
// get its own tag, so each became its own toast. Replies now share one tag and
// a backlog becomes one notification that counts them.
import { describe, expect, test } from "vitest";
import {
  BURST_MS,
  collapseReply,
  planNotification,
  REPLY_TAG,
  STALE_MS,
  turnCreatedAt,
  type NotificationPlan,
} from "./pushNotification";

const NOW = 1_800_000_000_000;

async function replyPlan(): Promise<NotificationPlan> {
  const r = await planNotification({ type: "turn_ready", ref: "t9" }, false, async () => "The ferry is on time.");
  return r.notification!;
}

describe("replies share one notification", () => {
  test("turn_ready and notify carry the reply tag", async () => {
    expect((await replyPlan()).tag).toBe(REPLY_TAG);
    const n = await planNotification({ type: "notify", ref: "n1", alert_body: "Bins tonight" }, false, async () => null);
    expect(n.notification?.tag).toBe(REPLY_TAG);
  });

  test("the first reply alerts normally", async () => {
    const out = collapseReply(await replyPlan(), null, NOW, NOW / 1000);
    expect(out.title).toBe("Marlow");
    expect(out.silent).toBeFalsy();
    expect(out.renotify).toBeFalsy();
    expect(out.collapse).toEqual({ count: 1, lastPushAt: NOW });
  });

  test("a backlog arriving together counts up and alerts once", async () => {
    let earlier: unknown = collapseReply(await replyPlan(), null, NOW, null).collapse;
    for (let i = 1; i < 5; i++) {
      const out = collapseReply(await replyPlan(), earlier, NOW + i * 1_000, null);
      expect(out.renotify).toBeFalsy();
      expect(out.silent).toBe(true);
      earlier = out.collapse;
    }
    const last = collapseReply(await replyPlan(), earlier, NOW + 5_000, null);
    expect(last.title).toBe("Marlow · 6 new");
    expect(last.body).toBe("The ferry is on time.");
  });

  test("a reply well after the last one alerts again, still counted", async () => {
    const earlier = { count: 2, lastPushAt: NOW - BURST_MS - 1 };
    const out = collapseReply(await replyPlan(), earlier, NOW, NOW / 1000);
    expect(out.renotify).toBe(true);
    expect(out.silent).toBeFalsy();
    expect(out.title).toBe("Marlow · 3 new");
  });

  test("an old reply arrives without a sound", async () => {
    const out = collapseReply(await replyPlan(), null, NOW, (NOW - STALE_MS - 1) / 1000);
    expect(out.silent).toBe(true);
    expect(out.renotify).toBeFalsy();
  });

  test("silent and renotify are never both set (the browser rejects that)", async () => {
    for (const earlier of [null, { count: 1, lastPushAt: NOW - 1 }, { count: 1, lastPushAt: 0 }]) {
      for (const created of [null, NOW / 1000, 0]) {
        const out = collapseReply(await replyPlan(), earlier, NOW, created);
        expect(out.silent && out.renotify).toBeFalsy();
      }
    }
  });

  test("junk left in an older notification's data counts as none", async () => {
    const out = collapseReply(await replyPlan(), { count: "x" }, NOW, null);
    expect(out.collapse.count).toBe(1);
  });

  test("turnCreatedAt reads the turn's epoch seconds, or null", () => {
    expect(turnCreatedAt({ created_at: 1_800_000_000.5 })).toBe(1_800_000_000.5);
    expect(turnCreatedAt({ created_at: "soon" })).toBeNull();
    expect(turnCreatedAt(null)).toBeNull();
  });
});
