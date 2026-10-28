import { describe, expect, it } from "vitest";
import {
  isIOS,
  PHONON_VOICE,
  PhononSpeech,
  usesPhonon,
  type AudioOut,
  type LoadStatus,
  type PhononEngine,
  type SpeechFallback,
} from "./phononSpeech";

const CHUNK = 1920; // 80 ms at 24 kHz, what the package yields
const SECONDS = CHUNK / 24_000;
const tick = () => new Promise<void>((r) => setTimeout(r, 0));

class FakeSource {
  buffer: { duration: number } | null = null;
  startedAt: number | null = null;
  stopped = false;
  onended: (() => void) | null = null;
  connect() {}
  start(at: number) {
    this.startedAt = at;
  }
  /** WebAudio fires `ended` for a stopped source too, which the guard must expect. */
  stop() {
    this.stopped = true;
    this.onended?.();
  }
  /** It played to its end. */
  finish() {
    this.onended?.();
  }
}

class FakeContext {
  currentTime = 0;
  state = "running";
  sources: FakeSource[] = [];
  destination = {};
  resume = async () => {};
  createBuffer(_channels: number, length: number, rate: number) {
    return { duration: length / rate, copyToChannel() {} };
  }
  createBufferSource() {
    const s = new FakeSource();
    this.sources.push(s);
    return s;
  }
}

/** One `stream()` call, fed by hand. */
class Req {
  private items: (Float32Array | Error | "end")[] = [];
  private wake: (() => void) | null = null;
  constructor(
    readonly text: string,
    readonly signal: AbortSignal,
    /** Keeps delivering after an abort: a chunk already in flight when the stop landed. */
    private readonly deaf: boolean,
  ) {
    signal.addEventListener("abort", () => this.wake?.());
  }
  push() {
    this.items.push(new Float32Array(CHUNK));
    this.wake?.();
  }
  end() {
    this.items.push("end");
    this.wake?.();
  }
  fail(e: Error) {
    this.items.push(e);
    this.wake?.();
  }
  async *[Symbol.asyncIterator]() {
    for (;;) {
      if (this.signal.aborted && !this.deaf) return;
      const item = this.items.shift();
      if (item === undefined) {
        await new Promise<void>((r) => (this.wake = r));
        continue;
      }
      if (item === "end") return;
      if (item instanceof Error) throw item;
      yield item;
    }
  }
}

class FakeEngine implements PhononEngine {
  sampleRate = 24_000;
  device = "cpu";
  threads = 1;
  reqs: Req[] = [];
  deaf = false;
  stream(text: string, { signal }: { signal: AbortSignal }) {
    const r = new Req(text, signal, this.deaf);
    this.reqs.push(r);
    return r;
  }
}

class FakeFallback implements SpeechFallback {
  onUtteranceFinished: (() => void) | null = null;
  onSpeak: (() => void) | null = null;
  voiceName = "Samantha (en-US, on device)";
  englishVoices = "Samantha (en-US)";
  spoken: string[] = [];
  stops = 0;
  unlocks = 0;
  speak(text: string) {
    this.spoken.push(text);
  }
  stopSpeaking() {
    this.stops++;
  }
  unlock() {
    this.unlocks++;
  }
  close() {}
  /** The oldest utterance it was given plays to its end. */
  finishOne() {
    this.onUtteranceFinished?.();
  }
}

function setup({ start = true } = {}) {
  const ctx = new FakeContext();
  const engine = new FakeEngine();
  const fallback = new FakeFallback();
  const clock = { t: 0 };
  let loads = 0;
  let contexts = 0;
  let resolveLoad!: (e: PhononEngine) => void;
  let rejectLoad!: (e: Error) => void;
  let progress!: (fraction: number | null) => void;
  const speech = new PhononSpeech({
    fallback,
    load: (onProgress) => {
      progress = onProgress;
      loads++;
      return new Promise<PhononEngine>((res, rej) => {
        resolveLoad = res;
        rejectLoad = rej;
      });
    },
    makeContext: () => {
      contexts++;
      return ctx as unknown as AudioOut;
    },
    now: () => clock.t,
  });
  const finished: number[] = [];
  speech.onUtteranceFinished = () => finished.push(finished.length + 1);
  const statuses: LoadStatus[] = [];
  speech.onLoad = (s) => statuses.push(s);
  if (start) speech.start();
  return {
    speech,
    ctx,
    engine,
    fallback,
    clock,
    finished,
    statuses,
    loaded: async () => {
      resolveLoad(engine);
      await tick();
    },
    failed: async (why: string) => {
      rejectLoad(new Error(why));
      await tick();
    },
    progress: (p: number | null) => progress(p),
    loads: () => loads,
    contexts: () => contexts,
  };
}

describe("PhononSpeech playback", () => {
  it("synthesizes utterances in order and plays them back to back", async () => {
    const t = setup();
    await t.loaded();
    t.speech.speak("one");
    t.speech.speak("two");
    await tick();
    // The second waits behind the first's synthesis, not behind its playback.
    expect(t.engine.reqs.map((r) => r.text)).toEqual(["one"]);

    t.engine.reqs[0].push();
    t.engine.reqs[0].push();
    await tick();
    expect(t.ctx.sources.map((s) => s.startedAt)).toEqual([0, expect.closeTo(SECONDS, 6)]);

    t.engine.reqs[0].end();
    await tick();
    // "two" is being made while "one" is still playing: none of its sources has ended.
    expect(t.engine.reqs.map((r) => r.text)).toEqual(["one", "two"]);
    t.engine.reqs[1].push();
    await tick();
    expect(t.ctx.sources[2].startedAt).toBeCloseTo(2 * SECONDS, 6);

    // A clock that has run past the schedule (synthesis slower than speech) starts at now.
    t.ctx.currentTime = 5;
    t.engine.reqs[1].push();
    await tick();
    expect(t.ctx.sources[3].startedAt).toBe(5);
  });

  it("finishes an utterance once, after its last chunk has played, and says it spoke once", async () => {
    const t = setup();
    let spoke = 0;
    t.speech.onSpeak = () => spoke++;
    await t.loaded();
    t.speech.speak("one");
    await tick();
    const [req] = t.engine.reqs;
    req.push();
    req.push();
    await tick();
    expect(spoke).toBe(1);

    // Everything scheduled so far has played but the stream is still going: not finished.
    t.ctx.sources[0].finish();
    t.ctx.sources[1].finish();
    expect(t.finished).toEqual([]);

    req.push();
    req.end();
    await tick();
    t.ctx.sources[2].finish();
    expect(t.finished).toEqual([1]);
  });

  it("an utterance with no audio still finishes", async () => {
    const t = setup();
    await t.loaded();
    t.speech.speak("...");
    await tick();
    t.engine.reqs[0].end();
    await tick();
    expect(t.finished).toEqual([1]);
  });

  it("reports generation time against audio length", async () => {
    const t = setup();
    const seen: [number, number][] = [];
    t.speech.onSynthesized = (ms, s) => seen.push([ms, s]);
    await t.loaded();
    t.clock.t = 1000;
    t.speech.speak("one");
    await tick();
    t.engine.reqs[0].push();
    t.engine.reqs[0].push();
    t.clock.t = 1300;
    t.engine.reqs[0].end();
    await tick();
    expect(seen).toEqual([[300, expect.closeTo(2 * SECONDS, 6)]]);
  });
});

describe("PhononSpeech stopSpeaking", () => {
  it("aborts the stream, stops the sources and finishes nothing, even for a chunk that arrives later", async () => {
    const t = setup();
    t.engine.deaf = true;
    await t.loaded();
    t.speech.speak("one");
    t.speech.speak("two");
    await tick();
    const [req] = t.engine.reqs;
    req.push();
    await tick();

    t.speech.stopSpeaking();
    expect(req.signal.aborted).toBe(true);
    expect(t.ctx.sources[0].stopped).toBe(true);
    expect(t.fallback.stops).toBe(1);

    // A chunk in flight when the stop landed, then the stream ending.
    req.push();
    req.end();
    await tick();
    expect(t.ctx.sources).toHaveLength(1);
    expect(t.engine.reqs).toHaveLength(1); // "two" was dropped with the queue
    expect(t.finished).toEqual([]);
  });

  it("speaks the next reply normally afterwards", async () => {
    const t = setup();
    await t.loaded();
    t.speech.speak("one");
    await tick();
    t.engine.reqs[0].push();
    await tick();
    t.speech.stopSpeaking();
    await tick();

    t.speech.speak("fresh");
    await tick();
    const req = t.engine.reqs[1];
    expect(req.text).toBe("fresh");
    req.push();
    req.end();
    await tick();
    // The stale source's ended event from the stop counted for nothing; this one does.
    expect(t.finished).toEqual([]);
    t.ctx.sources[1].finish();
    expect(t.finished).toEqual([1]);
  });
});

describe("PhononSpeech fallback", () => {
  it("uses the browser's voice while Phonon loads, then Phonon from the next utterance", async () => {
    const t = setup();
    t.speech.speak("early");
    await tick();
    expect(t.fallback.spoken).toEqual(["early"]);
    expect(t.engine.reqs).toHaveLength(0);

    await t.loaded();
    t.speech.speak("later");
    await tick();
    // Phonon doesn't talk over the browser: it waits for the browser utterance to end.
    expect(t.engine.reqs).toHaveLength(0);
    t.fallback.finishOne();
    await tick();
    expect(t.finished).toEqual([1]);
    expect(t.engine.reqs.map((r) => r.text)).toEqual(["later"]);
    expect(t.fallback.spoken).toEqual(["early"]);
  });

  it("keeps using the browser's voice after a failed load", async () => {
    const t = setup();
    await t.failed("this browser cannot run phonon-tts: it needs WebAssembly SIMD");
    t.speech.speak("hello");
    await tick();
    expect(t.fallback.spoken).toEqual(["hello"]);
    t.fallback.finishOne();
    expect(t.finished).toEqual([1]);
    expect(t.statuses.at(-1)).toEqual({ state: "failed", reason: "this browser cannot run phonon-tts: it needs WebAssembly SIMD" });
  });

  it("falls back for an utterance whose stream failed before any sound", async () => {
    const t = setup();
    await t.loaded();
    t.speech.speak("hello");
    await tick();
    t.engine.reqs[0].fail(new Error("worker died"));
    await tick();
    expect(t.fallback.spoken).toEqual(["hello"]);
    t.fallback.finishOne();
    expect(t.finished).toEqual([1]);
  });

  it("unlocks audio and the browser's voice from one tap", () => {
    const t = setup();
    t.speech.unlock();
    expect(t.fallback.unlocks).toBe(1);
  });

  it("makes the audio context in a tap that comes before the voice choice has loaded", () => {
    // The tap is iOS's only chance to unlock it; one made later stays silent.
    const t = setup({ start: false });
    t.speech.unlock();
    expect(t.contexts()).toBe(1);
  });
});

describe("PhononSpeech status", () => {
  it("says what is in use, for the debug report and the screen", async () => {
    const t = setup();
    expect(t.speech.voiceName).toBe("Samantha (en-US, on device) (Phonon not in use: loading)");
    t.progress(0.4);
    expect(t.speech.voiceName).toBe("Samantha (en-US, on device) (Phonon not in use: loading 40%)");
    await t.loaded();
    expect(t.speech.voiceName).toBe("Phonon (cpu, 1 thread)");
    expect(t.statuses).toEqual([{ state: "loading", fraction: null }, { state: "loading", fraction: 0.4 }, { state: "ready" }]);
  });

  it("says why Phonon isn't in use after a failure", async () => {
    const t = setup();
    await t.failed("offline");
    expect(t.speech.voiceName).toBe("Samantha (en-US, on device) (Phonon not in use: failed: offline)");
  });
});

describe("PhononSpeech when the choice is a device voice", () => {
  it("never loads Phonon and speaks through the browser at once", async () => {
    const t = setup({ start: false });
    t.speech.speak("hello");
    expect(t.fallback.spoken).toEqual(["hello"]);
    expect(t.loads()).toBe(0);
    t.fallback.finishOne();
    expect(t.finished).toEqual([1]);
    expect(t.speech.voiceName).toBe("Samantha (en-US, on device)");
  });
});

const nav = (userAgent: string, platform = "", maxTouchPoints = 0) => ({ userAgent, platform, maxTouchPoints });
const IPHONE = nav("Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 Version/18.7 Mobile/15E148 Safari/604.1");
const IPAD_AS_MAC = nav("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/18.7 Safari/605.1.15", "MacIntel", 5);
const MAC = nav("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/18.7 Safari/605.1.15", "MacIntel", 0);
const ANDROID = nav("Mozilla/5.0 (Linux; Android 16; Pixel 10) AppleWebKit/537.36 Chrome/149.0 Mobile Safari/537.36", "Linux armv81", 5);

describe("usesPhonon", () => {
  it("is the default on iOS and only there", () => {
    expect(usesPhonon(null, true)).toBe(true);
    expect(usesPhonon(null, false)).toBe(false);
  });

  it("follows an explicit choice anywhere", () => {
    expect(usesPhonon(PHONON_VOICE, false)).toBe(true);
    expect(usesPhonon(PHONON_VOICE, true)).toBe(true);
    expect(usesPhonon("com.apple.voice.compact.en-GB.Daniel", true)).toBe(false);
    expect(usesPhonon("com.apple.voice.compact.en-GB.Daniel", false)).toBe(false);
  });
});

describe("isIOS", () => {
  it("knows an iPhone, and an iPad that says it is a Mac", () => {
    expect(isIOS(IPHONE)).toBe(true);
    expect(isIOS(IPAD_AS_MAC)).toBe(true);
  });

  it("leaves a Mac without a touch screen and Android out", () => {
    expect(isIOS(MAC)).toBe(false);
    expect(isIOS(ANDROID)).toBe(false);
  });
});
