// The cases from iOS `VoiceModeTests.swift` ("Voice mode button", "Earcons"),
// carried over one for one, plus the web's own.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EARCON_RATE, earconSamples, type Earcon } from "./earcons";
import type { ChatMessage } from "./types";
import { VoiceModeController, type SpeechOutput, type VoiceRecorder } from "./voiceMode";

class FakeRecorder implements VoiceRecorder {
  begins = 0;
  finishes = 0;
  cancels = 0;
  private onResult: ((text: string | null) => void) | null = null;

  begin(onResult: (text: string | null) => void) {
    this.begins += 1;
    this.onResult = onResult;
  }
  finish() {
    this.finishes += 1;
  }
  cancel() {
    this.cancels += 1;
  }
  /** Unlike iOS's fake, a cancel doesn't drop the callback: the controller
   * itself must ignore a result that arrives after one. */
  deliver(text: string | null) {
    const report = this.onResult;
    this.onResult = null;
    report?.(text);
  }
}

class FakeSpeech implements SpeechOutput {
  onUtteranceFinished: (() => void) | null = null;
  spoken: string[] = [];
  stops = 0;
  pending = 0;
  speak(text: string) {
    this.spoken.push(text);
    this.pending += 1;
  }
  stopSpeaking() {
    this.stops += 1;
    this.pending = 0;
  }
  finishOne() {
    if (this.pending === 0) return;
    this.pending -= 1;
    this.onUtteranceFinished?.();
  }
}

const REPLY = "reply-1";
const HOUR = 3_600_000;

let recorder: FakeRecorder;
let speech: FakeSpeech;
let texts: string[];
let earcons: Earcon[];

beforeEach(() => {
  vi.useFakeTimers();
  recorder = new FakeRecorder();
  speech = new FakeSpeech();
  texts = [];
  earcons = [];
});
afterEach(() => vi.useRealTimers());

function make(opts: { first?: number; interval?: number; approval?: number; sendResult?: string | null } = {}) {
  return new VoiceModeController({
    recorder,
    speech,
    send: (text) => {
      texts.push(text);
      return opts.sendResult === undefined ? REPLY : opts.sendResult;
    },
    earcon: (k) => earcons.push(k),
    firstWorkingTickMs: opts.first ?? HOUR,
    workingTickIntervalMs: opts.interval ?? HOUR,
    approvalReminderMs: opts.approval ?? 10_000,
  });
}

/** Talk, send, and get as far as waiting on Marlow. */
function talk(c: VoiceModeController, text = "what's on today") {
  c.press();
  c.press();
  recorder.deliver(text);
}

const message = (status: ChatMessage["status"], text = ""): ChatMessage => ({
  id: REPLY,
  role: "assistant",
  text,
  createdAt: 0,
  status,
  turnId: "t1",
});

describe("VoiceModeController", () => {
  it("makes the first working sound soon after sending, not a full interval later", () => {
    const c = make({ first: 20 });
    talk(c);
    vi.advanceTimersByTime(25);
    expect(earcons).toEqual(["listening", "sent", "working"]);
  });

  it("talks on a press and sends on the next", () => {
    const c = make();
    c.press();
    expect(c.phase).toBe("recording");
    expect(recorder.begins).toBe(1);

    c.press();
    expect(c.phase).toBe("transcribing");
    expect(recorder.finishes).toBe(1);

    recorder.deliver("what's on today");
    expect(texts).toEqual(["what's on today"]);
    expect(c.phase).toBe("waiting");
    expect(earcons).toEqual(["listening", "sent"]);
  });

  it("does nothing on a press while transcribing", () => {
    const c = make();
    c.press();
    c.press();
    c.press();
    expect(c.phase).toBe("transcribing");
    expect(recorder.begins).toBe(1);
  });

  it("goes back to idle and sends nothing when nothing was heard", () => {
    const c = make();
    c.press();
    c.press();
    recorder.deliver(null);
    expect(c.phase).toBe("idle");
    expect(texts).toEqual([]);
  });

  it("goes back to idle when the send refuses", () => {
    const c = make({ sendResult: null });
    talk(c);
    expect(c.phase).toBe("idle");
    expect(earcons).toEqual(["listening"]);
  });

  it("speaks the reply as it streams and returns to idle", () => {
    const c = make();
    talk(c);
    c.replyProgress(REPLY, "You're free all morning. Lunch", []);
    expect(c.phase).toBe("speaking");
    expect(speech.spoken).toEqual(["You're free all morning."]);

    c.replySettled(REPLY, "You're free all morning. Lunch is at noon.");
    expect(speech.spoken).toEqual(["You're free all morning.", "Lunch is at noon."]);
    expect(c.phase, "still talking").toBe("speaking");

    speech.finishOne();
    speech.finishOne();
    expect(c.phase).toBe("idle");
  });

  it("purrs again after the opening sentence while Marlow keeps working", () => {
    const c = make({ first: 20 });
    talk(c);
    c.replyProgress(REPLY, "Checking your notes. ", [21]);
    expect(speech.spoken).toEqual(["Checking your notes."]);
    speech.finishOne();
    expect(c.phase, "the tools are still running").toBe("waiting");
    vi.advanceTimersByTime(25);
    expect(earcons).toEqual(["listening", "sent", "working"]);

    c.replySettled(REPLY, "Checking your notes. Nothing about games yet.");
    expect(c.phase).toBe("speaking");
    speech.finishOne();
    expect(c.phase).toBe("idle");
  });

  it("chimes for an approval card and reminds instead of purring until answered", () => {
    const c = make({ approval: 20 });
    talk(c);
    c.approvalChanged("req-1");
    expect(earcons, "at once, when the card appears").toEqual(["listening", "sent", "approval"]);
    vi.advanceTimersByTime(45);
    expect(earcons.length).toBeGreaterThanOrEqual(4);
    expect(earcons.slice(2).every((e) => e === "approval"), "reminders, not purrs").toBe(true);

    c.approvalChanged(null);
    const count = earcons.length;
    vi.advanceTimersByTime(100);
    expect(earcons.length, "answered: no more chimes").toBe(count);
    expect(c.phase).toBe("waiting");
  });

  it("lands no chime in a recording", () => {
    const c = make();
    c.press();
    c.approvalChanged("req-1");
    expect(earcons).toEqual(["listening"]);
  });

  it("chimes again when one request replaces another", () => {
    const c = make();
    talk(c);
    c.approvalChanged("req-1");
    c.approvalChanged("req-1");
    c.approvalChanged("req-2");
    expect(earcons).toEqual(["listening", "sent", "approval", "approval"]);
  });

  it("stops speech on an interrupt, starts listening, and ignores the rest of that reply", () => {
    const c = make();
    talk(c);
    c.replyProgress(REPLY, "First thing. Second thing. Third", []);
    expect(speech.spoken.length).toBe(2);

    c.press();
    expect(speech.stops).toBe(1);
    expect(c.phase).toBe("recording");
    expect(recorder.begins).toBe(2);

    c.replyProgress(REPLY, "First thing. Second thing. Third thing. Fourth.", []);
    c.replySettled(REPLY, "First thing. Second thing. Third thing. Fourth.");
    expect(speech.spoken.length, "an interrupted reply stays interrupted").toBe(2);
    expect(c.phase).toBe("recording");
  });

  it("skips the reply when pressed while Marlow is still thinking", () => {
    const c = make();
    talk(c);
    c.press();
    expect(c.phase).toBe("recording");
    c.replySettled(REPLY, "An answer you no longer want.");
    expect(speech.spoken).toEqual([]);
  });

  it("returns to idle with a tone when the reply fails", () => {
    const c = make();
    talk(c);
    c.replySettled(REPLY, null);
    expect(c.phase).toBe("idle");
    expect(earcons[earcons.length - 1]).toBe("failed");
  });

  it("stops the speech when a reply fails mid-speech", () => {
    const c = make();
    talk(c);
    c.replyProgress(REPLY, "One. Two. Three", []);
    c.replySettled(REPLY, null);
    expect(speech.stops).toBe(1);
    expect(c.phase).toBe("idle");
  });

  it("ends without speaking on an empty reply", () => {
    const c = make();
    talk(c);
    c.replySettled(REPLY, "");
    expect(c.phase).toBe("idle");
    expect(speech.spoken).toEqual([]);
  });

  it("does not read out markdown", () => {
    const c = make();
    talk(c);
    c.replySettled(REPLY, "**Two** things.");
    expect(speech.spoken).toEqual(["Two things."]);
  });

  it("reads the reply's terminal state off the chat", () => {
    const c = make();
    talk(c);
    c.messagesChanged([message("running")]);
    expect(c.phase).toBe("waiting");
    c.messagesChanged([message("stalled")]);
    expect(c.phase, "stalled is still running").toBe("waiting");
    c.messagesChanged([message("done", "Done.")]);
    expect(speech.spoken).toEqual(["Done."]);
  });

  it("counts a reply that vanishes from the chat as failed", () => {
    // Merged into a later turn, or lost to a server restart.
    const c = make();
    talk(c);
    c.messagesChanged([]);
    expect(c.phase).toBe("idle");
  });

  it("cancels and sends nothing when left mid-recording", () => {
    const c = make();
    c.press();
    c.stopAll();
    expect(recorder.cancels).toBe(1);
    expect(c.phase).toBe("idle");
    recorder.deliver("too late");
    expect(texts).toEqual([]);
  });

  it("cancels and sends nothing when left mid-transcription", () => {
    const c = make();
    c.press();
    c.press();
    c.stopAll();
    recorder.deliver("too late");
    expect(texts).toEqual([]);
    expect(c.phase).toBe("idle");
  });

  it("stops the speech when left while speaking", () => {
    const c = make();
    talk(c);
    c.replyProgress(REPLY, "Something long. And more", []);
    c.stopAll();
    expect(speech.stops).toBe(1);
    expect(c.phase).toBe("idle");
    expect(recorder.cancels, "nothing was recording").toBe(0);
  });

  it("stops the purr when left", () => {
    const c = make({ first: 20, interval: 20 });
    talk(c);
    c.stopAll();
    vi.advanceTimersByTime(200);
    expect(earcons).toEqual(["listening", "sent"]);
  });

  it("tells subscribers when the phase changes", () => {
    const c = make();
    const seen: string[] = [];
    c.subscribe(() => seen.push(c.getPhase()));
    talk(c);
    expect(seen).toEqual(["recording", "transcribing", "waiting"]);
  });
});

describe("earcons", () => {
  const peak = (s: Float32Array) => s.reduce((m, x) => Math.max(m, Math.abs(x)), 0);

  it("makes the working cue a quiet purr that fades in and out", () => {
    // The "still working" purr: 0.6 s, quiet, and silent at both ends so it
    // fades in and out instead of clicking every few seconds.
    const s = earconSamples("working");
    expect(Math.abs(s.length / EARCON_RATE - 0.6)).toBeLessThan(0.01);
    expect(peak(s)).toBeGreaterThan(0.1);
    expect(peak(s)).toBeLessThanOrEqual(0.2);
    expect(Math.abs(s[0])).toBeLessThan(0.001);
    expect(Math.abs(s[s.length - 1])).toBeLessThan(0.001);
  });

  it("makes the approval cue a short clear tink-tink", () => {
    // Louder than the purr, since it asks them to look, but short, and
    // without a click at either end.
    const s = earconSamples("approval");
    expect(Math.abs(s.length / EARCON_RATE - 0.59)).toBeLessThan(0.01);
    expect(peak(s)).toBeGreaterThan(0.3);
    expect(peak(s)).toBeLessThanOrEqual(0.45);
    expect(Math.abs(s[0])).toBeLessThan(0.001);
    expect(Math.abs(s[s.length - 1])).toBeLessThan(0.001);
  });

  it("builds every cue with sound in it", () => {
    for (const kind of ["listening", "sent", "working", "approval", "failed"] as Earcon[]) {
      const s = earconSamples(kind, 48_000);
      expect(s.length / 48_000, kind).toBeGreaterThan(0.05);
      expect(peak(s), kind).toBeGreaterThan(0.1);
    }
  });
});
