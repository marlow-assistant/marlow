import { describe, expect, it } from "vitest";
import { VoiceDebugLog } from "./voiceDebug";

describe("VoiceDebugLog", () => {
  it("reports how fast Phonon made the audio against how long it plays", () => {
    const log = new VoiceDebugLog(() => 0);
    const u = log.start();
    u.released();
    u.synthesized(900, 2);
    u.synthesized(600, 1);
    const text = log.format({ userAgent: "ua", build: "b" });
    // 1.5 s to make 3 s of audio: half the audio's length.
    expect(text).toContain("synthesis: 3.0 s of audio in 1.5 s (0.50 x real time)");
  });

  it("says nothing of synthesis where the browser's voice read the reply", () => {
    const log = new VoiceDebugLog(() => 0);
    log.start().released();
    expect(log.format()).not.toContain("synthesis");
  });
});
