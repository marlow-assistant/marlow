// Mutable holders over the pure playback state in partialText.ts and
// activity.ts, the shape ChatStore's timers want. The logic and its tests live
// in those two files; this only keeps the current state.

import * as activity from "./activity";
import * as partial from "./partialText";
import type { TurnActivity } from "./types";

export class TextPlayback {
  private s: partial.PartialTextState;

  constructor(joinedLate: boolean) {
    this.s = partial.createPartialTextState({ joinedLate });
  }

  ingest(text: string, marks: number[], ticksPerPoll: number): void {
    this.s = partial.ingest(this.s, text, marks, undefined, ticksPerPoll);
  }

  /** The revealed text after one tick, or null when there is nothing left to
   * reveal (the timer stops). */
  advance(jumpThreshold: number): string | null {
    const next = partial.tick(this.s, Date.now(), jumpThreshold);
    if (next === this.s) return null;
    this.s = next;
    return partial.visibleText(next);
  }

  revealedMarks(): number[] {
    return partial.revealedMarks(this.s);
  }
}

export class ActivityPlayback {
  private s = activity.createActivityPlaybackState();

  ingest(activities: readonly TurnActivity[]): void {
    this.s = activity.ingestActivities(this.s, activities);
  }

  /** The next queued label, or undefined when the queue is empty (the label
   * on screen stays). The caller holds each one for the dwell. */
  next(): string | undefined {
    const { state, label } = activity.nextLabel(this.s);
    this.s = state;
    return label ?? undefined;
  }
}
