// The web-only Voice section in Settings: which voice voice mode reads in
// (lib/browserSpeech.ts, choosableVoices / pickVoice).
import "fake-indexeddb/auto";
import { IDBFactory } from "fake-indexeddb";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { Api } from "../lib/api";
import type { VoiceEngine } from "../lib/browserSpeech";
import { DEFAULT_FACE } from "../lib/faces";
import { kvGet, kvSet, KV_VOICE, resetDbForTests } from "../lib/storage";
import { PHONON_VOICE } from "../lib/phononSpeech";
import { Settings } from "./Settings";

class FakeUtterance {
  voice: SpeechSynthesisVoice | null = null;
  lang = "";
  constructor(readonly text: string) {}
}

class FakeSynth {
  spoken: FakeUtterance[] = [];
  cancels = 0;
  speaking = false;
  pending = false;
  voices: SpeechSynthesisVoice[] = [];
  private listeners = new Set<() => void>();
  speak(u: FakeUtterance) {
    this.spoken.push(u);
  }
  cancel() {
    this.cancels++;
  }
  getVoices() {
    return this.voices;
  }
  addEventListener(_: string, fn: () => void) {
    this.listeners.add(fn);
  }
  removeEventListener(_: string, fn: () => void) {
    this.listeners.delete(fn);
  }
  voicesChanged(voices: SpeechSynthesisVoice[]) {
    this.voices = voices;
    this.listeners.forEach((fn) => fn());
  }
}

const voice = (name: string, lang: string) =>
  ({ name, lang, voiceURI: `uri.${name}`, default: false, localService: true }) as SpeechSynthesisVoice;

let synth: FakeSynth;

beforeEach(() => {
  globalThis.indexedDB = new IDBFactory();
  resetDbForTests();
  synth = new FakeSynth();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** Phonon needs a Worker (and WebAssembly SIMD, which Node has); jsdom has no Worker. */
const phononCanRun = () => vi.stubGlobal("Worker", class {});
const onIPhone = () =>
  vi.spyOn(navigator, "userAgent", "get").mockReturnValue("Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148");

function open(engine: VoiceEngine | null = { synth: synth as unknown as VoiceEngine["synth"], makeUtterance: (t) => new FakeUtterance(t) as unknown as SpeechSynthesisUtterance }) {
  return render(
    <Settings
      api={{} as Api}
      token="tok-abcdef"
      reauthNeeded={false}
      queuePaused={false}
      effort="low"
      serverModel={null}
      serverBuild={null}
      onEffort={vi.fn()}
      locationSharing="off"
      locationStatus="ok"
      onLocationSharing={vi.fn()}
      face={DEFAULT_FACE}
      onFace={vi.fn()}
      onUnpair={vi.fn()}
      onClose={vi.fn()}
      host="marlow.test"
      webBuild="deadbee"
      voiceEngine={engine}
    />,
  );
}

const group = () => screen.getByRole("radiogroup", { name: "Voice" });

describe("Settings › Voice", () => {
  it("lists the English voices, not the novelty ones, under Automatic", () => {
    synth.voices = [voice("Samantha", "en-US"), voice("Bubbles", "en-US"), voice("Daniel", "en-GB"), voice("Amélie", "fr-CA")];
    open();
    const names = within(group()).getAllByRole("radio").map((r) => r.textContent);
    expect(names[0]).toContain("Automatic");
    expect(names.join("|")).toContain("Daniel");
    expect(names.join("|")).toContain("Samantha");
    expect(names.join("|")).not.toContain("Bubbles");
    expect(names.join("|")).not.toContain("Amélie");
    expect(within(group()).getByRole("radio", { name: /Automatic/ }).getAttribute("aria-checked")).toBe("true");
  });

  it("a tap chooses the voice, keeps it on this device and plays a preview in it", async () => {
    synth.voices = [voice("Samantha", "en-US"), voice("Daniel", "en-GB")];
    open();
    fireEvent.click(within(group()).getByRole("radio", { name: /Daniel/ }));
    expect(within(group()).getByRole("radio", { name: /Daniel/ }).getAttribute("aria-checked")).toBe("true");
    expect(synth.spoken.at(-1)?.voice?.name).toBe("Daniel");
    await waitFor(async () => expect(await kvGet(KV_VOICE)).toBe("uri.Daniel"));

    fireEvent.click(within(group()).getByRole("radio", { name: /Automatic/ }));
    await waitFor(async () => expect(await kvGet(KV_VOICE)).toBeUndefined());
  });

  it("shows the stored choice when it opens", async () => {
    synth.voices = [voice("Samantha", "en-US"), voice("Daniel", "en-GB")];
    await kvSet(KV_VOICE, "uri.Daniel");
    open();
    await waitFor(() =>
      expect(within(group()).getByRole("radio", { name: /Daniel/ }).getAttribute("aria-checked")).toBe("true"),
    );
  });

  it("fills in when the engine's list arrives late, as Android Chrome's does", () => {
    open();
    expect(screen.queryByRole("radiogroup", { name: "Voice" })).toBeNull();
    act(() => synth.voicesChanged([voice("Daniel", "en-GB")]));
    expect(within(group()).getByRole("radio", { name: /Daniel/ })).toBeTruthy();
  });

  it("is left out where the browser can't speak", () => {
    open(null);
    expect(screen.queryByRole("region", { name: "Voice" })).toBeNull();
    expect(screen.queryByRole("radiogroup", { name: "Voice" })).toBeNull();
  });

  describe("Phonon", () => {
    it("is offered under Automatic where it can run, with no preview and no download from Settings", async () => {
      phononCanRun();
      synth.voices = [voice("Samantha", "en-US")];
      open();
      const radios = within(group()).getAllByRole("radio");
      expect(radios[0].textContent).toContain("Automatic");
      expect(radios[1].textContent).toContain("Phonon");
      expect(radios[1].textContent).toContain("Natural voice, 65 MB download");

      fireEvent.click(radios[1]);
      expect(within(group()).getByRole("radio", { name: /Phonon/ }).getAttribute("aria-checked")).toBe("true");
      await waitFor(async () => expect(await kvGet(KV_VOICE)).toBe(PHONON_VOICE));
      expect(synth.spoken).toHaveLength(0);
      expect(synth.cancels).toBeGreaterThan(0);

      fireEvent.click(within(group()).getByRole("radio", { name: /Automatic/ }));
      await waitFor(async () => expect(await kvGet(KV_VOICE)).toBeUndefined());
    });

    it("says what Automatic means: the device voice here, Phonon on an iPhone", () => {
      phononCanRun();
      synth.voices = [voice("Samantha", "en-US")];
      open();
      expect(within(group()).getByRole("radio", { name: /Automatic/ }).textContent).toContain("Device voice");
      cleanup();
      onIPhone();
      open();
      expect(within(group()).getByRole("radio", { name: /Automatic/ }).textContent).toContain("Phonon");
    });

    it("shows the stored choice when it opens", async () => {
      phononCanRun();
      synth.voices = [voice("Samantha", "en-US")];
      await kvSet(KV_VOICE, PHONON_VOICE);
      open();
      await waitFor(() =>
        expect(within(group()).getByRole("radio", { name: /Phonon/ }).getAttribute("aria-checked")).toBe("true"),
      );
    });

    it("keeps the section when the browser lists no voices, since Phonon doesn't need them", () => {
      phononCanRun();
      open(null);
      expect(within(group()).getAllByRole("radio")).toHaveLength(2);
    });

    it("is not offered where it can't run", () => {
      synth.voices = [voice("Samantha", "en-US")];
      open();
      expect(within(group()).queryByRole("radio", { name: /Phonon/ })).toBeNull();
    });
  });
});
