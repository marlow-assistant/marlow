// The cases from iOS `VoiceModeTests.swift` ("Sentence chunker", "Markdown to
// speech"), carried over one for one.
import { describe, expect, it } from "vitest";
import { CodeFenceFilter, plainSpeech, SentenceChunker } from "./spokenReply";

describe("SentenceChunker", () => {
  it("holds a sentence still being written", () => {
    expect(new SentenceChunker().ingest("You have three meet")).toEqual([]);
  });

  it("speaks a sentence followed by more text at once", () => {
    expect(new SentenceChunker().ingest("You have three meetings. The first")).toEqual(["You have three meetings."]);
  });

  it("speaks a terminated sentence when the poll after it brings nothing new", () => {
    // The 1–2.5s first-sentence delay the device spike measured: a lone
    // sentence with nothing after it waited for the turn to end.
    const chunker = new SentenceChunker();
    expect(chunker.ingest("Yes, it's at noon."), "could still be '...noon.5' or similar").toEqual([]);
    expect(chunker.ingest("Yes, it's at noon.")).toEqual(["Yes, it's at noon."]);
  });

  it("holds an unterminated sentence even when stable", () => {
    const chunker = new SentenceChunker();
    chunker.ingest("Checking your");
    expect(chunker.ingest("Checking your")).toEqual([]);
  });

  it("speaks text before a tool call without punctuation", () => {
    const text = "Let me check your calendar";
    expect(new SentenceChunker().ingest(text + "You have", [Array.from(text).length])).toEqual([text]);
  });

  it("counts tool-call offsets in code points, not UTF-16 units", () => {
    // An emoji is one scalar on the server and two units in JavaScript.
    const text = "Found it 📅";
    expect(new SentenceChunker().ingest(text + "Next", [Array.from(text).length])).toEqual([text]);
  });

  it("speaks nothing twice", () => {
    const chunker = new SentenceChunker();
    const first = chunker.ingest("One. Two");
    const second = chunker.ingest("One. Two. Three");
    const last = chunker.finish("One. Two. Three.");
    expect([...first, ...second, ...last]).toEqual(["One.", "Two.", "Three."]);
  });

  it("finish speaks the tail from the authoritative text", () => {
    const chunker = new SentenceChunker();
    chunker.ingest("Here's the plan. First");
    expect(chunker.finish("Here's the plan. First, pack.")).toEqual(["First, pack."]);
  });

  it("finish picks up after what was spoken when the result differs", () => {
    // `result_text` is reassembled server-side and needn't extend the
    // streamed text byte for byte; carry on after the last words spoken.
    const chunker = new SentenceChunker();
    chunker.ingest("I checked the calendar for tomorrow. More");
    expect(chunker.finish("Okay. I checked the calendar for tomorrow. You're free.")).toEqual(["You're free."]);
  });

  it("finish says nothing rather than reread when it can't find its place", () => {
    const chunker = new SentenceChunker();
    chunker.ingest("Something entirely different. ");
    expect(chunker.finish("No overlap at all.")).toEqual([]);
  });

  it("keeps a decimal inside its sentence", () => {
    expect(new SentenceChunker().ingest("It's 3.5 km away. Then")).toEqual(["It's 3.5 km away."]);
  });
});

describe("plainSpeech", () => {
  it("drops emphasis and headings", () => {
    expect(plainSpeech("## Today\n**Three** meetings, *one* moved.")).toBe("Today\nThree meetings, one moved.");
  });

  it("drops list markers but keeps their text", () => {
    expect(plainSpeech("- milk\n2. eggs")).toBe("milk\neggs");
  });

  it("reads links as their text", () => {
    expect(plainSpeech("See [the forecast](https://example.com/x).")).toBe("See the forecast.");
    expect(plainSpeech("At https://example.com/x today")).toBe("At a link today");
  });

  it("does not read out a code block spanning sentences", () => {
    const fences = new CodeFenceFilter();
    const spoken = ["Run this. ```", "rm -rf build. ", "echo done. ", "``` Then reboot."]
      .map((s) => plainSpeech(fences.filter(s)))
      .join(" ");
    expect(spoken).not.toContain("rm");
    expect(spoken).not.toContain("echo");
    expect(spoken).toContain("The code is in the chat.");
    expect(spoken, "text after the closing fence is kept").toContain("Then reboot.");
  });

  it("makes a markdown-only fragment empty", () => {
    expect(plainSpeech("---")).toBe("");
  });
});
