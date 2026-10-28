// Voice mode's short tones (iOS `Earcons` in VoiceModeAudio.swift), ported
// sample for sample so the two clients sound the same: "listening", "sent",
// "still working" (the purr), "needs your OK" (the tink-tink), "that failed".
// A tone isn't Marlow speaking, so it stays honest where spoken filler
// ("one moment!") was rejected. Synthesised, so there are no audio assets.
// Pure, so the shape is tested; app/feedback.ts plays them.

export type Earcon = "listening" | "sent" | "working" | "approval" | "failed";

export const EARCON_RATE = 44_100;

/** (frequency Hz, seconds) notes, played in order, and their volume. */
const NOTES: Record<"listening" | "sent" | "failed", [[number, number][], number]> = {
  listening: [[[660, 0.07], [880, 0.09]], 0.35],
  sent: [[[880, 0.07], [660, 0.09]], 0.3],
  failed: [[[330, 0.12], [262, 0.18]], 0.35],
};

/** -1…1 samples for the cue, each note with a short fade so it doesn't click. */
export function earconSamples(kind: Earcon, rate = EARCON_RATE): Float32Array<ArrayBuffer> {
  if (kind === "working") return purr(rate);
  if (kind === "approval") return tinkTink(rate);
  const [notes, volume] = NOTES[kind];
  const out: number[] = [];
  for (const [frequency, seconds] of notes) {
    const count = Math.floor(rate * seconds);
    const fade = Math.floor(rate * 0.01);
    for (let i = 0; i < count; i++) {
      const envelope = Math.min(1, Math.min(i, count - i) / fade);
      out.push(Math.sin((2 * Math.PI * frequency * i) / rate) * volume * envelope);
    }
  }
  return Float32Array.from(out);
}

/** "Still working": a low tone that swells and fades over 0.6 s while its
 * volume flutters. A single 60 ms blip read as a glitch, not as something
 * happening; a wibble (pitch wobbling around 560 Hz, then 420 Hz) sounded
 * like "a Star Trek theremin". Mike picked this one from six rendered
 * options, 2026-10-21: the flutter is in the volume, not the pitch. */
function purr(rate: number): Float32Array<ArrayBuffer> {
  const seconds = 0.6;
  const count = Math.floor(rate * seconds);
  const out = new Float32Array(count);
  for (let i = 0; i < count; i++) {
    const t = i / rate;
    // A smooth swell and fade, so it never clicks or startles.
    const swell = Math.sin((Math.PI * t) / seconds) ** 2;
    // The purr: the volume dips and recovers 11 times a second.
    const flutter = 0.65 + 0.35 * Math.sin(2 * Math.PI * 11 * t);
    // A quiet octave above makes 300 Hz rounder than a bare sine.
    const phase = 2 * Math.PI * 300 * t;
    const tone = Math.sin(phase) + 0.35 * Math.sin(2 * phase);
    out[i] = (tone / 1.35) * swell * flutter * 0.18;
  }
  return out;
}

/** The approval tink-tink, two taps on a glass, so one sound means "approval
 * needed" everywhere. Mike picked it from six rendered options, 2026-10-21. */
function tinkTink(rate: number): Float32Array<ArrayBuffer> {
  // A glass's overtones aren't whole multiples of the note, which is what
  // makes it a "tink" and not a beep.
  const tap = (t: number, length: number, decay: number) => {
    if (t < 0 || t >= length) return 0;
    const attack = Math.min(t / 0.005, 1);
    const tail = Math.min(1, (length - t) / 0.02);
    const f = 1568;
    const tone =
      Math.sin(2 * Math.PI * f * t) + 0.3 * Math.sin(2 * Math.PI * f * 2.76 * t) + 0.1 * Math.sin(2 * Math.PI * f * 5.4 * t);
    return tone * attack * Math.exp(-t / decay) * tail;
  };
  const count = Math.floor(rate * 0.59);
  const out = new Float32Array(count);
  for (let i = 0; i < count; i++) {
    const t = i / rate;
    out[i] = (tap(t, 0.35, 0.07) + tap(t - 0.14, 0.45, 0.09)) * 0.315;
  }
  return out;
}
