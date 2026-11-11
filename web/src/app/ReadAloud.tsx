// "Read aloud" on a reply's long-press menu (docs/tasks/read-aloud.md): the
// reader Chat owns, how a row reaches it, and the pill that stops it.
import { createContext, useContext, useEffect, useState, useSyncExternalStore } from "react";
import { BrowserSpeech } from "../lib/browserSpeech";
import { MessageReader } from "../lib/messageReader";
import { loadPhonon, PhononSpeech, usesPhonon } from "../lib/phononSpeech";
import { isPhononKey, phononVoiceFor } from "../lib/phononVoices";
import { kvGet, KV_VOICE } from "../lib/storage";

/** Voice mode's pair: Phonon, over the browser's voice it falls back to.
 * Speaks through the Phonon half. */
class PairSpeech {
  readonly browser = new BrowserSpeech();
  readonly phonon = new PhononSpeech({
    fallback: this.browser,
    load: loadPhonon,
    makeContext: () => new AudioContext(),
    now: () => performance.now(),
  });
  /** Phonon was started, so once loaded it reads everything it is handed. */
  started = false;
  /** Settles once Phonon is ready or has failed (then the browser's voice
   * reads, the only one left). Never rejects. */
  private loaded: Promise<void> | null = null;
  private settled = false;

  /** Starts Phonon, and resolves once it can read; nothing to wait for once
   * it has settled, so a later read never flashes "Preparing". */
  ready(): Promise<void> | void {
    if (this.settled) return;
    if (!this.loaded) {
      this.started = true;
      this.loaded = new Promise<void>((resolve) => {
        this.phonon.onLoad = (status) => {
          // Kept settled after a failure: `PhononSpeech.start` never
          // retries, so a fresh wait would never end.
          if (status.state === "loading") return;
          this.settled = true;
          resolve();
        };
      });
      this.phonon.start();
    }
    return this.loaded;
  }

  get onUtteranceFinished() {
    return this.phonon.onUtteranceFinished;
  }
  set onUtteranceFinished(fn: (() => void) | null) {
    this.phonon.onUtteranceFinished = fn;
  }
  speak(text: string) {
    this.phonon.speak(text);
  }
  stopSpeaking() {
    this.phonon.stopSpeaking();
  }
  unlock() {
    this.phonon.unlock();
  }
  close() {
    this.phonon.close();
  }
}

/** What rows use of the reader. */
export type Reader = Pick<MessageReader, "reading" | "preparing" | "read" | "stop" | "subscribe">;

export const ReaderContext = createContext<Reader | null>(null);

/** The reader, built with voice mode's speech and torn down on unmount.
 * Built in the effect, as VoiceMode's speech is, so StrictMode's remount
 * doesn't close the speech the second mount is using. Null where the browser
 * has no speech at all: then the menu has no Read aloud. */
export function useMessageReader(): Reader | null {
  const [reader, setReader] = useState<MessageReader<PairSpeech> | null>(null);
  useEffect(() => {
    if (!BrowserSpeech.supported()) return;
    const r = new MessageReader<PairSpeech>({
      makeSpeech: () => new PairSpeech(),
      storedVoice: () => kvGet<string>(KV_VOICE).then((v) => v ?? null),
      fits: (speech, stored) => usesPhonon(stored) || !speech.started,
      // As VoiceMode.tsx does: a Phonon voice is no voiceURI, so the
      // browser's voice keeps its default; Phonon loads only for someone
      // who reads with it. Unlike voice mode, the read waits for Phonon
      // rather than starting in the browser's voice (`MessageReaderDeps`).
      configure: (speech, stored) => {
        if (!isPhononKey(stored)) speech.browser.setChosenVoice(stored);
        speech.phonon.setVoice(phononVoiceFor(stored).name);
        if (usesPhonon(stored)) return speech.ready();
      },
    });
    setReader(r);
    return () => {
      r.close();
      setReader(null);
    };
  }, []);
  return reader;
}

const noSubscribe = () => () => {};

/** The id of the message being read, or null. */
export function useReading(reader: Reader | null): string | null {
  return useSyncExternalStore(reader?.subscribe ?? noSubscribe, () => reader?.reading ?? null);
}

/** Whether the read is waiting for its voice to load. */
export function usePreparing(reader: Reader | null): boolean {
  return useSyncExternalStore(reader?.subscribe ?? noSubscribe, () => reader?.preparing ?? false);
}

/** What a reply's menu needs, or undefined where there is no reader. Each row
 * watches only whether it is the one being read, so starting or ending a read
 * re-renders two rows, not the transcript (`/code-review high`, 2026-11-11). */
export function useReadAloud(id: string, text: string) {
  const reader = useContext(ReaderContext);
  const reading = useSyncExternalStore(reader?.subscribe ?? noSubscribe, () => reader?.reading === id);
  if (!reader) return undefined;
  return {
    reading,
    onRead: () => reader.read(id, text),
    onStop: () => reader.stop(),
  };
}

/** Floats above the composer while a reply is read; a tap stops it. While
 * Phonon loads (the first time, a ~65 MB download) it says so, since the
 * read is silent until then. */
export function ReadingPill({ onStop, preparing = false }: { onStop: () => void; preparing?: boolean }) {
  return (
    <button
      type="button"
      className="reading-pill"
      onClick={onStop}
      data-testid="reading-pill"
      aria-label="Stop reading"
    >
      {preparing ? (
        <span className="spinner" aria-hidden="true" />
      ) : (
        <span className="reading-pill-stop" aria-hidden="true" />
      )}
      {preparing ? "Preparing the voice…" : "Stop reading"}
    </button>
  );
}
