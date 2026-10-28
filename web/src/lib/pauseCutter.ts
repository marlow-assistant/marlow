// When to cut a recording into the next segment (docs/tasks/voice-mode-android.md,
// "Recording: one Opus file per segment, cut at a pause"). Pure, so the rule
// is tested without a microphone; segmentRecorder.ts feeds it a level every
// ~50 ms and starts a new MediaRecorder when it says so.
//
// Why cut at all: each segment is uploaded and transcribed while the speaker
// keeps talking, so on release only the last one is left to wait for. Why at
// a pause: a cut mid-word splits it across two transcriptions.

export interface PauseCutterOptions {
  /** No cut before a segment is this long: short segments cost a request and
   * a little accuracy each, and a short utterance needs no cutting. */
  minSegmentMs: number;
  /** Quiet this long is a pause worth cutting in. */
  pauseMs: number;
  /** From here a shorter gap will do: the gap between two words. */
  urgentAfterMs: number;
  urgentPauseMs: number;
  /** Cut regardless. Keeps every segment under the server's 35 s refusal
   * (server/transcribe.py) with room for the recorder's own slack. */
  hardCutMs: number;
}

export const DEFAULT_CUTTER: PauseCutterOptions = {
  minSegmentMs: 12_000,
  pauseMs: 500,
  // The doc's "hard cut at ~25 s, at the quietest moment of the last 3 s":
  // a recorder can't cut in the past, so the last 3 s accept any word gap
  // instead, and 25 s cuts wherever it is.
  urgentAfterMs: 22_000,
  urgentPauseMs: 150,
  hardCutMs: 25_000,
};

/** Above the noise floor by this factor (~10 dB) is speech. */
const SPEECH_RATIO = 3;
/** And never below this RMS, so a near-silent room doesn't make breath speech. */
const MIN_THRESHOLD = 0.01;
/** The floor follows a quieter room at once and a louder one slowly. Per
 * sample, at ~20 a second: below the threshold it settles in about a second;
 * above it (speech, or a room that got much louder) over ~10 minutes, or
 * 25 s of talking would become the floor and its pauses would vanish. A room
 * that jumps above the threshold for good costs the pause cuts, not the
 * recording: the 25 s hard cut still holds. */
const FLOOR_RISE_QUIET = 0.05;
const FLOOR_RISE_LOUD = 0.0001;
/** The first second only lowers the floor, from the quietest reading. People
 * rarely speak in the first few hundred ms after a press, and even running
 * speech dips between syllables; taking the first reading as the floor made
 * a recording that opened mid-word read its own speech as silence. */
const WARM_UP_MS = 1_000;

export class PauseCutter {
  private floor: number | null = null;
  private segmentStart = 0;
  private quietSince: number | null = null;
  private firstSample: number | null = null;
  /** Why the last `true` from sample() cut, for the debug report. */
  lastReason: "pause" | "word gap" | "hard" = "pause";

  constructor(private readonly opts: PauseCutterOptions = DEFAULT_CUTTER) {}

  /** A new segment started at `now` (ms). */
  reset(now: number): void {
    this.segmentStart = now;
    this.quietSince = null;
  }

  get threshold(): number {
    return Math.max(MIN_THRESHOLD, (this.floor ?? 0) * SPEECH_RATIO);
  }

  /** One level reading (RMS, 0–1) at `now` (ms). True means cut here. */
  sample(level: number, now: number): boolean {
    // Exactly zero is no reading, not a silent room: a fresh AudioContext's
    // analyser reads all zeros until it has rendered, and taken as the floor
    // that zero made every sound in a noisy car speech, so only the 25 s hard
    // cut fired (`/code-review high`, 2026-10-22). Only the hard cut is
    // checked, so a stream that goes dead still ends its segment.
    if (level === 0) return this.because("hard", now - this.segmentStart >= this.opts.hardCutMs);
    this.firstSample ??= now;
    const warming = now - this.firstSample < WARM_UP_MS;
    const quiet = level < this.threshold;
    if (this.floor === null || level < this.floor) this.floor = level;
    else if (!warming) this.floor += (level - this.floor) * (quiet ? FLOOR_RISE_QUIET : FLOOR_RISE_LOUD);

    if (!quiet) this.quietSince = null;
    else if (this.quietSince === null) this.quietSince = now;

    const age = now - this.segmentStart;
    if (age >= this.opts.hardCutMs) return this.because("hard", true);
    if (this.quietSince === null) return false;
    const quietFor = now - this.quietSince;
    if (age >= this.opts.urgentAfterMs) return this.because("word gap", quietFor >= this.opts.urgentPauseMs);
    return this.because("pause", age >= this.opts.minSegmentMs && quietFor >= this.opts.pauseMs);
  }

  private because(reason: PauseCutter["lastReason"], cut: boolean): boolean {
    if (cut) this.lastReason = reason;
    return cut;
  }
}

/** RMS of a time-domain buffer from `AnalyserNode.getFloatTimeDomainData`. */
export function rms(samples: Float32Array): number {
  let sum = 0;
  for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
  return samples.length ? Math.sqrt(sum / samples.length) : 0;
}
