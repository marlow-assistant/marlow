// Web Push, client half (docs/tasks/portable-web-client.md). The service
// worker (src/sw.ts) shows the notifications; this registers it and keeps the
// browser's subscription registered with the box.
//
// The service worker and push need a secure context: the box's tailnet
// certificate gives the deployed client one. The dev server's plain HTTP on
// 127.0.0.1 also counts as secure (localhost is), so the worker registers
// there too; push itself is Mike's to check on the real box.

import { useEffect, useState } from "react";
import { CancelledError, isTransientSendError, type Api } from "../lib/api";
import { PERMISSION_TAG_PREFIX } from "../lib/pushNotification";

/** `failed`: permission granted, but the box refused or could not store the
 * subscription (for example a push service outside the server's allowlist,
 * such as Edge on Windows). Said out loud rather than shown as "on". */
export type PushState = "unsupported" | "off" | "on" | "denied" | "failed";

export function registerServiceWorker(): void {
  if (!("serviceWorker" in navigator) || import.meta.env.DEV) return;
  // Registered from the site root so its scope is the whole app. The server
  // serves /sw.js no-cache, so a deploy reaches the next load.
  void navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
}

/** Set when the last subscription attempt failed for a reason a retry will
 * not fix (the box refused the subscription, the browser's push service
 * refused us). Module-level, not per hook: Settings mounts long after the
 * attempt and must still say "failed", and the browser's permission alone
 * reads "granted" whether or not push works (review, 2026-10-02). */
let subscribeFailed = false;

export function pushState(): PushState {
  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
    return "unsupported";
  }
  if (Notification.permission === "denied") return "denied";
  if (Notification.permission !== "granted") return "off";
  return subscribeFailed ? "failed" : "on";
}

/** Test support: forget the last attempt, as a fresh launch does. */
export function resetPushStateForTests(): void {
  subscribeFailed = false;
}

function announce(): void {
  window.dispatchEvent(new CustomEvent(PUSH_CHANGED));
}

/** From a user gesture (the prompt or Settings): ask, then subscribe. Every
 * `usePushState` hears the result, so turning it on in one place updates the
 * other. */
export async function enablePush(api: Api): Promise<PushState> {
  if (pushState() === "unsupported") return "unsupported";
  const permission = await Notification.requestPermission();
  // The user is waiting on this tap, so any failure is reported, transient or
  // not; the next launch's sync clears it if the box takes the subscription.
  if (permission === "granted" && !(await syncPushSubscription(api))) subscribeFailed = true;
  announce();
  return pushState();
}

const PUSH_CHANGED = "marlow:push-changed";

/** The push state, kept current: a subscription attempt anywhere, the
 * permission changing in the browser's or Android's settings, or the app
 * coming back to the foreground (where a permission change made elsewhere
 * shows up when the Permissions API has no change event). */
export function usePushState(): PushState {
  const [state, setState] = useState<PushState>(pushState);
  useEffect(() => {
    const recheck = () => setState(pushState());
    const visible = () => {
      if (document.visibilityState === "visible") recheck();
    };
    window.addEventListener(PUSH_CHANGED, recheck);
    document.addEventListener("visibilitychange", visible);
    let status: PermissionStatus | null = null;
    let cancelled = false;
    void navigator.permissions
      ?.query({ name: "notifications" })
      .then((s) => {
        if (cancelled) return;
        status = s;
        s.addEventListener("change", recheck);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
      window.removeEventListener(PUSH_CHANGED, recheck);
      document.removeEventListener("visibilitychange", visible);
      status?.removeEventListener("change", recheck);
    };
  }, []);
  return state;
}

/** On every launch, like iOS re-registering its APNs token: subscriptions
 * rotate (cleared site data, a browser update), and re-registering is
 * idempotent on the server. Silent when permission was never granted.
 *
 * The outcome feeds pushState: success clears `failed`, a permanent failure
 * sets it, and a transient one (box unreachable on a wake, a 5xx) leaves it
 * as it was rather than tell the user push is broken. */
export async function syncPushSubscription(api: Api): Promise<boolean> {
  if (pushState() === "unsupported" || Notification.permission !== "granted") return false;
  try {
    const reg = await navigator.serviceWorker.ready;
    const key = await api.webPushKey();
    const keyBytes = b64urlToBytes(key);
    let sub = await reg.pushManager.getSubscription();
    // A subscription made with another box's key (re-paired elsewhere, or the
    // box's key regenerated) can never be delivered to: replace it.
    if (sub && !sameKey(sub.options.applicationServerKey, keyBytes)) {
      await sub.unsubscribe();
      sub = null;
    }
    sub ??= await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes });
    await api.registerWebPush(sub.toJSON());
    if (subscribeFailed) {
      subscribeFailed = false;
      announce();
    }
    return true;
  } catch (e) {
    // No push is a degraded client, not a broken one: the 15s sync still
    // brings everything in while the tab is open.
    if (!subscribeFailed && !isTransientSendError(e) && !(e instanceof CancelledError)) {
      subscribeFailed = true;
      announce();
    }
    return false;
  }
}

function b64urlToBytes(s: string): Uint8Array<ArrayBuffer> {
  const b64 = s.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (s.length % 4)) % 4);
  const bin = atob(b64);
  const out = new Uint8Array(new ArrayBuffer(bin.length));
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function sameKey(a: ArrayBuffer | null, b: Uint8Array): boolean {
  if (!a) return false;
  const x = new Uint8Array(a);
  return x.length === b.length && x.every((v, i) => v === b[i]);
}

/** The open app shows any pending prompt itself (its permissions poll), so a
 * permission notification is redundant once the app is in front, and stale if
 * the prompt was settled elsewhere. Called when the page becomes visible. */
export async function closePermissionNotifications(): Promise<void> {
  try {
    if (!("serviceWorker" in navigator)) return;
    const reg = await navigator.serviceWorker.getRegistration();
    for (const n of (await reg?.getNotifications()) ?? []) {
      if (n.tag.startsWith(PERMISSION_TAG_PREFIX)) n.close();
    }
  } catch {
    // Best effort: the next turn_ready closes it anyway.
  }
}

/** Unpairing, or a 401: drop the browser's subscription. The server still
 * holds it against the old token (revoking a device token server-side is a
 * pairing change this client does not make), but an unsubscribed endpoint
 * answers 410 at the next push, which prunes it there. Without this the
 * browser would go on receiving the box's notifications after it was told to
 * forget the box, and twice over once it re-paired. */
export async function unsubscribePush(): Promise<void> {
  try {
    if (!("serviceWorker" in navigator)) return;
    const reg = await navigator.serviceWorker.getRegistration();
    const sub = await reg?.pushManager.getSubscription();
    await sub?.unsubscribe();
  } catch {
    // Nothing more this side can do.
  }
}
