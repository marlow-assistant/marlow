// What the line under Marlow's avatar says: nothing in particular, or what the
// turn in flight is doing right now (contract STA-1).
//
// Pacing and coalescing of the labels happens elsewhere; this only picks WHICH
// message's label the header shows. It used to sit inside that message's row,
// which scrolled away with the row. The header does not.

import type { ChatMessage } from "./types";

export type ChatPresence = { kind: "idle" } | { kind: "active"; label: string };

export const THINKING = "Thinking…";
export const WRITING = "Writing…";
export const SENDING = "Sending…";

/** How far back from the tail to look for a turn in flight. Placeholders are
 * appended at send time, so a live one is always among the newest rows.
 * Bounding the search keeps an idle transcript, where nothing matches, from
 * costing a full walk on every publish. */
export const SEARCH_WINDOW = 20;

/** The web keeps streaming state outside `ChatMessage`, so the caller supplies
 * it per message id. */
export interface PresenceLookups {
  /** The placeholder's current activity label, if it has one. */
  activityLabel?: (messageId: string) => string | undefined;
  /** Whether the placeholder has streamed any text yet. */
  hasStreamedText?: (messageId: string) => boolean;
}

const IDLE: ChatPresence = { kind: "idle" };

/** The OLDEST in-flight reply wins, not the newest. The server runs one turn at
 * a time, so with two sends queued the older one is the one actually working,
 * and the newer sits on its default "Thinking…" label. Showing the newest would
 * say "Thinking…" while the real turn searches the web.
 *
 * `stalled` is left out on purpose. Its row carries its own explanation (the
 * fast poll window closed and checking continues in the background), and a
 * header still claiming "Thinking…" half an hour later would be a claim nobody
 * is checking. */
export function presenceOf(messages: ChatMessage[], lookups: PresenceLookups = {}): ChatPresence {
  const tail = messages.slice(-SEARCH_WINDOW);
  const reply = tail.find(isInFlightReply);
  if (!reply) return IDLE;

  if (reply.status === "sending") return { kind: "active", label: SENDING };
  const label = lookups.activityLabel?.(reply.id) ?? THINKING;
  // The reply is already typing itself into the transcript, so "Thinking…"
  // would contradict what is on screen.
  if (label === THINKING && lookups.hasStreamedText?.(reply.id)) {
    return { kind: "active", label: WRITING };
  }
  return { kind: "active", label };
}

function isInFlightReply(m: ChatMessage): boolean {
  return m.role === "assistant" && (m.status === "sending" || m.status === "running");
}
