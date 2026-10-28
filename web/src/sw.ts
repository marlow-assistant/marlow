/// <reference lib="webworker" />
// The service worker: Web Push notifications and the app badge. The
// decisions live in lib/pushNotification.ts, where they are tested; this file
// only wires them to the worker's events.
//
// It does not cache the app for offline use: the chat is useless without the
// box, and a stale cached shell after a deploy is a worse failure than a
// clear one.

import {
  chosenFaceIcon,
  DENY_ACTION,
  decodePushData,
  mustShowEveryPush,
  PERMISSION_TAG_PREFIX,
  planNotification,
  settlesPermissionPrompts,
  turnText,
} from "./lib/pushNotification";
import { getToken, kvGet, KV_FACE } from "./lib/storage";

declare const self: ServiceWorkerGlobalScope;

const FETCH_TIMEOUT_MS = 8_000;

self.addEventListener("install", () => {
  void self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", (event) => {
  event.waitUntil(handlePush(event));
});

async function handlePush(event: PushEvent): Promise<void> {
  let raw: unknown = null;
  try {
    raw = event.data?.json();
  } catch {
    raw = null;
  }
  const data = decodePushData(raw);
  if (settlesPermissionPrompts(data)) {
    for (const n of await self.registration.getNotifications()) {
      if (n.tag.startsWith(PERMISSION_TAG_PREFIX)) n.close();
    }
  }
  const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  const focused = windows.some((c) => c.focused && c.visibilityState === "visible");
  const plan = await planNotification(
    data,
    focused,
    fetchTurnText,
    mustShowEveryPush(self.navigator.userAgent),
  );
  if (plan.badge) await setBadge();
  if (plan.notification) {
    const { title, closeImmediately, requestId, ...rest } = plan.notification;
    await self.registration.showNotification(title, {
      ...rest,
      icon: await chosenFaceIcon(() => kvGet(KV_FACE)),
      data: { url: "/", requestId },
    } as NotificationOptions);
    if (closeImmediately) {
      for (const n of await self.registration.getNotifications({ tag: rest.tag })) n.close();
    }
  }
}

/** The reply's own text, fetched from the box with the stored token; the
 * push service never saw it. */
async function fetchTurnText(ref: string): Promise<string | null> {
  const token = await getToken();
  if (!token) return null;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  try {
    const r = await fetch(`/v1/turn/${encodeURIComponent(ref)}`, {
      headers: { Authorization: `Bearer ${token}` },
      signal: controller.signal,
      cache: "no-store",
    });
    if (!r.ok) return null;
    return turnText(await r.json());
  } finally {
    clearTimeout(timer);
  }
}

async function answerDeny(requestId: string): Promise<void> {
  const token = await getToken();
  if (!token) return;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  try {
    await fetch(`/v1/permission/${encodeURIComponent(requestId)}`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ decision: "deny" }),
      signal: controller.signal,
      cache: "no-store",
    });
  } catch {
    // See the caller.
  } finally {
    clearTimeout(timer);
  }
}

/** A binary unread mark, not an invented count: the server has no per-device
 * read state (push.py's APNs `badge: 1`, same reasoning). The page clears it
 * when it becomes visible. An explicit 1, not the no-argument "flag" badge:
 * an iPhone home-screen web app showed no mark at all with the flag (Mike,
 * 2026-10-25); with 1 the mark shows (confirmed the same day). Android launchers draw a
 * dot either way; desktop Chrome shows "1". */
async function setBadge(): Promise<void> {
  try {
    await (self.navigator as Navigator & { setAppBadge?: (n?: number) => Promise<void> }).setAppBadge?.(1);
  } catch {
    // Badging unsupported here.
  }
}

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const requestId = (event.notification.data as { requestId?: unknown } | null)?.requestId;
  if (event.action === DENY_ACTION && typeof requestId === "string") {
    // Answered from here, without opening the app. A failure (too late, or
    // offline) needs no reporting: the request times out to a deny anyway.
    event.waitUntil(answerDeny(requestId));
    return;
  }
  event.waitUntil(
    (async () => {
      const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      if (windows[0]) {
        await windows[0].focus();
        return;
      }
      await self.clients.openWindow("/");
    })(),
  );
});
