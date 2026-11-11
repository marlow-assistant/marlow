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
export type Reader = Pick<MessageReader, "reading" | "read" | "stop" | "subscribe">;

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
      // who reads with it.
      configure: (speech, stored) => {
        if (!isPhononKey(stored)) speech.browser.setChosenVoice(stored);
        speech.phonon.setVoice(phononVoiceFor(stored).name);
        if (usesPhonon(stored)) {
          speech.phonon.start();
          speech.started = true;
        }
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

/** Floats above the composer while a reply is read; a tap stops it. */
export function ReadingPill({ onStop }: { onStop: () => void }) {
  return (
    <button type="button" className="reading-pill" onClick={onStop} data-testid="reading-pill">
      <span className="reading-pill-stop" aria-hidden="true" />
      Stop reading
    </button>
  );
}
