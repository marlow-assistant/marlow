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
import type { PhononEngine } from "../lib/phononSpeech";
import { Settings } from "./Settings";

// The shared model is faked: Settings' previews go through the real
// PhononSpeech, which asks loadPhonon for it.
const load = vi.hoisted(() => vi.fn());
vi.mock("../lib/phononSpeech", async (original) => ({
  ...(await original<typeof import("../lib/phononSpeech")>()),
  loadPhonon: load,
}));

class FakeEngine implements PhononEngine {
  sampleRate = 24_000;
  device = "cpu";
  threads = 1;
  added: string[] = [];
  streams: { text: string; voice: string | undefined }[] = [];
  async addVoice(name: string) {
    this.added.push(name);
  }
  async *stream(text: string, { voice }: { voice?: string }) {
    this.streams.push({ text, voice });
    yield new Float32Array(1920);
  }
}

class FakeSource {
  buffer: unknown = null;
  stopped = false;
  onended: (() => void) | null = null;
  connect() {}
  start() {}
  stop() {
    this.stopped = true;
  }
}

class FakeAudio {
  static made: FakeAudio[] = [];
  currentTime = 0;
  state = "running";
  destination = {};
  sources: FakeSource[] = [];
  constructor() {
    FakeAudio.made.push(this);
  }
  resume = async () => {};
  close = async () => {};
  createBuffer(_c: number, length: number, rate: number) {
    return { duration: length / rate, copyToChannel() {} };
  }
  createBufferSource() {
    const source = new FakeSource();
    this.sources.push(source);
    return source;
  }
}

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
  FakeAudio.made = [];
  load.mockReset();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** Phonon needs a Worker (and WebAssembly SIMD, which Node has); jsdom has no Worker. */
const phononCanRun = () => {
  vi.stubGlobal("Worker", class {});
  vi.stubGlobal("AudioContext", FakeAudio);
};
const settle = () => act(() => new Promise<void>((r) => setTimeout(r, 0)));

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
    it("is what Automatic means where it can run, Alfie, with no preview and no download from Settings", async () => {
      phononCanRun();
      synth.voices = [voice("Samantha", "en-US")];
      await kvSet(KV_VOICE, "Samantha");
      open();
      const automatic = () => within(group()).getByRole("radio", { name: /Automatic/ });
      // The device voice stored makes Automatic mean the device voice until it is chosen.
      await waitFor(() => expect(automatic().textContent).toContain("Device voice"));
      fireEvent.click(automatic());
      expect(automatic().getAttribute("aria-checked")).toBe("true");
      expect(automatic().textContent).toContain("Phonon, Alfie");
      await waitFor(async () => expect(await kvGet(KV_VOICE)).toBeUndefined());
      expect(synth.spoken).toHaveLength(0);
      expect(load).not.toHaveBeenCalled();
    });

    it("lists its 25 voices in a group of their own, above the device's", () => {
      phononCanRun();
      synth.voices = [voice("Samantha", "en-US")];
      open();
      const phonon = within(group()).getByRole("group", { name: "Phonon" });
      const rows = within(phonon).getAllByRole("radio");
      expect(rows).toHaveLength(25);
      expect(rows[0].textContent).toContain("Alfie");
      expect(rows[0].textContent).toContain("British");
      expect(within(phonon).getByRole("radio", { name: /Zoey/ }).textContent).toContain("American");
      const device = within(group()).getByRole("group", { name: "This device" });
      expect(within(device).getAllByRole("radio").map((r) => r.textContent)).toEqual([expect.stringContaining("Samantha")]);
      expect(phonon.compareDocumentPosition(device) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    });

    it("checks Alfie, not Automatic, once he is chosen by name", async () => {
      phononCanRun();
      await kvSet(KV_VOICE, "phonon:Alfie");
      open();
      await waitFor(() =>
        expect(within(group()).getByRole("radio", { name: /^Alfie/ }).getAttribute("aria-checked")).toBe("true"),
      );
      expect(within(group()).getByRole("radio", { name: /Automatic/ }).getAttribute("aria-checked")).toBe("false");
    });

    it("keeps the section when the browser lists no voices, since Phonon doesn't need them", () => {
      phononCanRun();
      open(null);
      expect(within(group()).getAllByRole("radio")).toHaveLength(26);
      expect(screen.queryByRole("group", { name: "This device" })).toBeNull();
    });

    it("labels Automatic as the device voice when a stored voice has gone, as voice mode reads it", async () => {
      // A stored key is a device voice to voice mode (usesPhonon), even once
      // the engine stops listing it, so Automatic must not claim Phonon.
      phononCanRun();
      synth.voices = [voice("Samantha", "en-US")];
      await kvSet(KV_VOICE, "gone-voice");
      open();
      await waitFor(() =>
        expect(within(group()).getByRole("radio", { name: /Automatic/ }).textContent).toContain("Device voice"),
      );
    });

    it("shows a stored Phonon voice as chosen when it opens", async () => {
      phononCanRun();
      await kvSet(KV_VOICE, "phonon:Zoey");
      open();
      await waitFor(() =>
        expect(within(group()).getByRole("radio", { name: /Zoey/ }).getAttribute("aria-checked")).toBe("true"),
      );
      const automatic = within(group()).getByRole("radio", { name: /Automatic/ });
      expect(automatic.getAttribute("aria-checked")).toBe("false");
    });

    it("a tap chooses the voice, keeps it as phonon:<Name> and previews it through the shared model", async () => {
      phononCanRun();
      const engine = new FakeEngine();
      load.mockResolvedValue(engine);
      open();
      fireEvent.click(within(group()).getByRole("radio", { name: /Zoey/ }));
      expect(within(group()).getByRole("radio", { name: /Zoey/ }).getAttribute("aria-checked")).toBe("true");
      // Made inside the tap: iOS keeps a context made later silent.
      expect(FakeAudio.made).toHaveLength(1);
      await waitFor(async () => expect(await kvGet(KV_VOICE)).toBe("phonon:Zoey"));
      await settle();
      expect(engine.added).toEqual(["Zoey"]);
      expect(engine.streams).toEqual([{ text: "Hi, this is how I'll sound in voice mode.", voice: "Zoey" }]);
    });

    it("shows the download on the chosen voice, then previews it if it is still the choice", async () => {
      phononCanRun();
      const engine = new FakeEngine();
      let ready!: (e: FakeEngine) => void;
      load.mockImplementation((onProgress: (f: number | null) => void) => {
        onProgress(0.4);
        return new Promise<FakeEngine>((r) => (ready = r));
      });
      open();
      fireEvent.click(within(group()).getByRole("radio", { name: /Zoey/ }));
      expect(within(group()).getByRole("radio", { name: /Zoey/ }).textContent).toContain("40%");
      expect(within(group()).getByRole("radio", { name: /^Alfie/ }).textContent).not.toContain("40%");

      // A different pick while the model downloads: only that one is heard.
      fireEvent.click(within(group()).getByRole("radio", { name: /Hugh/ }));
      ready(engine);
      await settle();
      expect(engine.streams.map((s) => s.voice)).toEqual(["Hugh"]);
      expect(within(group()).getByRole("radio", { name: /Hugh/ }).textContent).not.toContain("%");
    });

    it("a tap on another voice cuts off a Phonon preview, and a Phonon tap cuts off a browser one", async () => {
      phononCanRun();
      synth.voices = [voice("Daniel", "en-GB")];
      load.mockResolvedValue(new FakeEngine());
      open();
      fireEvent.click(within(group()).getByRole("radio", { name: /Zoey/ }));
      await settle();
      expect(synth.cancels).toBeGreaterThan(0);
      const playing = FakeAudio.made[0].sources;
      expect(playing).toHaveLength(1);

      fireEvent.click(within(group()).getByRole("radio", { name: /Daniel/ }));
      expect(playing[0].stopped).toBe(true);
      expect(synth.spoken.at(-1)?.voice?.name).toBe("Daniel");
      await waitFor(async () => expect(await kvGet(KV_VOICE)).toBe("uri.Daniel"));
    });

    it("is not mentioned where it can't run", () => {
      synth.voices = [voice("Samantha", "en-US")];
      open();
      expect(screen.getByRole("region", { name: "Voice" }).textContent).not.toContain("Phonon");
      expect(within(group()).getByRole("radio", { name: /Automatic/ }).textContent).toContain("Device voice");
      expect(screen.queryByRole("group", { name: "Phonon" })).toBeNull();
    });
  });
});
