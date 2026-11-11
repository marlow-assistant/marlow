// "Read aloud" on a reply's long-press menu (docs/tasks/read-aloud.md): the
// reader Chat owns, how a row reaches it, and the pill that stops it.
import { createContext, useContext, useEffect, useState, useSyncExternalStore } from "react";
import { BrowserSpeech } from "../lib/browserSpeech";
import { MessageReader } from "../lib/messageReader";
import { loadPhonon, PhononSpeech, usesPhonon } from "../lib/phononSpeech";
import { isPhononKey, phononVoiceFor } from "../lib/phononVoices";
import { kvGet, KV_VOICE } from "../lib/storage";

export const ReaderContext = createContext<MessageReader | null>(null);

/** The reader, built with voice mode's speech and torn down on unmount.
 * Built in the effect, as VoiceMode's speech is, so StrictMode's remount
 * doesn't close the speech the second mount is using. Null where the browser
 * has no speech at all: then the menu has no Read aloud. */
export function useMessageReader(): MessageReader | null {
  const [reader, setReader] = useState<MessageReader | null>(null);
  useEffect(() => {
    if (!BrowserSpeech.supported()) return;
    const r = new MessageReader({
      makeSpeech: () =>
        new PhononSpeech({
          fallback: new BrowserSpeech(),
          load: loadPhonon,
          makeContext: () => new AudioContext(),
          now: () => performance.now(),
        }),
      storedVoice: () => kvGet<string>(KV_VOICE).then((v) => v ?? null),
      // As VoiceMode.tsx does: a Phonon voice is no voiceURI, so the
      // browser's voice keeps its default; Phonon loads only for someone
      // who reads with it.
      configure: (speech, stored) => {
        const phonon = speech as PhononSpeech;
        const browser = (phonon as unknown as { fallback: BrowserSpeech }).fallback;
        if (!isPhononKey(stored)) browser.setChosenVoice(stored);
        phonon.setVoice(phononVoiceFor(stored).name);
        if (usesPhonon(stored)) phonon.start();
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

/** The id of the message being read, or null. */
export function useReading(reader: MessageReader | null): string | null {
  return useSyncExternalStore(
    reader?.subscribe ?? noSubscribe,
    () => reader?.reading ?? null,
  );
}

const noSubscribe = () => () => {};

/** What a reply's menu needs, or undefined where there is no reader. */
export function useReadAloud(id: string, text: string) {
  const reader = useContext(ReaderContext);
  const reading = useReading(reader);
  if (!reader) return undefined;
  return {
    reading: reading === id,
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
