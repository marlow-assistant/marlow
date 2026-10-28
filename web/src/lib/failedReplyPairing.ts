// Messages-style failed send (contract SND-8; Mike, 2026-09-28, on device): the
// retry arrow beside HIS bubble and the reason under it, rather than a Retry
// button down on the placeholder row near the bottom of the screen.
//
// Only pairs with both halves in the rendered window; a placeholder whose
// message has scrolled out keeps its own inline Retry, so retry is never lost.

import { sameId } from "./types";
import type { ChatMessage } from "./types";

/** user message id -> the failed reply placeholder id it pairs with. */
export function failedReplies(messages: ChatMessage[]): Map<string, string> {
  const users = new Map<string, string>();
  for (const m of messages) if (m.role === "user") users.set(m.id.toLowerCase(), m.id);
  const result = new Map<string, string>();
  for (const m of messages) {
    if (m.role !== "assistant" || m.status !== "error") continue;
    if (!m.clientMsgId) continue;
    const userId = users.get(m.clientMsgId.toLowerCase());
    if (userId !== undefined && sameId(userId, m.clientMsgId)) result.set(userId, m.id);
  }
  return result;
}
