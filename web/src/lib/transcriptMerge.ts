// Folds server transcript rows into the client's `messages`
// (docs/tasks/ios-server-transcript.md; contract SYN-3, SYN-4). Pure: no store,
// no network, no clock, so every rule can be pinned by a test and the store
// only has to carry out the `MergeEffects` it returns. Inputs are never mutated.
//
// **Local identity wins.** A message this client created keeps its own `id` for
// life, and the server row that confirms it is folded into it. Only a row with
// no local counterpart becomes a new `ChatMessage`, with the row's own id.
// Swapping the `id` of a message already on screen is a row teardown (a React
// remount), and mid-stream it would break follow-scroll.

import { sameId } from "./types";
import type { ChatMessage, MessageStatus, TranscriptRow } from "./types";

/** What the store must do besides render `messages`. Kept out of the merge
 * because each one touches tasks, queues or the network. */
export interface MergeEffects {
  /** Turn ids for receive feedback. Only for a reply that newly landed, and
   * never on the bootstrap fetch. */
  completedTurnIds: string[];
  /** A placeholder inserted for a user row nothing has answered: poll its
   * turn, so a reply sent from another device streams here. */
  startPolls: { messageId: string; turnId: string }[];
  fetchAttachments: { messageId: string; attachmentId: string }[];
  /** Placeholders a row resolved or removed: their poll has nothing left to
   * learn, and left running would report the turn a second time. */
  cancelPolls: string[];
}

export const GENERIC_ERROR = "Something went wrong.";

function newId(): string {
  return globalThis.crypto.randomUUID();
}

function messageIdFor(row: TranscriptRow): string {
  return row.id ? row.id.toLowerCase() : newId();
}

/** `announce` is false for the bootstrap fetch (an empty cache) and for earlier
 * pages, so neither plays a receive sound per row it backfills. */
export function applyRows(
  rows: TranscriptRow[],
  messages: ChatMessage[],
  announce: boolean,
): { messages: ChatMessage[]; effects: MergeEffects } {
  const out = messages.slice();
  const effects: MergeEffects = {
    completedTurnIds: [],
    startPolls: [],
    fetchAttachments: [],
    cancelPolls: [],
  };
  // A row already on a message is skipped, which is what makes a page
  // idempotent: a retried or overlapping fetch changes nothing.
  const seen = new Set<number>();
  for (const m of out) if (m.seq !== undefined) seen.add(m.seq);
  const ordered = rows.slice().sort((a, b) => a.seq - b.seq);

  // Turns an assistant row in this batch answers, including rows later in the
  // batch than the user row asking. A user row from another device arrives in
  // the same page as its reply whenever the client was away.
  const answeredInBatch = new Set<string>();
  for (const row of ordered) {
    if (row.role !== "assistant") continue;
    answeredInBatch.add(row.turn_id);
    for (const a of row.answers) answeredInBatch.add(a);
  }

  for (const row of ordered) {
    if (row.role === "other" || seen.has(row.seq)) continue;
    seen.add(row.seq);
    if (row.role === "user") mergeUser(row, out, effects, answeredInBatch);
    else mergeAssistant(row, out, effects, announce);
  }
  return { messages: out, effects };
}

function mergeUser(
  row: TranscriptRow,
  messages: ChatMessage[],
  effects: MergeEffects,
  answeredInBatch: Set<string>,
): void {
  // This client's own send: the server row confirms the bubble that is already
  // on screen. Nothing else about it changes, in particular the local
  // `imageKeys`, so the photos are not downloaded back.
  const own = messages.findIndex((m) => m.role === "user" && sameId(m.id, row.client_msg_id));
  if (own >= 0) {
    // The local quote came from the idea the tap was on; the row's is the
    // server's reading of the same id, so it only fills a gap.
    const quote = messages[own].quote ?? row.quote ?? undefined;
    messages[own] = { ...messages[own], seq: row.seq, ...(quote ? { quote } : {}) };
    return;
  }

  const id = messageIdFor(row);
  const existing = messages.findIndex((m) => sameId(m.id, id));
  if (existing >= 0) {
    messages[existing] = { ...messages[existing], seq: messages[existing].seq ?? row.seq };
    return;
  }

  const user: ChatMessage = {
    id,
    role: "user",
    text: row.text,
    createdAt: row.created_at * 1000,
    turnId: row.turn_id,
    status: "done",
    seq: row.seq,
    ...(row.quote ? { quote: row.quote } : {}),
  };
  const images = row.attachments.filter((a) => a.mime.startsWith("image/"));
  if (images.length > 0) {
    user.attachmentIds = images.map((a) => a.id);
    for (const image of images) effects.fetchAttachments.push({ messageId: id, attachmentId: image.id });
  }
  const index = insertionIndex(row.seq, messages);
  messages.splice(index, 0, user);

  // "In flight" is exactly "no assistant row answers this turn". Checked
  // against what is already merged as well as the batch: the reply may have
  // arrived in an earlier page, or already be a placeholder here.
  if (answeredInBatch.has(row.turn_id) || isAnswered(row.turn_id, messages)) return;
  const placeholder: ChatMessage = {
    id: newId(),
    role: "assistant",
    text: "",
    createdAt: row.created_at * 1000,
    turnId: row.turn_id,
    status: "running",
  };
  messages.splice(index + 1, 0, placeholder);
  effects.startPolls.push({ messageId: placeholder.id, turnId: row.turn_id });
}

/** Whether any assistant message already stands for `turnId`: a server row that
 * answers it, or a placeholder polling it. */
function isAnswered(turnId: string, messages: ChatMessage[]): boolean {
  return messages.some(
    (m) => m.role === "assistant" && (m.turnId === turnId || (m.answers?.includes(turnId) ?? false)),
  );
}

function mergeAssistant(
  row: TranscriptRow,
  messages: ChatMessage[],
  effects: MergeEffects,
  announce: boolean,
): void {
  const status: MessageStatus = row.status === "error" ? "error" : "done";
  const text = status === "error" && row.text === "" ? GENERIC_ERROR : row.text;

  // The placeholder this reply finishes: by its own turn first, then by a turn
  // it answers. The second is the retry: the row's turn is the fresh one B and
  // it answers the original A, but the placeholder may still be carrying A
  // (docs/tasks/server-transcript.md).
  const waiting = (m: ChatMessage) => m.role === "assistant" && m.seq === undefined;
  const answersTurn = (m: ChatMessage) => m.turnId !== undefined && row.answers.includes(m.turnId);
  let index = messages.findIndex((m) => waiting(m) && m.turnId === row.turn_id);
  if (index < 0) index = messages.findIndex((m) => waiting(m) && answersTurn(m));

  if (index < 0) {
    const message: ChatMessage = {
      id: messageIdFor(row),
      role: "assistant",
      text,
      createdAt: row.created_at * 1000,
      turnId: row.turn_id,
      status,
      seq: row.seq,
      answers: row.answers.slice(),
    };
    messages.splice(insertionIndex(row.seq, messages), 0, message);
    if (status === "done" && announce) effects.completedTurnIds.push(row.turn_id);
    return;
  }

  // A placeholder its own poll already finished has sounded: the row only
  // confirms it. Announcing again would play the receive sound a second time
  // for one reply. One that is not `done` (still running, or an error the poll
  // gave up with) has not.
  const alreadySounded = messages[index].status === "done";
  messages[index] = {
    ...messages[index],
    seq: row.seq,
    text,
    status,
    answers: row.answers.slice(),
  };
  const resolved = messages[index].id;
  effects.cancelPolls.push(resolved);
  if (status === "done" && announce && !alreadySounded) effects.completedTurnIds.push(row.turn_id);

  // Every other placeholder for a turn this reply answers was merged into it.
  // The poll does the same on a `merged` status; the row may simply get there
  // first, and one reply must not sit under two bubbles.
  const merged = new Set(
    messages.filter((m) => waiting(m) && m.id !== resolved && answersTurn(m)).map((m) => m.id),
  );
  if (merged.size > 0) {
    for (let i = messages.length - 1; i >= 0; i--) if (merged.has(messages[i].id)) messages.splice(i, 1);
    effects.cancelPolls.push(...merged);
  }
}

/** Right after the last message a server row backs with a lower `seq`, or at
 * the start if there is none. A still-unconfirmed user message that already
 * followed that point stays after the insert, which is what puts a cron
 * briefing above this client's unconfirmed send, in server order.
 *
 * The exception is a live reply placeholder (local-only, `sending`, `running`
 * or `stalled`) directly under a confirmed user message: the insert goes below
 * it. Without that, a row landing mid-stream sat between a confirmed send and
 * the reply streaming under it, and follow-scroll treats that user bubble and
 * reply as an adjacent pair (review, 2026-10-01).
 *
 * By `seq`, never by arrival and never by clock. On 2026-09-09 Mike unlocked to
 * the morning briefing with the *nightly consolidate*, the older turn, rendered
 * under it, and a 30-minute timestamp divider on the older message then read as
 * a lie: the notification prefetch and the later drain did not deliver turns in
 * the order they were made. The server is the only writer, so its order is the
 * display order, and rows are applied oldest-first so receive sounds queue the
 * same way. */
function insertionIndex(seq: number, messages: ChatMessage[]): number {
  let last = -1;
  for (let i = messages.length - 1; i >= 0; i--) {
    const s = messages[i].seq;
    if (s !== undefined && s < seq) {
      last = i;
      break;
    }
  }
  if (last < 0) return 0;
  let index = last + 1;
  while (index < messages.length && isLivePlaceholder(messages[index])) index++;
  return index;
}

function isLivePlaceholder(m: ChatMessage): boolean {
  if (m.role !== "assistant" || m.seq !== undefined) return false;
  return m.status === "sending" || m.status === "running" || m.status === "stalled";
}
