// "Read aloud" on a reply's long-press menu (docs/tasks/read-aloud.md). The
// cases from iOS `MessageReaderTests.swift`, plus the web's own: the unlock
// inside the tap, and the stored voice applied before the first sentence.
import { describe, expect, it } from "vitest";
import { MessageReader, type ReaderSpeech } from "./messageReader";

class FakeSpeech implements ReaderSpeech {
  onUtteranceFinished: (() => void) | null = null;
  spoken: string[] = [];
  stops = 0;
  pending = 0;
  unlocks = 0;
  closes = 0;
  speak(text: string) {
    this.spoken.push(text);
    this.pending += 1;
  }
  stopSpeaking() {
    this.stops += 1;
    this.pending = 0;
  }
  unlock() {
    this.unlocks += 1;
  }
  close() {
    this.closes += 1;
  }
  finishOne() {
    if (this.pending === 0) return;
    this.pending -= 1;
    this.onUtteranceFinished?.();
  }
}

function make(stored: string | null = null) {
  const speeches: FakeSpeech[] = [];
  const configured: (string | null)[] = [];
  const reader = new MessageReader({
    makeSpeech: () => {
      const s = new FakeSpeech();
      speeches.push(s);
      return s;
    },
    storedVoice: () => Promise.resolve(stored),
    configure: (_speech, voice) => configured.push(voice),
  });
  return { reader, speeches, configured };
}

const tick = () => new Promise((r) => setTimeout(r, 0));

describe("MessageReader", () => {
  it("reads sentence by sentence as plain speech, with code kept out", async () => {
    const { reader, speeches } = make();
    reader.read("a", "**Rain** at three. Bring a [coat](https://x.test).\n\n```\nls -la\n```");
    expect(reader.reading).toBe("a");
    await tick();
    const spoken = speeches[0].spoken.map((s) => s.trim());
    expect(spoken[0]).toBe("Rain at three.");
    expect(spoken).toContain("Bring a coat.");
    expect(spoken.join(" ")).not.toContain("ls -la");
    expect(spoken.join(" ")).not.toContain("*");
  });

  it("unlocks inside the tap, before waiting on the stored voice", () => {
    const { reader, speeches } = make();
    reader.read("a", "Hello.");
    expect(speeches[0].unlocks).toBe(1);
    expect(speeches[0].spoken).toEqual([]);
  });

  it("applies the voice picked in Settings before the first sentence", async () => {
    const { reader, configured, speeches } = make("phonon:Hugh");
    reader.read("a", "Hello.");
    await tick();
    expect(configured).toEqual(["phonon:Hugh"]);
    expect(speeches[0].spoken).toEqual(["Hello."]);
  });

  it("ends the read when the last sentence finishes, and tells subscribers", async () => {
    const { reader, speeches } = make();
    const seen: (string | null)[] = [];
    reader.subscribe(() => seen.push(reader.reading));
    reader.read("a", "One. Two.");
    await tick();
    const speech = speeches[0];
    const count = speech.spoken.length;
    expect(count).toBeGreaterThan(1);
    for (let i = 0; i < count - 1; i++) speech.finishOne();
    expect(reader.reading).toBe("a");
    speech.finishOne();
    expect(reader.reading).toBeNull();
    expect(seen).toEqual(["a", null]);
  });

  it("stop cuts it off", async () => {
    const { reader, speeches } = make();
    reader.read("a", "One. Two.");
    await tick();
    reader.stop();
    expect(reader.reading).toBeNull();
    expect(speeches[0].stops).toBe(1);
  });

  it("a stop before the stored voice arrives says nothing", async () => {
    const { reader, speeches } = make();
    reader.read("a", "One. Two.");
    reader.stop();
    await tick();
    expect(speeches[0].spoken).toEqual([]);
  });

  it("reading another message replaces the first, on the same speech", async () => {
    const { reader, speeches } = make();
    reader.read("a", "First reply.");
    await tick();
    reader.read("b", "Second reply.");
    await tick();
    expect(reader.reading).toBe("b");
    expect(speeches).toHaveLength(1);
    expect(speeches[0].stops).toBe(1);
    expect(speeches[0].spoken.at(-1)).toBe("Second reply.");
    speeches[0].finishOne();
    expect(reader.reading).toBeNull();
  });

  it("nothing to say reads nothing", () => {
    const { reader, speeches } = make();
    reader.read("a", "  **  ");
    expect(reader.reading).toBeNull();
    expect(speeches).toHaveLength(0);
  });

  it("close stops and frees the speech", async () => {
    const { reader, speeches } = make();
    reader.read("a", "Hello.");
    await tick();
    reader.close();
    expect(reader.reading).toBeNull();
    expect(speeches[0].closes).toBe(1);
  });
});
