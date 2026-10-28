// The one place the client talks HTTP. Same-origin, so every path is relative:
// the client never knows a server URL (docs/tasks/portable-web-client.md).
//
// Errors are classified here, once, because three callers read them three
// different ways (chat-ux-contract.md STA-4, SND-4, POL-5):
//   - TransportError: the request never completed (network down, DNS, a
//     timeout this client raised). The only thing "Can't reach Marlow" may be
//     claimed over.
//   - HttpError: any HTTP response that was not 2xx. Reachable.
//   - DecodeError: a 2xx whose body would not decode. Reachable.
//   - CancelledError: this client aborted the call itself (sign-out, a loop
//     being replaced). Reachable too: counting our own cancel as unreachable
//     would paint "Can't reach Marlow" on every sign-out (STA-4).

import { decodeIdeas, decodePermissionSummary, decodeUsageReport, type Idea, type UsageReport } from "./apiModels";
import { isClockTime, type DayTimes } from "./dayFormat";
import { decodeChanges, type ChangeEntry } from "./whatsNew";
import {
  decodePage,
  decodeTurn,
  type HealthResponse,
  type PermissionDecision,
  type PermissionRequestInfo,
  type TranscriptPage,
  type Turn,
} from "./types";

export class TransportError extends Error {
  readonly timedOut: boolean;
  constructor(message: string, timedOut = false) {
    super(message);
    this.name = "TransportError";
    this.timedOut = timedOut;
  }
}

export class HttpError extends Error {
  readonly status: number;
  readonly detail: string | null;
  constructor(status: number, detail: string | null) {
    super(detail ? `Server error ${status}: ${detail}` : `Server error ${status}`);
    this.name = "HttpError";
    this.status = status;
    this.detail = detail;
  }
}

export class DecodeError extends Error {
  constructor(message = "The server's answer could not be read.") {
    super(message);
    this.name = "DecodeError";
  }
}

export class CancelledError extends Error {
  constructor() {
    super("cancelled");
    this.name = "CancelledError";
  }
}

/** SND-4: the send path's rule. Everything else is permanent. */
export function isTransientSendError(e: unknown): boolean {
  if (e instanceof TransportError) return true;
  if (e instanceof HttpError) return e.status >= 500;
  return false;
}

/** STA-4: only a request that never completed is a claim about the network. */
export function isTransportFailure(e: unknown): boolean {
  return e instanceof TransportError;
}

/** WEB-2: `fetch` has no useful default timeout. iOS's sync and health ride
 * the platform's 60s; the web picks 20s for everything, the number the send
 * already uses (SND-2), so one flat bound covers every call. An attachment
 * is the one exception: a photo over a poor link can legitimately take longer
 * than a JSON page. */
export const DEFAULT_TIMEOUT_MS = 20_000;
export const ATTACHMENT_TIMEOUT_MS = 60_000;
/** A segment waits behind any other being transcribed on the box, one at a
 * time, and a 25 s segment took ~4 s on the slowest box measured. */
export const TRANSCRIBE_TIMEOUT_MS = 45_000;

export interface RequestOptions {
  method?: "GET" | "POST";
  body?: unknown;
  timeoutMs?: number;
  signal?: AbortSignal;
  /** Unauthenticated calls (pairing) pass false. */
  auth?: boolean;
  /** Sent as is, with its own type, instead of `body` as JSON: a recorded
   * audio segment (`transcribeSegment`). */
  raw?: Blob;
}

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export interface ApiOptions {
  getToken: () => string | null;
  /** A 401 on an authenticated call means the token is gone (revoked, or the
   * box's pairing store was reset). The app returns to pairing (WEB-8). */
  onUnauthorized?: () => void;
  fetch?: FetchLike;
  /** Injected for tests; the page's own visibility and focus by default. */
  isOnScreen?: () => boolean;
}

/** The service worker's test for a page that announces a push itself
 * (sw.ts: `c.focused && c.visibilityState === "visible"`). */
const pageOnScreen = () =>
  typeof document !== "undefined" && document.visibilityState === "visible" && document.hasFocus();

export interface SendMessageBody {
  text: string;
  client_msg_id: string;
  timezone?: string;
  effort?: string;
  /** Absent means "no location from this device right now"; the server clears
   * what it holds. Never null, never an empty object (message-location.md). */
  location?: { latitude: number; longitude: number; accuracy_m?: number };
  images?: { b64: string; mime: "image/jpeg" }[];
  /** "Do it" on an idea (docs/tasks/ideas-panel.md). Absent for a normal
   * send; the server warns and drops an id that is not open, and still sends. */
  idea_id?: string;
  /** Spoken in voice mode; the reply will be read aloud. Absent for a typed
   * message (server `MessageRequest.spoken`). */
  spoken?: true;
}

export class Api {
  private readonly getToken: () => string | null;
  private readonly onUnauthorized: () => void;
  private readonly fetchImpl: FetchLike;
  private readonly isOnScreen: () => boolean;

  constructor(opts: ApiOptions) {
    this.getToken = opts.getToken;
    this.onUnauthorized = opts.onUnauthorized ?? (() => {});
    this.fetchImpl = opts.fetch ?? ((input, init) => globalThis.fetch(input, init));
    this.isOnScreen = opts.isOnScreen ?? pageOnScreen;
  }

  /** One request, bounded by a wall-clock timeout, with the outcome
   * classified. `read` consumes the body INSIDE that bound: the timer runs
   * until the body is read, not just until the headers arrive, so a link that
   * stalls mid-response (a phone dropping off the tailnet on a wake from
   * Doze) times out like one that never answered instead of hanging the sync
   * loop for good (review, 2026-10-02). */
  private async request<T>(path: string, opts: RequestOptions, read: (r: Response) => Promise<T>): Promise<T> {
    const timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    const controller = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeoutMs);
    const outer = opts.signal;
    const onOuterAbort = () => controller.abort();
    if (outer) {
      if (outer.aborted) {
        clearTimeout(timer);
        throw new CancelledError();
      }
      outer.addEventListener("abort", onOuterAbort, { once: true });
    }

    try {
      const headers: Record<string, string> = {};
      if (opts.auth !== false) {
        const token = this.getToken();
        if (!token) {
          this.onUnauthorized();
          throw new HttpError(401, "not paired");
        }
        headers.Authorization = `Bearer ${token}`;
        // The box holds back a web push to a page on screen, which iOS Safari
        // would otherwise flash a blank banner over
        // (docs/tasks/web-push-on-screen.md). A hidden tab still polls, so
        // only a request made on screen says so.
        if (this.isOnScreen()) headers["X-Marlow-Foreground"] = "1";
      }
      if (opts.body !== undefined) headers["Content-Type"] = "application/json";
      else if (opts.raw) headers["Content-Type"] = opts.raw.type;

      let response: Response;
      try {
        response = await this.fetchImpl(path, {
          method: opts.method ?? "GET",
          headers,
          body: opts.body !== undefined ? JSON.stringify(opts.body) : opts.raw,
          signal: controller.signal,
          // Same-origin only. No credentials beyond the bearer header, and the
          // token never goes in a URL (SYN-7).
          credentials: "same-origin",
          cache: "no-store",
          referrerPolicy: "no-referrer",
        });
      } catch (e) {
        // A timeout we raised is a transport failure (SND-2: it must retry, so it
        // must not look like a cancel). Our own cancel is told apart from it by
        // the flag, not by the error's name, which is AbortError either way.
        if (timedOut) throw new TransportError("timed out", true);
        if (outer?.aborted) throw new CancelledError();
        throw new TransportError(e instanceof Error ? e.message : "network error");
      }

      if (!response.ok) {
        let detail: string | null = null;
        try {
          const body: unknown = await response.json();
          if (body && typeof body === "object" && "detail" in body) {
            const d = (body as { detail: unknown }).detail;
            detail = typeof d === "string" ? d : JSON.stringify(d);
          }
        } catch {
          // A body that will not decode is still a response: reachable.
        }
        if (response.status === 401 && opts.auth !== false) this.onUnauthorized();
        throw new HttpError(response.status, detail);
      }

      try {
        return await read(response);
      } catch (e) {
        // Same precedence as the fetch itself: our timeout, then our cancel,
        // then whatever `read` made of it (DecodeError for a bad body).
        if (timedOut) throw new TransportError("timed out", true);
        if (outer?.aborted) throw new CancelledError();
        throw e;
      }
    } finally {
      clearTimeout(timer);
      outer?.removeEventListener("abort", onOuterAbort);
    }
  }

  async json(path: string, opts: RequestOptions = {}): Promise<unknown> {
    return this.request(path, opts, async (response) => {
      try {
        return await response.json();
      } catch {
        throw new DecodeError();
      }
    });
  }

  // --- pairing (unauthenticated) ---------------------------------------------

  async pair(deviceName: string, invite: string): Promise<string> {
    const r = await this.json("/v1/pair", {
      method: "POST",
      body: { device_name: deviceName, invite },
      auth: false,
    });
    return field(r, "device_token");
  }

  /** Revokes this device's token on the box, and its push registration with
   * it. The caller clears local state whether or not this succeeds. */
  async unpair(signal?: AbortSignal): Promise<void> {
    await this.json("/v1/device/unpair", { method: "POST", signal });
  }

  // --- chat --------------------------------------------------------------------

  async sendMessage(body: SendMessageBody, signal?: AbortSignal): Promise<string> {
    const r = await this.json("/v1/message", { method: "POST", body, signal });
    return field(r, "turn_id");
  }

  async getTurn(turnId: string, signal?: AbortSignal): Promise<Turn> {
    return decodeTurn(await this.json(`/v1/turn/${encodeURIComponent(turnId)}`, { signal }));
  }

  async transcript(
    q: { after?: number; before?: number; limit: number },
    signal?: AbortSignal,
  ): Promise<TranscriptPage> {
    const params = new URLSearchParams({ limit: String(q.limit) });
    if (q.after !== undefined) params.set("after", String(q.after));
    if (q.before !== undefined) params.set("before", String(q.before));
    return decodePage(await this.json(`/v1/transcript?${params}`, { signal }));
  }

  async attachment(id: string, signal?: AbortSignal): Promise<Blob> {
    return this.request(
      `/v1/transcript/attachment/${encodeURIComponent(id)}`,
      { signal, timeoutMs: ATTACHMENT_TIMEOUT_MS },
      (r) => r.blob(),
    );
  }

  async permissions(signal?: AbortSignal): Promise<PermissionRequestInfo[]> {
    const r = await this.json("/v1/permissions", { signal });
    const list = (r as { permission_requests?: unknown })?.permission_requests;
    if (!Array.isArray(list)) return [];
    return list
      .filter((p): p is Record<string, unknown> => !!p && typeof p === "object")
      .filter((p) => typeof p.request_id === "string")
      .map((p) => ({
        request_id: p.request_id as string,
        tool: typeof p.tool === "string" ? p.tool : "a tool",
        tool_input:
          p.tool_input && typeof p.tool_input === "object"
            ? (p.tool_input as Record<string, unknown>)
            : {},
        created_at: typeof p.created_at === "number" ? p.created_at : 0,
        expires_at: typeof p.expires_at === "number" ? p.expires_at : 0,
        summary: decodePermissionSummary(p.summary),
      }));
  }

  async answerPermission(requestId: string, decision: PermissionDecision): Promise<boolean> {
    const r = await this.json(`/v1/permission/${encodeURIComponent(requestId)}`, {
      method: "POST",
      body: { decision },
    });
    return (r as { accepted?: unknown })?.accepted === true;
  }

  async health(signal?: AbortSignal): Promise<HealthResponse> {
    const r = await this.json("/health", { signal });
    return (r && typeof r === "object" ? r : {}) as HealthResponse;
  }

  // --- views (phase 5) -------------------------------------------------------

  async activity(before?: number, signal?: AbortSignal): Promise<unknown> {
    const params = new URLSearchParams({ limit: "50" });
    if (before !== undefined) params.set("before", String(before));
    return this.json(`/v1/activity?${params}`, { signal });
  }

  /** What a background job said (docs/tasks/default-infrastructure.md). 404
   * (HttpError) when the turn kept nothing. */
  async activityOutput(turnId: string, signal?: AbortSignal): Promise<unknown> {
    return this.json(`/v1/activity/${encodeURIComponent(turnId)}/output`, { signal });
  }

  async identity(signal?: AbortSignal): Promise<unknown> {
    return this.json("/v1/identity", { signal });
  }

  async identityFile(id: string, signal?: AbortSignal): Promise<unknown> {
    return this.json(`/v1/identity/file?${new URLSearchParams({ id })}`, { signal });
  }

  /** `GET /v1/usage` (docs/tasks/usage-visibility.md). Facts only; the
   * wording is the client's (usageFormat.ts). */
  async usage(signal?: AbortSignal): Promise<UsageReport> {
    return decodeUsageReport(await this.json("/v1/usage", { signal }));
  }

  // --- speech to text ---------------------------------------------------------

  /** `GET /v1/transcribe`: can this box turn speech into text? The mic and
   * voice mode show only when it can (server/transcribe.py). */
  async transcribeAvailable(signal?: AbortSignal): Promise<boolean> {
    const r = await this.json("/v1/transcribe", { signal });
    return (r as { available?: unknown } | null)?.available === true;
  }

  /** `POST /v1/transcribe`: one recorded segment of an utterance, in order
   * from 0. The `final` one answers with the whole utterance's text. */
  async transcribeSegment(
    q: { utteranceId: string; seq: number; final: boolean },
    audio: Blob,
    signal?: AbortSignal,
  ): Promise<{ text: string; transcript: string; serverSeconds: number | null }> {
    const params = new URLSearchParams({ utterance_id: q.utteranceId, seq: String(q.seq), final: String(q.final) });
    const r = (await this.json(`/v1/transcribe?${params}`, {
      method: "POST",
      raw: audio,
      signal,
      timeoutMs: TRANSCRIBE_TIMEOUT_MS,
    })) as { text?: unknown; transcript?: unknown; server_seconds?: unknown } | null;
    if (typeof r?.text !== "string" || typeof r.transcript !== "string") throw new DecodeError();
    const serverSeconds = typeof r.server_seconds === "number" ? r.server_seconds : null;
    return { text: r.text, transcript: r.transcript, serverSeconds };
  }

  // --- ideas -----------------------------------------------------------------

  /** `GET /v1/ideas`: open ideas only, in the store's order. */
  async ideas(signal?: AbortSignal): Promise<Idea[]> {
    return decodeIdeas(await this.json("/v1/ideas", { signal }));
  }

  /** `POST /v1/ideas/{id}/dismiss`. Repeatable: a later call with a reason
   * replaces the earlier one, which is how "Not now" stays one tap and the
   * reason comes after. 404 (HttpError) when the idea is no longer open. */
  async dismissIdea(id: string, reason: string | null, signal?: AbortSignal): Promise<void> {
    await this.json(`/v1/ideas/${encodeURIComponent(id)}/dismiss`, { method: "POST", body: { reason }, signal });
  }

  // --- what's new ---------------------------------------------------------------

  /** `GET /v1/changes`: the box's half of What's new, `server` entries only
   * (docs/tasks/whats-new.md), and whether first-run onboarding has ended.
   * The app's half is bundled. */
  async changes(signal?: AbortSignal): Promise<{ entries: ChangeEntry[]; onboardingComplete: boolean }> {
    const raw = await this.json("/v1/changes", { signal });
    // A box older than the flag has no onboarding to wait for.
    const flag = (raw as { onboarding_complete?: unknown } | null)?.onboarding_complete;
    return { entries: decodeChanges(raw), onboardingComplete: flag !== false };
  }

  // --- preferences -------------------------------------------------------------

  /** `GET /v1/preferences`: what every client shares (lib/preferenceSync.ts).
   * Each is null until picked anywhere. */
  async preferences(signal?: AbortSignal): Promise<{ face: number | null; effort: string | null }> {
    const raw = (await this.json("/v1/preferences", { signal })) as { face?: unknown; effort?: unknown } | null;
    const face = raw?.face;
    const effort = raw?.effort;
    return {
      face: typeof face === "number" && Number.isInteger(face) ? face : null,
      effort: typeof effort === "string" ? effort : null,
    };
  }

  /** Only what is pending; the box keeps the rest. */
  async setPreferences(update: { face?: number | string; effort?: number | string }, signal?: AbortSignal): Promise<void> {
    await this.json("/v1/preferences", { method: "POST", body: update, signal });
  }

  /** `GET /v1/preferences`, the "Your day" half. Server state only: unlike
   * face and effort there is no device copy, so Settings asks each time it
   * opens. A box that predates the setting has no such fields: DecodeError. */
  async dayTimes(signal?: AbortSignal): Promise<DayTimes> {
    return decodeDayTimes(await this.json("/v1/preferences", { signal }));
  }

  /** Only the field that changed. Answers with the box's pair; a refusal is
   * an HttpError 422 whose `detail` is the code (lib/dayFormat.ts). */
  async setDayTimes(
    update: { day_start?: string; day_end?: string; checkin_every_hours?: number },
    signal?: AbortSignal,
  ): Promise<DayTimes> {
    return decodeDayTimes(await this.json("/v1/preferences", { method: "POST", body: update, signal }));
  }

  // --- web push --------------------------------------------------------------

  async webPushKey(): Promise<string> {
    return field(await this.json("/v1/webpush/key"), "public_key");
  }

  async registerWebPush(sub: PushSubscriptionJSON): Promise<void> {
    await this.json("/v1/device/webpush-subscription", {
      method: "POST",
      body: { endpoint: sub.endpoint, keys: sub.keys },
    });
  }
}

function decodeDayTimes(body: unknown): DayTimes {
  const b = body && typeof body === "object" ? (body as Record<string, unknown>) : {};
  const { day_start: start, day_end: end } = b;
  if (typeof start !== "string" || !isClockTime(start) || typeof end !== "string" || !isClockTime(end)) {
    throw new DecodeError();
  }
  const every = b.checkin_every_hours;
  return {
    dayStart: start,
    dayEnd: end,
    checkinEveryHours: typeof every === "number" && Number.isInteger(every) && every >= 0 && every <= 6 ? every : null,
  };
}

function field(body: unknown, key: string): string {
  const v = body && typeof body === "object" ? (body as Record<string, unknown>)[key] : undefined;
  if (typeof v !== "string" || v === "") throw new DecodeError();
  return v;
}
