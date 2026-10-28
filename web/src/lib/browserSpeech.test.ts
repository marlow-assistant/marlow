import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BrowserSpeech, choosableVoices, pickVoice, previewVoice, voiceKey, type BrowserSpeechDeps } from "./browserSpeech";

class FakeUtterance {
  voice: SpeechSynthesisVoice | null = null;
  lang = "";
  onstart: (() => void) | null = null;
  onend: (() => void) | null = null;
  onerror: ((e: { error: string }) => void) | null = null;
  constructor(readonly text: string) {}
}

class FakeSynth {
  queue: FakeUtterance[] = [];
  speaking = false;
  pending = false;
  voices: SpeechSynthesisVoice[] = [];
  private listeners = new Set<() => void>();
  speak(u: FakeUtterance) {
    this.queue.push(u);
    if (!this.speaking) this.startNext();
    else this.pending = true;
  }
  startNext() {
    const u = this.queue[0];
    this.speaking = !!u;
    this.pending = this.queue.length > 1;
    u?.onstart?.();
  }
  /** The head finishes; `fire` false drops its `onend`, as Chrome Android may. */
  finishHead(fire = true) {
    const u = this.queue.shift();
    if (fire) u?.onend?.();
    this.startNext();
  }
  cancel() {
    // Chrome: every cancelled utterance errors with "interrupted".
    const all = this.queue;
    this.queue = [];
    this.speaking = this.pending = false;
    all.forEach((u) => u.onerror?.({ error: "interrupted" }));
  }
  getVoices() {
    return this.voices;
  }
  addEventListener(_: string, fn: () => void) {
    this.listeners.add(fn);
  }
  removeEventListener(_: string, fn: () => void) {
    this.listeners.delete(fn);
  }
  voicesChanged(voices: SpeechSynthesisVoice[]) {
    this.voices = voices;
    this.listeners.forEach((fn) => fn());
  }
}

const voice = (lang: string, extra: Partial<SpeechSynthesisVoice> = {}) =>
  ({ lang, name: lang, default: false, localService: true, voiceURI: lang, ...extra }) as SpeechSynthesisVoice;

let synth: FakeSynth;
let now: number;
let finished: number;

function make() {
  const s = new BrowserSpeech({
    synth: synth as unknown as BrowserSpeechDeps["synth"],
    makeUtterance: (t) => new FakeUtterance(t) as unknown as SpeechSynthesisUtterance,
    language: "en-CA",
    now: () => now,
  });
  s.onUtteranceFinished = () => finished++;
  return s;
}

beforeEach(() => {
  vi.useFakeTimers();
  synth = new FakeSynth();
  now = 0;
  finished = 0;
});
afterEach(() => vi.useRealTimers());

describe("BrowserSpeech", () => {
  it("reports each utterance that plays to its end", () => {
    const s = make();
    s.speak("One.");
    s.speak("Two.");
    synth.finishHead();
    expect(finished).toBe(1);
    synth.finishHead();
    expect(finished).toBe(2);
  });

  it("unlocks with one empty utterance, once, that never counts as a reply's", () => {
    const s = make();
    let spoke = 0;
    s.onSpeak = () => spoke++;
    s.unlock();
    s.unlock();
    expect(synth.queue.map((u) => u.text)).toEqual([""]);
    synth.finishHead();
    expect(finished).toBe(0);
    expect(spoke).toBe(0);
    s.speak("One.");
    synth.finishHead();
    expect(finished).toBe(1);
  });

  it("reports nothing for speech it cut off itself, though the engine errors each one", () => {
    const s = make();
    s.speak("One.");
    s.speak("Two.");
    s.stopSpeaking();
    expect(finished).toBe(0);
  });

  it("treats an interruption it didn't ask for as the end, so the reply doesn't hang", () => {
    const s = make();
    s.speak("One.");
    synth.queue[0].onerror?.({ error: "interrupted" });
    expect(finished).toBe(1);
  });

  it("ends an utterance whose onend never came once the engine goes quiet", () => {
    const s = make();
    s.speak("One.");
    s.speak("Two.");
    synth.finishHead(false);
    synth.finishHead(false);
    vi.advanceTimersByTime(1_000);
    expect(finished, "one quiet check is not enough").toBe(0);
    vi.advanceTimersByTime(1_000);
    expect(finished).toBe(2);
  });

  it("ends a lost earlier utterance when a later one ends", () => {
    const s = make();
    s.speak("One.");
    s.speak("Two.");
    synth.finishHead(false);
    synth.finishHead(true);
    expect(finished).toBe(2);
  });

  it("ends an utterance that runs far past its length", () => {
    const s = make();
    s.speak("Short.");
    now = 60_000;
    vi.advanceTimersByTime(1_000);
    expect(finished).toBe(1);
  });

  it("uses the engine's default voice until the list arrives, then the phone's locale", () => {
    const s = make();
    s.speak("Before.");
    expect(synth.queue[0].voice).toBeNull();
    expect(synth.queue[0].lang).toBe("en-CA");
    synth.voicesChanged([voice("en-US"), voice("en-CA"), voice("fr-CA")]);
    s.speak("After.");
    expect(synth.queue[1].voice?.lang).toBe("en-CA");
  });

  it("speaks in the voice chosen in Settings once it is listed", () => {
    const s = make();
    s.setChosenVoice("daniel");
    synth.voicesChanged([voice("en-CA"), voice("en-GB", { name: "Daniel", voiceURI: "daniel" })]);
    s.speak("Hello.");
    expect(synth.queue[0].voice?.name).toBe("Daniel");
    expect(s.voiceName).toContain("Daniel");
    s.setChosenVoice(null);
    s.speak("Again.");
    expect(synth.queue[1].voice?.lang, "back to the ranking").toBe("en-CA");
  });
});

describe("pickVoice", () => {
  it("prefers the phone's own English, then a default English voice", () => {
    expect(pickVoice([voice("en-GB"), voice("en_US")], "en-US")?.lang).toBe("en_US");
    expect(pickVoice([voice("en-GB"), voice("en-AU", { default: true })], "en-NZ")?.lang).toBe("en-AU");
    expect(pickVoice([voice("de-DE")], "de-DE")).toBeNull();
    expect(pickVoice([], "en-US")).toBeNull();
  });

  it("takes a premium or enhanced voice over the basic one, as the native app does", () => {
    const basic = voice("en-US", { name: "Samantha", voiceURI: "com.apple.voice.compact.en-US.Samantha", default: true });
    const enhanced = voice("en-GB", { name: "Daniel (Enhanced)", voiceURI: "com.apple.voice.enhanced.en-GB.Daniel" });
    const premium = voice("en-US", { name: "Zoe (Premium)", voiceURI: "com.apple.voice.premium.en-US.Zoe" });
    expect(pickVoice([basic, enhanced], "en-US")?.name).toBe("Daniel (Enhanced)");
    expect(pickVoice([basic, enhanced, premium], "en-US")?.name).toBe("Zoe (Premium)");
    expect(pickVoice([voice("en-US", { name: "Albert" }), basic], "en-US")?.name, "the default among equals").toBe("Samantha");
  });
});

describe("pickVoice with a chosen voice", () => {
  const samantha = voice("en-US", { name: "Samantha", voiceURI: "samantha", default: true });
  const daniel = voice("en-GB", { name: "Daniel", voiceURI: "daniel" });

  it("takes the chosen voice over the ranking while it is still listed", () => {
    expect(pickVoice([samantha, daniel], "en-US", "daniel")?.name).toBe("Daniel");
  });

  it("never lands on a novelty voice, even where it comes first among equals", () => {
    const albert = voice("en-US", { name: "Albert", voiceURI: "albert" });
    expect(pickVoice([albert, voice("en-US", { name: "Fred", voiceURI: "fred" })], "en-US")?.name).toBe("Fred");
  });

  it("keys a voice the engine gives no voiceURI by its name", () => {
    const a = voice("en-US", { name: "Aaron", voiceURI: "" });
    const b = voice("en-US", { name: "Fred", voiceURI: "" });
    expect(choosableVoices([a, b])).toHaveLength(2);
    expect(pickVoice([a, b], "en-US", voiceKey(b))?.name).toBe("Fred");
  });

  it("falls back to the ranking when the chosen voice is gone", () => {
    expect(pickVoice([samantha, daniel], "en-US", "uninstalled")?.name).toBe("Samantha");
    expect(pickVoice([samantha, daniel], "en-US", null)?.name).toBe("Samantha");
  });
});

describe("choosableVoices", () => {
  // The en-US half of iOS Safari 18.7's list, as a debug report gave it
  // (2026-10-25), plus two other English voices and a French one.
  const ios = [
    "Samantha", "Albert", "Bad News", "Bahh", "Bells", "Boing", "Bubbles", "Cellos", "Wobble", "Fred",
    "Good News", "Jester", "Junior", "Kathy", "Organ", "Superstar", "Ralph", "Trinoids", "Whisper", "Zarvox",
  ].map((name) => voice("en-US", { name, voiceURI: `uri.${name}` }));
  const others = [
    voice("en-GB", { name: "Daniel", voiceURI: "uri.Daniel" }),
    voice("en-AU", { name: "Karen", voiceURI: "uri.Karen" }),
    voice("fr-CA", { name: "Amélie", voiceURI: "uri.Amelie" }),
  ];

  it("lists English voices without the novelty ones, best quality first, then by name", () => {
    const enhanced = voice("en-US", { name: "Ava (Enhanced)", voiceURI: "uri.Ava" });
    expect(choosableVoices([...ios, ...others, enhanced]).map((v) => v.name)).toEqual([
      "Ava (Enhanced)", "Daniel", "Fred", "Junior", "Karen", "Kathy", "Ralph", "Samantha",
    ]);
  });

  it("lists a voice the engine names twice only once", () => {
    expect(choosableVoices([others[0], others[0]])).toHaveLength(1);
  });
});

describe("previewVoice", () => {
  it("cuts off any preview still playing and says a line in that voice", () => {
    const daniel = voice("en-GB", { name: "Daniel", voiceURI: "uri.Daniel" });
    synth.speak(new FakeUtterance("earlier preview"));
    previewVoice(
      {
        synth: synth as unknown as BrowserSpeechDeps["synth"],
        makeUtterance: (t) => new FakeUtterance(t) as unknown as SpeechSynthesisUtterance,
      },
      daniel,
    );
    expect(synth.queue).toHaveLength(1);
    expect(synth.queue[0].voice?.name).toBe("Daniel");
    expect(synth.queue[0].lang).toBe("en-GB");
    expect(synth.queue[0].text.length).toBeGreaterThan(0);
  });
});
