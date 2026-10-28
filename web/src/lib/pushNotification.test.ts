import { describe, expect, test, vi } from "vitest";
import {
  chosenFaceIcon,
  decodePushData,
  DENY_ACTION,
  REVIEW_ACTION,
  GENERIC_BODY,
  mustShowEveryPush,
  PERMISSION_TAG_PREFIX,
  planNotification,
  PREVIEW_CHARS,
  preview,
  settlesPermissionPrompts,
  turnText,
} from "./pushNotification";

describe("planNotification", () => {
  test("turn_ready fetches the reply's text by ref and shows it", async () => {
    const fetchText = vi.fn(async () => "Your 3pm moved to 4.");
    const r = await planNotification({ type: "turn_ready", ref: "t1" }, false, fetchText);
    expect(fetchText).toHaveBeenCalledWith("t1");
    expect(r.notification?.body).toBe("Your 3pm moved to 4.");
    expect(r.badge).toBe(true);
  });

  test("turn_ready whose fetch fails shows the generic line, never an empty notification", async () => {
    for (const fetchText of [async () => null, async () => "   ", async () => Promise.reject(new Error("offline"))]) {
      const r = await planNotification({ type: "turn_ready", ref: "t1" }, false, fetchText);
      expect(r.notification?.body).toBe(GENERIC_BODY);
    }
  });

  test("turn_ready without a ref does not fetch and is generic", async () => {
    const fetchText = vi.fn(async () => "x");
    const r = await planNotification({ type: "turn_ready" }, false, fetchText);
    expect(fetchText).not.toHaveBeenCalled();
    expect(r.notification?.body).toBe(GENERIC_BODY);
  });

  test("notify uses its own body and fetches nothing", async () => {
    const fetchText = vi.fn(async () => "x");
    const r = await planNotification({ type: "notify", ref: "t2", alert_body: "Water the plants" }, false, fetchText);
    expect(fetchText).not.toHaveBeenCalled();
    expect(r.notification?.body).toBe("Water the plants");
  });

  test("a focused tab gets no banner and no badge for a reply (it plays its own sound)", async () => {
    const r = await planNotification({ type: "turn_ready", ref: "t1" }, true, async () => "hi");
    expect(r).toEqual({ notification: null, badge: false });
  });

  test("a permission request shows the server's title and body, with Review and Deny but never Allow", async () => {
    const r = await planNotification(
      { type: "permission_request", ref: "p1", alert_title: "Search your notes", alert_body: "Looking for the trip notes" },
      false,
      async () => null,
    );
    expect(r.notification).toMatchObject({
      title: "Search your notes",
      body: "Looking for the trip notes",
      requireInteraction: true,
      requestId: "p1",
    });
    expect(r.notification?.actions?.map((a) => a.title)).toEqual(["Review", "Deny"]);
    expect(r.notification?.actions?.map((a) => a.action)).toEqual([REVIEW_ACTION, DENY_ACTION]);
  });

  test("a long permission body is cut like any preview", async () => {
    const r = await planNotification({ type: "permission_request", ref: "p1", alert_body: "x".repeat(500) }, false, async () => null);
    expect(Array.from(r.notification!.body)).toHaveLength(PREVIEW_CHARS);
  });

  test("a permission request opens the app, carries no tool input, and stays until acted on", async () => {
    const r = await planNotification({ type: "permission_request", ref: "p1" }, false, async () => "x");
    expect(r.notification?.title).toBe("Marlow needs your OK");
    expect(r.notification?.requireInteraction).toBe(true);
    expect(r.notification?.body).not.toMatch(/rm -rf|command/);
  });

  test("reauth notifications show even when focused: the strip says it too, but this one must not be missed", async () => {
    const r = await planNotification({ type: "reauth_needed", ref: "x" }, true, async () => null);
    expect(r.notification?.body).toMatch(/expired/);
  });

  test("an unknown type from a newer server is generic, not empty", async () => {
    const r = await planNotification({ type: "something_new" }, false, async () => null);
    expect(r.notification?.body).toBe(GENERIC_BODY);
  });
});

describe("Safari must show a notification for every push", () => {
  test("a focused-tab push on Safari becomes a silent notification closed at once, never nothing", async () => {
    const r = await planNotification({ type: "turn_ready", ref: "t1" }, true, async () => "hi", true);
    expect(r.notification).toMatchObject({ silent: true, closeImmediately: true });
    expect(r.notification?.body).toBe(GENERIC_BODY);
  });

  test("an unfocused push on Safari is the ordinary notification", async () => {
    const r = await planNotification({ type: "turn_ready", ref: "t1" }, false, async () => "hi", true);
    expect(r.notification).toMatchObject({ body: "hi" });
    expect(r.notification?.closeImmediately).toBeUndefined();
  });

  test("only WebKit Safari needs it", () => {
    const safari = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15";
    const chrome = "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Mobile Safari/537.36";
    const firefox = "Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0";
    const edge = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36 Edg/129.0";
    expect(mustShowEveryPush(safari)).toBe(true);
    expect(mustShowEveryPush(chrome)).toBe(false);
    expect(mustShowEveryPush(firefox)).toBe(false);
    expect(mustShowEveryPush(edge)).toBe(false);
  });
});

describe("helpers", () => {
  test("preview truncates by code point with an ellipsis", () => {
    const long = "😀".repeat(PREVIEW_CHARS + 10);
    const p = preview(long);
    expect(Array.from(p)).toHaveLength(PREVIEW_CHARS);
    expect(p.endsWith("…")).toBe(true);
    expect(p).not.toMatch(/�/);
  });

  test("turnText takes the result, or the error for a failed turn", () => {
    expect(turnText({ status: "done", result_text: "ok" })).toBe("ok");
    expect(turnText({ status: "error", error: "boom", result_text: null })).toBe("boom");
    expect(turnText({ status: "running" })).toBeNull();
    expect(turnText("nonsense")).toBeNull();
  });

  test("decodePushData is lenient", () => {
    expect(decodePushData(null)).toEqual({});
    expect(decodePushData({ type: 1, ref: "r" })).toEqual({ type: undefined, ref: "r", alert_body: undefined });
  });
});

describe("a sticky permission notification is closed once its turn has finished", () => {
  test("turn_ready and notify settle it; another prompt or a reauth does not", () => {
    expect(settlesPermissionPrompts({ type: "turn_ready", ref: "t1" })).toBe(true);
    expect(settlesPermissionPrompts({ type: "notify", ref: "t1" })).toBe(true);
    expect(settlesPermissionPrompts({ type: "permission_request", ref: "p2" })).toBe(false);
    expect(settlesPermissionPrompts({ type: "reauth_needed" })).toBe(false);
    expect(settlesPermissionPrompts({})).toBe(false);
  });

  test("the permission notification carries the tag the closer looks for", async () => {
    const plan = await planNotification({ type: "permission_request", ref: "p1" }, false, async () => null);
    expect(plan.notification?.tag.startsWith(PERMISSION_TAG_PREFIX)).toBe(true);
    expect(plan.notification?.requireInteraction).toBe(true);
  });
});

describe("chosenFaceIcon", () => {
  test("shows the picked face, not the home-screen icon", async () => {
    expect(await chosenFaceIcon(async () => 7)).toBe("/faces/icon-7.jpg");
  });

  test("face 1 when nothing was picked, the record is unknown, or the store fails", async () => {
    expect(await chosenFaceIcon(async () => undefined)).toBe("/faces/icon-1.jpg");
    expect(await chosenFaceIcon(async () => 42)).toBe("/faces/icon-1.jpg");
    expect(await chosenFaceIcon(() => Promise.reject(new Error("no IndexedDB")))).toBe("/faces/icon-1.jpg");
  });
});
