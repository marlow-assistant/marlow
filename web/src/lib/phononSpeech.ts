// Voice mode's natural voice on the web: Phonon 31m, made on the phone by
// `phonon-tts` (WebAssembly in a worker) and played through an AudioContext,
// as the native app does (docs/tasks/voice-mode-tts-engines.md). Mike heard
// the package's own demo on his iPhone and it beat Safari's built-in voices
// by a distance (2026-10-28). It is the default everywhere: first on iPhone
// only, but the Android user found Google's voices poor too (Mike, 2026-10-28).
//
// Until the model is loaded (the first time is a ~65 MB download), or if it
// can't be (no WebAssembly SIMD, offline, a CSP that blocks it), each
// utterance goes to the browser's own voice instead: voice mode is never
// silent. The route is decided as an utterance starts, and the two never talk
// over each other: a change of route waits for the earlier speech to end.

import { isPhononKey, phononVoiceFor, phononVoiceKey, phononVoiceUrl, type PhononVoice } from "./phononVoices";
import type { SpeechOutput } from "./voiceMode";

/** Whether voice mode reads with Phonon: when no device voice was picked in
 * Settings (Automatic), or a Phonon voice was, wherever it can run. Any device
 * voice the person picked stays the browser's. */
export function usesPhonon(stored: string | null): boolean {
  return stored === null || isPhononKey(stored);
}

/** Can this browser run it at all: module workers and WebAssembly SIMD, which
 * the package needs and rejects without. A 5-byte SIMD function, validated. */
export function phononCanRun(): boolean {
  if (typeof Worker === "undefined" || typeof WebAssembly === "undefined") return false;
  try {
    return WebAssembly.validate(
      new Uint8Array([0, 97, 115, 109, 1, 0, 0, 0, 1, 5, 1, 96, 0, 1, 123, 3, 2, 1, 0, 10, 10, 1, 8, 0, 65, 0, 253, 15, 253, 98, 11]),
    );
  } catch {
    return false;
  }
}

/** The part of `PhononTTS` this uses, so tests fake it. */
export interface PhononEngine {
  readonly sampleRate: number;
  readonly device: string;
  readonly threads: number;
  /** Registers a voice from the URL of its file. */
  addVoice(name: string, source: string): Promise<void>;
  /** No `voice` is the model's own default. */
  stream(text: string, options: { signal: AbortSignal; voice?: string }): AsyncIterable<Float32Array>;
}

export type AudioOut = Pick<
  AudioContext,
  "currentTime" | "state" | "resume" | "close" | "createBuffer" | "createBufferSource" | "destination"
>;

/** What the browser's voice (`BrowserSpeech`) provides. */
export interface SpeechFallback extends SpeechOutput {
  onSpeak: (() => void) | null;
  readonly voiceName: string;
  readonly englishVoices: string;
  unlock(): void;
  close(): void;
}

/** `fraction` is the download's, 0 to 1, or null when there is none to show
 * (the model came from the browser's cache, or the size is unknown). */
export type LoadStatus = { state: "loading"; fraction: number | null } | { state: "ready" } | { state: "failed"; reason: string };

export interface PhononSpeechDeps {
  fallback: SpeechFallback;
  load: (onProgress: (fraction: number | null) => void) => Promise<PhononEngine>;
  makeContext: () => AudioOut;
  now: () => number;
}

// One model per page session, shared by every mount of voice mode and never
// disposed: it is a worker holding ~100 MB, but reopening voice mode would
// otherwise spend seconds rebuilding it from the cache, every time. A failed
// load is forgotten so the next mount tries again.
let shared: { promise: Promise<PhononEngine>; listeners: Set<(fraction: number | null) => void> } | null = null;

/** Loads Phonon 31m (English, q8, the package's defaults) on first use. The
 * `import()` is its own chunk, so nothing of it is fetched until voice mode
 * is opened by someone who reads with Phonon. */
export function loadPhonon(onProgress: (fraction: number | null) => void): Promise<PhononEngine> {
  if (!shared) {
    const listeners = new Set<(fraction: number | null) => void>();
    const promise = import("phonon-tts").then(({ PhononTTS }) =>
      PhononTTS.load({
        lang: "en",
        // The weights are nearly all of the download; the other files are KB.
        onProgress: (p) => {
          if (p.file !== "weights") return;
          const fraction = !p.cached && p.total ? p.loaded / p.total : null;
          listeners.forEach((fn) => fn(fraction));
        },
      }),
    );
    const mine = { promise, listeners };
    promise.catch(() => {
      if (shared === mine) shared = null;
    });
    shared = mine;
  }
  shared.listeners.add(onProgress);
  return shared.promise;
}

// Voices already added, per engine and at module level like the engine itself:
// each mount of voice mode, and Settings' previews, share one model, and a voice
// is added to it once. A failed add is forgotten so the next utterance retries.
const addedVoices = new WeakMap<PhononEngine, Map<string, Promise<void>>>();

/** Adds a voice to the engine if it hasn't been, and resolves once it is there. */
export function ensureVoice(engine: PhononEngine, voice: PhononVoice): Promise<void> {
  let added = addedVoices.get(engine);
  if (!added) addedVoices.set(engine, (added = new Map()));
  let pending = added.get(voice.name);
  if (!pending) {
    pending = engine.addVoice(voice.name, phononVoiceUrl(voice));
    added.set(voice.name, pending);
    const map = added;
    pending.catch(() => {
      if (map.get(voice.name) === pending) map.delete(voice.name);
    });
  }
  return pending;
}

export class PhononSpeech implements SpeechOutput {
  onUtteranceFinished: (() => void) | null = null;
  /** Each utterance's first sound, for the debug report's release-to-first-speech. */
  onSpeak: (() => void) | null = null;
  onLoad: ((status: LoadStatus) => void) | null = null;
  /** Per utterance Phonon made: how long generating took, and the audio's length. */
  onSynthesized: ((generationMs: number, audioSeconds: number) => void) | null = null;

  private readonly deps: PhononSpeechDeps;
  private readonly fallback: SpeechFallback;
  private engine: PhononEngine | null = null;
  private status: LoadStatus | null = null;
  private started = false;
  private closed = false;
  private ctx: AudioOut | null = null;
  /** Where the next chunk goes on the context's clock. */
  private cursor = 0;
  private sources = new Set<AudioBufferSourceNode>();
  /** Bumped by every stop. Work started before it sees the change and goes
   * quiet: a stop can't be followed by a stale chunk, or a stale "finished"
   * that ends the next reply early. */
  private gen = 0;
  private queue: string[] = [];
  private looping = false;
  private abort: AbortController | null = null;
  /** Utterances handed on and not yet finished, and the route the last took. */
  private active = 0;
  private route: "phonon" | "browser" | null = null;
  private idle: (() => void)[] = [];
  private voice: PhononVoice = phononVoiceFor(null);
  /** Why the last utterance read in the model's own voice, not `voice`. */
  private voiceError: string | null = null;

  constructor(deps: PhononSpeechDeps) {
    this.deps = deps;
    this.fallback = deps.fallback;
    this.fallback.onUtteranceFinished = () => this.finish();
    this.fallback.onSpeak = () => this.onSpeak?.();
  }

  /** The voice, by name, for utterances made from now on; a name not on the
   * list is Alfie. */
  setVoice(name: string): void {
    this.voice = phononVoiceFor(phononVoiceKey(name));
  }

  /** Begins loading Phonon. Not called for someone reading with a device
   * voice: then this is the browser's voice with nothing downloaded. */
  start(): void {
    if (this.started || this.closed) return;
    this.started = true;
    this.setStatus({ state: "loading", fraction: null });
    this.deps
      .load((fraction) => {
        if (this.status?.state === "loading") this.setStatus({ state: "loading", fraction });
      })
      .then(
        (engine) => {
          if (this.closed) return;
          this.engine = engine;
          this.setStatus({ state: "ready" });
        },
        (e) => {
          if (!this.closed) this.setStatus({ state: "failed", reason: e instanceof Error ? e.message : String(e) });
        },
      );
  }

  private setStatus(status: LoadStatus): void {
    this.status = status;
    this.onLoad?.(status);
  }

  /** What reads replies, and if it isn't Phonon, why not: for the debug report. */
  get voiceName(): string {
    const s = this.status;
    if (s === null) return this.fallback.voiceName;
    if (s.state === "ready" && this.engine) {
      const who = this.voiceError === null ? this.voice.name : `default voice, ${this.voice.name} failed: ${this.voiceError}`;
      return `Phonon ${who} (${this.engine.device}, ${this.engine.threads} thread${this.engine.threads === 1 ? "" : "s"})`;
    }
    const why =
      s.state === "failed"
        ? `failed: ${s.reason}`
        : `loading${s.state === "loading" && s.fraction !== null ? ` ${Math.round(s.fraction * 100)}%` : ""}`;
    return `${this.fallback.voiceName} (Phonon not in use: ${why})`;
  }

  get englishVoices(): string {
    return this.fallback.englishVoices;
  }

  /** Call from a tap. iOS Safari keeps an AudioContext suspended, and a page
   * silent, until one is created or resumed inside a gesture, and a reply
   * arrives long after the tap (the same wall as `BrowserSpeech.unlock`). The
   * context takes the device's own rate: the 24 kHz buffers are resampled by
   * the browser, and a context at an unusual rate is where Safari has bugs.
   * Made even before `start()`: the tap can beat the stored choice's read,
   * and a context first made later, outside a gesture, stays silent. */
  unlock(): void {
    this.fallback.unlock();
    try {
      this.ctx ??= this.deps.makeContext();
      if (this.ctx.state === "suspended") void this.ctx.resume().catch(() => {});
    } catch {
      // The first speak() tries again, and falls back if it can't.
    }
  }

  speak(text: string): void {
    if (!this.started) {
      this.active++;
      this.route = "browser";
      this.fallback.speak(text);
      return;
    }
    this.queue.push(text);
    if (!this.looping) {
      this.looping = true;
      void this.run(this.gen);
    }
  }

  stopSpeaking(): void {
    this.gen++;
    this.queue = [];
    this.looping = false;
    this.active = 0;
    this.route = null;
    this.cursor = 0;
    this.abort?.abort();
    this.abort = null;
    for (const source of [...this.sources]) {
      // `ended` fires for a stopped source too, and must count for nothing.
      source.onended = null;
      try {
        source.stop();
      } catch {
        // Already ended.
      }
    }
    this.sources.clear();
    this.flushIdle();
    this.fallback.stopSpeaking();
  }

  /** Leaving voice mode. The model stays loaded for the next visit. */
  close(): void {
    this.closed = true;
    this.stopSpeaking();
    // Safari caps the AudioContexts a page holds, and each visit makes one.
    void this.ctx?.close().catch(() => {});
    this.ctx = null;
    this.onUtteranceFinished = null;
    this.onLoad = null;
    this.fallback.close();
  }

  /** Hands utterances on in order. Phonon's are handed on once they are made,
   * not once they have played, so the next one is made while this one plays. */
  private async run(gen: number): Promise<void> {
    while (this.queue.length > 0) {
      const text = this.queue.shift()!;
      const engine = this.engine;
      const route = engine ? "phonon" : "browser";
      if (this.route !== null && this.route !== route) {
        await this.untilIdle();
        if (gen !== this.gen) return;
      }
      this.route = route;
      this.active++;
      if (engine) await this.synthesize(engine, text, gen);
      else this.fallback.speak(text);
      if (gen !== this.gen) return;
    }
    this.looping = false;
  }

  private async synthesize(engine: PhononEngine, text: string, gen: number): Promise<void> {
    const abort = new AbortController();
    this.abort = abort;
    const heard = { chunks: 0, ended: 0, madeAll: false, finished: false };
    const maybeFinish = () => {
      if (heard.finished || !heard.madeAll || heard.ended < heard.chunks) return;
      heard.finished = true;
      this.finish();
    };
    const startedAt = this.deps.now();
    let seconds = 0;
    // A voice that can't be added reads in the model's own: never silence.
    const voice = this.voice;
    let voiceName: string | undefined = voice.name;
    try {
      await ensureVoice(engine, voice);
      this.voiceError = null;
    } catch (e) {
      voiceName = undefined;
      this.voiceError = e instanceof Error ? e.message : String(e);
    }
    if (gen !== this.gen) return;
    try {
      const ctx = (this.ctx ??= this.deps.makeContext());
      if (ctx.state !== "running") void ctx.resume().catch(() => {});
      for await (const chunk of engine.stream(text, { voice: voiceName, signal: abort.signal })) {
        if (gen !== this.gen) break;
        const buffer = ctx.createBuffer(1, chunk.length, engine.sampleRate);
        buffer.copyToChannel(chunk as Float32Array<ArrayBuffer>, 0);
        const source = ctx.createBufferSource();
        source.buffer = buffer;
        source.connect(ctx.destination);
        source.onended = () => {
          this.sources.delete(source);
          heard.ended++;
          maybeFinish();
        };
        const at = Math.max(this.cursor, ctx.currentTime);
        source.start(at);
        this.cursor = at + buffer.duration;
        this.sources.add(source);
        seconds += buffer.duration;
        if (++heard.chunks === 1) this.onSpeak?.();
      }
    } catch {
      if (gen !== this.gen) return;
      if (heard.chunks === 0) {
        // Nothing was said: the browser's voice says it, in this one's place.
        this.route = "browser";
        this.fallback.speak(text);
        return;
      }
    }
    if (gen !== this.gen) return;
    if (seconds > 0) this.onSynthesized?.(this.deps.now() - startedAt, seconds);
    heard.madeAll = true;
    maybeFinish();
  }

  private finish(): void {
    if (this.active <= 0) return;
    this.active--;
    this.onUtteranceFinished?.();
    if (this.active === 0) this.flushIdle();
  }

  private untilIdle(): Promise<void> {
    return this.active === 0 ? Promise.resolve() : new Promise((resolve) => this.idle.push(resolve));
  }

  private flushIdle(): void {
    const waiting = this.idle;
    this.idle = [];
    waiting.forEach((resolve) => resolve());
  }
}
