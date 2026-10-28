// Shared fakes for the chat store's tests. The Api under test is the REAL Api
// with an injected `fetch`, so the error classification the store depends on
// (rejected fetch = TransportError, non-2xx = HttpError, bad JSON =
// DecodeError, our own abort = CancelledError) is exercised, not assumed.

import { vi } from "vitest";
import { Api } from "./api";
import {
  ChatStore,
  DEFAULT_POLICY,
  type ChatStoreOptions,
  type FeedbackEvent,
  type PollingPolicy,
} from "./chatStore";
import type { ChatPersistence, CursorRecord } from "./storage";
import type { ChatMessage, TranscriptRow } from "./types";

// --- persistence -------------------------------------------------------------

export interface SaveRecord {
  put: ChatMessage[];
  remove: string[];
  cursor: CursorRecord;
}

export class MemPersistence implements ChatPersistence {
  messages = new Map<string, ChatMessage>();
  cursor: CursorRecord | undefined;
  images = new Map<string, Blob>();
  saves: SaveRecord[] = [];
  failPutImage = false;
  failLoad = false;
  loads = 0;

  constructor(seed?: { messages?: ChatMessage[]; cursor?: CursorRecord }) {
    for (const m of seed?.messages ?? []) this.messages.set(m.id, m);
    this.cursor = seed?.cursor;
  }

  async load() {
    this.loads++;
    if (this.failLoad) throw new Error("unreadable cache");
    // Deliberately unordered, like IndexedDB's key order.
    const messages = [...this.messages.values()].sort((a, b) => (a.id < b.id ? -1 : 1));
    return { cursor: this.cursor, messages };
  }
  async save(put: ChatMessage[], remove: string[], cursor: CursorRecord) {
    this.saves.push({ put: [...put], remove: [...remove], cursor: { ...cursor } });
    for (const m of put) this.messages.set(m.id, m);
    for (const id of remove) this.messages.delete(id);
    this.cursor = cursor;
  }
  async putImage(key: string, blob: Blob) {
    if (this.failPutImage) throw new Error("quota");
    this.images.set(key, blob);
  }
  async getImage(key: string) {
    return this.images.get(key);
  }
  async deleteImages(keep: (key: string) => boolean) {
    for (const k of [...this.images.keys()]) if (!keep(k)) this.images.delete(k);
  }

  /** Every message id written, across all saves. */
  get putCount(): number {
    return this.saves.reduce((n, s) => n + s.put.length, 0);
  }
}

// --- the fake server -----------------------------------------------------------

export interface Req {
  method: string;
  url: string;
  path: string;
  query: URLSearchParams;
  body: any;
  headers: Record<string, string>;
  /** Fake-clock time (Date.now) when the request was made. */
  at: number;
  signal?: AbortSignal | null;
}

export type Handler = (req: Req) => Response | Promise<Response>;

export const json = (body: unknown, status = 200): Handler => () =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

/** A 200 whose body is not JSON: DecodeError. */
export const badJson = (): Handler => () => new Response("<html>not json", { status: 200 });

/** fetch() rejects: TransportError. */
export const netFail = (): Handler => () => {
  throw new TypeError("Failed to fetch");
};

/** Never answers, but honours abort like a real fetch. */
export const hang = (): Handler => (req) =>
  new Promise<Response>((_, reject) => {
    req.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), {
      once: true,
    });
  });

/** Sends 200 and its headers at once, then never finishes the body; a real
 * fetch errors the body stream when its request is aborted, and so does this. */
export const stallBody = (): Handler => (req) =>
  new Response(
    new ReadableStream<Uint8Array>({
      start(c) {
        c.enqueue(new TextEncoder().encode('{"rows": ['));
        req.signal?.addEventListener("abort", () => c.error(new DOMException("aborted", "AbortError")), {
          once: true,
        });
      },
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );

/** Answers `inner` after `ms` of fake time. */
export const after = (ms: number, inner: Handler): Handler => (req) =>
  new Promise<Response>((resolve, reject) => {
    const t = setTimeout(() => Promise.resolve(inner(req)).then(resolve, reject), ms);
    req.signal?.addEventListener("abort", () => {
      clearTimeout(t);
      reject(new DOMException("aborted", "AbortError"));
    });
  });

/** Holds the response until `release()`; deliberately ignores abort, so a test
 * can prove the store drops a late answer rather than relying on fetch. */
export class Gate {
  private resolveOpen!: () => void;
  readonly opened = new Promise<void>((r) => (this.resolveOpen = r));
  reached = false;
  constructor(private readonly inner: Handler) {}
  handler: Handler = async (req) => {
    this.reached = true;
    await this.opened;
    return this.inner(req);
  };
  release(): void {
    this.resolveOpen();
  }
}

export type Route =
  | "POST /v1/message"
  | "GET /v1/turn"
  | "GET /v1/transcript"
  | "GET /v1/permissions"
  | "POST /v1/permission"
  | "GET /health"
  | "GET /v1/usage"
  | "GET /v1/transcript/attachment";

function routeOf(method: string, path: string): Route | null {
  if (path === "/v1/message") return `${method} /v1/message` as Route;
  if (path.startsWith("/v1/turn/")) return "GET /v1/turn";
  if (path.startsWith("/v1/transcript/attachment/")) return "GET /v1/transcript/attachment";
  if (path === "/v1/transcript") return "GET /v1/transcript";
  if (path === "/v1/permissions") return "GET /v1/permissions";
  if (path.startsWith("/v1/permission/")) return "POST /v1/permission";
  if (path === "/health") return "GET /health";
  if (path === "/v1/usage") return "GET /v1/usage";
  return null;
}

export class FakeServer {
  requests: Req[] = [];
  private queues = new Map<Route, Handler[]>();
  private fallbacks = new Map<Route, Handler>();
  inFlight = 0;
  maxInFlight = new Map<Route, number>();
  private flight = new Map<Route, number>();

  constructor() {
    this.fallbacks.set("POST /v1/message", json({ turn_id: "turn-1" }));
    this.fallbacks.set("GET /v1/turn", json({ turn_id: "turn-1", status: "running" }));
    this.fallbacks.set("GET /v1/transcript", json({ rows: [], latest_seq: 0, has_more: false }));
    this.fallbacks.set("GET /v1/permissions", json({ permission_requests: [] }));
    this.fallbacks.set("POST /v1/permission", json({ accepted: true }));
    this.fallbacks.set("GET /health", json({ status: "ok", auth_ok: true }));
    this.fallbacks.set("GET /v1/usage", json({ plan: null, conversation: null, marlow: null }));
  }

  /** Queue one-shot handlers, consumed in order, then the fallback. */
  script(route: Route, ...handlers: Handler[]): this {
    const q = this.queues.get(route) ?? [];
    q.push(...handlers);
    this.queues.set(route, q);
    return this;
  }

  /** Replace what answers once the queue is empty. */
  always(route: Route, handler: Handler): this {
    this.fallbacks.set(route, handler);
    return this;
  }

  fetch = async (input: string, init?: RequestInit): Promise<Response> => {
    const u = new URL(input, "http://marlow.test");
    const method = init?.method ?? "GET";
    const req: Req = {
      method,
      url: input,
      path: u.pathname,
      query: u.searchParams,
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
      headers: (init?.headers ?? {}) as Record<string, string>,
      at: Date.now(),
      signal: init?.signal,
    };
    this.requests.push(req);
    const route = routeOf(method, u.pathname);
    if (!route) return new Response("{}", { status: 404 });
    const handler = this.queues.get(route)?.shift() ?? this.fallbacks.get(route)!;
    const n = (this.flight.get(route) ?? 0) + 1;
    this.flight.set(route, n);
    this.maxInFlight.set(route, Math.max(this.maxInFlight.get(route) ?? 0, n));
    try {
      return await handler(req);
    } finally {
      this.flight.set(route, (this.flight.get(route) ?? 1) - 1);
    }
  };

  to(route: Route): Req[] {
    return this.requests.filter((r) => routeOf(r.method, r.path) === route);
  }
  count(route: Route): number {
    return this.to(route).length;
  }
}

// --- rows and turns ------------------------------------------------------------

export function row(over: Partial<TranscriptRow> & { seq: number }): TranscriptRow {
  return {
    id: `row-${over.seq}`,
    role: "assistant",
    source: "chat",
    turn_id: `t${over.seq}`,
    client_msg_id: null,
    text: `text ${over.seq}`,
    status: "done",
    created_at: Date.now() / 1000,
    answers: [],
    attachments: [],
    ...over,
  };
}

export const page = (rows: TranscriptRow[], opts: { latest?: number; hasMore?: boolean } = {}): Handler =>
  json({
    rows,
    latest_seq: opts.latest ?? rows.reduce((a, r) => Math.max(a, r.seq), 0),
    has_more: opts.hasMore ?? false,
  });

export const turn = (over: Record<string, unknown> = {}): Handler =>
  json({
    turn_id: "turn-1",
    status: "running",
    result_text: null,
    error: null,
    merged_into: null,
    activities: [],
    partial_text: "",
    partial_text_marks: [],
    ...over,
  });

export function msg(over: Partial<ChatMessage> & { id: string }): ChatMessage {
  return { role: "user", text: "hi", createdAt: Date.now(), status: "done", ...over };
}

// --- the harness -----------------------------------------------------------------

export interface Harness {
  store: ChatStore;
  server: FakeServer;
  persist: MemPersistence;
  events: FeedbackEvent[];
  /** Every distinct `connection` value the store published, in order. */
  connections: string[];
  unauthorized: number;
  ids: string[];
}

export const TZ = "America/Edmonton";

export function makeHarness(
  opts: {
    server?: FakeServer;
    persist?: MemPersistence;
    policy?: Partial<PollingPolicy>;
    isForeground?: boolean;
    store?: Partial<ChatStoreOptions>;
  } = {},
): Harness {
  const server = opts.server ?? new FakeServer();
  const persist = opts.persist ?? new MemPersistence();
  let n = 0;
  const ids: string[] = [];
  const h: Harness = { server, persist, events: [], connections: ["idle"], unauthorized: 0, ids } as unknown as Harness;
  const api = new Api({
    getToken: () => "tok",
    onUnauthorized: () => h.unauthorized++,
    fetch: server.fetch,
  });
  const store = new ChatStore({
    api,
    persistence: persist,
    policy: { ...DEFAULT_POLICY, ...opts.policy },
    newId: () => {
      const id = `id-${++n}`;
      ids.push(id);
      return id;
    },
    timezone: () => TZ,
    isForeground: () => opts.isForeground ?? true,
    ...opts.store,
  });
  h.store = store;
  store.onFeedback((e) => h.events.push(e));
  store.subscribe(() => {
    const c = store.getState().connection;
    if (h.connections[h.connections.length - 1] !== c) h.connections.push(c);
  });
  return h;
}

export const advance = (ms: number) => vi.advanceTimersByTimeAsync(ms);

/** Let queued microtasks and zero-delay work run. */
export const flush = () => vi.advanceTimersByTimeAsync(0);

export const START_TIME = new Date("2026-10-01T12:00:00Z");

export function useFakeClock(): void {
  vi.useFakeTimers();
  vi.setSystemTime(START_TIME);
}

/** The assistant placeholder id after `send` with the harness's id counter. */
export const userIdOf = (h: Harness) => h.store.getState().messages.find((m) => m.role === "user")!.id;
export const placeholderOf = (h: Harness) =>
  h.store.getState().messages.find((m) => m.role === "assistant" && m.clientMsgId)!;
