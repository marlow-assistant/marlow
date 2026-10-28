// Speech to text through the box (docs/tasks/voice-mode-android.md): record
// with the pause-cut recorder, upload each segment as it ends, and take the
// whole utterance's text from the final one. Shared by the composer's mic and
// voice mode.

import { CancelledError, HttpError, TransportError, type Api } from "./api";
import { Microphone, micErrorText, recordingMimeType, SegmentRecorder } from "./segmentRecorder";
import type { UtteranceDebug, VoiceDebugLog } from "./voiceDebug";

/** Retries per segment for a dropped connection or a gateway hiccup. Not for
 * any other 5xx: a 503 is a box without the model, and a 500 is the engine
 * failing on this very audio, which a retry would only feed it again while
 * other devices' segments wait behind it on the box's one worker. */
const UPLOAD_ATTEMPTS = 3;
const RETRY_MS = 1_000;

function retryable(e: unknown): boolean {
  return e instanceof TransportError || (e instanceof HttpError && (e.status === 502 || e.status === 504));
}

export class DictationError extends Error {}

/** A segment failed after earlier ones were heard. `heard` is their text. */
export class PartialTranscriptError extends Error {
  constructor(
    readonly heard: string,
    readonly cause: unknown,
  ) {
    super("part of the utterance could not be transcribed");
  }
}

/** One utterance's segments to the server, one at a time and in order: the
 * server refuses a segment whose predecessor it hasn't seen. Each upload starts
 * when the previous answered, so the box transcribes segment n while the
 * speaker is still saying n+1. */
export class SegmentUploader {
  private seq = 0;
  private chain: Promise<string> = Promise.resolve("");
  private failed: unknown = null;
  private last: Blob | null = null;
  /** The utterance's text so far, from the latest answer. */
  private heard = "";
  readonly utteranceId: string;

  constructor(
    private readonly api: Pick<Api, "transcribeSegment">,
    private readonly signal: AbortSignal,
    newId: () => string = () => crypto.randomUUID(),
    private readonly sleep: (ms: number) => Promise<void> = (ms) => new Promise((r) => setTimeout(r, ms)),
    private readonly debug: UtteranceDebug | null = null,
  ) {
    this.utteranceId = newId();
  }

  /** A segment that is not the last. */
  add(blob: Blob): void {
    if (blob.size === 0) return;
    this.last = blob;
    const seq = this.seq++;
    this.chain = this.chain.then(() => this.upload(seq, blob, false));
    // Kept, not thrown here: finish() reports it.
    this.chain.catch((e) => (this.failed ??= e));
  }

  /** The last segment; resolves with the whole utterance's text. */
  async finish(blob: Blob): Promise<string> {
    await this.chain.catch(() => {});
    if (this.failed) throw this.heard ? new PartialTranscriptError(this.heard, this.failed) : this.failed;
    if (blob.size > 0) return this.upload(this.seq++, blob, true);
    // A last segment with nothing in it: the server still needs to hear the
    // utterance is over, so the one before is sent again as final, which it
    // answers from what it already has.
    if (this.last) return this.upload(this.seq - 1, this.last, true);
    return "";
  }

  private async upload(seq: number, blob: Blob, final: boolean): Promise<string> {
    for (let attempt = 1; ; attempt++) {
      if (this.signal.aborted) throw new CancelledError();
      try {
        const sentAt = performance.now();
        const r = await this.api.transcribeSegment({ utteranceId: this.utteranceId, seq, final }, blob, this.signal);
        this.debug?.uploaded(seq, blob.size, performance.now() - sentAt, r.serverSeconds, attempt - 1);
        this.heard = r.transcript;
        return r.transcript;
      } catch (e) {
        if (e instanceof CancelledError || this.signal.aborted) throw new CancelledError();
        if (!retryable(e) || attempt >= UPLOAD_ATTEMPTS) throw e;
        await this.sleep(RETRY_MS * attempt);
      }
    }
  }
}

/** Words for a failed transcription. */
export function transcribeErrorText(e: unknown): string {
  if (e instanceof DictationError) return e.message;
  if (e instanceof PartialTranscriptError) return "Part of that couldn't be transcribed.";
  if (e instanceof HttpError && e.status === 503) return "Speech to text isn't set up on Marlow's server.";
  if (e instanceof HttpError && e.status === 413) return "That was too long to transcribe in one go.";
  return "Couldn't transcribe that.";
}

export interface DictationSession {
  /** Starts recording. Rejects with a DictationError the user can read. */
  begin(): Promise<void>;
  /** Stops and transcribes. The text, or "" when nothing was heard. */
  finish(): Promise<string>;
  /** Stops and reports nothing. */
  cancel(): void;
}

/** Mic → pause-cut recorder → uploader. One per utterance; `microphone` may
 * be shared and outlive it (voice mode keeps it open). */
export class Dictation implements DictationSession {
  private recorder: SegmentRecorder | null = null;
  private uploader: SegmentUploader | null = null;
  private readonly controller = new AbortController();
  /** begin()'s work, so a finish() pressed while the mic is still opening
   * waits for it instead of finding nothing recorded. */
  private started: Promise<void> | null = null;

  constructor(
    private readonly api: Pick<Api, "transcribeSegment">,
    private readonly microphone: Microphone,
    private readonly onLevel: (level: number, threshold: number) => void = () => {},
    private readonly debug: UtteranceDebug | null = null,
  ) {}

  begin(): Promise<void> {
    this.started ??= this.start();
    return this.started;
  }

  private async start(): Promise<void> {
    const mimeType = recordingMimeType();
    if (!mimeType) throw new DictationError("This browser can't record audio Marlow understands.");
    let stream: MediaStream;
    try {
      stream = await this.microphone.open();
    } catch (e) {
      // Cancelled while the mic opened (left, or the page hid): nothing to say.
      if (this.controller.signal.aborted) return;
      throw new DictationError(micErrorText(e));
    }
    if (this.controller.signal.aborted) return;
    const uploader = new SegmentUploader(this.api, this.controller.signal, undefined, undefined, this.debug);
    this.uploader = uploader;
    this.recorder = new SegmentRecorder(stream, mimeType, (blob) => uploader.add(blob), this.onLevel);
    if (this.debug) {
      const debug = this.debug;
      this.recorder.onCut = (reason, seconds, threshold) => debug.cut(reason, seconds, threshold);
    }
    this.recorder.start();
  }

  async finish(): Promise<string> {
    await this.started?.catch(() => {});
    if (this.controller.signal.aborted) throw new CancelledError();
    if (!this.recorder || !this.uploader) return "";
    const last = await this.recorder.stop();
    return (await this.uploader.finish(last)).trim();
  }

  cancel(): void {
    this.controller.abort();
    this.recorder?.cancel();
  }
}

/** Voice mode's recorder (lib/voiceMode.ts `VoiceRecorder`) over one
 * Dictation per press, sharing the microphone the mode holds open. */
export class VoiceDictation {
  private current: { dictation: Dictation; report: (text: string | null) => void } | null = null;

  constructor(
    private readonly api: Pick<Api, "transcribeSegment">,
    private readonly microphone: Microphone,
    /** A failure the screen should show, or null when one clears. */
    private readonly onError: (message: string | null) => void,
    private readonly onLevel: (level: number, threshold: number) => void = () => {},
    private readonly log: VoiceDebugLog | null = null,
  ) {}

  begin(onResult: (text: string | null) => void): void {
    this.onError(null);
    const debug = this.log?.start() ?? null;
    const dictation = new Dictation(this.api, this.microphone, this.onLevel, debug);
    let reported = false;
    const report = (text: string | null) => {
      if (reported) return;
      reported = true;
      debug?.transcribed(text);
      if (this.current?.dictation === dictation) this.current = null;
      onResult(text);
    };
    this.current = { dictation, report };
    dictation.begin().catch((e) => {
      this.onError(transcribeErrorText(e));
      report(null);
    });
  }

  finish(): void {
    const current = this.current;
    if (!current) return;
    this.log?.latest?.released();
    current.dictation.finish().then(
      (text) => current.report(text || null),
      (e) => {
        if (e instanceof CancelledError) return;
        this.log?.latest?.failed(e instanceof HttpError ? `HTTP ${e.status}` : e instanceof Error ? e.name : "failed");
        // Never the part that was heard: a truncated turn can't be taken back.
        this.onError(
          e instanceof PartialTranscriptError
            ? "Part of that couldn't be transcribed, so nothing was sent."
            : transcribeErrorText(e),
        );
        current.report(null);
      },
    );
  }

  cancel(): void {
    if (this.current) this.log?.latest?.failed("cancelled");
    this.current?.dictation.cancel();
    this.current = null;
  }
}
