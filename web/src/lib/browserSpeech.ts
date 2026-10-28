// Voice mode's reading voice on the web: `speechSynthesis`, which on Android
// Chrome is the phone's own text-to-speech engine, on the device
// (docs/tasks/voice-mode-android.md, "Text to speech on Android"). Three
// things measured on the emulator (Chrome 149, 2026-10-21) shape it:
//
//  - The voice list is empty at page load and fills ~6 s later. Utterances
//    spoken before then use the engine's default voice for the language;
//    the list is read again on `voiceschanged`.
//  - A cancelled utterance fires `onerror` with "interrupted", never `onend`.
//    A chunker waiting on `onend` hangs on barge-in, so an error ends an
//    utterance like `onend` does.
//  - `onend` fired every time in short runs, but Chrome on Android is known
//    to drop it. A watchdog ends an utterance the engine has stopped
//    speaking, or one running far past its length.

import type { SpeechOutput } from "./voiceMode";

/** Speech runs ~15 characters a second; well past this, the end was lost. */
const MS_PER_CHAR = 150;
const WATCHDOG_SLACK_MS = 4_000;
const POLL_MS = 1_000;

type Synth = Pick<SpeechSynthesis, "speak" | "cancel" | "getVoices" | "speaking" | "pending"> & {
  addEventListener(type: "voiceschanged", fn: () => void): void;
  removeEventListener(type: "voiceschanged", fn: () => void): void;
};

export interface BrowserSpeechDeps {
  synth: Synth;
  makeUtterance: (text: string) => SpeechSynthesisUtterance;
  language: string;
  now: () => number;
}

/** The voice for `language`: the best quality the engine lists (premium,
 * then enhanced, as iOS `VoiceModeAudio.voice` ranks them), then its own
 * locale, on the device, the engine's default, in that order. iOS Safari's
 * first en-US voice is the basic Samantha, which Mike found "pretty bad"
 * beside the native app's Phonon (2026-10-25). The same day's debug report showed
 * iOS Safari lists plain names only, no "Enhanced", so this ranking can't
 * help there. On Android each locale has one voice, chosen in the phone's
 * Settings → Text-to-speech, not here. So the person can choose one in
 * Settings (`choosableVoices`); `chosen`, a `voiceKey`, wins while the engine
 * still lists it. Daniel, an en-GB voice on that same list, sounded better
 * to Mike, though every voice Safari offered still sounded bad: it lists
 * only the basic built-in voices (the picker on his iPhone, 2026-10-25). */
export function pickVoice(
  voices: SpeechSynthesisVoice[],
  language: string,
  chosen: string | null = null,
): SpeechSynthesisVoice | null {
  const picked = chosen === null ? undefined : voices.find((v) => voiceKey(v) === chosen);
  if (picked) return picked;
  const want = norm(language);
  const score = (v: SpeechSynthesisVoice) =>
    [tier(v), norm(v.lang) === want ? 1 : 0, v.localService ? 1 : 0, v.default ? 1 : 0];
  const better = (a: number[], b: number[]) => {
    for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return a[i] > b[i];
    return false;
  };
  let best: SpeechSynthesisVoice | null = null;
  for (const v of voices) {
    // Novelty voices out here too, not just from Settings' list: where no
    // voice is marked default, the first among equals wins, and Apple lists
    // Albert first.
    if (isReadingVoice(v) && (best === null || better(score(v), score(best)))) best = v;
  }
  return best;
}

const norm = (l: string) => l.replace("_", "-").toLowerCase();
const isEnglish = (v: SpeechSynthesisVoice) => norm(v.lang).startsWith("en");

function tier(v: SpeechSynthesisVoice): number {
  const id = `${v.voiceURI} ${v.name}`.toLowerCase();
  return id.includes("premium") ? 2 : id.includes("enhanced") ? 1 : 0;
}

/** Apple's novelty voices, which iOS Safari lists as en-US beside the real
 * ones (the 2026-10-25 debug report). Nobody wants a reply read by Bubbles.
 * Fred, Junior, Kathy and Ralph are old but plain voices, so they stay. */
const NOVELTY = new Set(
  [
    "Albert", "Bad News", "Bahh", "Bells", "Boing", "Bubbles", "Cellos", "Deranged", "Good News", "Hysterical",
    "Jester", "Organ", "Pipe Organ", "Princess", "Superstar", "Trinoids", "Whisper", "Wobble", "Zarvox",
  ].map((n) => n.toLowerCase()),
);

const isReadingVoice = (v: SpeechSynthesisVoice) => isEnglish(v) && !NOVELTY.has(v.name.toLowerCase());

/** How a choice is stored: the `voiceURI`, or the name where an engine leaves
 * that empty. */
export const voiceKey = (v: SpeechSynthesisVoice) => v.voiceURI || v.name;

/** The voices Settings offers: English, no novelty voices, each once, best
 * quality first and then by name. */
export function choosableVoices(voices: SpeechSynthesisVoice[]): SpeechSynthesisVoice[] {
  const seen = new Set<string>();
  const out: SpeechSynthesisVoice[] = [];
  for (const v of voices) {
    if (!isReadingVoice(v) || seen.has(voiceKey(v))) continue;
    seen.add(voiceKey(v));
    out.push(v);
  }
  return out.sort((a, b) => tier(b) - tier(a) || a.name.localeCompare(b.name));
}

/** What Settings needs to list voices and preview one. */
export type VoiceEngine = Pick<BrowserSpeechDeps, "synth" | "makeUtterance">;

/** The browser's, or null where it can't speak. */
export function browserVoiceEngine(): VoiceEngine | null {
  if (!BrowserSpeech.supported()) return null;
  return { synth: window.speechSynthesis, makeUtterance: (text) => new SpeechSynthesisUtterance(text) };
}

/** Settings' preview on a tap, which is also the user gesture iOS Safari
 * wants before it speaks. Cuts off the last preview, so tapping down the list
 * plays one voice at a time. */
export function previewVoice(engine: VoiceEngine, voice: SpeechSynthesisVoice): void {
  engine.synth.cancel();
  const utterance = engine.makeUtterance("Hi, this is how I'll sound in voice mode.");
  utterance.voice = voice;
  utterance.lang = voice.lang;
  engine.synth.speak(utterance);
}

interface Live {
  utterance: SpeechSynthesisUtterance;
  startedAt: number | null;
  length: number;
}

export class BrowserSpeech implements SpeechOutput {
  onUtteranceFinished: (() => void) | null = null;
  /** Each speak(), for the debug report's release-to-first-speech. */
  onSpeak: (() => void) | null = null;

  private readonly deps: BrowserSpeechDeps;
  private voice: SpeechSynthesisVoice | null = null;
  private chosen: string | null = null;
  /** Utterances queued since the last stop, oldest first. An event for one
   * not in here belongs to speech already cut off and is dropped, so a stop
   * can't be followed by a stale "finished" that ends the next reply early. */
  private live: Live[] = [];
  private watchdog: ReturnType<typeof setInterval> | null = null;
  /** Checks in a row that found the engine idle. Two, because a just-queued
   * utterance can read as neither speaking nor pending for a moment while a
   * cold engine starts. */
  private quietChecks = 0;
  private readonly onVoices = () => this.loadVoices();
  private unlocked = false;

  constructor(deps?: Partial<BrowserSpeechDeps>) {
    this.deps = {
      synth: deps?.synth ?? window.speechSynthesis,
      makeUtterance: deps?.makeUtterance ?? ((text) => new SpeechSynthesisUtterance(text)),
      language: deps?.language ?? (navigator.language || "en-US"),
      now: deps?.now ?? (() => Date.now()),
    };
    this.deps.synth.addEventListener("voiceschanged", this.onVoices);
    this.loadVoices();
  }

  static supported(): boolean {
    return typeof window !== "undefined" && "speechSynthesis" in window && typeof SpeechSynthesisUtterance !== "undefined";
  }

  /** The voice chosen in Settings, its `voiceKey`; null leaves it to pickVoice. */
  setChosenVoice(key: string | null): void {
    this.chosen = key;
    this.loadVoices();
  }

  private loadVoices(): void {
    this.voice = pickVoice(this.deps.synth.getVoices(), this.deps.language, this.chosen) ?? this.voice;
  }

  /** The English voices the engine lists, for the debug report: whether a
   * better one than the pick is even on offer. */
  get englishVoices(): string {
    const names = this.deps.synth
      .getVoices()
      .filter(isEnglish)
      .map((v) => `${v.name} (${v.lang})`);
    return names.length ? names.join(", ") : "none listed";
  }

  /** The voice in use, for the debug report. */
  get voiceName(): string {
    return this.voice ? `${this.voice.name} (${this.voice.lang}${this.voice.localService ? ", on device" : ", network"})` : `the engine's default for ${this.deps.language}`;
  }

  /** Call from a tap, before any reply. iOS Safari only lets a page speak
   * once a speak() has run inside a user gesture; the reply arrives long
   * after the tap, so voice mode came back silent on an iPhone home-screen
   * web app (Mike, 2026-10-25); this unlock fixed it, confirmed the same day. An empty
   * utterance is enough. It is not in `live`, so it never counts as a reply's
   * utterance finishing. Android Chrome needs none of this. */
  unlock(): void {
    if (this.unlocked) return;
    this.unlocked = true;
    try {
      this.deps.synth.speak(this.deps.makeUtterance(""));
    } catch {
      // Nothing to unlock here.
    }
  }

  speak(text: string): void {
    this.onSpeak?.();
    const utterance = this.deps.makeUtterance(text);
    if (this.voice) {
      utterance.voice = this.voice;
      utterance.lang = this.voice.lang;
    } else {
      utterance.lang = this.deps.language;
    }
    const entry: Live = { utterance, startedAt: null, length: text.length };
    utterance.onstart = () => {
      entry.startedAt = this.deps.now();
    };
    utterance.onend = () => this.ended(entry);
    // "interrupted" and "canceled" included: see the header.
    utterance.onerror = () => this.ended(entry);
    this.live.push(entry);
    this.deps.synth.speak(utterance);
    this.watchdog ??= setInterval(() => this.check(), POLL_MS);
  }

  stopSpeaking(): void {
    this.live = [];
    this.stopWatchdog();
    this.deps.synth.cancel();
  }

  /** Leaving voice mode for good. */
  close(): void {
    this.stopSpeaking();
    this.onUtteranceFinished = null;
    this.deps.synth.removeEventListener("voiceschanged", this.onVoices);
  }

  private ended(entry: Live): void {
    const at = this.live.indexOf(entry);
    if (at < 0) return;
    // In order: a lost end for an earlier utterance is that one's end too.
    const done = this.live.splice(0, at + 1);
    if (this.live.length === 0) this.stopWatchdog();
    for (let i = 0; i < done.length; i++) this.onUtteranceFinished?.();
  }

  /** The watchdog: the engine went quiet with utterances still owed an end,
   * or the one speaking has run far past its length. */
  check(): void {
    if (this.live.length === 0) return;
    const synth = this.deps.synth;
    if (!synth.speaking && !synth.pending) {
      if (++this.quietChecks >= 2) this.ended(this.live[this.live.length - 1]);
      return;
    }
    this.quietChecks = 0;
    const head = this.live[0];
    if (head.startedAt !== null && this.deps.now() - head.startedAt > head.length * MS_PER_CHAR + WATCHDOG_SLACK_MS) {
      this.ended(head);
    }
  }

  private stopWatchdog(): void {
    if (this.watchdog !== null) clearInterval(this.watchdog);
    this.watchdog = null;
    this.quietChecks = 0;
  }
}
