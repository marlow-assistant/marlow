// Whether the transcript should move to keep a reply in view while it streams,
// after a send from this client. Port of ios ReplyFollow.swift (SCR-5..SCR-8).
//
// The problem it fixes (Mike, 2026-09-29): with a long scrollback, a send
// scrolled to his bubble and the reply's first line, then the reply ran off the
// bottom, because the transcript only scrolled when a *row* was added and a
// streaming reply grows inside one. He was managing the scroll by hand.
//
// The rule Mike agreed: follow the growing reply until the start of the
// exchange reaches the top of the visible region (just under the floating
// header), then leave the scroll alone and let the rest stream in below the
// fold. The start of the exchange is his own message, unless that message is by
// itself taller than the viewport (a long dictation): pinning its top would
// scroll *back up* and hide the reply, so the reply's own top is the pin point.
//
// Stopping is a pin, not a last follow step: once one more follow would carry
// the anchor above the top, the anchor is pinned to the top exactly, so the
// view lands on the point instead of overshooting and snapping back (SCR-7).
//
// Pure so it can be tested without a scroll container; the view supplies the
// geometry and carries out the answer. Everything is in one coordinate space,
// where y grows downward.

export interface Geometry {
  /** The user's message row. If a timestamp divider sits above it, this is the
   * divider's top (SCR-13): measure the thing you scroll to. */
  userTop: number;
  userHeight: number;
  /** The reply row (the placeholder that fills as text streams). */
  replyTop: number;
  replyBottom: number;
  /** The top and bottom of what can be read: under any floating header. */
  visibleTop: number;
  visibleBottom: number;
}

export type FollowAction = "hold" | "followReply" | "pinUser" | "pinReply";

/** Half a point: layout reports fractional positions, and a comparison that
 * flips on rounding noise would scroll by nothing, or pin a frame early. */
export const TOLERANCE = 0.5;

export function visibleHeight(g: Geometry): number {
  return g.visibleBottom - g.visibleTop;
}

export function action(g: Geometry): FollowAction {
  // Unmeasured: the viewport reader or header reader hasn't reported. Moving on
  // a guess is worse than a frame of not moving.
  const vh = visibleHeight(g);
  if (!(vh > 0)) return "hold";

  const userIsTallerThanViewport = g.userHeight > vh + TOLERANCE;
  const anchorTop = userIsTallerThanViewport ? g.replyTop : g.userTop;
  // How much has to be on screen at once to show the exchange from its start to
  // the reply's latest line. Independent of scroll position: scrolling moves
  // both ends together.
  const span = g.replyBottom - anchorTop;

  if (span > vh + TOLERANCE) {
    return userIsTallerThanViewport ? "pinReply" : "pinUser";
  }
  if (g.replyBottom > g.visibleBottom + TOLERANCE) return "followReply";
  return "hold";
}

/** Pins end following; hold and follow do not. */
export function endsFollowing(a: FollowAction): boolean {
  return a === "pinUser" || a === "pinReply";
}

/** Remembers the last geometry acted on, so a frame that is only a scroll can be
 * told apart from one where the reply grew (SCR-8: only growth is news). A
 * scroll moves both rows together and leaves the span from the message's top to
 * the reply's bottom unchanged. A class because it is per-exchange mutable
 * bookkeeping; make a fresh one when following starts. */
export class FollowTracker {
  private last: { span: number; visibleTop: number; visibleBottom: number } | null = null;

  /** Call first, and on every frame, so the baseline keeps moving. The first
   * measurement is recorded, not acted on: it lands while the send's own
   * animated scroll to the new rows is still under way, and that scroll already
   * shows them. */
  isNews(g: Geometry): boolean {
    const now = {
      span: g.replyBottom - g.userTop,
      visibleTop: g.visibleTop,
      visibleBottom: g.visibleBottom,
    };
    const last = this.last;
    this.last = now;
    if (!last) return false;
    return (
      Math.abs(now.span - last.span) > TOLERANCE ||
      now.visibleTop !== last.visibleTop ||
      now.visibleBottom !== last.visibleBottom
    );
  }
}

export interface FollowMessage {
  id: string;
  role: "user" | "assistant";
}

export interface FollowedExchange {
  userId: string;
  replyId: string;
}

/** SCR-5: following starts when the newest two messages are the user message
 * and placeholder THIS client just added (`localSendIds` holds the ids of the
 * rows a local send created). It never starts for another device's send or a
 * briefing, which keep SCR-2. iOS decides this at the scroll-to-bottom request
 * a local send raises; the web has no such request, so the ids carry it. */
export function shouldStartFollowing(
  messages: readonly FollowMessage[],
  localSendIds: ReadonlySet<string>,
): FollowedExchange | null {
  if (messages.length < 2) return null;
  const reply = messages[messages.length - 1];
  const user = messages[messages.length - 2];
  if (reply.role !== "assistant" || user.role !== "user") return null;
  const mine = (id: string) => localSendIds.has(id) || localSendIds.has(id.toLowerCase());
  if (!mine(reply.id) || !mine(user.id)) return null;
  return { userId: user.id, replyId: reply.id };
}

/** SCR-9: do nothing until the reply has words: act only once the placeholder
 * has streamed text, or is done or error. Before that the only changes are the
 * send's own arrival, and row heights interpolating under the arrival animation
 * read as growth (simulator, 2026-09-29). */
export function replyHasWords(reply: { streamedText?: string | null; status: string }): boolean {
  return (reply.streamedText ?? "") !== "" || reply.status === "done" || reply.status === "error";
}

/** SCR-4: the content's end is more than `max(160, clientHeight / 3)` below the
 * visible bottom. Until the height is measured (clientHeight <= 0) the answer
 * is "not far": deciding against height 0 made a full transcript clear the
 * threshold instantly, and the jump button sat on screen at launch. iOS
 * compares against the whole viewport rather than the visible region (gaps item
 * 18, parked); this is the stated rule, not that bug. */
export function isFarFromBottom(
  scrollHeight: number,
  scrollTop: number,
  clientHeight: number,
): boolean {
  if (!(clientHeight > 0)) return false;
  return scrollHeight - scrollTop - clientHeight > Math.max(160, clientHeight / 3);
}
