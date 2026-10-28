// Section 9: sent, reply started, reply finished. Fired unconditionally (no
// "only when silent" check; iOS deliberately has none, gaps item 16).
//
// The web's haptic is `navigator.vibrate`, which only Android implements; a
// desktop or Safari simply has none. The sounds are synthesised with Web
// Audio rather than shipped as files: two short tones need no asset, and an
// AudioContext starts suspended until a user gesture, so it is created (or
// resumed) on the first send, which is one.

import { earconSamples, type Earcon } from "../lib/earcons";

let ctx: AudioContext | null = null;

/** The chat's own sounds and taps, held while voice mode is open (it has its
 * own cues, lib/earcons.ts) or a dictation is recording (they would land in
 * it). One switch here, so a new sound can't forget to check
 * (`/code-review high`, 2026-10-22). Voice mode's earcons ignore it. */
let muted = false;
export function setChatFeedbackMuted(on: boolean): void {
  muted = on;
}

/** Call from a user gesture (the send button, Enter). */
export function unlockAudio(): void {
  try {
    if (!ctx) ctx = new AudioContext();
    if (ctx.state === "suspended") void ctx.resume();
  } catch {
    ctx = null;
  }
}

function tone(freqs: number[], durationMs: number, gain: number): void {
  if (!ctx || ctx.state !== "running") return;
  const now = ctx.currentTime;
  const out = ctx.createGain();
  out.gain.setValueAtTime(0, now);
  out.gain.linearRampToValueAtTime(gain, now + 0.01);
  out.gain.exponentialRampToValueAtTime(0.0001, now + durationMs / 1000);
  out.connect(ctx.destination);
  freqs.forEach((f, i) => {
    const osc = ctx!.createOscillator();
    osc.type = "sine";
    osc.frequency.setValueAtTime(f, now + (i * durationMs) / 1000 / freqs.length);
    osc.connect(out);
    osc.start(now + (i * durationMs) / 1000 / freqs.length);
    osc.stop(now + durationMs / 1000);
  });
}

function vibrate(ms: number): void {
  try {
    navigator.vibrate?.(ms);
  } catch {
    // Not supported, or blocked without a recent gesture: nothing to do.
  }
}

/** Light tap plus the sent sound. */
export function feedbackSent(): void {
  if (muted) return;
  vibrate(10);
  tone([880], 90, 0.05);
}

/** Soft tap: the thinking time is over (FBK-1). No sound, as on iOS. */
export function feedbackStarted(): void {
  if (muted) return;
  vibrate(8);
}

/** Medium tap plus the receive sound (FBK-2). */
export function feedbackFinished(): void {
  if (muted) return;
  vibrate(20);
  tone([660, 990], 180, 0.06);
}

/** "Needs your OK": the iOS approval tink-tink, two taps on a glass, so one
 * sound means an approval card on every client (Mike, 2026-10-21: "it will
 * train me"). A glass's overtones aren't whole multiples of the note, which
 * is what makes it a tink, not a beep. Louder than the other cues, since it
 * asks them to look. Silent until a send has unlocked audio this session. */
export function feedbackApproval(): void {
  if (muted) return;
  vibrate(30);
  if (!ctx || ctx.state !== "running") return;
  const tap = (at: number, decay: number) => {
    const start = ctx!.currentTime + at;
    const out = ctx!.createGain();
    out.gain.setValueAtTime(0, start);
    out.gain.linearRampToValueAtTime(0.12, start + 0.005);
    out.gain.setTargetAtTime(0, start + 0.005, decay);
    out.connect(ctx!.destination);
    for (const [ratio, level] of [[1, 1], [2.76, 0.3], [5.4, 0.1]]) {
      const osc = ctx!.createOscillator();
      const g = ctx!.createGain();
      g.gain.value = level;
      osc.frequency.value = 1568 * ratio;
      osc.connect(g).connect(out);
      osc.start(start);
      osc.stop(start + 0.45);
    }
  };
  tap(0, 0.07);
  tap(0.14, 0.09);
}

const earconBuffers = new Map<Earcon, AudioBuffer>();

/** Voice mode's cues (lib/earcons.ts), the same samples as iOS. Voice mode
 * opens with a tap, which unlocks audio (`unlockAudio`). */
export function playEarcon(kind: Earcon): void {
  if (!ctx || ctx.state !== "running") return;
  let buffer = earconBuffers.get(kind);
  if (!buffer) {
    const samples = earconSamples(kind, ctx.sampleRate);
    buffer = ctx.createBuffer(1, samples.length, ctx.sampleRate);
    buffer.copyToChannel(samples, 0);
    earconBuffers.set(kind, buffer);
  }
  const source = ctx.createBufferSource();
  source.buffer = buffer;
  source.connect(ctx.destination);
  source.start();
}
