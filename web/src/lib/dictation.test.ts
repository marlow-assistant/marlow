import { describe, expect, it } from "vitest";
import { HttpError, TransportError } from "./api";
import { PartialTranscriptError, SegmentUploader, transcribeErrorText } from "./dictation";
import { VoiceDebugLog } from "./voiceDebug";
import { DEFAULT_CUTTER, PauseCutter, rms } from "./pauseCutter";
import { Microphone, SegmentRecorder, type LevelSource } from "./segmentRecorder";

// --- the pause cutter ------------------------------------------------------------

/** Feeds `levels` at 50 ms a step from t=0; the times (ms) it cut at. */
function cuts(levels: number[], cutter = new PauseCutter()): number[] {
  const at: number[] = [];
  cutter.reset(0);
  levels.forEach((level, i) => {
    const now = i * 50;
    if (cutter.sample(level, now)) {
      at.push(now);
      cutter.reset(now);
    }
  });
  return at;
}

const SPEECH = 0.2;
const ROOM = 0.002;
const seconds = (s: number, level: number) => Array<number>(Math.round((s * 1000) / 50)).fill(level);

describe("PauseCutter", () => {
  it("never cuts a short utterance, pauses and all", () => {
    expect(cuts([...seconds(1, ROOM), ...seconds(4, SPEECH), ...seconds(2, ROOM), ...seconds(4, SPEECH)])).toEqual([]);
  });

  it("cuts at the first half-second pause after twelve seconds", () => {
    const levels = [...seconds(1, ROOM), ...seconds(13, SPEECH), ...seconds(0.3, ROOM), ...seconds(1, SPEECH), ...seconds(1, ROOM)];
    const [at] = cuts(levels);
    // The 0.3 s gap at 14 s is too short; the pause from 15.3 s is cut 0.5 s in.
    expect(at).toBeGreaterThanOrEqual(15_300 + 500);
    expect(at).toBeLessThan(15_300 + 600);
  });

  it("takes a word gap in the last three seconds before the hard cut", () => {
    const levels = [...seconds(0.5, ROOM), ...seconds(22, SPEECH), ...seconds(0.2, ROOM), ...seconds(5, SPEECH)];
    const [at] = cuts(levels);
    expect(at).toBeGreaterThanOrEqual(22_650);
    expect(at).toBeLessThan(22_800);
  });

  it("cuts at twenty-five seconds of unbroken speech", () => {
    expect(cuts([...seconds(0.5, ROOM), ...seconds(60, SPEECH)])).toEqual([25_000, 50_000]);
  });

  it("still cuts at twenty-five seconds when speech starts before the first reading", () => {
    // The floor is then speech itself, and the pauses are lost: the hard cut
    // is what keeps the server's limit.
    expect(cuts(seconds(30, SPEECH))[0]).toBeLessThanOrEqual(25_000);
  });

  it("learns a noisy room, so its hum is not taken for speech", () => {
    const hum = 0.05;
    const levels = [...seconds(14, hum), ...seconds(1, hum * 6), ...seconds(1, hum)];
    const cutter = new PauseCutter();
    const at = cuts(levels, cutter);
    expect(cutter.threshold).toBeGreaterThan(hum);
    expect(at.length).toBe(1);
  });

  it("takes an all-zero reading from a context not yet rendering as no reading, not as the floor", () => {
    const car = 0.03;
    const levels = [0, 0, ...seconds(0.5, car), ...seconds(13, car * 8), ...seconds(1, car)];
    const [at] = cuts(levels);
    expect(at).toBeGreaterThan(13_000);
    expect(at).toBeLessThan(15_000);
  });

  it("keeps every segment under the server's 35 s refusal", () => {
    expect(DEFAULT_CUTTER.hardCutMs).toBeLessThan(30_000);
  });

  it("says why it cut", () => {
    const pause = new PauseCutter();
    cuts([...seconds(0.5, ROOM), ...seconds(13, SPEECH), ...seconds(1, ROOM)], pause);
    expect(pause.lastReason).toBe("pause");
    const hard = new PauseCutter();
    cuts([...seconds(0.5, ROOM), ...seconds(30, SPEECH)], hard);
    expect(hard.lastReason).toBe("hard");
  });

  it("measures RMS", () => {
    expect(rms(new Float32Array([0.5, -0.5, 0.5, -0.5]))).toBeCloseTo(0.5);
    expect(rms(new Float32Array(0))).toBe(0);
  });
});

// --- the recorder: one MediaRecorder per segment -----------------------------------

class FakeMediaRecorder extends EventTarget {
  state: "inactive" | "recording" = "inactive";
  constructor(readonly label: string) {
    super();
  }
  start() {
    this.state = "recording";
  }
  stop() {
    if (this.state === "inactive") return;
    this.state = "inactive";
    const data = new Blob([this.label], { type: "audio/webm" });
    queueMicrotask(() => {
      this.dispatchEvent(Object.assign(new Event("dataavailable"), { data }));
      this.dispatchEvent(new Event("stop"));
    });
  }
}

function harness(levels: number[]) {
  let now = 0;
  let tick: (() => void) | null = null;
  const recorders: FakeMediaRecorder[] = [];
  const blobs: Blob[] = [];
  let closed = false;
  const source: LevelSource = { level: () => levels[Math.min(now / 50, levels.length - 1)], close: () => (closed = true) };
  const rec = new SegmentRecorder(
    {} as MediaStream,
    "audio/webm",
    (blob) => blobs.push(blob),
    () => {},
    {
      createRecorder: () => {
        const r = new FakeMediaRecorder(`seg${recorders.length}`);
        recorders.push(r);
        return r as unknown as MediaRecorder;
      },
      createLevelSource: () => source,
      now: () => now,
      setInterval: (fn) => {
        tick = fn;
        return 1 as unknown as ReturnType<typeof setInterval>;
      },
      clearInterval: () => (tick = null),
    },
  );
  return {
    rec,
    recorders,
    segments: () => Promise.all(blobs.map((b) => b.text())),
    closed: () => closed,
    run: () => {
      for (let i = 1; i < levels.length && tick; i++) {
        now = i * 50;
        tick();
      }
    },
  };
}

const settle = () => new Promise((r) => setTimeout(r, 0));

describe("SegmentRecorder", () => {
  it("stops one recorder and starts the next at each cut, handing segments on in order", async () => {
    const h = harness([...seconds(0.5, ROOM), ...seconds(59.5, SPEECH)]);
    h.rec.start();
    h.run();
    const last = await h.rec.stop();
    await settle();
    expect(h.recorders.map((r) => r.label)).toEqual(["seg0", "seg1", "seg2"]);
    expect(await h.segments()).toEqual(["seg0", "seg1"]);
    expect(await last.text()).toBe("seg2");
    expect(h.recorders.every((r) => r.state === "inactive")).toBe(true);
    expect(h.closed()).toBe(true);
  });

  it("hands on nothing more once cancelled", async () => {
    const h = harness(seconds(30, SPEECH));
    h.rec.start();
    h.run();
    h.rec.cancel();
    await settle();
    expect(await h.segments()).toEqual([]);
    expect(h.recorders.every((r) => r.state === "inactive")).toBe(true);
  });
});

// --- the uploader ----------------------------------------------------------------

function fakeApi(fail: (seq: number, attempt: number) => unknown = () => null) {
  const calls: { seq: number; final: boolean; body: string }[] = [];
  const texts: string[] = [];
  const attempts = new Map<number, number>();
  return {
    calls,
    api: {
      async transcribeSegment(q: { utteranceId: string; seq: number; final: boolean }, audio: Blob) {
        const attempt = (attempts.get(q.seq) ?? 0) + 1;
        attempts.set(q.seq, attempt);
        const body = await audio.text();
        calls.push({ seq: q.seq, final: q.final, body });
        const err = fail(q.seq, attempt);
        if (err) throw err;
        texts[q.seq] = body;
        return { text: body, transcript: texts.filter(Boolean).join(" "), serverSeconds: 0.25 };
      },
    },
  };
}

const blob = (s: string) => new Blob([s], { type: "audio/webm" });
const noSleep = () => Promise.resolve();

describe("SegmentUploader", () => {
  it("uploads segments in order and returns the final transcript", async () => {
    const f = fakeApi();
    const up = new SegmentUploader(f.api, new AbortController().signal, () => "utt-1", noSleep);
    up.add(blob("one"));
    up.add(blob("two"));
    expect(await up.finish(blob("three"))).toBe("one two three");
    expect(f.calls.map((c) => [c.seq, c.final])).toEqual([[0, false], [1, false], [2, true]]);
  });

  it("retries a dropped connection, but not a refusal", async () => {
    const f = fakeApi((seq, attempt) => (seq === 0 && attempt === 1 ? new TransportError("offline") : null));
    const up = new SegmentUploader(f.api, new AbortController().signal, () => "utt-2", noSleep);
    expect(await up.finish(blob("hi"))).toBe("hi");
    expect(f.calls.length).toBe(2);

    const g = fakeApi(() => new HttpError(413, "too long"));
    const up2 = new SegmentUploader(g.api, new AbortController().signal, () => "utt-3", noSleep);
    await expect(up2.finish(blob("hi"))).rejects.toBeInstanceOf(HttpError);
    expect(g.calls.length).toBe(1);
  });

  it("does not retry a box without the model, or the engine failing on this audio", async () => {
    for (const status of [500, 503]) {
      const f = fakeApi(() => new HttpError(status, null));
      const up = new SegmentUploader(f.api, new AbortController().signal, () => "utt-7", noSleep);
      await expect(up.finish(blob("hi"))).rejects.toBeInstanceOf(HttpError);
      expect(f.calls.length, String(status)).toBe(1);
    }
    const g = fakeApi((_, attempt) => (attempt === 1 ? new HttpError(502, null) : null));
    expect(await new SegmentUploader(g.api, new AbortController().signal, () => "utt-8", noSleep).finish(blob("hi"))).toBe("hi");
  });

  it("hands back what was heard before a later segment failed", async () => {
    const f = fakeApi((seq) => (seq === 1 ? new HttpError(413, "too long") : null));
    const up = new SegmentUploader(f.api, new AbortController().signal, () => "utt-9", noSleep);
    up.add(blob("one"));
    up.add(blob("two"));
    const err = await up.finish(blob("three")).catch((e) => e);
    expect(err).toBeInstanceOf(PartialTranscriptError);
    expect((err as PartialTranscriptError).heard).toBe("one");
    expect(transcribeErrorText(err)).toMatch(/Part of that/);
  });

  it("reports a failed early segment on finish, without sending the rest", async () => {
    const f = fakeApi((seq) => (seq === 0 ? new HttpError(400, "bad") : null));
    const up = new SegmentUploader(f.api, new AbortController().signal, () => "utt-4", noSleep);
    up.add(blob("one"));
    up.add(blob("two"));
    await expect(up.finish(blob("three"))).rejects.toBeInstanceOf(HttpError);
    expect(f.calls.map((c) => c.seq)).toEqual([0]);
  });

  it("marks the utterance finished with the last real segment when the final one is empty", async () => {
    const f = fakeApi();
    const up = new SegmentUploader(f.api, new AbortController().signal, () => "utt-5", noSleep);
    up.add(blob("one"));
    expect(await up.finish(blob(""))).toBe("one");
    expect(f.calls.map((c) => [c.seq, c.final])).toEqual([[0, false], [0, true]]);
    expect(await new SegmentUploader(f.api, new AbortController().signal, () => "utt-6", noSleep).finish(blob(""))).toBe("");
  });

  it("feeds the debug report each upload's size, round trip, box time and retries, never the words", async () => {
    let t = 0;
    const log = new VoiceDebugLog(() => (t += 100));
    const u = log.start();
    u.cut("pause", 13.2, 0.031);
    u.cut("release", 4.1, 0.031);
    const f = fakeApi((seq, attempt) => (seq === 1 && attempt === 1 ? new TransportError("x") : null));
    const up = new SegmentUploader(f.api, new AbortController().signal, () => "utt-d", noSleep, u);
    up.add(blob("first words"));
    u.released();
    u.transcribed(await up.finish(blob("then more")));
    u.sent();
    u.spoke();
    const report = log.format({ userAgent: "test-agent", build: "abc123" });
    expect(report).toContain("seg 0: 13.2 s, cut: pause, threshold 0.031");
    expect(report).toContain("box 250 ms");
    expect(report).toContain("1 retries");
    expect(report).toMatch(/release -> text: \d+ ms \(4 words\)/);
    expect(report).toContain("release -> first speech:");
    expect(report).not.toContain("first words");
    expect(report).not.toContain("then more");
  });

  it("keeps only the last few utterances", () => {
    const log = new VoiceDebugLog(() => 0, 2);
    for (let i = 0; i < 4; i++) log.start().failed(`n${i}`);
    const report = log.format({ userAgent: "a", build: "b" });
    expect(report).not.toContain("n1");
    expect(report).toContain("n2");
    expect(report).toContain("n3");
  });

  it("says what went wrong in words", () => {
    expect(transcribeErrorText(new HttpError(503, null))).toMatch(/isn't set up/);
    expect(transcribeErrorText(new TransportError("x"))).toBe("Couldn't transcribe that.");
  });
});

// --- the microphone --------------------------------------------------------------

describe("Microphone", () => {
  /** A getUserMedia whose answers are released by hand, and the tracks it made. */
  function fakeMedia() {
    const made: { stopped: boolean }[] = [];
    const pending: (() => void)[] = [];
    const getUserMedia = () =>
      new Promise<MediaStream>((resolve) => {
        const track = {
          readyState: "live",
          stopped: false,
          stop() {
            this.stopped = true;
            this.readyState = "ended";
          },
        };
        made.push(track);
        const stream = { getTracks: () => [track], getAudioTracks: () => [track] } as unknown as MediaStream;
        pending.push(() => resolve(stream));
      });
    return { getUserMedia, made, grant: () => pending.shift()!() };
  }

  it("stops a stream that arrives after it was closed", async () => {
    const media = fakeMedia();
    const mic = new Microphone(media.getUserMedia);
    const opened = mic.open();
    mic.close();
    media.grant();
    await expect(opened).rejects.toThrow();
    expect(media.made.map((t) => t.stopped)).toEqual([true]);
  });

  it("shares one stream between opens that overlap", async () => {
    const media = fakeMedia();
    const mic = new Microphone(media.getUserMedia);
    const first = mic.open();
    const second = mic.open();
    media.grant();
    expect(await first).toBe(await second);
    expect(media.made).toHaveLength(1);
    mic.close();
    expect(media.made[0].stopped).toBe(true);
  });

  it("opens afresh after a close", async () => {
    const media = fakeMedia();
    const mic = new Microphone(media.getUserMedia);
    const first = mic.open();
    media.grant();
    await first;
    mic.close();
    const again = mic.open();
    media.grant();
    await again;
    expect(media.made.map((t) => t.stopped)).toEqual([true, false]);
  });
});
