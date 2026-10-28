// The chat's state machine: send with retry, poll a turn, sync the transcript,
// watch /health, and answer tool prompts. A port of iOS's `ChatStore.swift`;
// the rule ids (SND-3, POL-1, …) are docs/chat-ux-contract.md's, and the
// "why" behind each lives there and in the Swift. This file keeps the why
// where the web had to decide something the iOS app never did.
//
// Framework-free on purpose: React subscribes through `subscribe` /
// `getState` (useSyncExternalStore), and the streaming text and activity of
// each in-flight placeholder live in a separate per-message channel
// (`subscribeStream`), so a 50ms typewriter tick re-renders one bubble, not the
// list (STR-12).

import {
  CancelledError,
  HttpError,
  isTransientSendError,
  isTransportFailure,
  TransportError,
  type Api,
} from "./api";
import type { OutgoingLocation } from "./location";
import { applyRows, type MergeEffects } from "./transcriptMerge";
import { ActivityPlayback, TextPlayback } from "./playback";
import type { UsageReport } from "./apiModels";
import type { ChatPersistence, CursorRecord } from "./storage";
import type {
  ChatMessage,
  HealthResponse,
  IdeaQuote,
  MessageStatus,
  PermissionDecision,
  PermissionRequestInfo,
  TranscriptRow,
} from "./types";
import { attachmentIdsOf, imageKeysOf, ownImageKey, sameId } from "./types";

export interface PollingPolicy {
  burstInterval: number;
  burstDuration: number;
  burstAttempts: number;
  fastInterval: number;
  fastAttempts: number;
  slowInterval: number;
  slowAttempts: number;
  healthInterval: number;
  /** Least time between `/v1/usage` reads in the health loop, for the >= 90%
   * status row. Plan usage only moves when Marlow runs, so the health tick
   * would be far more often than it can change. */
  usageInterval: number;
  activityDwell: number;
  connectionNoticeDelay: number;
  /** STA-8: how long a loop run keeps a transport failure to itself. */
  reconnectGrace: number;
  /** STA-8: the pass interval while that grace runs. */
  reconnectRetry: number;
  partialTextTick: number;
  partialTextJumpThreshold: number;
  sendMaxAttempts: number;
  sendBaseInterval: number;
  sendMaxInterval: number;
  noticeDuration: number;
}

/** iOS `PollingPolicy.default`, in milliseconds. */
export const DEFAULT_POLICY: PollingPolicy = {
  burstInterval: 500,
  burstDuration: 6_000,
  burstAttempts: 16,
  fastInterval: 2_000,
  fastAttempts: 150,
  slowInterval: 30_000,
  slowAttempts: 120,
  healthInterval: 15_000,
  usageInterval: 300_000,
  activityDwell: 700,
  connectionNoticeDelay: 600,
  reconnectGrace: 3_500,
  reconnectRetry: 1_000,
  partialTextTick: 50,
  partialTextJumpThreshold: 2_000,
  sendMaxAttempts: 8,
  sendBaseInterval: 2_000,
  sendMaxInterval: 8_000,
  noticeDuration: 3_000,
};

export const RETENTION_MS = 30 * 24 * 60 * 60 * 1000;
const PAGE_SIZE = 200;

export const TEXT = {
  stalled: "Marlow hasn't answered yet — still checking in the background.",
  neverAnswered: "Marlow never answered this one. Try sending it again.",
  lostTrack: "Marlow lost track of this message (the server may have restarted). Try sending it again.",
  somethingWrong: "Something went wrong.",
  interruptedBeforeReply: "Interrupted before Marlow replied — please resend.",
  interruptedBeforeSend: "Interrupted before sending — please resend.",
  // Section 16: iOS shows the platform's own string here. Say plainly what
  // happened and that Retry exists.
  sendExhausted: "Couldn't reach Marlow, so this wasn't sent. Tap retry to try again.",
  movedOn: "Marlow already moved on — that request was no longer open.",
  photoSaveFailed: "Couldn't save the photo — sending your message without it.",
  photoMissing: "Couldn't find the photo to resend — sending without it.",
} as const;

/** STA-5, plus the section-16 gap: `trouble` is a server that answers, but
 * with errors (a 5xx, or a body that will not decode). iOS shows nothing for
 * it; the web says so, at the same foot slot, because a box that is up but
 * broken otherwise looks exactly like a quiet conversation. */
export type ConnectionActivity = "idle" | "checking" | "unreachable" | "trouble";

export type Effort = "low" | "medium" | "high";
// Matches the server's MARLOW_EFFORT default, so an untouched client and an
// unconfigured server agree. Medium since 2026-10-02: usefulness on day one
// beats latency (server/config.py).
export const DEFAULT_EFFORT: Effort = "medium";

export interface ChatState {
  messages: ChatMessage[];
  hasOlderOnServer: boolean;
  awaitingFirstFetch: boolean;
  loadingEarlier: boolean;
  reauthNeeded: boolean;
  reauthPrewarn: boolean;
  queuePaused: boolean;
  serverModel: string | null;
  serverBuild: string | null;
  connection: ConnectionActivity;
  pendingPermission: PermissionRequestInfo | null;
  /** The latest `/v1/usage` reading, kept only to drive the >= 90% status
   * row. Whether the row shows depends on the clock (a reset clears it), so
   * the view asks `usageFormat.highUsage` rather than this holding a flag. */
  usageReport: UsageReport | null;
  /** STA-6: a transient notice by the composer. */
  notice: string | null;
  effort: Effort;
}

/** The ephemeral per-placeholder state (STR-6): never persisted. */
export interface StreamState {
  label?: string;
  text?: string;
  /** Code-point offsets the typing cursor has passed (STR-4). */
  marks?: number[];
}

export type FeedbackEvent =
  | { kind: "sent"; userId: string; placeholderId: string }
  | { kind: "started"; placeholderIds: string[] }
  | { kind: "finished"; turnIds: string[] };

export interface Clock {
  now(): number;
  sleep(ms: number, signal: AbortSignal): Promise<void>;
}

export const realClock: Clock = {
  now: () => Date.now(),
  sleep: (ms, signal) =>
    new Promise<void>((resolve) => {
      if (signal.aborted) return resolve();
      const t = setTimeout(done, ms);
      function done() {
        signal.removeEventListener("abort", done);
        clearTimeout(t);
        resolve();
      }
      signal.addEventListener("abort", done, { once: true });
    }),
};

export interface ChatStoreOptions {
  api: Api;
  persistence: ChatPersistence;
  policy?: PollingPolicy;
  clock?: Clock;
  newId?: () => string;
  timezone?: () => string | undefined;
  /** Whether the page is visible, for the receive feedback (FBK-2). */
  isForeground?: () => boolean;
  initialEffort?: Effort;
  saveEffort?: (e: Effort) => void;
  /** The location to attach to a message sent now; undefined sends no key. */
  location?: () => OutgoingLocation | undefined;
}

type Task = { controller: AbortController };

export class ChatStore {
  private state: ChatState = {
    messages: [],
    hasOlderOnServer: false,
    awaitingFirstFetch: true,
    loadingEarlier: false,
    reauthNeeded: false,
    reauthPrewarn: false,
    queuePaused: false,
    serverModel: null,
    serverBuild: null,
    connection: "idle",
    pendingPermission: null,
    usageReport: null,
    notice: null,
    effort: DEFAULT_EFFORT,
  };
  private listeners = new Set<() => void>();
  private streamListeners = new Map<string, Set<() => void>>();
  private streams = new Map<string, StreamState>();
  private feedbackListeners = new Set<(e: FeedbackEvent) => void>();
  private replyProgressListeners = new Set<(id: string, text: string, marks: number[]) => void>();

  private latestSeq: number | null = null;
  private readonly api: Api;
  private readonly persistence: ChatPersistence;
  private readonly policy: PollingPolicy;
  private readonly clock: Clock;
  private readonly newId: () => string;
  private readonly timezone: () => string | undefined;
  private readonly isForeground: () => boolean;
  private readonly saveEffort: (e: Effort) => void;
  private readonly location: () => OutgoingLocation | undefined;

  /** Keyed by placeholder id (or a fixed key for a non-turn task). */
  private tasks = new Map<string, Task>();
  private loop: Task | null = null;
  private stopped = false;
  private connectionNoticeTimer: ReturnType<typeof setTimeout> | null = null;
  private noticeToken = 0;
  private noticeTimer: ReturnType<typeof setTimeout> | null = null;
  private permissionGeneration = 0;
  private syncInFlight: Promise<void> | null = null;

  private textPlayback = new Map<string, TextPlayback>();
  private textTimers = new Map<string, ReturnType<typeof setTimeout>>();
  private activityPlayback = new Map<string, ActivityPlayback>();
  private activityTimers = new Map<string, ReturnType<typeof setTimeout>>();
  /** Placeholders whose turn was already under way when this client attached
   * to it (a reload, or another device's turn found by sync): their first
   * slab shows whole (STR-3). */
  private resumed = new Set<string>();
  /** FBK-1: never pruned, so it can only under-fire. */
  private announcedStarts = new Set<string>();
  /** Attachments already tried this load (SYN-7: once per load, not per tick). */
  private attachmentTried = new Set<string>();
  private imageUrls = new Map<string, Promise<string | null>>();

  /** What the persistence layer last wrote, by reference, so a save writes
   * only what changed (SYN-6). */
  private persisted = new Map<string, ChatMessage>();
  private saveChain: Promise<void> = Promise.resolve();

  constructor(opts: ChatStoreOptions) {
    this.api = opts.api;
    this.persistence = opts.persistence;
    this.policy = opts.policy ?? DEFAULT_POLICY;
    this.clock = opts.clock ?? realClock;
    this.newId = opts.newId ?? (() => crypto.randomUUID());
    this.timezone =
      opts.timezone ??
      (() => {
        try {
          return Intl.DateTimeFormat().resolvedOptions().timeZone || undefined;
        } catch {
          return undefined;
        }
      });
    this.isForeground = opts.isForeground ?? (() => true);
    this.saveEffort = opts.saveEffort ?? (() => {});
    this.location = opts.location ?? (() => undefined);
    this.state.effort = opts.initialEffort ?? DEFAULT_EFFORT;
  }

  // --- subscription ----------------------------------------------------------

  getState = (): ChatState => this.state;

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  getStream = (id: string): StreamState | undefined => this.streams.get(id);

  subscribeStream = (id: string, fn: () => void): (() => void) => {
    let set = this.streamListeners.get(id);
    if (!set) this.streamListeners.set(id, (set = new Set()));
    set.add(fn);
    return () => {
      set!.delete(fn);
      if (set!.size === 0) this.streamListeners.delete(id);
    };
  };

  onFeedback(fn: (e: FeedbackEvent) => void): () => void {
    this.feedbackListeners.add(fn);
    return () => this.feedbackListeners.delete(fn);
  }

  private set(patch: Partial<ChatState>): void {
    this.state = { ...this.state, ...patch };
    for (const fn of this.listeners) fn();
  }

  private setMessages(messages: ChatMessage[]): void {
    this.set({ messages });
  }

  private setStream(id: string, patch: StreamState | null): void {
    if (patch === null) {
      if (!this.streams.has(id)) return;
      this.streams.delete(id);
    } else {
      this.streams.set(id, { ...this.streams.get(id), ...patch });
    }
    for (const fn of this.streamListeners.get(id) ?? []) fn();
  }

  private emit(e: FeedbackEvent): void {
    for (const fn of this.feedbackListeners) fn(e);
  }

  // --- lifecycle -------------------------------------------------------------

  /** Load the cache, reconcile the outbox (SND-7), and start the sync/health
   * loop. A cold start renders the cache, then fetches past the cursor. */
  async start(): Promise<void> {
    let cursor: CursorRecord | undefined;
    let messages: ChatMessage[] = [];
    try {
      const loaded = await this.persistence.load();
      cursor = loaded.cursor;
      messages = loaded.messages.filter(isPlausibleMessage);
    } catch {
      // An unreadable cache means start empty and bootstrap (SYN-6).
      cursor = undefined;
      messages = [];
    }
    if (this.stopped) return;
    // IndexedDB returns records in key order, not transcript order. Server
    // rows sort by seq; a local-only message keeps its place after the row it
    // was created behind, which createdAt approximates across a reload.
    messages = orderLoaded(messages);
    // Every loaded record counts as persisted, including the ones retention
    // drops next: the first save then deletes them from storage instead of
    // re-dropping them on every load while the store grows.
    for (const m of messages) this.persisted.set(m.id, m);
    const cutoff = this.clock.now() - RETENTION_MS;
    const kept = messages.filter((m) => m.seq === undefined || m.createdAt >= cutoff);
    // Rows dropped here may still be on the server (its own 30 days run from
    // its clock, not ours), so "Show earlier" must be able to reach them even
    // if the cache once held the whole history.
    const droppedRows = kept.length !== messages.length;
    messages = kept;
    this.latestSeq = cursor?.latestSeq ?? null;
    this.set({
      messages,
      hasOlderOnServer: (cursor?.hasOlderOnServer ?? false) || droppedRows,
      awaitingFirstFetch: this.latestSeq === null,
    });
    this.reconcilePending();
    this.retryMissingAttachments();
    this.persist();
    this.restartLoop();
    // Images no kept message references: dropped by retention, or orphaned
    // by a send that never got persisted.
    const keep = new Set<string>();
    for (const m of this.state.messages) for (const k of imageKeysOf(m)) keep.add(k);
    void this.persistence.deleteImages((k) => keep.has(k)).catch(() => {});
  }

  /** Sign-out and tab hand-over: stop every request and timer. The persisted
   * transcript is left alone (the caller wipes it on unpair). POL-6. */
  stop(): void {
    this.stopped = true;
    this.loop?.controller.abort();
    this.loop = null;
    for (const t of this.tasks.values()) t.controller.abort();
    this.tasks.clear();
    for (const id of new Set([...this.textPlayback.keys(), ...this.activityPlayback.keys(), ...this.resumed])) {
      this.clearProgress(id);
    }
    if (this.connectionNoticeTimer) clearTimeout(this.connectionNoticeTimer);
    if (this.noticeTimer) clearTimeout(this.noticeTimer);
    for (const url of this.imageUrls.values()) {
      void url.then((u) => u && URL.revokeObjectURL(u));
    }
    this.imageUrls.clear();
  }

  /** POL-7 / WEB-1: on the tab becoming visible, or coming back online,
   * replace the loop and run a pass at once, sync first. Replace, never add a
   * second: one loop, one owner. */
  restartLoop(): void {
    if (this.stopped) return;
    this.loop?.controller.abort();
    const task: Task = { controller: new AbortController() };
    this.loop = task;
    void this.runLoop(task.controller.signal);
  }

  /** Test support: settle every in-flight send, poll and fetch. */
  async idle(): Promise<void> {
    await this.saveChain;
  }

  setEffort(effort: Effort): void {
    this.set({ effort });
    this.saveEffort(effort);
  }

  // --- sending ---------------------------------------------------------------

  /** CMP-1/CMP-2/SND-1. `images` are already-downscaled JPEGs. Returns false
   * when there is nothing to send, so the composer can put its draft back. */
  async send(text: string, images: Blob[] = [], quote?: IdeaQuote): Promise<boolean> {
    if (this.stopped) return false;
    const trimmed = text.trim();
    if (!trimmed && images.length === 0) return false;
    const userId = this.newId();
    // Saved for retry before anything is sent: what is sent and what a retry
    // can re-read must agree (CMP-6). If any save fails the message goes
    // text-only, and says so, rather than carrying some of the images: a
    // caption about photos Marlow only partly saw is the failure CMP-4 refuses.
    let imagesToSend: Blob[] = images;
    let imageKeys: string[] | undefined;
    if (images.length > 0) {
      try {
        const keys = images.map((_, i) => ownImageKey(userId, i));
        for (const [i, image] of images.entries()) await this.persistence.putImage(keys[i], image);
        imageKeys = keys;
      } catch {
        imagesToSend = [];
      }
      if (imagesToSend.length === 0 && !trimmed) return false;
      if (imagesToSend.length === 0) this.showNotice(TEXT.photoSaveFailed);
    }
    this.post(trimmed, userId, imageKeys, imagesToSend, quote, false);
    return true;
  }

  /** Voice mode's send (docs/tasks/voice-mode-android.md): marked spoken, so
   * Marlow knows the reply will be read aloud, and returns the reply's
   * placeholder id so the speaker can follow that one reply. Null when there
   * was nothing to send. A later manual retry of it is an ordinary send:
   * nobody is listening by then. */
  sendSpoken(text: string): string | null {
    if (this.stopped) return null;
    const trimmed = text.trim();
    if (!trimmed) return null;
    return this.post(trimmed, this.newId(), undefined, [], undefined, true);
  }

  /** Text the poll loop has received for a running reply, cumulative, with
   * the server's tool-call offsets: the raw stream, ahead of the bubble's
   * typing-out. Voice mode speaks from this; terminal states it reads off
   * `messages`, which every path that settles a reply goes through. */
  onReplyProgress(fn: (placeholderId: string, text: string, marks: number[]) => void): () => void {
    this.replyProgressListeners.add(fn);
    return () => this.replyProgressListeners.delete(fn);
  }

  /** The user's row and the reply's placeholder, then the send. */
  private post(
    trimmed: string,
    userId: string,
    imageKeys: string[] | undefined,
    images: Blob[],
    quote: IdeaQuote | undefined,
    spoken: boolean,
  ): string {
    const now = this.clock.now();
    const user: ChatMessage = { id: userId, role: "user", text: trimmed, createdAt: now, status: "done", imageKeys, ...(quote ? { quote } : {}) };
    const placeholder: ChatMessage = {
      id: this.newId(),
      role: "assistant",
      text: "",
      createdAt: now,
      status: "sending",
      clientMsgId: userId,
    };
    // Emitted just before the rows publish, so the view's "a local send is
    // landing" flag is already set when it lays them out (SCR-3, FBK-3),
    // whenever React chooses to render.
    this.emit({ kind: "sent", userId, placeholderId: placeholder.id });
    this.setMessages([...this.state.messages, user, placeholder]);
    this.persist();
    this.startSubmit(trimmed, userId, placeholder.id, images, quote?.idea_id, spoken);
    return placeholder.id;
  }

  /** SND-6: only an `error` placeholder whose user message is still here. */
  retry(placeholderId: string): void {
    const message = this.find(placeholderId);
    if (!message || message.status !== "error") return;
    const r = this.resendable(message);
    if (!r) return;
    this.update(placeholderId, (m) => ({
      ...m,
      status: "sending",
      text: "",
      // An error row confirmed this placeholder and gave it a seq. A retry
      // must make it local-only again, or the retry's reply row cannot match
      // it and lands as a second bubble (review, 2026-10-01).
      seq: undefined,
      answers: undefined,
    }));
    this.persist();
    this.startSubmit(r.text, r.clientMsgId, placeholderId, { keys: r.imageKeys }, r.quote?.idea_id);
  }

  private resendable(
    m: ChatMessage,
  ): { clientMsgId: string; text: string; imageKeys: string[]; quote?: IdeaQuote } | null {
    if (!m.clientMsgId) return null;
    const user = this.state.messages.find((u) => u.role === "user" && sameId(u.id, m.clientMsgId));
    if (!user) return null;
    return { clientMsgId: m.clientMsgId, text: user.text, imageKeys: imageKeysOf(user), quote: user.quote };
  }

  private startSubmit(
    text: string,
    clientMsgId: string,
    placeholderId: string,
    images: Blob[] | { keys: string[] },
    ideaId?: string,
    spoken = false,
  ): void {
    this.runTask(placeholderId, async (signal) => {
      let blobs: Blob[];
      if (Array.isArray(images)) blobs = images;
      else {
        // A resend re-reads every saved image. If ANY is gone it sends none
        // and says so (CMP-6): never the caption with some photos missing.
        blobs = [];
        let missing = false;
        for (const key of images.keys) {
          const blob = await this.persistence.getImage(key).catch(() => undefined);
          if (signal.aborted) return;
          if (blob) blobs.push(blob);
          else missing = true;
        }
        if (missing) {
          blobs = [];
          this.showNotice(TEXT.photoMissing);
        }
      }
      await this.submit(text, clientMsgId, placeholderId, blobs, signal, ideaId, spoken);
    });
  }

  /** SND-2..SND-5. One flat 20s bound per attempt (the Api's own timeout is a
   * wall-clock abort, so a trickling connection cannot stretch it), 8
   * attempts, backoff min(2s << n, 8s). Stays `sending` throughout. */
  private async submit(
    text: string,
    clientMsgId: string,
    placeholderId: string,
    images: Blob[],
    signal: AbortSignal,
    ideaId?: string,
    spoken = false,
  ): Promise<void> {
    // Encoded once, before the loop (CMP-3).
    const imagesB64 = await Promise.all(images.map(blobToBase64));
    if (signal.aborted) return;
    let attempt = 0;
    for (;;) {
      try {
        // Read per attempt, like effort: freshness is judged when the request
        // goes out, and a key only when there is a value (absence clears the
        // server's copy).
        const location = this.location();
        const turnId = await this.api.sendMessage(
          {
            text,
            client_msg_id: clientMsgId,
            timezone: this.timezone(),
            // Read per attempt, so a retry carries what the header shows now
            // (CMP-7).
            effort: this.state.effort,
            ...(location ? { location } : {}),
            ...(imagesB64.length > 0 ? { images: imagesB64.map((b64) => ({ b64, mime: "image/jpeg" as const })) } : {}),
            ...(ideaId ? { idea_id: ideaId } : {}),
            ...(spoken ? { spoken: true } : {}),
          },
          signal,
        );
        if (signal.aborted) return;
        this.update(placeholderId, (m) => ({ ...m, turnId, status: "running" }));
        this.persist();
        await this.pollTurn(turnId, placeholderId, signal);
        return;
      } catch (e) {
        if (signal.aborted || e instanceof CancelledError) return;
        attempt += 1;
        if (!isTransientSendError(e) || attempt >= this.policy.sendMaxAttempts) {
          this.update(placeholderId, (m) => ({ ...m, status: "error", text: sendErrorText(e) }));
          this.persist();
          return;
        }
        await this.clock.sleep(sendBackoff(attempt, this.policy), signal);
        // A cancelled client must not send one more request after the sleep.
        if (signal.aborted) return;
      }
    }
  }

  // --- polling a turn --------------------------------------------------------

  private startPoll(turnId: string, placeholderId: string): void {
    this.runTask(placeholderId, (signal) => this.pollTurn(turnId, placeholderId, signal));
  }

  /** POL-1: burst, fast, then slow, with `stalled` between. */
  private async pollTurn(turnId: string, id: string, signal: AbortSignal): Promise<void> {
    const p = this.policy;
    if (await this.pollLoop(turnId, id, p.burstAttempts, p.burstInterval, false, signal, this.clock.now() + p.burstDuration)) return;
    if (signal.aborted) return;
    if (await this.pollLoop(turnId, id, p.fastAttempts, p.fastInterval, true, signal)) return;
    if (signal.aborted) return;
    this.clearProgress(id);
    this.update(id, (m) => ({ ...m, status: "stalled", text: TEXT.stalled }));
    this.persist();
    if (await this.pollLoop(turnId, id, p.slowAttempts, p.slowInterval, false, signal)) return;
    if (signal.aborted) return;
    this.clearProgress(id);
    this.update(id, (m) => ({ ...m, status: "error", text: TEXT.neverAnswered }));
    this.persist();
  }

  /** True when the caller should stop: terminal, or cancelled. Sleep, then
   * request: never two requests in flight for one turn, and the interval is a
   * ceiling on the rate, not a floor (POL-2). */
  private async pollLoop(
    turnId: string,
    id: string,
    attempts: number,
    interval: number,
    checkPermissions: boolean,
    signal: AbortSignal,
    deadline?: number,
  ): Promise<boolean> {
    for (let i = 0; i < attempts; i++) {
      if (signal.aborted) return true;
      await this.clock.sleep(interval, signal);
      if (signal.aborted) return true;
      // After the sleep: an expired phase hands over without one more request.
      if (deadline !== undefined && this.clock.now() >= deadline) return false;
      if (checkPermissions) {
        await this.checkPermissions(signal);
        if (signal.aborted) return true;
      }
      let turn;
      try {
        turn = await this.api.getTurn(turnId, signal);
      } catch (e) {
        if (signal.aborted) return true;
        if (e instanceof HttpError && e.status === 404) {
          this.clearProgress(id);
          this.update(id, (m) => ({ ...m, status: "error", text: TEXT.lostTrack }));
          this.persist();
          return true;
        }
        // POL-5: anything else is transient; playback keeps pacing what it
        // already has, and the next cumulative response fills the gap.
        continue;
      }
      if (signal.aborted) return true;
      switch (turn.status) {
        case "merged":
          this.clearProgress(id);
          this.setMessages(this.state.messages.filter((m) => m.id !== id));
          this.persist();
          return true;
        case "done":
          this.clearProgress(id);
          this.update(id, (m) => ({ ...m, status: "done", text: turn.result_text ?? "" }));
          this.persist();
          this.completed([turnId]);
          return true;
        case "error":
          this.clearProgress(id);
          this.update(id, (m) => ({ ...m, status: "error", text: turn.error ?? TEXT.somethingWrong }));
          this.persist();
          return true;
        default:
          // STR-7: only while running. `stalled` shows its own words.
          if (this.find(id)?.status === "running") {
            this.ingestActivities(id, turn.activities);
            this.ingestText(id, turn.partial_text, turn.partial_text_marks);
            for (const fn of this.replyProgressListeners) fn(id, turn.partial_text, turn.partial_text_marks);
          }
      }
    }
    return false;
  }

  // --- streaming playback ----------------------------------------------------

  private ingestActivities(id: string, activities: Parameters<ActivityPlayback["ingest"]>[0]): void {
    let pb = this.activityPlayback.get(id);
    if (!pb) this.activityPlayback.set(id, (pb = new ActivityPlayback()));
    pb.ingest(activities);
    this.pumpActivity(id);
  }

  /** STR-9: each label holds for at least the dwell; the last stays up until a
   * newer one or the terminal state clears it. */
  private pumpActivity(id: string): void {
    if (this.activityTimers.has(id)) return;
    const pb = this.activityPlayback.get(id);
    const label = pb?.next();
    if (label === undefined) return;
    this.setStream(id, { label });
    this.activityTimers.set(
      id,
      setTimeout(() => {
        this.activityTimers.delete(id);
        this.pumpActivity(id);
      }, this.policy.activityDwell),
    );
  }

  private ingestText(id: string, text: string, marks: number[]): void {
    if (!text) return;
    let pb = this.textPlayback.get(id);
    if (!pb) this.textPlayback.set(id, (pb = new TextPlayback(this.resumed.has(id))));
    pb.ingest(text, marks, Math.max(1, Math.floor(this.policy.fastInterval / this.policy.partialTextTick)));
    this.pumpText(id);
  }

  /** STR-2: one reveal per 50ms tick. */
  private pumpText(id: string): void {
    if (this.textTimers.has(id)) return;
    const pb = this.textPlayback.get(id);
    if (!pb) return;
    const shown = pb.advance(this.policy.partialTextJumpThreshold);
    if (shown === null) return;
    this.setStream(id, { text: shown, marks: pb.revealedMarks() });
    if (shown && !this.announcedStarts.has(id)) {
      this.announcedStarts.add(id);
      this.emit({ kind: "started", placeholderIds: [id] });
    }
    this.textTimers.set(
      id,
      setTimeout(() => {
        this.textTimers.delete(id);
        this.pumpText(id);
      }, this.policy.partialTextTick),
    );
  }

  private clearProgress(id: string): void {
    const at = this.activityTimers.get(id);
    if (at) clearTimeout(at);
    this.activityTimers.delete(id);
    this.activityPlayback.delete(id);
    const tt = this.textTimers.get(id);
    if (tt) clearTimeout(tt);
    this.textTimers.delete(id);
    this.textPlayback.delete(id);
    this.resumed.delete(id);
    this.setStream(id, null);
  }

  // --- reconcile on load (SND-7) ---------------------------------------------

  private reconcilePending(): void {
    for (const m of this.state.messages) {
      if (m.seq !== undefined && (m.status === "done" || m.status === "error")) continue;
      switch (m.status) {
        case "running":
        case "stalled":
          if (!m.turnId) {
            this.update(m.id, (x) => ({ ...x, status: "error", text: TEXT.interruptedBeforeReply }));
            continue;
          }
          // Resumes as a late joiner: label and text cleared (STR-6), first
          // slab whole (STR-3).
          this.resumed.add(m.id);
          this.startPoll(m.turnId, m.id);
          break;
        case "sending": {
          const r = this.resendable(m);
          if (!r) {
            this.update(m.id, (x) => ({ ...x, status: "error", text: TEXT.interruptedBeforeSend }));
            continue;
          }
          this.update(m.id, (x) => ({ ...x, seq: undefined, answers: undefined }));
          this.startSubmit(r.text, r.clientMsgId, m.id, { keys: r.imageKeys }, r.quote?.idea_id);
          break;
        }
        default:
          break;
      }
    }
  }

  // --- tool approval (PRM-1..PRM-3) ------------------------------------------

  private async checkPermissions(signal: AbortSignal): Promise<void> {
    const generation = this.permissionGeneration;
    let list: PermissionRequestInfo[];
    try {
      list = await this.api.permissions(signal);
    } catch {
      return; // a transient poll failure; never disturbs turn polling
    }
    if (signal.aborted || generation !== this.permissionGeneration) return;
    const now = this.clock.now() / 1000;
    const next = list.find((r) => r.expires_at > now) ?? null;
    const cur = this.state.pendingPermission;
    if (cur?.request_id !== next?.request_id) this.set({ pendingPermission: next });
  }

  /** `requestId` is the prompt the user was reading. If a poll has swapped in
   * another request since, the answer is dropped rather than applied to a
   * request nobody read: that would be a blind approval (PRM-2). */
  async answerPermission(decision: PermissionDecision, requestId?: string): Promise<void> {
    const request = this.state.pendingPermission;
    if (!request) return;
    if (requestId !== undefined && request.request_id !== requestId) return;
    let accepted: boolean;
    try {
      accepted = await this.api.answerPermission(request.request_id, decision);
    } catch {
      // Leave the prompt: the next poll still shows it (safe to retry) or has
      // cleared it.
      return;
    }
    if (this.state.pendingPermission?.request_id === request.request_id) {
      this.permissionGeneration += 1;
      this.set({ pendingPermission: null });
    }
    if (!accepted) this.showNotice(TEXT.movedOn);
  }

  /** The countdown reached zero: clear it as a backstop to the next poll. */
  expirePermission(requestId: string): void {
    if (this.state.pendingPermission?.request_id === requestId) {
      this.permissionGeneration += 1;
      this.set({ pendingPermission: null });
    }
  }

  /** STA-6: identify the timer, not the text. */
  showNotice(text: string): void {
    const token = ++this.noticeToken;
    this.set({ notice: text });
    if (this.noticeTimer) clearTimeout(this.noticeTimer);
    this.noticeTimer = setTimeout(() => {
      if (this.noticeToken === token) this.set({ notice: null });
    }, this.policy.noticeDuration);
  }

  // --- the sync/health loop (POL-7, STA-4, STA-5) ----------------------------

  private async runLoop(signal: AbortSignal): Promise<void> {
    let first = true;
    const graceUntil = this.clock.now() + this.policy.reconnectGrace;
    // Per run, so the first pass of a run always reads usage (the one someone
    // is waiting on) and later passes are throttled by `usageInterval`.
    let lastUsageRead: number | null = null;
    while (!signal.aborted) {
      if (first) this.beginConnectionNotice();
      first = false;
      const sync = await this.syncTranscript(signal);
      const health = await this.refreshHealth(signal);
      if (signal.aborted) return;
      // STA-8: a phone waking from Doze gets its foreground event before the
      // tailnet is back, so the first pass fails and "Can't reach Marlow"
      // flashed red for a moment on every wake (Android emulator, 2026-10-02).
      // Within the grace, retry quickly and leave the spinner up instead.
      if (
        (sync === "unreachable" || health === "unreachable") &&
        this.state.connection !== "unreachable" &&
        this.clock.now() < graceUntil
      ) {
        await this.clock.sleep(this.policy.reconnectRetry, signal);
        continue;
      }
      this.settleConnection(sync, health);
      // After the settle: usage is not a health signal, and the reachability
      // state must not wait on one more round trip. Stamped only on success,
      // so a failed read at launch retries on the next tick rather than
      // leaving the row dark for the whole interval (iOS review, 2026-10-04).
      if (
        (lastUsageRead === null || this.clock.now() - lastUsageRead >= this.policy.usageInterval) &&
        (await this.refreshUsage(signal))
      ) {
        lastUsageRead = this.clock.now();
      }
      if (signal.aborted) return;
      await this.clock.sleep(this.policy.healthInterval, signal);
    }
  }

  /** Only the first pass of a loop run announces itself, and only if it is
   * still running 600ms in. A new pass never downgrades `unreachable`. */
  private beginConnectionNotice(): void {
    if (this.connectionNoticeTimer) clearTimeout(this.connectionNoticeTimer);
    this.connectionNoticeTimer = setTimeout(() => {
      this.connectionNoticeTimer = null;
      if (this.state.connection === "idle") this.set({ connection: "checking" });
    }, this.policy.connectionNoticeDelay);
  }

  private settleConnection(sync: PassResult, health: PassResult): void {
    if (this.connectionNoticeTimer) clearTimeout(this.connectionNoticeTimer);
    this.connectionNoticeTimer = null;
    const connection: ConnectionActivity =
      sync === "unreachable" || health === "unreachable"
        ? "unreachable"
        : sync === "trouble" || health === "trouble"
          ? "trouble"
          : "idle";
    if (connection !== this.state.connection) this.set({ connection });
  }

  private async refreshHealth(signal: AbortSignal): Promise<PassResult> {
    let h: HealthResponse;
    try {
      h = await this.api.health(signal);
    } catch (e) {
      return classifyPassError(e, signal);
    }
    if (signal.aborted) return "ok";
    const patch: Partial<ChatState> = {};
    // STA-3: keyed on auth_ok, not status.
    if (typeof h.auth_ok === "boolean") patch.reauthNeeded = !h.auth_ok;
    const reauthNeeded = patch.reauthNeeded ?? this.state.reauthNeeded;
    if (typeof h.reauth_prewarn === "boolean") patch.reauthPrewarn = h.reauth_prewarn && !reauthNeeded;
    if (typeof h.queue_paused === "boolean") patch.queuePaused = h.queue_paused;
    if (typeof h.model === "string" && h.model) patch.serverModel = h.model;
    if (typeof h.git_branch === "string" && typeof h.git_commit === "string") {
      patch.serverBuild = `${h.git_branch}@${h.git_commit}`;
    }
    const changed = (Object.keys(patch) as (keyof ChatState)[]).some((k) => patch[k] !== this.state[k]);
    if (changed) this.set(patch);
    return "ok";
  }

  /** Reads the plan windows for the >= 90% row. Any failure keeps the previous
   * reading and says nothing: a stale row beats a flicker, and `/v1/usage`
   * failing is not evidence about the network, so this never touches
   * `connection`. Returns whether a reading landed. */
  private async refreshUsage(signal: AbortSignal): Promise<boolean> {
    try {
      const report = await this.api.usage(signal);
      if (signal.aborted) return false;
      this.set({ usageReport: report });
      return true;
    } catch {
      return false;
    }
  }

  /** SYN-1. A sync that starts while another is in flight waits for it. */
  private async syncTranscript(signal: AbortSignal): Promise<PassResult> {
    while (this.syncInFlight) {
      await this.syncInFlight;
      if (signal.aborted) return "ok";
    }
    let release!: () => void;
    this.syncInFlight = new Promise<void>((r) => (release = r));
    try {
      if (this.latestSeq === null) {
        const page = await this.api.transcript({ limit: PAGE_SIZE }, signal);
        if (signal.aborted) return "ok";
        const maxSeq = page.rows.reduce<number | null>((a, r) => (a === null || r.seq > a ? r.seq : a), null);
        this.latestSeq = maxSeq ?? page.latest_seq;
        this.set({ hasOlderOnServer: page.has_more, awaitingFirstFetch: false });
        this.applyPage([...page.rows].reverse(), false);
        this.persist();
        return "ok";
      }
      for (;;) {
        const cursor: number = this.latestSeq;
        const page = await this.api.transcript({ after: cursor, limit: PAGE_SIZE }, signal);
        if (signal.aborted) return "ok";
        this.applyPage(page.rows, true);
        const last = page.rows.reduce<number | null>((a, r) => (a === null || r.seq > a ? r.seq : a), null);
        if (last === null || last <= cursor) break;
        this.latestSeq = last;
        this.persist();
        if (!page.has_more) break;
      }
      return "ok";
    } catch (e) {
      return classifyPassError(e, signal);
    } finally {
      this.syncInFlight = null;
      release();
    }
  }

  /** WIN-3: a tap, not load-on-scroll. The web renders every row it holds
   * (no window: WIN-2), so "Show earlier" always means the server's next
   * page. */
  showEarlier(): void {
    if (!this.state.hasOlderOnServer || this.tasks.has(EARLIER_KEY)) return;
    const oldest = this.state.messages.find((m) => m.seq !== undefined)?.seq;
    if (oldest === undefined) return;
    this.set({ loadingEarlier: true });
    this.runTask(EARLIER_KEY, async (signal) => {
      try {
        const page = await this.api.transcript({ before: oldest, limit: PAGE_SIZE }, signal);
        if (signal.aborted) return;
        this.applyPage([...page.rows].reverse(), false);
        this.set({ hasOlderOnServer: page.has_more });
        this.persist();
      } catch {
        // The button stays; the next tap tries again.
      } finally {
        if (!signal.aborted) this.set({ loadingEarlier: false });
      }
    });
  }

  private applyPage(rows: TranscriptRow[], announce: boolean): void {
    if (rows.length === 0) return;
    const { messages, effects } = applyRows(rows, this.state.messages, announce);
    this.setMessages(messages);
    this.perform(effects);
    this.persist();
  }

  /** SYN-5: cancel polls, start polls, queue completions, fetch attachments. */
  private perform(effects: MergeEffects): void {
    for (const id of effects.cancelPolls) {
      this.cancelTask(id);
      this.clearProgress(id);
    }
    for (const s of effects.startPolls) {
      this.resumed.add(s.messageId);
      this.startPoll(s.turnId, s.messageId);
    }
    if (effects.completedTurnIds.length) this.completed(effects.completedTurnIds);
    for (const f of effects.fetchAttachments) this.fetchAttachment(f.messageId, f.attachmentId);
  }

  /** FBK-2: drained unconditionally; sound and haptic only in the
   * foreground. A turn the web push already sounded finished while the page
   * was hidden, so the foreground check is the suppression. */
  private completed(turnIds: string[]): void {
    if (this.isForeground()) this.emit({ kind: "finished", turnIds });
  }

  // --- attachments and images (SYN-7) ----------------------------------------

  private fetchAttachment(messageId: string, attachmentId: string): void {
    const key = `att:${attachmentId}`;
    if (this.attachmentTried.has(attachmentId)) return;
    this.attachmentTried.add(attachmentId);
    this.runTask(`attachment:${messageId}:${attachmentId}`, async (signal) => {
      let blob = await this.persistence.getImage(key).catch(() => undefined);
      if (!blob) {
        try {
          blob = await this.api.attachment(attachmentId, signal);
        } catch {
          return;
        }
        if (signal.aborted) return;
        await this.persistence.putImage(key, blob).catch(() => {});
      }
      if (signal.aborted) return;
      this.update(messageId, (m) => {
        const have = imageKeysOf(m);
        return have.includes(key) ? m : { ...m, imageKey: undefined, imageKeys: [...have, key] };
      });
      this.persist();
    });
  }

  private retryMissingAttachments(): void {
    for (const m of this.state.messages) {
      const have = imageKeysOf(m);
      for (const id of attachmentIdsOf(m)) if (!have.includes(`att:${id}`)) this.fetchAttachment(m.id, id);
    }
  }

  /** An object URL for a stored image, made once and reused. */
  imageUrl(key: string): Promise<string | null> {
    let p = this.imageUrls.get(key);
    if (!p) {
      p = this.persistence
        .getImage(key)
        .then((b) => (b ? URL.createObjectURL(b) : null))
        .catch(() => null);
      this.imageUrls.set(key, p);
    }
    return p;
  }

  // --- helpers ---------------------------------------------------------------

  private runTask(key: string, body: (signal: AbortSignal) => Promise<void>): void {
    if (this.stopped) return;
    this.cancelTask(key);
    const task: Task = { controller: new AbortController() };
    this.tasks.set(key, task);
    const signal = task.controller.signal;
    void body(signal)
      .catch(() => {})
      .finally(() => {
        if (this.tasks.get(key) === task) this.tasks.delete(key);
      });
  }

  private cancelTask(key: string): void {
    const t = this.tasks.get(key);
    if (t) {
      t.controller.abort();
      this.tasks.delete(key);
    }
  }

  /** Test support: is anything still in flight? */
  get busy(): boolean {
    return this.tasks.size > 0;
  }

  private find(id: string): ChatMessage | undefined {
    return this.state.messages.find((m) => m.id === id);
  }

  private update(id: string, fn: (m: ChatMessage) => ChatMessage): void {
    const i = this.state.messages.findIndex((m) => m.id === id);
    if (i < 0) return;
    const next = this.state.messages.slice();
    next[i] = fn(next[i]);
    this.setMessages(next);
  }

  /** SYN-6: on every change, never per typewriter tick (streaming state is
   * not in `messages`). Writes only what changed, in order. */
  private persist(): void {
    const put: ChatMessage[] = [];
    const seen = new Set<string>();
    for (const m of this.state.messages) {
      seen.add(m.id);
      if (this.persisted.get(m.id) !== m) {
        put.push(m);
        this.persisted.set(m.id, m);
      }
    }
    const remove: string[] = [];
    for (const id of this.persisted.keys()) {
      if (!seen.has(id)) remove.push(id);
    }
    for (const id of remove) this.persisted.delete(id);
    const cursor: CursorRecord = { latestSeq: this.latestSeq, hasOlderOnServer: this.state.hasOlderOnServer };
    this.saveChain = this.saveChain
      .then(() => this.persistence.save(put, remove, cursor))
      .catch(() => {
        // A failed write must not wedge later ones. Force the next save to
        // rewrite these by forgetting what we thought was stored.
        for (const m of put) this.persisted.delete(m.id);
      });
  }
}

const EARLIER_KEY = "earlier-page";

type PassResult = "ok" | "unreachable" | "trouble";

function classifyPassError(e: unknown, signal: AbortSignal): PassResult {
  if (signal.aborted || e instanceof CancelledError) return "ok";
  if (isTransportFailure(e)) return "unreachable";
  // A 401 is handled by the Api (back to pairing); anything else that came
  // back is a server answering badly.
  return "trouble";
}

export function sendBackoff(attempt: number, policy: PollingPolicy): number {
  return Math.min(policy.sendBaseInterval * 2 ** Math.min(attempt, 10), policy.sendMaxInterval);
}

export function sendErrorText(e: unknown): string {
  if (e instanceof TransportError) return TEXT.sendExhausted;
  if (e instanceof HttpError) {
    if (e.status >= 500) return TEXT.sendExhausted.replace("Couldn't reach Marlow", `Marlow's server failed (${e.status})`);
    return e.message;
  }
  return e instanceof Error && e.message ? e.message : TEXT.somethingWrong;
}

function isPlausibleMessage(m: unknown): m is ChatMessage {
  if (!m || typeof m !== "object") return false;
  const x = m as Record<string, unknown>;
  const statuses: MessageStatus[] = ["sending", "running", "stalled", "done", "error"];
  return (
    typeof x.id === "string" &&
    (x.role === "user" || x.role === "assistant") &&
    typeof x.text === "string" &&
    typeof x.createdAt === "number" &&
    statuses.includes(x.status as MessageStatus)
  );
}

/** Rebuild display order from an unordered store: server rows by seq, each
 * local-only message after the last row created no later than it. */
export function orderLoaded(messages: ChatMessage[]): ChatMessage[] {
  const rows = messages.filter((m) => m.seq !== undefined).sort((a, b) => a.seq! - b.seq!);
  const local = messages
    .filter((m) => m.seq === undefined)
    .sort((a, b) => a.createdAt - b.createdAt || (a.role === "user" ? -1 : 1));
  const out: ChatMessage[] = [];
  let li = 0;
  for (const r of rows) {
    while (li < local.length && local[li].createdAt < r.createdAt) out.push(local[li++]);
    out.push(r);
  }
  while (li < local.length) out.push(local[li++]);
  return out;
}

async function blobToBase64(blob: Blob): Promise<string> {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}
