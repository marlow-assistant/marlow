import { describe, expect, it } from "vitest";
import { DEFAULT_PHONON_VOICE, isPhononKey, PHONON_VOICES, phononVoiceFor, phononVoiceKey, phononVoiceUrl } from "./phononVoices";

describe("Phonon voices", () => {
  it("are the native app's 25 English voices, British first", () => {
    expect(PHONON_VOICES).toHaveLength(25);
    const accents = PHONON_VOICES.map((v) => v.accent);
    expect(accents.filter((a) => a === "British")).toHaveLength(13);
    expect(accents.indexOf("American")).toBe(13);
    expect(new Set(PHONON_VOICES.map((v) => v.name)).size).toBe(25);
    expect(new Set(PHONON_VOICES.map((v) => v.id)).size).toBe(25);
  });

  it("are fetched from one pinned revision, never main", () => {
    const alfie = PHONON_VOICES[0];
    expect(alfie).toEqual({ name: "Alfie", accent: "British", id: "X6NceqF285Gr04gb" });
    expect(phononVoiceUrl(alfie)).toBe(
      "https://huggingface.co/gradium/phonon-voices/resolve/0ee17205f254b5abc43b266c8a0f215565c352f7/voices/X6NceqF285Gr04gb.safetensors",
    );
  });

  it("read a stored choice: the named voice, else Alfie", () => {
    expect(DEFAULT_PHONON_VOICE).toBe("Alfie");
    expect(phononVoiceFor("phonon:Zoey").name).toBe("Zoey");
    expect(phononVoiceFor(null).name).toBe("Alfie");
    expect(phononVoiceFor("phonon:Nobody").name).toBe("Alfie");
    expect(phononVoiceFor("com.apple.voice.compact.en-GB.Daniel").name).toBe("Alfie");
    expect(phononVoiceKey("Zoey")).toBe("phonon:Zoey");
    expect(isPhononKey("phonon:Zoey")).toBe(true);
    expect(isPhononKey("uri.Daniel")).toBe(false);
    expect(isPhononKey(null)).toBe(false);
  });
});
