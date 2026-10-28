// The Phonon voices Settings offers: the same 25 English voices the native
// app has, from the `gradium/phonon-voices` catalog, each a small safetensors
// file that `PhononTTS.addVoice` fetches on first use. Automatic is Alfie
// (Mike, 2026-10-28).

/** The catalog's revision, pinned 2026-10-28 so a change upstream can't swap a
 * voice under people or break the list; `main` moves. */
const VOICES_REVISION = "0ee17205f254b5abc43b266c8a0f215565c352f7";

export interface PhononVoice {
  name: string;
  accent: "British" | "American";
  /** The voice file's name in the catalog. */
  id: string;
}

export const DEFAULT_PHONON_VOICE = "Alfie";

/** British, then American, each in the catalog's alphabetical order. */
export const PHONON_VOICES: PhononVoice[] = [
  { name: "Alfie", accent: "British", id: "X6NceqF285Gr04gb" },
  { name: "Bryony", accent: "British", id: "gfP44RlHwr2kEwk7" },
  { name: "Edwin", accent: "British", id: "zUCxzrOrugTSis03" },
  { name: "Elodie-Rose", accent: "British", id: "y9bQqIDnyxwqK01t" },
  { name: "Freya", accent: "British", id: "GgfEkEJtxZR7gnpy" },
  { name: "Hugh", accent: "British", id: "YyKcxPG7iIgS3jsK" },
  { name: "Imogen", accent: "British", id: "gDT1nz7Ie36ZhL-C" },
  { name: "Lottie", accent: "British", id: "m9MxpY6IUGXtWp76" },
  { name: "Maeve", accent: "British", id: "6PWnV0Nq4wu7RVBT" },
  { name: "Pippa", accent: "British", id: "uem82D50GRv2Dwma" },
  { name: "Spencer", accent: "British", id: "JOW0vchyswkQUMDS" },
  { name: "Teddy", accent: "British", id: "Zn7g6NxLqXlJb7Qi" },
  { name: "Toby", accent: "British", id: "dME3IWyZBvmh1n1q" },
  { name: "Delia", accent: "American", id: "nGzMHMPAWGaJFHOo" },
  { name: "Easton", accent: "American", id: "LMDIYE0Oa377Qh6Z" },
  { name: "Everly", accent: "American", id: "vjFyp2BcCyTlUmYb" },
  { name: "Hadley", accent: "American", id: "bXwrkHhURL1VFJdG" },
  { name: "Harper", accent: "American", id: "4SZHfMpw-p46Ywgs" },
  { name: "Juniper", accent: "American", id: "NIWIOv5vTi5eRMj8" },
  { name: "Micah", accent: "American", id: "40QrMVgAxFiUnP9f" },
  { name: "Miles", accent: "American", id: "d9XCyhjo4JhkHF1d" },
  { name: "Preston", accent: "American", id: "qIxlZ7Ei8KvWlyQH" },
  { name: "Russell", accent: "American", id: "_6Aslh2DxfmnRLmP" },
  { name: "Trevor", accent: "American", id: "0zLApsZfVLLNaEGl" },
  { name: "Zoey", accent: "American", id: "NbpkqMVS3CJeq2j8" },
];

export function phononVoiceUrl(voice: PhononVoice): string {
  return `https://huggingface.co/gradium/phonon-voices/resolve/${VOICES_REVISION}/voices/${voice.id}.safetensors`;
}

// A prefix no `voiceURI` has, so one KV_VOICE holds either kind of choice.
const PREFIX = "phonon:";

export function phononVoiceKey(name: string): string {
  return PREFIX + name;
}

export function isPhononKey(stored: string | null): boolean {
  return stored !== null && stored.startsWith(PREFIX);
}

/** The Phonon voice a stored choice means: the named one, or Alfie for
 * Automatic, a device voice, or a name since dropped from the list. */
export function phononVoiceFor(stored: string | null): PhononVoice {
  const named = isPhononKey(stored) ? PHONON_VOICES.find((v) => phononVoiceKey(v.name) === stored) : undefined;
  return named ?? PHONON_VOICES.find((v) => v.name === DEFAULT_PHONON_VOICE)!;
}
