// What the service worker shows for a Web Push payload, as a pure function so
// it can be tested without a worker. The payload mirrors push.py's
// PushPayload: {type, ref}, plus alert_body for `notify` and
// `permission_request`, and alert_title for `permission_request`
// (server/approval_summary.push_text); webpush.py adds `sent_at` to every one. A `turn_ready`
// carries no text (the push service never sees the reply), so its body comes
// from /v1/turn/{ref}, fetched with the stored token, the same design as iOS's
// NotificationService. If that fails the body is a generic line: never an
// empty notification.

import { DEFAULT_FACE, faceNotificationIcon, parseFace } from "./faces";

export const GENERIC_BODY = "Marlow has something for you.";
/** Every permission notification's tag starts with this, so they can be
 * found again and closed once settled. */
export const PERMISSION_TAG_PREFIX = "permission-";
export const PREVIEW_CHARS = 180;
/** The notification action that answers Deny without opening the app. */
export const DENY_ACTION = "deny";
export const REVIEW_ACTION = "review";

export interface PushPayloadData {
  type?: string;
  ref?: string;
  alert_body?: string;
  alert_title?: string;
  /** When the box sent it, epoch seconds (webpush.py). */
  sent_at?: number;
}

export interface NotificationPlan {
  title: string;
  body: string;
  tag: string;
  requireInteraction?: boolean;
  silent?: boolean;
  /** Show it (the browser requires one per push) and close it at once. */
  closeImmediately?: boolean;
  actions?: { action: string; title: string }[];
  /** The permission request a Deny action answers. */
  requestId?: string;
}

export interface PlanResult {
  /** null: show nothing (a focused tab announces it itself). */
  notification: NotificationPlan | null;
  /** Set the app badge (Badging API). */
  badge: boolean;
}

/** Fetches a turn's settled text, or null on any failure. */
export type FetchTurnText = (turnId: string) => Promise<string | null>;

export function decodePushData(raw: unknown): PushPayloadData {
  if (!raw || typeof raw !== "object") return {};
  const r = raw as Record<string, unknown>;
  return {
    type: typeof r.type === "string" ? r.type : undefined,
    ref: typeof r.ref === "string" ? r.ref : undefined,
    alert_body: typeof r.alert_body === "string" ? r.alert_body : undefined,
    alert_title: typeof r.alert_title === "string" ? r.alert_title : undefined,
    sent_at: typeof r.sent_at === "number" ? r.sent_at : undefined,
  };
}

export function preview(text: string | null | undefined): string {
  const t = (text ?? "").trim();
  if (!t) return GENERIC_BODY;
  const cps = Array.from(t);
  return cps.length > PREVIEW_CHARS ? `${cps.slice(0, PREVIEW_CHARS - 1).join("")}…` : t;
}

/** Chrome and Firefox let a push with a focused tab show nothing. Safari
 * (WebKit) has no such exemption: a push that shows no notification counts
 * against the site, and enough of them revoke its push permission [derived:
 * from WebKit's documented rule, not observed here]. There, the "nothing"
 * becomes a silent notification closed at once. */
export function mustShowEveryPush(userAgent: string): boolean {
  return /Safari\//.test(userAgent) && !/(Chrome|Chromium|CriOS|Edg|Firefox|FxiOS)\//.test(userAgent);
}

export async function planNotification(
  data: PushPayloadData,
  focused: boolean,
  fetchTurnText: FetchTurnText,
  mustShow = false,
): Promise<PlanResult> {
  const plan = await planFor(data, focused, fetchTurnText);
  if (plan.notification === null && mustShow) {
    return {
      notification: { title: "Marlow", body: GENERIC_BODY, tag: "marlow-silent", silent: true, closeImmediately: true },
      badge: plan.badge,
    };
  }
  return plan;
}

async function planFor(
  data: PushPayloadData,
  focused: boolean,
  fetchTurnText: FetchTurnText,
): Promise<PlanResult> {
  switch (data.type) {
    case "turn_ready":
    case "notify": {
      // A focused tab plays the receive sound itself (FBK-2); a banner on top
      // would announce the same reply twice, and the badge means "arrived
      // while you were away".
      if (focused) return { notification: null, badge: false };
      let body: string;
      if (data.type === "notify") {
        body = preview(data.alert_body);
      } else {
        const text = data.ref ? await fetchTurnText(data.ref).catch(() => null) : null;
        body = preview(text);
      }
      return { notification: { title: "Marlow", body, tag: REPLY_TAG }, badge: true };
    }
    case "permission_request":
      // The only path that reaches an idle desktop (PRM-4). Review and Deny,
      // never Allow, as on iOS's lock screen (Mike, 2026-10-02): approving
      // happens only on the card, beside the preview and the raw details.
      // Deny is one tap from here because denying is always safe. The title
      // and body are the server's reading of the request; an older server
      // sends neither, and the old generic wording stands in.
      if (focused) return { notification: null, badge: false };
      return {
        notification: {
          title: data.alert_title?.trim() ? data.alert_title : "Marlow needs your OK",
          body: data.alert_body?.trim()
            ? preview(data.alert_body)
            : "Marlow wants to use a tool. Open Marlow to see what and decide.",
          tag: `${PERMISSION_TAG_PREFIX}${data.ref ?? ""}`,
          requireInteraction: true,
          actions: [
            { action: REVIEW_ACTION, title: "Review" },
            { action: DENY_ACTION, title: "Deny" },
          ],
          requestId: data.ref,
        },
        badge: false,
      };
    case "reauth_needed":
      return {
        notification: {
          title: "Marlow",
          body: "Marlow's Claude session has expired. Sign in again to restart it.",
          tag: "reauth",
        },
        badge: false,
      };
    case "reauth_prewarn":
      return {
        notification: {
          title: "Marlow",
          body: "Marlow's Claude session expires soon — sign in again when you get a chance.",
          tag: "reauth",
        },
        badge: false,
      };
    default:
      // A type from a newer server: still never an empty notification.
      if (focused) return { notification: null, badge: false };
      return { notification: { title: "Marlow", body: GENERIC_BODY, tag: "marlow" }, badge: false };
  }
}

/** The permission notification is sticky (requireInteraction), and nothing
 * else ever closes it, so an approval made on another device left "Marlow
 * needs your OK" up on an idle desktop indefinitely (review, 2026-10-02). A
 * `turn_ready` or `notify` means the turn that asked has finished: the work
 * queue runs one turn at a time, so any prompt it raised is settled by then. */
export function settlesPermissionPrompts(data: PushPayloadData): boolean {
  return data.type === "turn_ready" || data.type === "notify";
}

/** Every reply notification (`turn_ready`, `notify`) shares this tag, so a
 * newer one replaces the older instead of stacking. A tag per turn turned a
 * computer waking from sleep into minutes of back-to-back toasts: the push
 * service holds a day of pushes (webpush.py's TTL) and delivers them all at
 * once (raised 2026-11-10). */
export const REPLY_TAG = "marlow-reply";
/** A push that reaches the worker this long after it left the box was held
 * by the push service: the machine was asleep or offline, and the reply was
 * very likely read on another device meanwhile. It is shown without a sound.
 * The server keeps no read state per device, so lateness is the signal. Five
 * minutes leaves room for a computer clock that is off by a little. */
export const LATE_MS = 5 * 60_000;

/** What a reply notification remembers in its data, for the next one. */
export interface ReplyCollapse {
  count: number;
  /** The last reply's ref, so a repeated push for it isn't counted twice. */
  lastRef: string;
}

export interface CollapsedReply extends NotificationPlan {
  renotify?: boolean;
  collapse: ReplyCollapse;
}

/** Folds a reply into the one already showing, if any. `earlier` is that
 * notification's `collapse` data (anything else counts as none); `sentAt` is
 * the push's `sent_at`, epoch seconds, when the box sent one. Never sets both
 * `silent` and `renotify`: showNotification throws on that pair. */
export function collapseReply(
  plan: NotificationPlan,
  earlier: unknown,
  now: number,
  sentAt: number | null,
  ref: string,
): CollapsedReply {
  const prev = asCollapse(earlier);
  const repeat = prev !== null && prev.lastRef === ref;
  const count = repeat ? prev.count : (prev?.count ?? 0) + 1;
  const lateArrival = sentAt !== null && now - sentAt * 1000 > LATE_MS;
  const quiet = repeat || lateArrival;
  return {
    ...plan,
    title: count > 1 ? `Marlow · ${count} new` : plan.title,
    silent: quiet || undefined,
    // Without it, replacing a showing notification makes no sound or toast.
    renotify: (!quiet && prev !== null) || undefined,
    collapse: { count, lastRef: ref },
  };
}

/** A runner that starts each step only after the previous one has settled.
 * The worker handles pushes concurrently, and each awaits its reply fetch, so
 * a wake-up backlog would otherwise read the showing reply notification
 * together and fold into it as though each were the first ("2 new" for
 * eight). One failed step doesn't stop the next. */
export function oneAtATime(): (step: () => Promise<void>) => Promise<void> {
  let tail: Promise<void> = Promise.resolve();
  return (step) => {
    const next = tail.then(step, step);
    tail = next.catch(() => {});
    return next;
  };
}

function asCollapse(v: unknown): ReplyCollapse | null {
  if (!v || typeof v !== "object") return null;
  const c = v as Record<string, unknown>;
  return typeof c.count === "number" && typeof c.lastRef === "string"
    ? { count: c.count, lastRef: c.lastRef }
    : null;
}

/** The text a finished turn would show: its result, or its error. */
export function turnText(turn: unknown): string | null {
  if (!turn || typeof turn !== "object") return null;
  const t = turn as { status?: unknown; result_text?: unknown; error?: unknown };
  const v = t.status === "error" ? t.error : t.result_text;
  return typeof v === "string" ? v : null;
}

/** The icon for a notification: the face the user picked, read from the page's
 * own store. It used to be the home-screen icon, so every notification wore
 * face 1 whatever was picked (Mike, 2026-10-16). A store that can't be read
 * gives face 1, never a notification without an icon. */
export async function chosenFaceIcon(readFace: () => Promise<unknown>): Promise<string> {
  try {
    return faceNotificationIcon(parseFace(await readFace()));
  } catch {
    return faceNotificationIcon(DEFAULT_FACE);
  }
}
