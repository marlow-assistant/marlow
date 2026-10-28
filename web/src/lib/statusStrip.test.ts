import { describe, expect, test } from "vitest";
import { INITIAL_FACTS, banners, healthToFacts } from "./statusStrip";

const all = { reauthNeeded: true, queuePaused: true, reauthPrewarn: true };

describe("banners", () => {
  test("nothing is shown when nothing is wrong", () => {
    expect(banners(INITIAL_FACTS)).toEqual([]);
  });

  test("reauth needed is red and exact", () => {
    expect(banners({ ...INITIAL_FACTS, reauthNeeded: true })).toEqual([
      { id: "reauthNeeded", tone: "red", text: "Marlow needs reauthorization — SSH in and run `claude`." },
    ]);
  });

  test("paused is orange", () => {
    expect(banners({ ...INITIAL_FACTS, queuePaused: true })).toEqual([
      { id: "queuePaused", tone: "orange", text: "Marlow is paused." },
    ]);
  });

  test("prewarn is orange alone", () => {
    expect(banners({ ...INITIAL_FACTS, reauthPrewarn: true })).toEqual([
      {
        id: "reauthPrewarn",
        tone: "orange",
        text: "Marlow's Claude session expires soon — SSH in and run `claude`.",
      },
    ]);
  });

  test("reauth needed suppresses the prewarn banner", () => {
    expect(banners(all).map((b) => b.id)).toEqual(["reauthNeeded", "queuePaused"]);
  });

  test("order is reauth, paused, prewarn", () => {
    expect(banners({ ...all, reauthNeeded: false }).map((b) => b.id)).toEqual(["queuePaused", "reauthPrewarn"]);
  });
});

describe("healthToFacts", () => {
  test("auth_ok false raises reauth even when status is not reauth_needed", () => {
    const f = healthToFacts(INITIAL_FACTS, { status: "error", auth_ok: false });
    expect(f.reauthNeeded).toBe(true);
    expect(f.queuePaused).toBe(false);
  });

  test("auth_ok true clears reauth", () => {
    const f = healthToFacts({ ...INITIAL_FACTS, reauthNeeded: true }, { status: "ok", auth_ok: true });
    expect(f.reauthNeeded).toBe(false);
  });

  test("a response without auth_ok leaves reauth as it was", () => {
    expect(healthToFacts(INITIAL_FACTS, { status: "ok" }).reauthNeeded).toBe(false);
    expect(healthToFacts({ ...INITIAL_FACTS, reauthNeeded: true }, { status: "ok" }).reauthNeeded).toBe(true);
  });

  test("prewarn raises its own banner and reauth needed suppresses it", () => {
    const prewarn = healthToFacts(INITIAL_FACTS, { auth_ok: true, reauth_prewarn: true });
    expect(prewarn.reauthPrewarn).toBe(true);
    expect(prewarn.reauthNeeded).toBe(false);
    const both = healthToFacts(INITIAL_FACTS, { auth_ok: false, reauth_prewarn: true });
    expect(both.reauthNeeded).toBe(true);
    expect(both.reauthPrewarn).toBe(false);
  });

  test("queue paused raises the banner", () => {
    const f = healthToFacts(INITIAL_FACTS, { auth_ok: true, queue_paused: true });
    expect(f.queuePaused).toBe(true);
    expect(f.reauthNeeded).toBe(false);
  });

  test("model and build only overwrite when reported", () => {
    const first = healthToFacts(INITIAL_FACTS, { auth_ok: true, model: "opus", git_branch: "main", git_commit: "abc" });
    expect(first.model).toBe("opus");
    expect(first.build).toBe("main@abc");
    const older = healthToFacts(first, { auth_ok: true });
    expect(older.model).toBe("opus");
    expect(older.build).toBe("main@abc");
    const partial = healthToFacts(first, { auth_ok: true, git_branch: "x" });
    expect(partial.build).toBe("main@abc");
  });

  test("does not mutate the previous facts", () => {
    const prev = { ...INITIAL_FACTS };
    healthToFacts(prev, { auth_ok: false, queue_paused: true });
    expect(prev).toEqual(INITIAL_FACTS);
  });
});

describe("the >= 90% usage row", () => {
  test("is yellow and ranks below every other status", () => {
    const all = banners({ reauthNeeded: true, queuePaused: true, reauthPrewarn: false, usageHigh: "93% of this 5 hours used — resets 8:59 PM." });
    expect(all.map((b) => b.id)).toEqual(["reauthNeeded", "queuePaused", "usageHigh"]);
    expect(all[2]).toEqual({ id: "usageHigh", tone: "yellow", text: "93% of this 5 hours used — resets 8:59 PM." });
  });

  test("is absent without a line", () => {
    expect(banners({ ...INITIAL_FACTS, usageHigh: null })).toEqual([]);
  });
});
