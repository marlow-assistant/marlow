// "Read aloud" on a reply's long-press menu (docs/tasks/read-aloud.md): one
// reply read in the voice picked in Settings, outside voice mode. The web's
// half of iOS `MessageReader`.
//
// Read the way voice mode reads a reply: one speak() per sentence, so Phonon
// makes the next while this one plays, with code and markdown kept out. The
// speech is voice mode's own (`PhononSpeech` over the browser's voice). A
// read waits for Phonon to load; only where it can't does the browser's voice
// read (`MessageReaderDeps.configure`).

import { CodeFenceFilter, plainSpeech, SentenceChunker } from "./spokenReply";
import type { SpeechOutput } from "./voiceMode";

export interface ReaderSpeech extends SpeechOutput {
  /** Called inside the tap: iOS Safari lets a page speak only once it has
   * done so in a user gesture (`BrowserSpeech.unlock`). */
  unlock(): void;
  close(): void;
}

export interface MessageReaderDeps<S extends ReaderSpeech> {
  /** Built on the first read and kept: loading Phonon takes seconds. */
  makeSpeech: () => S;
  /** The voice stored in Settings, read fresh on every read so a change
   * there applies to the next one. */
  storedVoice: () => Promise<string | null>;
  /** Whether the kept speech can still read in that voice. Phonon, once
   * loaded, reads everything it is handed, so a device voice picked after it
   * needs a speech that never loaded it (`/code-review high`, 2026-11-11). */
  fits: (speech: S, stored: string | null) => boolean;
  /** Points the speech at that voice, and starts Phonon if it reads with it.
   * A promise holds the read until the voice is ready: reading in a stand-in
   * voice meanwhile was worse than waiting (Mike on Chrome's default voice,
   * 2026-11-11: "awful"). Voice mode is the other way round, never silent,
   * since there the alternative is silence in a car. */
  configure: (speech: S, stored: string | null) => void | Promise<void>;
}

/** The sentences of `text` worth hearing, as voice mode would say them. */
export function readableSentences(text: string): string[] {
  const fences = new CodeFenceFilter();
  return new SentenceChunker()
    .finish(text)
    .map((s) => plainSpeech(fences.filter(s)))
    .filter((s) => s !== "");
}

export class MessageReader<S extends ReaderSpeech = ReaderSpeech> {
  /** The id of the message being read, for the stop pill and the menu. */
  reading: string | null = null;
  /** Waiting for the voice (Phonon loading, or its first download). */
  preparing = false;

  private readonly deps: MessageReaderDeps<S>;
  private speech: S | null = null;
  private left = 0;
  /** Bumped by every stop, so a read stopped while the stored voice was
   * still being fetched never starts. */
  private gen = 0;
  private listeners = new Set<() => void>();

  constructor(deps: MessageReaderDeps<S>) {
    this.deps = deps;
  }

  /** For `useSyncExternalStore`. */
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  read(id: string, text: string): void {
    this.stop();
    const sentences = readableSentences(text);
    if (sentences.length === 0) return;
    // Unlocked inside the tap, before anything is awaited.
    (this.speech ??= this.deps.makeSpeech()).unlock();
    const gen = this.gen;
    this.left = sentences.length;
    this.setReading(id);
    void this.deps
      .storedVoice()
      .catch(() => null)
      .then((stored) => {
        if (gen !== this.gen || !this.speech) return;
        if (!this.deps.fits(this.speech, stored)) {
          // The page's speech is already unlocked, and a speech that never
          // loaded Phonon needs no AudioContext, so no gesture is lost here.
          this.speech.close();
          this.speech = this.deps.makeSpeech();
          this.speech.unlock();
        }
        const speech = this.speech;
        speech.onUtteranceFinished = () => {
          if (this.reading === null) return;
          if (--this.left <= 0) this.stop();
        };
        const ready = this.deps.configure(speech, stored);
        const go = () => {
          if (gen !== this.gen) return;
          this.setPreparing(false);
          sentences.forEach((s) => speech.speak(s));
        };
        if (!ready) return go();
        this.setPreparing(true);
        void ready.then(go, go);
      });
  }

  stop(): void {
    if (this.reading === null) return;
    this.gen++;
    this.left = 0;
    this.speech?.stopSpeaking();
    this.preparing = false;
    this.setReading(null);
  }

  private setPreparing(on: boolean): void {
    if (this.preparing === on) return;
    this.preparing = on;
    this.listeners.forEach((l) => l());
  }

  /** Leaving the chat: Safari caps the AudioContexts a page holds. */
  close(): void {
    this.stop();
    this.speech?.close();
    this.speech = null;
  }

  private setReading(id: string | null): void {
    this.reading = id;
    this.listeners.forEach((l) => l());
  }
}
