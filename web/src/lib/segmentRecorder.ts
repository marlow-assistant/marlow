// Records one utterance as a run of independently decodable Opus files, cut
// at pauses (docs/tasks/voice-mode-android.md, "Recording"). The rule for
// where to cut is pauseCutter.ts; this is the browser glue.
//
// One MediaRecorder per segment, on the same live stream. MediaRecorder's
// timeslice chunks are not independently decodable (only the first carries
// the WebM header), so a segment ends by stopping its recorder and starting a
// new one. Stop-then-start lost ~11 ms per cut on the Android emulator, noise
// against a 500 ms pause; starting the new one first bought nothing
// (measured 2026-10-21).

import { DEFAULT_CUTTER, PauseCutter, rms, type PauseCutterOptions } from "./pauseCutter";
import type { CutReason } from "./voiceDebug";

/** Opus, the only codec the server takes. WebM on Chrome; Ogg where a browser
 * records only that. Null when this browser can record neither. */
export function recordingMimeType(): string | null {
  if (typeof MediaRecorder === "undefined" || typeof MediaRecorder.isTypeSupported !== "function") return null;
  for (const type of ["audio/webm;codecs=opus", "audio/ogg;codecs=opus"]) {
    if (MediaRecorder.isTypeSupported(type)) return type;
  }
  return null;
}

export function canRecord(): boolean {
  return typeof navigator !== "undefined" && !!navigator.mediaDevices?.getUserMedia && recordingMimeType() !== null;
}

/** 24 kbps Opus, ~3 KB a second: a tenth of raw PCM, and transcribed as well
 * as the WAV on the box's smoke test (2026-10-21). */
const BITS_PER_SECOND = 24_000;
const POLL_MS = 50;

export interface LevelSource {
  /** RMS 0–1 of the latest audio. */
  level(): number;
  close(): void;
}

export interface SegmentRecorderDeps {
  createRecorder: (stream: MediaStream, mimeType: string) => MediaRecorder;
  createLevelSource: (stream: MediaStream) => LevelSource;
  now: () => number;
  setInterval: (fn: () => void, ms: number) => ReturnType<typeof setInterval>;
  clearInterval: (t: ReturnType<typeof setInterval>) => void;
}

function analyserLevel(stream: MediaStream): LevelSource {
  const ctx = new AudioContext();
  const source = ctx.createMediaStreamSource(stream);
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 2048;
  source.connect(analyser);
  const buf = new Float32Array(analyser.fftSize);
  return {
    level: () => {
      analyser.getFloatTimeDomainData(buf);
      return rms(buf);
    },
    close: () => {
      source.disconnect();
      void ctx.close().catch(() => {});
    },
  };
}

const browserDeps: SegmentRecorderDeps = {
  createRecorder: (stream, mimeType) => new MediaRecorder(stream, { mimeType, audioBitsPerSecond: BITS_PER_SECOND }),
  createLevelSource: analyserLevel,
  now: () => performance.now(),
  setInterval: (fn, ms) => setInterval(fn, ms),
  clearInterval: (t) => clearInterval(t),
};

/** One recorder's output as a Blob, once it has stopped. */
function collect(recorder: MediaRecorder, mimeType: string): Promise<Blob> {
  return new Promise((resolve) => {
    const parts: Blob[] = [];
    recorder.addEventListener("dataavailable", (e) => {
      if (e.data.size) parts.push(e.data);
    });
    recorder.addEventListener("stop", () => resolve(new Blob(parts, { type: mimeType })), { once: true });
    // An error ends the recorder without the usual stop on some engines.
    recorder.addEventListener("error", () => resolve(new Blob(parts, { type: mimeType })), { once: true });
  });
}

export class SegmentRecorder {
  private readonly deps: SegmentRecorderDeps;
  private readonly cutter: PauseCutter;
  private recorder: MediaRecorder | null = null;
  private current: Promise<Blob> | null = null;
  private levels: LevelSource | null = null;
  private timer: ReturnType<typeof setInterval> | null = null;
  /** Earlier segments, handed on in order (each recorder's stop is async). */
  private delivered: Promise<void> = Promise.resolve();
  private done = false;
  /** stop() still hands on segments already cut; cancel() drops them. */
  private keepAfterStop = false;
  private segmentStartedAt = 0;
  /** Each segment as it ends: why, how long, and the cutter's threshold
   * (the voice-mode debug report, lib/voiceDebug.ts). */
  onCut: ((reason: CutReason, seconds: number, threshold: number) => void) | null = null;

  constructor(
    private readonly stream: MediaStream,
    private readonly mimeType: string,
    /** Every segment but the last, in order. */
    private readonly onSegment: (blob: Blob) => void,
    private readonly onLevel: (level: number, threshold: number) => void = () => {},
    deps: Partial<SegmentRecorderDeps> = {},
    cutter: PauseCutterOptions = DEFAULT_CUTTER,
  ) {
    this.deps = { ...browserDeps, ...deps };
    this.cutter = new PauseCutter(cutter);
  }

  start(): void {
    this.levels = this.deps.createLevelSource(this.stream);
    this.begin();
    this.timer = this.deps.setInterval(() => this.tick(), POLL_MS);
  }

  private begin(): void {
    const recorder = this.deps.createRecorder(this.stream, this.mimeType);
    this.current = collect(recorder, this.mimeType);
    this.recorder = recorder;
    recorder.start();
    this.segmentStartedAt = this.deps.now();
    this.cutter.reset(this.segmentStartedAt);
  }

  private reportCut(reason: CutReason): void {
    this.onCut?.(reason, (this.deps.now() - this.segmentStartedAt) / 1000, this.cutter.threshold);
  }

  private tick(): void {
    if (this.done || !this.levels) return;
    const level = this.levels.level();
    this.onLevel(level, this.cutter.threshold);
    if (this.cutter.sample(level, this.deps.now())) this.cut();
  }

  /** Ends this segment and starts the next, inside the pause. */
  private cut(): void {
    const finished = this.current!;
    this.reportCut(this.cutter.lastReason);
    this.recorder!.stop();
    this.begin();
    this.delivered = this.delivered.then(() => finished).then((blob) => {
      if (!this.done || this.keepAfterStop) this.onSegment(blob);
    });
  }

  /** Stops, hands on any segment still in flight, and resolves with the last. */
  async stop(): Promise<Blob> {
    const last = this.current;
    if (!this.done) this.reportCut("release");
    this.keepAfterStop = true;
    this.halt();
    await this.delivered;
    return last ? await last : new Blob([], { type: this.mimeType });
  }

  /** Stops and hands on nothing more. */
  cancel(): void {
    this.keepAfterStop = false;
    this.halt();
  }

  private halt(): void {
    if (this.done) return;
    this.done = true;
    if (this.timer !== null) this.deps.clearInterval(this.timer);
    this.timer = null;
    try {
      if (this.recorder && this.recorder.state !== "inactive") this.recorder.stop();
    } catch {
      // Already stopped by the browser (the track ended).
    }
    this.levels?.close();
    this.levels = null;
  }
}

/** The microphone, held open across recordings where that helps: voice mode
 * opens it on entry and keeps the stream, so a press starts recording at
 * once and can't pick up audio from before it (the emulator's one 15 s
 * recording of 12 s, docs/tasks/voice-mode-android.md, "Unexplained"). */
export class Microphone {
  private stream: MediaStream | null = null;
  /** A getUserMedia still waiting (often on the permission prompt). Shared, so
   * voice mode's open on entry and an early press get one stream, not two
   * with one never stopped. */
  private opening: Promise<MediaStream> | null = null;
  /** Bumped by close(): a stream that arrives after it is stopped at once,
   * or leaving during the prompt left the phone's mic indicator on
   * (`/code-review high`, 2026-10-23). */
  private closes = 0;

  constructor(
    private readonly getUserMedia: (c: MediaStreamConstraints) => Promise<MediaStream> = (c) =>
      navigator.mediaDevices.getUserMedia(c),
  ) {}

  open(): Promise<MediaStream> {
    if (this.stream && this.stream.getAudioTracks().some((t) => t.readyState === "live")) {
      return Promise.resolve(this.stream);
    }
    if (this.opening) return this.opening;
    const closes = this.closes;
    const opening = this.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    })
      .then((stream) => {
        if (closes !== this.closes) {
          stream.getTracks().forEach((t) => t.stop());
          throw new DOMException("The microphone was closed while it opened.", "AbortError");
        }
        this.stream = stream;
        return stream;
      })
      .finally(() => {
        if (this.opening === opening) this.opening = null;
      });
    this.opening = opening;
    return opening;
  }

  close(): void {
    this.closes++;
    this.opening = null;
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
  }
}

/** Words for a failed `getUserMedia`. */
export function micErrorText(e: unknown): string {
  const name = e instanceof DOMException ? e.name : "";
  if (name === "NotAllowedError" || name === "SecurityError") {
    return "The microphone is blocked for Marlow. Allow it in the browser's site settings.";
  }
  if (name === "NotFoundError") return "No microphone found.";
  return "Couldn't start the microphone.";
}
