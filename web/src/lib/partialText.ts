// Paces the server's cumulative reply text out as typing. Port of ios
// `PartialTextPlaybackState` (ChatStore.swift) and the pacing constants in
// PollingPolicy (STR-1..STR-4, FBK-1).
//
// Note what is NOT here, in contrast to the activity playback: no cursor, no
// overlap search, no dedupe. The server sends a growing string and the whole
// thing every time, so "what is new" is just "what is longer", which is why a
// missed poll costs nothing and there is no resync path to get wrong.
//
// Immutable and pure: every function returns a new state and takes the clock as
// an argument. No timers; the caller runs the 50ms tick.
//
// EVERYTHING here counts in Unicode CODE POINTS (STR-4). The server measures
// `len(partial_text)` in Python, which counts code points, and JavaScript's
// `.length` counts UTF-16 units, so on the first emoji the two drift and a mark
// lands in the wrong place. Text is held as `Array.from(text)` and sliced by
// code point; never `.length` on the string.
// (Swift reveals by grapheme cluster and splits marks by scalar; the contract
// says code points, so the web reveals by code point throughout. The only
// visible difference is that a multi-scalar emoji may show its parts a tick
// apart.)

/** One poll interval of generation, as the burst phase's `fastInterval` / tick:
 * 2000 / 50. The divisor uses the 2s interval even during the 500ms burst. */
export const PARTIAL_TEXT_TICK_MS = 50;
export const FAST_INTERVAL_MS = 2000;
export const TICKS_PER_POLL = FAST_INTERVAL_MS / PARTIAL_TEXT_TICK_MS; // 40
/** A phone back from a tunnel with the whole answer buffered should show the
 * answer, not perform it. */
export const PARTIAL_TEXT_JUMP_THRESHOLD = 2000;

export interface PartialTextState {
  /** Everything the server has sent, as code points. Only ever grows: a shorter
   * payload means the turn was coalesced or the server restarted, and rewinding
   * a bubble the reader is mid-sentence in is worse than stale text the
   * terminal `result_text` is about to replace (STR-1). */
  readonly target: readonly string[];
  /** The text on screen, a code-point prefix of `target`. */
  readonly revealed: string;
  readonly revealedCount: number;
  /** Code points revealed per tick, computed once when a slab lands and HELD.
   * Recomputing from the shrinking backlog decays geometrically and never
   * finishes, which leaves the tail of every reply arriving late: the exact
   * latency tax smoothing is supposed to avoid (found 2026-09-01). */
  readonly stride: number;
  /** Offsets (code points) where a tool call interrupted the reply, straight off
   * the server. Replaced only when the new list is at least as long as the one
   * held: the server evicts its oldest marks past a cap, and losing a boundary
   * would silently re-fuse two thoughts. */
  readonly marks: readonly number[];
  /** Set when this state attached to a turn that was ALREADY producing text
   * (relaunch, tab reload, a placeholder created by sync for another device's
   * turn). Typing that out is a REPLAY of a reply generated minutes or hours ago
   * (Mike, 2026-09-05). The rule is "type only what arrived while we were
   * watching": the first slab a late joiner receives is revealed whole and
   * everything after it paces normally. Deliberately NOT keyed on "is this the
   * first ingest", which is also true of a live turn's first text and would
   * flatten the effect this whole feature exists for. */
  readonly catchingUp: boolean;
  /** The `now` of the tick that first put characters on screen, else null.
   * FBK-1: reply-started fires at the reveal, not when the slab arrives. */
  readonly firstVisibleAt: number | null;
}

export function createPartialTextState(opts: { joinedLate?: boolean } = {}): PartialTextState {
  return {
    target: [],
    revealed: "",
    revealedCount: 0,
    stride: 1,
    marks: [],
    catchingUp: opts.joinedLate === true,
    firstVisibleAt: null,
  };
}

/** Code points the server has sent but we have not shown. */
export function backlog(s: PartialTextState): number {
  return Math.max(0, s.target.length - s.revealedCount);
}

/** The marks a reader can actually see yet: the ones inside what has been
 * revealed. A mark past the typing cursor has nothing to split. */
export function revealedMarks(s: PartialTextState): number[] {
  return s.marks.filter((m) => m > 0 && m < s.revealedCount);
}

/** Take a poll's cumulative `partial_text` and `partial_text_marks`. `now` is
 * accepted for call-site symmetry with `tick` and is unused: ingest never
 * reveals anything itself, `tick` is the single path that publishes. */
export function ingest(
  state: PartialTextState,
  text: string,
  marks: readonly number[] = [],
  _now?: number,
  ticksPerPoll: number = TICKS_PER_POLL,
): PartialTextState {
  let s = state;
  if (marks.length >= s.marks.length) s = { ...s, marks: [...marks] };
  const cps = Array.from(text);
  if (cps.length <= s.target.length) return s;
  const withTarget: PartialTextState = { ...s, target: cps };
  if (s.catchingUp) {
    // Everything on the next tick, rather than revealing here: `tick` is the
    // single path that publishes to the bubble, and a state that arrived already
    // drained would never start its player at all (the caller starts the timer
    // only for a non-empty backlog), leaving the caught-up text held and unshown.
    return { ...withTarget, catchingUp: false, stride: backlog(withTarget) };
  }
  const pending = backlog(withTarget);
  return { ...withTarget, stride: Math.max(1, Math.ceil(pending / Math.max(1, ticksPerPoll))) };
}

/** Reveal the next stride (or everything, at or over the jump threshold).
 * Returns the same state object when there is nothing left to reveal, so
 * `tick(s) === s` means "idle: stop the timer". */
export function tick(
  state: PartialTextState,
  now: number = 0,
  jumpThreshold: number = PARTIAL_TEXT_JUMP_THRESHOLD,
): PartialTextState {
  const pending = backlog(state);
  if (pending <= 0) return state;
  const step = pending >= jumpThreshold ? pending : state.stride;
  const revealedCount = Math.min(state.target.length, state.revealedCount + step);
  const revealed = state.target.slice(0, revealedCount).join("");
  return {
    ...state,
    revealed,
    revealedCount,
    firstVisibleAt: state.firstVisibleAt ?? (revealed !== "" ? now : null),
  };
}

/** FBK-1: did this tick put the first characters on screen? */
export function firstCharactersBecameVisible(
  prev: PartialTextState,
  next: PartialTextState,
): boolean {
  return prev.firstVisibleAt === null && next.firstVisibleAt !== null;
}

export function visibleText(s: PartialTextState): string {
  return s.revealed;
}

/** The revealed text broken at the marks the cursor has passed (STR-4). */
export function visiblePieces(s: PartialTextState): string[] {
  return segments(s.revealed, revealedMarks(s));
}

/** Splits streamed text at the server's tool-call offsets, dropping
 * whitespace-only pieces. Without this the 11% of turns that go text, tool, text
 * render as "I'll check your calendar.You have three meetings": two separate
 * thoughts fused into one sentence. Offsets in code points; marks at the edges,
 * duplicates, negatives and out-of-range values are ignored. */
export function segments(text: string, splitAt: readonly number[]): string[] {
  const cps = Array.from(text);
  const cuts = [...new Set(splitAt.filter((m) => m > 0 && m < cps.length))].sort((a, b) => a - b);
  if (cuts.length === 0) return text === "" ? [] : [text];

  const pieces: string[] = [];
  let start = 0;
  for (const cut of [...cuts, cps.length]) {
    const piece = cps.slice(start, cut).join("");
    if (piece.trim() !== "") pieces.push(piece);
    start = cut;
  }
  return pieces;
}

/** FBK-1: reply-started fires once per placeholder. A set of announced ids keeps
 * it from firing again however often the text is republished. Keyed on the
 * placeholder's id because a push only announces a *finished* turn, so there is
 * nothing to match a start against. NEVER pruned: that can only under-fire,
 * where pruning at terminal would re-arm it for a stray late update. */
export class ReplyStartAnnouncer {
  private readonly announced = new Set<string>();

  /** True the first time this id's characters are on screen, false after. */
  announce(placeholderId: string, visible: string): boolean {
    if (visible === "") return false;
    if (this.announced.has(placeholderId)) return false;
    this.announced.add(placeholderId);
    return true;
  }
}
