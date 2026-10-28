// Voice mode's one button and what it does in each state, ported from iOS
// `VoiceModeController.swift` (docs/tasks/voice-mode-hands-free.md, Mike
// 2026-10-20). Press to talk, press again to send; Marlow's reply is read out
// sentence by sentence as it streams; pressing while Marlow is working or
// speaking stops the speech and starts listening — "stop whatever Marlow is
// doing out loud and listen to me." The turn itself is never cancelled
// (`claude -p` can't be): it finishes, and its whole text is in the chat.
//
// Never endpoints on silence. The press is the only end of a turn, because
// Mike pauses mid-thought and a truncated turn can't be taken back.

import type { Earcon } from "./earcons";
import { CodeFenceFilter, plainSpeech, SentenceChunker } from "./spokenReply";
import type { ChatMessage } from "./types";

export type VoicePhase = "idle" | "recording" | "transcribing" | "waiting" | "speaking";

/** Recording behind a seam, so the state machine is tested without a mic. */
export interface VoiceRecorder {
  /** Starts listening. `onResult` fires exactly once per `begin` that isn't
   * cancelled: the transcript, or null when nothing came of it. */
  begin(onResult: (text: string | null) => void): void;
  /** Stops listening and transcribes; the result goes to `begin`'s callback. */
  finish(): void;
  /** Stops listening and reports nothing. */
  cancel(): void;
}

/** Speaking, behind a seam so the controller is tested without a synthesizer. */
export interface SpeechOutput {
  /** Once per utterance that played to its end. Not called for one cut off
   * by `stopSpeaking()`. */
  onUtteranceFinished: (() => void) | null;
  speak(text: string): void;
  stopSpeaking(): void;
}

export interface VoiceModeOptions {
  recorder: VoiceRecorder;
  speech: SpeechOutput;
  /** The reply placeholder's id, or null when nothing was sent. */
  send: (text: string) => string | null;
  earcon?: (kind: Earcon) => void;
  firstWorkingTickMs?: number;
  workingTickIntervalMs?: number;
  approvalReminderMs?: number;
}

export class VoiceModeController {
  phase: VoicePhase = "idle";

  private readonly recorder: VoiceRecorder;
  private readonly speech: SpeechOutput;
  private readonly send: (text: string) => string | null;
  private readonly earcon: (kind: Earcon) => void;
  private readonly firstWorkingTickMs: number;
  private readonly workingTickIntervalMs: number;
  private readonly approvalReminderMs: number;
  private readonly listeners = new Set<() => void>();

  /** The reply being followed. Only one: an interrupted reply is dropped from
   * here, so its later polls are ignored rather than spoken. */
  reply: string | null = null;
  private chunker = new SentenceChunker();
  private fences = new CodeFenceFilter();
  private replyFinished = false;
  private queued = 0;
  private workingTimer: ReturnType<typeof setTimeout> | null = null;
  /** The approval card that is up, if any: Marlow is waiting on them, not
   * working. An id, not a flag, so one request replacing another chimes too. */
  private approval: string | null = null;
  /** Bumped by every begin and cancel, so a stale recorder result is dropped. */
  private recording = 0;

  constructor(opts: VoiceModeOptions) {
    this.recorder = opts.recorder;
    this.speech = opts.speech;
    this.send = opts.send;
    this.earcon = opts.earcon ?? (() => {});
    this.firstWorkingTickMs = opts.firstWorkingTickMs ?? 800;
    this.workingTickIntervalMs = opts.workingTickIntervalMs ?? 3_000;
    this.approvalReminderMs = opts.approvalReminderMs ?? 10_000;
    this.speech.onUtteranceFinished = () => this.utteranceFinished();
  }

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  getPhase = (): VoicePhase => this.phase;

  private setPhase(phase: VoicePhase): void {
    if (phase === this.phase) return;
    this.phase = phase;
    for (const fn of this.listeners) fn();
  }

  // --- the button --------------------------------------------------------------

  press(): void {
    switch (this.phase) {
      case "idle":
        this.listen();
        break;
      case "recording":
        this.setPhase("transcribing");
        this.recorder.finish();
        break;
      case "transcribing":
        // Inert: the transcript is a second away and a new recording would
        // race it.
        break;
      case "waiting":
      case "speaking":
        this.silenceReply();
        this.listen();
        break;
    }
  }

  /** Leaving the mode, or anything that takes the audio away (backgrounding):
   * stop talking, stop listening, send nothing. */
  stopAll(): void {
    this.silenceReply();
    if (this.phase === "recording" || this.phase === "transcribing") {
      this.recording++;
      this.recorder.cancel();
    }
    this.setPhase("idle");
  }

  private listen(): void {
    this.setPhase("recording");
    this.earcon("listening");
    const mine = ++this.recording;
    this.recorder.begin((text) => {
      if (mine === this.recording) this.transcribed(text);
    });
  }

  private transcribed(text: string | null): void {
    // Exited or cancelled in the meantime.
    if (this.phase !== "recording" && this.phase !== "transcribing") return;
    const id = text ? this.send(text) : null;
    if (!id) {
      this.setPhase("idle");
      return;
    }
    this.reply = id;
    this.chunker = new SentenceChunker();
    this.fences = new CodeFenceFilter();
    this.replyFinished = false;
    this.queued = 0;
    this.setPhase("waiting");
    this.earcon("sent");
    this.startWorkingTicks();
  }

  // --- the reply ---------------------------------------------------------------

  /** The raw streamed text for a running reply (`ChatStore.onReplyProgress`). */
  replyProgress(id: string, text: string, marks: number[]): void {
    if (id !== this.reply || this.replyFinished) return;
    this.speak(this.chunker.ingest(text, marks));
  }

  /** The reply's terminal state: its final text, or null when it failed or
   * vanished (merged into a later turn, lost to a server restart). */
  replySettled(id: string, text: string | null): void {
    if (id !== this.reply || this.replyFinished) return;
    this.replyFinished = true;
    if (text === null) {
      this.stopWorkingTicks();
      this.speech.stopSpeaking();
      this.queued = 0;
      this.earcon("failed");
      this.reply = null;
      if (this.phase === "waiting" || this.phase === "speaking") this.setPhase("idle");
      return;
    }
    this.speak(this.chunker.finish(text));
    this.finishIfDrained();
  }

  /** Reads a reply's terminal state off the chat's messages, which every path
   * that settles a reply goes through (poll, sync). */
  messagesChanged(messages: ChatMessage[]): void {
    const id = this.reply;
    if (!id || this.replyFinished) return;
    const message = messages.find((m) => m.id === id);
    if (!message) {
      this.replySettled(id, null);
      return;
    }
    if (message.status === "done") this.replySettled(id, message.text);
    else if (message.status === "error") this.replySettled(id, null);
  }

  private speak(sentences: string[]): void {
    const spoken = sentences.map((s) => plainSpeech(this.fences.filter(s))).filter((s) => s !== "");
    if (spoken.length === 0) return;
    if (this.phase === "waiting") {
      this.stopWorkingTicks();
      this.setPhase("speaking");
    }
    for (const sentence of spoken) {
      this.queued += 1;
      this.speech.speak(sentence);
    }
  }

  private utteranceFinished(): void {
    this.queued = Math.max(0, this.queued - 1);
    this.finishIfDrained();
    // Said everything so far, but Marlow is still going: back to waiting,
    // with the purr. A spoken reply now opens with "Checking your notes."
    // before its tools run, and without this the tools, or an approval card,
    // were silence (2026-10-21).
    if (this.queued === 0 && !this.replyFinished && this.phase === "speaking") {
      this.setPhase("waiting");
      this.startWorkingTicks();
    }
  }

  private finishIfDrained(): void {
    if (!this.replyFinished || this.queued !== 0) return;
    if (this.phase !== "waiting" && this.phase !== "speaking") return;
    this.stopWorkingTicks();
    this.reply = null;
    this.setPhase("idle");
  }

  /** Stops the current reply being spoken, now and for its remaining polls. */
  private silenceReply(): void {
    this.stopWorkingTicks();
    this.speech.stopSpeaking();
    this.queued = 0;
    this.reply = null;
    this.replyFinished = false;
  }

  // --- "still working" ---------------------------------------------------------

  /** A quiet tick while Marlow works and nothing is being said: 3–5 s before
   * the first word is normal (the spike, 2026-10-20), and in a car silence
   * that long reads as broken. The first comes just after the "sent" tone,
   * then every 3 s: waiting 4 s, then 3 s, for the first was "way too long"
   * (Mike, 2026-10-21), since the countdown only starts once the speech has
   * been transcribed.
   *
   * While an approval card is up, the tick is the approval chime instead,
   * every 10 s: a purr says "Marlow is busy", which is wrong when Marlow is
   * waiting on them. */
  private startWorkingTicks(): void {
    this.stopWorkingTicks();
    const [kind, first, interval]: [Earcon, number, number] = this.approval !== null
      ? ["approval", this.approvalReminderMs, this.approvalReminderMs]
      : ["working", this.firstWorkingTickMs, this.workingTickIntervalMs];
    const tick = (wait: number) => {
      this.workingTimer = setTimeout(() => {
        if (this.phase !== "waiting") {
          this.workingTimer = null;
          return;
        }
        this.earcon(kind);
        tick(interval);
      }, wait);
    };
    tick(first);
  }

  private stopWorkingTicks(): void {
    if (this.workingTimer !== null) clearTimeout(this.workingTimer);
    this.workingTimer = null;
  }

  // --- approvals ---------------------------------------------------------------

  /** The approval card appeared, went, or was replaced by another. It shows over the voice screen, but
   * in a car nobody is looking at it: Mike approved one by chance, having
   * heard only purrs (2026-10-21). A chime says "look at the phone"; once
   * answered, the purr resumes until Marlow speaks. */
  approvalChanged(requestId: string | null): void {
    if (requestId === this.approval) return;
    this.approval = requestId;
    // A chime while recording would land in the recording.
    if (requestId !== null && this.phase !== "recording" && this.phase !== "transcribing") this.earcon("approval");
    if (this.phase === "waiting") this.startWorkingTicks();
  }
}
