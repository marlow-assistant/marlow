// What voice mode did, for a user to copy and paste back
// (docs/tasks/voice-mode-android.md, "A voice-mode debug report"). Per
// utterance: each segment's length and why it was cut, the cutter's
// threshold, each upload's time against the box's own transcription time,
// release-to-text and release-to-first-speech, and the voice. Never the words:
// a report goes into a chat with someone else, and the words aren't needed to
// tell cutting, network, the box and the voice apart.

export type CutReason = "pause" | "word gap" | "hard" | "release";

interface Segment {
  seconds: number;
  reason: CutReason;
  threshold: number;
  bytes?: number;
  uploadMs?: number;
  serverMs?: number;
  retries?: number;
}

export class UtteranceDebug {
  readonly segments: Segment[] = [];
  releasedAt: number | null = null;
  textAt: number | null = null;
  sentAt: number | null = null;
  firstSpeechAt: number | null = null;
  words: number | null = null;
  /** Phonon only: time spent generating, and the audio's length, summed over
   * the reply's sentences. Under 1 the phone keeps ahead of the speech. */
  synthMs = 0;
  synthSeconds = 0;
  outcome: string | null = null;

  constructor(
    readonly startedAt: number,
    private readonly now: () => number,
  ) {}

  /** The recorder ended a segment. */
  cut(reason: CutReason, seconds: number, threshold: number): void {
    this.segments.push({ seconds, reason, threshold });
  }

  /** Segment `seq` came back from the box. */
  uploaded(seq: number, bytes: number, uploadMs: number, serverSeconds: number | null, retries: number): void {
    const s = this.segments[seq];
    if (!s) return;
    Object.assign(s, { bytes, uploadMs, retries, ...(serverSeconds !== null ? { serverMs: serverSeconds * 1000 } : {}) });
  }

  released(): void {
    this.releasedAt = this.now();
  }

  transcribed(text: string | null): void {
    this.textAt = this.now();
    this.words = text ? text.split(/\s+/).filter(Boolean).length : 0;
  }

  sent(): void {
    this.sentAt = this.now();
  }

  spoke(): void {
    this.firstSpeechAt ??= this.now();
  }

  synthesized(generationMs: number, audioSeconds: number): void {
    this.synthMs += generationMs;
    this.synthSeconds += audioSeconds;
  }

  failed(why: string): void {
    this.outcome = why;
  }
}

const ms = (n: number | null | undefined) => (n === null || n === undefined ? "-" : `${Math.round(n)} ms`);
const s1 = (n: number) => `${n.toFixed(1)} s`;

/** The last few utterances of one voice-mode session. */
export class VoiceDebugLog {
  private readonly utterances: UtteranceDebug[] = [];
  voice = "the engine's default";
  /** Every English voice on offer, so a poor pick can be told from a poor list. */
  voices = "";

  constructor(
    private readonly now: () => number = () => performance.now(),
    private readonly keep = 5,
  ) {}

  start(): UtteranceDebug {
    const u = new UtteranceDebug(this.now(), this.now);
    this.utterances.push(u);
    if (this.utterances.length > this.keep) this.utterances.shift();
    return u;
  }

  /** The utterance whose reply is being spoken. */
  get latest(): UtteranceDebug | null {
    return this.utterances[this.utterances.length - 1] ?? null;
  }

  format(context: { userAgent?: string; build?: string } = {}): string {
    const lines = [
      "Marlow voice mode debug",
      `browser: ${context.userAgent ?? (typeof navigator !== "undefined" ? navigator.userAgent : "?")}`,
      `web build: ${context.build ?? "?"}`,
      `voice: ${this.voice}`,
    ];
    if (this.voices) lines.push(`voices: ${this.voices}`);
    if (this.utterances.length === 0) lines.push("", "(no utterances yet)");
    this.utterances.forEach((u, i) => {
      const spoken = u.releasedAt !== null ? (u.releasedAt - u.startedAt) / 1000 : null;
      lines.push("", `utterance ${i + 1}${spoken !== null ? `: ${s1(spoken)} recorded` : ""}${u.outcome ? ` (${u.outcome})` : ""}`);
      u.segments.forEach((seg, n) => {
        const parts = [`  seg ${n}: ${s1(seg.seconds)}, cut: ${seg.reason}, threshold ${seg.threshold.toFixed(3)}`];
        if (seg.bytes !== undefined) parts.push(`${Math.round(seg.bytes / 1024)} KB`);
        if (seg.uploadMs !== undefined) parts.push(`round trip ${ms(seg.uploadMs)}`);
        if (seg.serverMs !== undefined) parts.push(`box ${ms(seg.serverMs)}`);
        if (seg.retries) parts.push(`${seg.retries} retries`);
        lines.push(parts.join(", "));
      });
      if (u.releasedAt !== null) {
        lines.push(`  release -> text: ${ms(u.textAt !== null ? u.textAt - u.releasedAt : null)} (${u.words ?? 0} words)`);
        if (u.sentAt !== null) lines.push(`  release -> first speech: ${ms(u.firstSpeechAt !== null ? u.firstSpeechAt - u.releasedAt : null)}`);
        if (u.synthSeconds > 0) {
          lines.push(`  synthesis: ${s1(u.synthSeconds)} of audio in ${s1(u.synthMs / 1000)} (${(u.synthMs / 1000 / u.synthSeconds).toFixed(2)} x real time)`);
        }
      }
    });
    return lines.join("\n");
  }
}
