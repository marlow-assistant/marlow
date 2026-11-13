// The flood after a computer wakes (raised 2026-11-10): the push service
// delivers everything it held while the machine slept, and each reply used to
// get its own tag, so each became its own toast. Replies now share one tag and
// a backlog becomes one notification that counts them. A push that arrives
// late (its `sent_at` well past) is the backlog, so it makes no sound; one
// that arrives on time always alerts, however soon after the last.
import { describe, expect, test } from "vitest";
import {
  collapseReply,
  decodePushData,
  LATE_MS,
  oneAtATime,
  planNotification,
  REPLY_TAG,
  type NotificationPlan,
} from "./pushNotification";

const NOW = 1_800_000_000_000;
const onTime = NOW / 1000;
const late = (NOW - LATE_MS - 1) / 1000;

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
    const out = collapseReply(await replyPlan(), null, NOW, onTime, "a");
    expect(out.title).toBe("Marlow");
    expect(out.silent).toBeFalsy();
    expect(out.renotify).toBeFalsy();
    expect(out.collapse).toEqual({ count: 1, lastRef: "a" });
  });

  test("a backlog delivered late counts up without a sound", async () => {
    let earlier: unknown = null;
    for (let i = 0; i < 6; i++) {
      const out = collapseReply(await replyPlan(), earlier, NOW + i * 200, late, `r${i}`);
      expect(out.silent).toBe(true);
      expect(out.renotify).toBeFalsy();
      earlier = out.collapse;
    }
    const last = collapseReply(await replyPlan(), earlier, NOW + 2_000, late, "r6");
    expect(last.title).toBe("Marlow · 7 new");
    expect(last.body).toBe("The ferry is on time.");
  });

  test("a reply on time alerts again even seconds after the last, still counted", async () => {
    // Two real replies 40 s apart on a phone each get heard.
    const out = collapseReply(await replyPlan(), { count: 2, lastRef: "a" }, NOW, onTime, "b");
    expect(out.renotify).toBe(true);
    expect(out.silent).toBeFalsy();
    expect(out.title).toBe("Marlow · 3 new");
  });

  test("a second push for the same reply neither counts nor alerts", async () => {
    const out = collapseReply(await replyPlan(), { count: 2, lastRef: "a" }, NOW, onTime, "a");
    expect(out.collapse.count).toBe(2);
    expect(out.title).toBe("Marlow · 2 new");
    expect(out.silent).toBe(true);
    expect(out.renotify).toBeFalsy();
  });

  test("no sent_at (an older box) counts as on time", async () => {
    const out = collapseReply(await replyPlan(), null, NOW, null, "a");
    expect(out.silent).toBeFalsy();
  });

  test("silent and renotify are never both set (the browser rejects that)", async () => {
    for (const earlier of [null, { count: 1, lastRef: "a" }, { count: 1, lastRef: "b" }]) {
      for (const sent of [null, onTime, late]) {
        const out = collapseReply(await replyPlan(), earlier, NOW, sent, "a");
        expect(out.silent && out.renotify).toBeFalsy();
      }
    }
  });

  test("junk left in an older notification's data counts as none", async () => {
    const out = collapseReply(await replyPlan(), { count: "x" }, NOW, null, "a");
    expect(out.collapse.count).toBe(1);
  });

  test("oneAtATime starts a step only after the one before it finished, even one that threw", async () => {
    const run = oneAtATime();
    const order: string[] = [];
    const step = (name: string, ms: number, fail = false) => () =>
      new Promise<void>((resolve, reject) =>
        setTimeout(() => {
          order.push(name);
          if (fail) reject(new Error(name));
          else resolve();
        }, ms),
      );
    const a = run(step("a", 30, true));
    const b = run(step("b", 1));
    await expect(a).rejects.toThrow("a");
    await b;
    expect(order).toEqual(["a", "b"]);
  });

  test("decodePushData reads sent_at only as a number", () => {
    expect(decodePushData({ type: "turn_ready", ref: "r", sent_at: 1_800_000_000.5 }).sent_at).toBe(1_800_000_000.5);
    expect(decodePushData({ type: "turn_ready", ref: "r", sent_at: "now" }).sent_at).toBeUndefined();
  });
});
