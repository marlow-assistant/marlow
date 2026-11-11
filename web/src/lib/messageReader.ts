// "Read aloud" on a reply's long-press menu (docs/tasks/read-aloud.md): one
// reply read in the voice picked in Settings, outside voice mode. The web's
// half of iOS `MessageReader`.
//
// Read the way voice mode reads a reply: one speak() per sentence, so Phonon
// makes the next while this one plays, with code and markdown kept out. The
// speech is voice mode's own (`PhononSpeech` over the browser's voice), so
// until Phonon has loaded, and wherever it can't, the browser's voice reads.

import { CodeFenceFilter, plainSpeech, SentenceChunker } from "./spokenReply";
import type { SpeechOutput } from "./voiceMode";

export interface ReaderSpeech extends SpeechOutput {
  /** Called inside the tap: iOS Safari lets a page speak only once it has
   * done so in a user gesture (`BrowserSpeech.unlock`). */
  unlock(): void;
  close(): void;
}

export interface MessageReaderDeps {
  /** Built on the first read and kept: loading Phonon takes seconds. */
  makeSpeech: () => ReaderSpeech;
  /** The voice stored in Settings, read fresh on every read so a change
   * there applies to the next one. */
  storedVoice: () => Promise<string | null>;
  /** Points the speech at that voice, and starts Phonon if it reads with it. */
  configure: (speech: ReaderSpeech, stored: string | null) => void;
}

/** The sentences of `text` worth hearing, as voice mode would say them. */
export function readableSentences(text: string): string[] {
  const fences = new CodeFenceFilter();
  return new SentenceChunker()
    .finish(text)
    .map((s) => plainSpeech(fences.filter(s)))
    .filter((s) => s !== "");
}

export class MessageReader {
  /** The id of the message being read, for the stop pill and the menu. */
  reading: string | null = null;

  private readonly deps: MessageReaderDeps;
  private speech: ReaderSpeech | null = null;
  private left = 0;
  /** Bumped by every stop, so a read stopped while the stored voice was
   * still being fetched never starts. */
  private gen = 0;
  private listeners = new Set<() => void>();

  constructor(deps: MessageReaderDeps) {
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
    const speech = (this.speech ??= this.deps.makeSpeech());
    speech.unlock();
    speech.onUtteranceFinished = () => {
      if (this.reading === null) return;
      if (--this.left <= 0) this.stop();
    };
    const gen = this.gen;
    this.left = sentences.length;
    this.setReading(id);
    void this.deps
      .storedVoice()
      .catch(() => null)
      .then((stored) => {
        if (gen !== this.gen) return;
        this.deps.configure(speech, stored);
        sentences.forEach((s) => speech.speak(s));
      });
  }

  stop(): void {
    if (this.reading === null) return;
    this.gen++;
    this.left = 0;
    this.speech?.stopSpeaking();
    this.setReading(null);
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
