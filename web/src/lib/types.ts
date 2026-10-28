// Wire types and the client's message model. The server is the source of
// truth for the wire shapes (server/app.py, server/transcript.py); the iOS
// client's `APIModels.swift` and `ChatMessage.swift` are the reference for the
// model. Every decoder here is lenient (SYN-1): a page that fails to decode is
// retried forever against the same bytes, so an unknown key or value must not
// throw.

export type Role = "user" | "assistant";

/** `stalled` is deliberately not terminal: the slow poll phase still covers it
 * and a reload resumes it (SND-7). An unknown status decodes as `error`. */
export type MessageStatus = "sending" | "running" | "stalled" | "done" | "error";

/** The idea a "Do it" message answers, shown as a quote above the text. The
 * bubble says "Do it"; the quote says what. Wire shape of a transcript row's
 * `quote` (docs/tasks/ideas-panel.md). */
export interface IdeaQuote {
  idea_id: string;
  emoji: string;
  title: string;
}

export interface ChatMessage {
  /** Stable for life (SYN-3): React keys and follow-scroll depend on it. A
   * lowercase UUID string. */
  id: string;
  role: Role;
  text: string;
  /** Epoch milliseconds. */
  createdAt: number;
  turnId?: string;
  status: MessageStatus;
  /** Set only on an assistant placeholder this client created: the paired
   * user message's `id`. */
  clientMsgId?: string;
  /** The server row backing this message; undefined while local-only. */
  seq?: number;
  /** The server's image attachments for this row, in order. Fetched into the
   * image store under `att:<id>`; a bubble shows a placeholder for each until
   * it is there (see `imageKeys`). */
  attachmentIds?: string[];
  /** Image-store keys this client holds for the row: its own send (`<id>`,
   * `<id>:1`, ...), or fetched attachments. */
  imageKeys?: string[];
  /** Before several images per message these were single values. They are
   * still read (a cache written by an earlier build) through `attachmentIdsOf`
   * and `imageKeysOf`, and never written. */
  attachmentId?: string;
  imageKey?: string;
  /** A user message sent by "Do it" on an idea. Set at once from the idea
   * the tap came from, and kept so a retry or an outbox resume sends the same
   * `idea_id` again. A server row's own quote fills it only when absent. */
  quote?: IdeaQuote;
  /** Assistant rows: the user-row turn ids this reply resolves. */
  answers?: string[];
}

export function attachmentIdsOf(m: ChatMessage): string[] {
  return m.attachmentIds ?? (m.attachmentId ? [m.attachmentId] : []);
}

export function imageKeysOf(m: ChatMessage): string[] {
  return m.imageKeys ?? (m.imageKey ? [m.imageKey] : []);
}

/** Per-image key in the image store for this client's own send. Index 0 keeps
 * the bare id, which is what a single-image send was always saved under. */
export function ownImageKey(messageId: string, index: number): string {
  return index === 0 ? messageId : `${messageId}:${index}`;
}

export interface TranscriptAttachment {
  id: string;
  mime: string;
}

/** `role` is `"other"` for anything the server sends that this build does not
 * know: the merge skips it but its `seq` still advances the cursor. */
export interface TranscriptRow {
  seq: number;
  id: string;
  role: Role | "other";
  source: string;
  turn_id: string;
  client_msg_id: string | null;
  text: string;
  status: "done" | "error" | null;
  /** Server epoch SECONDS. */
  created_at: number;
  answers: string[];
  attachments: TranscriptAttachment[];
  /** Null from an older server, or when the message is not a "Do it". */
  quote?: IdeaQuote | null;
}

export interface TranscriptPage {
  rows: TranscriptRow[];
  latest_seq: number;
  has_more: boolean;
}

export interface TurnActivity {
  kind: string;
  tool: string | null;
  /** Server epoch seconds. */
  at: number;
}

export interface Turn {
  turn_id: string;
  status: string;
  result_text: string | null;
  error: string | null;
  merged_into: string | null;
  activities: TurnActivity[];
  partial_text: string;
  partial_text_marks: number[];
}

export interface PermissionRequestInfo {
  request_id: string;
  tool: string;
  tool_input: Record<string, unknown>;
  /** Server epoch seconds. */
  created_at: number;
  expires_at: number;
  /** The server's plain-language reading of the request
   * (server/approval_summary.py). Null from an older server: the card then
   * falls back to the tool name and the raw input. */
  summary: PermissionSummary | null;
}

/** What the approval card says about a request. The split matters and the
 * card keeps it visible: `title` and `preview` are facts the server lifted
 * from the tool input; `marlowSays` is the model's own account of why, which
 * an injected prompt could write too, so it is only ever shown as a quotation. */
export interface PermissionSummary {
  title: string;
  preview: PermissionPreviewLine[];
  marlowSays: string | null;
  /** Set when the server offers "Allow similar for this reply". The server
   * answers 400 to `allow_similar` without one, so the button exists only
   * when this does. */
  similar: { label: string } | null;
}

export interface PermissionPreviewLine {
  /** Any other value the server may grow later renders as plain text. */
  op: "remove" | "add" | "text";
  text: string;
}

export type PermissionDecision = "allow" | "deny" | "allow_similar";

export interface HealthResponse {
  status?: string;
  auth_ok?: boolean;
  reauth_prewarn?: boolean;
  queue_paused?: boolean;
  queue_depth?: number;
  model?: string | null;
  effort?: string | null;
  git_commit?: string;
  git_branch?: string;
}

function str(v: unknown, fallback = ""): string {
  return typeof v === "string" ? v : fallback;
}

function num(v: unknown, fallback = 0): number {
  return typeof v === "number" && Number.isFinite(v) ? v : fallback;
}

function strArray(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
}

/** An absent, null or malformed quote is no quote. */
export function decodeQuote(raw: unknown): IdeaQuote | null {
  if (!raw || typeof raw !== "object") return null;
  const q = raw as Record<string, unknown>;
  if (typeof q.idea_id !== "string" || q.idea_id === "" || typeof q.title !== "string" || q.title === "") return null;
  return { idea_id: q.idea_id, emoji: str(q.emoji), title: q.title };
}

/** Lenient row decode. Returns null only when the row has no usable `seq`,
 * which is the one thing the cursor cannot do without. */
export function decodeRow(raw: unknown): TranscriptRow | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  if (typeof r.seq !== "number" || !Number.isFinite(r.seq)) return null;
  const role = r.role === "user" || r.role === "assistant" ? r.role : "other";
  const status = r.status == null ? null : r.status === "done" ? "done" : "error";
  const attachments = Array.isArray(r.attachments)
    ? r.attachments
        .filter((a): a is Record<string, unknown> => !!a && typeof a === "object")
        .map((a) => ({ id: str(a.id), mime: str(a.mime) }))
        .filter((a) => a.id !== "")
    : [];
  return {
    seq: r.seq,
    id: str(r.id),
    role,
    source: str(r.source),
    turn_id: str(r.turn_id),
    client_msg_id: typeof r.client_msg_id === "string" ? r.client_msg_id : null,
    text: str(r.text),
    status,
    created_at: num(r.created_at),
    answers: strArray(r.answers),
    attachments,
    quote: decodeQuote(r.quote),
  };
}

export function decodePage(raw: unknown): TranscriptPage {
  const r = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  const rows = Array.isArray(r.rows)
    ? r.rows.map(decodeRow).filter((x): x is TranscriptRow => x !== null)
    : [];
  return { rows, latest_seq: num(r.latest_seq), has_more: r.has_more === true };
}

export function decodeTurn(raw: unknown): Turn {
  const r = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  const activities = Array.isArray(r.activities)
    ? r.activities
        .filter((a): a is Record<string, unknown> => !!a && typeof a === "object")
        .map((a) => ({
          kind: str(a.kind),
          tool: typeof a.tool === "string" ? a.tool : null,
          at: num(a.at),
        }))
    : [];
  return {
    turn_id: str(r.turn_id),
    status: str(r.status),
    result_text: typeof r.result_text === "string" ? r.result_text : null,
    error: typeof r.error === "string" ? r.error : null,
    merged_into: typeof r.merged_into === "string" ? r.merged_into : null,
    activities,
    partial_text: str(r.partial_text),
    partial_text_marks: Array.isArray(r.partial_text_marks)
      ? r.partial_text_marks.filter((m): m is number => typeof m === "number")
      : [],
  };
}

/** Case-insensitive UUID comparison (SND-1). */
export function sameId(a: string | null | undefined, b: string | null | undefined): boolean {
  return !!a && !!b && a.toLowerCase() === b.toLowerCase();
}
