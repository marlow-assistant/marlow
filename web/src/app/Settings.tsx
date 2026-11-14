import { useEffect, useLayoutEffect, useMemo, useRef, useState, type MouseEvent, type ReactNode } from "react";
import { HttpError, type Api } from "../lib/api";
import type { Effort } from "../lib/chatStore";
import { isInstalledApp } from "../lib/installed";
import {
  CHECKIN_HOURS,
  CHECKIN_SAVE_FAILED,
  checkinLabel,
  DAY_OFFLINE,
  dayErrorMessage,
  isClockTime,
  type DayTimes,
} from "../lib/dayFormat";
import { facePropName, type Face } from "../lib/faces";
import type { LocationSharing, LocationStatus } from "../lib/location";
import { MODEL_CHOICES, modelChoice, type ModelChoice } from "../lib/modelName";
import { NOTICES, noticeText, type Notice } from "../lib/notices";
import { accentOf, browserVoiceEngine, choosableVoices, previewVoice, voiceKey, type VoiceEngine } from "../lib/browserSpeech";
import {
  loadPhonon,
  phononCanRun,
  PhononSpeech,
  usesPhonon,
  type LoadStatus,
  type SpeechFallback,
} from "../lib/phononSpeech";
import {
  PHONON_VOICES,
  phononVoiceFor,
  phononVoiceKey,
  type PhononVoice,
} from "../lib/phononVoices";
import { KV_TEXT_SIZE, KV_VOICE, kvDelete, kvGet, kvSet } from "../lib/storage";
import { applyTextSize, currentTextSize, TEXT_SIZES, type TextSize } from "../lib/textSize";
import { FaceGrid, FaceImage } from "./Face";
import { WaveformIcon } from "./Icons";
import { enablePush, usePushState } from "./push";
import type { WhatsNewModel } from "./useWhatsNew";
import { ChangeRow } from "./WhatsNew";

// "Think time", not "Intelligence" (Mike, 2026-11-12): what the level buys is
// time spent thinking, and a quick one is a fair choice for snappier replies.
// Still low/medium/high on the wire.
const LEVELS: { value: Effort; label: string }[] = [
  { value: "low", label: "Quick" },
  { value: "medium", label: "Balanced" },
  { value: "high", label: "Thorough" },
];

const LOCATION_LEVELS: { value: LocationSharing; label: string }[] = [
  { value: "off", label: "Off" },
  { value: "approximate", label: "Approximate" },
  { value: "precise", label: "Precise" },
];

export interface SettingsProps {
  api: Api;
  /** The device token; only its last six characters are ever shown. */
  token: string;
  reauthNeeded: boolean;
  queuePaused: boolean;
  effort: Effort;
  serverModel: string | null;
  serverBuild: string | null;
  onEffort: (e: Effort) => void;
  /** After the box has taken a model, so every view shows it at once rather
   * than at the next /health. Optional so a test not about it needn't. */
  onModel?: (m: ModelChoice) => void;
  locationSharing: LocationSharing;
  /** "blocked": the browser refused the permission; "unavailable": no API. */
  locationStatus: LocationStatus;
  onLocationSharing: (s: LocationSharing) => void;
  face: Face;
  onFace: (f: Face) => void;
  onUnpair: () => void;
  onClose: () => void;
  /** Chat's, so the switch here and the card it raises agree. Optional so a
   * test that isn't about updates needn't build one. */
  whatsNew?: Pick<WhatsNewModel, "entries" | "muted" | "setMuted" | "acknowledge">;
  /** Injected for tests. */
  host?: string;
  webBuild?: string;
  /** The browser's speech engine by default; null where it can't speak. */
  voiceEngine?: VoiceEngine | null;
}

/** One level below the main pane; `notice` is one below that. */
type Pane =
  | { id: "avatar" | "textSize" | "voice" | "server" | "about" | "notices" }
  | { id: "notice"; notice: Notice };

const PANE_TITLES: Record<Exclude<Pane["id"], "notice">, string> = {
  avatar: "Avatar",
  textSize: "Text size",
  voice: "Voice",
  server: "Server connection",
  about: "About",
  notices: "Open-source notices",
};

const paneTitle = (pane: Pane) => (pane.id === "notice" ? pane.notice.name : PANE_TITLES[pane.id]);

/** Port of iOS `SettingsView`, plus what only a browser needs: Text size and
 * Notifications (iOS gets both from the OS). The sheet has no navigation of
 * its own, so this is `NavigationStack`'s small equivalent: every level stays
 * mounted under the one on top, which keeps Your day's answer, a Voice preview
 * and the row to go back to. */
export function Settings(p: SettingsProps) {
  const [confirming, setConfirming] = useState(false);
  const [stack, setStack] = useState<Pane[]>([]);
  const [textSize, setTextSize] = useState(currentTextSize);
  // Once per sheet: a new engine object each render would resubscribe the
  // Voice section's listener every time.
  const voiceEngine = useMemo(
    () => (p.voiceEngine === undefined ? browserVoiceEngine() : p.voiceEngine),
    [p.voiceEngine],
  );
  const voice = useVoiceChoice(voiceEngine);
  const currentModel = modelChoice(p.serverModel);
  const [modelError, setModelError] = useState(false);
  // Server state, not a device copy like effort: the box reads it for every
  // turn, so it shows as picked only once the box has it.
  const pickModel = (m: ModelChoice) => {
    if (m === currentModel) return;
    setModelError(false);
    p.api.setModel(m).then(
      () => p.onModel?.(m),
      () => setModelError(true),
    );
  };
  const connection = p.reauthNeeded ? "Needs reauthorization" : p.queuePaused ? "Paused" : "Connected";

  const heading = useRef<HTMLHeadingElement>(null);
  const mainBody = useRef<HTMLDivElement>(null);
  // The row that opened each level, to go back to.
  const openers = useRef<HTMLElement[]>([]);
  const mainScroll = useRef(0);
  const goingBack = useRef(false);

  const open = (pane: Pane, e: MouseEvent<HTMLElement>) => {
    openers.current.push(e.currentTarget);
    if (stack.length === 0) mainScroll.current = mainBody.current?.scrollTop ?? 0;
    setStack((s) => [...s, pane]);
  };
  const back = () => {
    goingBack.current = true;
    setStack((s) => s.slice(0, -1));
  };

  // Before paint, so the main pane never shows at the top and jumps: its two
  // rows are at the bottom, and hiding it loses the scroll position.
  useLayoutEffect(() => {
    if (goingBack.current) {
      goingBack.current = false;
      if (stack.length === 0 && mainBody.current) mainBody.current.scrollTop = mainScroll.current;
      openers.current.pop()?.focus();
    } else if (stack.length > 0) {
      heading.current?.focus();
    }
  }, [stack]);

  const top = stack.at(-1);

  const paneBody = (pane: Pane) => {
    switch (pane.id) {
      case "avatar":
        return (
          <AvatarPicker
            face={p.face}
            onFace={(f) => {
              p.onFace(f);
              back();
            }}
          />
        );
      case "textSize":
        return <TextSizePicker size={textSize} onSize={setTextSize} />;
      case "voice":
        return voice?.picker;
      case "server":
        return (
          <>
            <section className="settings-section" aria-label="Server connection">
              <dl className="settings-group">
                <CopyRow label="Server" value={p.host ?? location.host} />
                <Row label="Connection" value={connection} />
                {p.token && <Row label="Device" value={p.token.slice(-6)} />}
              </dl>
            </section>
            <section className="settings-section">
              <div className="settings-group">
                <button type="button" className="settings-danger" onClick={() => setConfirming(true)}>
                  Unpair
                </button>
              </div>
            </section>
          </>
        );
      case "about":
        return (
          <>
            {/* Both sides, deliberately adjacent and in the same format: the
                question this answers is "do these two agree?", which is exactly
                what a branch deploy makes worth asking. */}
            <section className="settings-section" aria-label="Build">
              <h3 className="view-day">Build</h3>
              <dl className="settings-group">
                <CopyRow label="App" value={p.webBuild ?? __WEB_BUILD__} />
                {p.serverBuild ? <CopyRow label="Server" value={p.serverBuild} /> : <Row label="Server" value="—" />}
              </dl>
            </section>
            <section className="settings-section">
              <div className="settings-group">
                <PaneRow label="Open-source notices" onOpen={(e) => open({ id: "notices" }, e)} />
              </div>
            </section>
          </>
        );
      case "notices":
        return (
          <div className="settings-group">
            {NOTICES.map((n) => (
              <PaneRow
                key={n.name}
                label={
                  <span className="notice-row">
                    <span>{n.name}</span>
                    <span className="notice-row-license">{n.license}</span>
                  </span>
                }
                onOpen={(e) => open({ id: "notice", notice: n }, e)}
              />
            ))}
          </div>
        );
      case "notice":
        return <NoticeText notice={pane.notice} />;
    }
  };

  return (
    <div className="sheet" role="dialog" aria-label="Settings">
      {top ? (
        <header className="sheet-header sheet-header-pane">
          <button type="button" className="sheet-back" onClick={back}>
            <span aria-hidden="true">‹ </span>Back
          </button>
          <h2 ref={heading} tabIndex={-1}>
            {paneTitle(top)}
          </h2>
        </header>
      ) : (
        <header className="sheet-header">
          <h2>Settings</h2>
          <button type="button" onClick={p.onClose}>
            Done
          </button>
        </header>
      )}
      <div ref={mainBody} className="sheet-body settings-body" hidden={top !== undefined}>
        <section className="settings-section" aria-label="Appearance and voice">
          <h3 className="view-day">Appearance and voice</h3>
          <div className="settings-group">
            <PaneRow
              icon={
                <span className="face-circle face-circle-small">
                  <FaceImage face={p.face} size={28} crop="large" />
                </span>
              }
              label="Avatar"
              value={facePropName(p.face)}
              onOpen={(e) => open({ id: "avatar" }, e)}
            />
            <PaneRow
              icon={
                <span className="settings-icon" aria-hidden="true">
                  Aa
                </span>
              }
              label="Text size"
              value={textSize.label}
              onOpen={(e) => open({ id: "textSize" }, e)}
            />
            {voice && (
              <PaneRow
                icon={
                  <span className="settings-icon" aria-hidden="true">
                    <WaveformIcon size={18} />
                  </span>
                }
                label="Voice"
                value={voice.summary}
                onOpen={(e) => open({ id: "voice" }, e)}
              />
            )}
          </div>
        </section>

        {/* The model is box-wide: chat and every scheduled job run on it
            (docs/tasks/model-picker.md), so a pick shows only once the box
            has it. Effort ("Think time") is a LATENCY dial on this workload,
            not a cost one: ~2,900 thinking tokens against 5.8M cache reads in
            a day (docs/tasks/model-and-effort-config.md). So the footer puts
            the cost on the model and only the wait on think time. */}
        <section className="settings-section" aria-label="Model">
          <h3 className="view-day">Model</h3>
          <div className="settings-group">
            <div className="segmented" role="radiogroup" aria-label="Model">
              {MODEL_CHOICES.map((m) => (
                <button
                  key={m.value}
                  type="button"
                  role="radio"
                  aria-checked={currentModel === m.value}
                  className="segment"
                  onClick={() => pickModel(m.value)}
                >
                  {m.label}
                </button>
              ))}
            </div>
          </div>
          <p className="settings-footer">
            {modelError
              ? "Couldn't change the model. Try again when Marlow is reachable."
              : "Opus has the best judgment but uses your plan's limits about twice as fast."}
          </p>
        </section>

        <section className="settings-section" aria-label="Think time">
          <h3 className="view-day">Think time</h3>
          <div className="settings-group">
            <div className="segmented" role="radiogroup" aria-label="Think time">
              {LEVELS.map((l) => (
                <button
                  key={l.value}
                  type="button"
                  role="radio"
                  aria-checked={p.effort === l.value}
                  className="segment"
                  onClick={() => p.onEffort(l.value)}
                >
                  {l.label}
                </button>
              ))}
            </div>
          </div>
          <p className="settings-footer">
            Longer think time helps with hard questions, but Marlow will take longer to respond.
          </p>
        </section>

        <section className="settings-section" aria-label="Location">
          <h3 className="view-day">Location</h3>
          <div className="settings-group">
            <div className="segmented" role="radiogroup" aria-label="Location">
              {LOCATION_LEVELS.map((l) => (
                <button
                  key={l.value}
                  type="button"
                  role="radio"
                  aria-checked={p.locationSharing === l.value}
                  className="segment"
                  onClick={() => p.onLocationSharing(l.value)}
                >
                  {l.label}
                </button>
              ))}
            </div>
          </div>
          <p className="settings-footer">
            {p.locationStatus === "unavailable"
              ? "Location isn't available in this browser."
              : p.locationStatus === "blocked"
                ? "Location is blocked for this site in the browser's settings."
                : "Sent with each message so Marlow knows where you are. Approximate shares your area to about a kilometre; Precise shares your exact position."}
          </p>
        </section>

        <YourDaySection api={p.api} />

        <NotificationsSection api={p.api} />

        {p.whatsNew && <UpdatesSection whatsNew={p.whatsNew} />}

        {!isInstalledApp() && (
          // Only an installed app is in Android's share sheet. No heading: it is
          // one line of guidance, and it goes away once the app is installed.
          <section className="settings-section">
            <p className="settings-footer" data-testid="install-hint">
              Install Marlow to your home screen to share to it.
            </p>
          </section>
        )}

        <section className="settings-section">
          <div className="settings-group">
            <PaneRow label="Server connection" onOpen={(e) => open({ id: "server" }, e)} />
            <PaneRow label="About" onOpen={(e) => open({ id: "about" }, e)} />
          </div>
        </section>
      </div>

      {stack.map((pane, i) => (
        <div key={i} className="sheet-body settings-body" hidden={i !== stack.length - 1}>
          {paneBody(pane)}
        </div>
      ))}

      {confirming && (
        <div className="modal-backdrop">
          <div className="modal" role="alertdialog" aria-modal="true" aria-labelledby="unpair-title" aria-describedby="unpair-text">
            <h2 id="unpair-title">Unpair from Marlow?</h2>
            <p id="unpair-text" className="unpair-text">
              This stops active work and revokes this device on Marlow. Reconnecting needs a new pairing link.
            </p>
            <div className="perm-buttons">
              <button type="button" onClick={() => setConfirming(false)}>
                Cancel
              </button>
              <button type="button" className="danger-button" onClick={p.onUnpair}>
                Unpair
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/** A row naming the current choice that opens its picker one level down, as
 * iOS does. Inline, the face grid and the voice list each took a screen of
 * scrolling (Mike, 2026-10-28). */
function PaneRow(p: { icon?: ReactNode; label: ReactNode; value?: string; onOpen: (e: MouseEvent<HTMLElement>) => void }) {
  return (
    <button type="button" className="settings-disclosure" onClick={p.onOpen}>
      {p.icon}
      <span>{p.label}</span>
      <span className="settings-disclosure-value">{p.value}</span>
    </button>
  );
}

/** Shown large, as the avatar's own crop: monocle against glasses is part of
 * the choice, and at the header's 52px it is hard to see. A pick goes back,
 * being the whole errand. No icon footer: a web app's home-screen icon is
 * fixed by its manifest. */
function AvatarPicker({ face, onFace }: { face: Face; onFace: (f: Face) => void }) {
  return (
    <div className="settings-group">
      <FaceGrid face={face} onFace={onFace} scrollToCurrent />
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="settings-row">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

/** How long "Copied" stands in for the value. */
const COPIED_MS = 1500;

/** A value someone may need to paste into a bug report: one line, however
 * long, and a tap copies all of it. The end of a build is the commit, the part
 * that tells two builds apart, so it never clips; what clips is the branch.
 * iOS's `CopyableRow` cuts the middle, which CSS can't. */
function CopyRow({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false);
  const at = value.lastIndexOf("@");

  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(false), COPIED_MS);
    return () => clearTimeout(timer);
  }, [copied]);

  return (
    <div className="settings-row">
      <dt>{label}</dt>
      <dd className="settings-copy-value">
        <button
          type="button"
          className="settings-copy"
          onClick={() => {
            // No clipboard outside a secure context; then the row just says nothing.
            void navigator.clipboard?.writeText(value).then(
              () => setCopied(true),
              () => {},
            );
          }}
        >
          {copied ? (
            "Copied"
          ) : at > 0 ? (
            <>
              <span className="settings-copy-clip">{value.slice(0, at)}</span>
              <span>{value.slice(at)}</span>
            </>
          ) : (
            <span className="settings-copy-clip">{value}</span>
          )}
        </button>
      </dd>
    </div>
  );
}

/** Lazily, and failing quietly to the link: the text is a separate file that
 * an offline app may not have yet. */
function NoticeText({ notice }: { notice: Notice }) {
  const [text, setText] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    void noticeText(notice).then(
      (t) => {
        if (live) setText(t);
      },
      () => {},
    );
    return () => {
      live = false;
    };
  }, [notice]);

  return (
    <>
      <a className="notice-link" href={notice.url} target="_blank" rel="noreferrer">
        {notice.url}
      </a>
      {text !== null && (
        <pre className="notice-text" data-testid="notice-text">
          {text}
        </pre>
      )}
    </>
  );
}

/** Web-only: iOS text follows the system's Dynamic Type setting, which a
 * browser does not pass to a page (lib/textSize.ts). */
function TextSizePicker({ size, onSize }: { size: TextSize; onSize: (s: TextSize) => void }) {
  return (
    <section className="settings-section">
      <div className="settings-group">
        <div className="segmented" role="radiogroup" aria-label="Text size">
          {TEXT_SIZES.map((s) => (
            <button
              key={s.id}
              type="button"
              role="radio"
              aria-checked={size.id === s.id}
              className="segment"
              onClick={() => {
                applyTextSize(s);
                onSize(s);
                void kvSet(KV_TEXT_SIZE, s.id).catch(() => {});
              }}
            >
              {s.label}
            </button>
          ))}
        </div>
      </div>
      <p className="settings-footer">For this device only.</p>
    </section>
  );
}

const PREVIEW_TEXT = "Hi, this is how I'll sound in voice mode.";

/** Settings previews Phonon's voices only, so PhononSpeech's browser-voice
 * fallback is never wanted: a preview is spoken once the model is ready. */
const NO_BROWSER_VOICE: SpeechFallback = {
  onUtteranceFinished: null,
  onSpeak: null,
  voiceName: "",
  englishVoices: "",
  speak() {},
  stopSpeaking() {},
  unlock() {},
  close() {},
};

/** Voice mode's reading voice: Automatic, which is Phonon's Alfie wherever it
 * can run (Mike, 2026-10-28), one of Phonon's 25 voices, or one of the
 * browser's own `speechSynthesis` voices. Phonon is the default because the
 * built-in voices sounded bad on an iPhone (Mike, 2026-10-25) and on Android
 * (the Android user, 2026-10-28). Choosing a Phonon voice previews it, which
 * starts the 65 MB model download if this is the first use; Automatic isn't
 * previewed, so nothing downloads for someone who only looks. Kept on this
 * device only, like text size: the voices are this device's. The device voices
 * are hidden until the engine lists any, which on Android Chrome is seconds
 * after load; where Phonon can run, its voices keep the row up. A hook because
 * the main pane's row names the choice and the pane below it lists them; null
 * where there is nothing to choose. */
function useVoiceChoice(engine: VoiceEngine | null): { summary: string; picker: ReactNode } | null {
  const [voices, setVoices] = useState(() => (engine ? choosableVoices(engine.synth.getVoices()) : []));
  const [chosen, setChosen] = useState<string | null>(null);
  // A tap before the stored choice has loaded wins over it.
  const tapped = useRef(false);
  const phonon = useMemo(() => phononCanRun(), []);
  // The preview's PhononSpeech, made in the first Phonon tap: the tap is when
  // the AudioContext has to be made for iOS (PhononSpeech.unlock).
  const speech = useRef<PhononSpeech | null>(null);
  const ready = useRef(false);
  // A preview waiting for the model to finish downloading.
  const waiting = useRef(false);
  const [load, setLoad] = useState<LoadStatus | null>(null);

  useEffect(() => {
    if (!engine) return;
    const load = () => setVoices(choosableVoices(engine.synth.getVoices()));
    engine.synth.addEventListener("voiceschanged", load);
    load();
    return () => engine.synth.removeEventListener("voiceschanged", load);
  }, [engine]);

  useEffect(() => {
    let live = true;
    void kvGet<string>(KV_VOICE)
      .then((v) => {
        if (live && v && !tapped.current) setChosen(v);
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, []);

  // Safari caps the AudioContexts a page holds.
  useEffect(
    () => () => {
      speech.current?.close();
      speech.current = null;
    },
    [],
  );

  if (voices.length === 0 && !phonon) return null;

  const stopPreviews = () => {
    waiting.current = false;
    speech.current?.stopSpeaking();
    engine?.synth.cancel();
  };

  const previewPhonon = (voice: PhononVoice) => {
    let s = speech.current;
    if (!s) {
      s = new PhononSpeech({
        fallback: NO_BROWSER_VOICE,
        load: loadPhonon,
        makeContext: () => new AudioContext(),
        now: () => performance.now(),
      });
      const mine = s;
      mine.onLoad = (status) => {
        setLoad(status);
        if (status.state === "ready") {
          ready.current = true;
          if (waiting.current) {
            waiting.current = false;
            mine.speak(PREVIEW_TEXT);
          }
        } else if (status.state === "failed") {
          // The next tap starts a fresh try.
          mine.close();
          speech.current = null;
          waiting.current = false;
        }
      };
      speech.current = s;
    }
    stopPreviews();
    s.setVoice(voice.name);
    s.unlock();
    if (ready.current) {
      s.speak(PREVIEW_TEXT);
    } else {
      waiting.current = true;
      s.start();
    }
  };

  const choose = (voice: SpeechSynthesisVoice | PhononVoice | null) => {
    tapped.current = true;
    if (voice === null) {
      stopPreviews();
      setChosen(null);
      void kvDelete(KV_VOICE).catch(() => {});
    } else if ("accent" in voice) {
      setChosen(phononVoiceKey(voice.name));
      void kvSet(KV_VOICE, phononVoiceKey(voice.name)).catch(() => {});
      previewPhonon(voice);
    } else {
      setChosen(voiceKey(voice));
      void kvSet(KV_VOICE, voiceKey(voice)).catch(() => {});
      speech.current?.stopSpeaking();
      waiting.current = false;
      if (engine) previewVoice(engine, voice);
    }
  };
  // A stored voice since uninstalled reads as Automatic, which is what
  // pickVoice falls back to. Voice mode still counts it a device voice, so
  // Automatic says so rather than claim Phonon. A Phonon name since dropped
  // from the list is Alfie, as it is in voice mode.
  const current = voices.some((v) => voiceKey(v) === chosen)
    ? chosen
    : phonon && PHONON_VOICES.some((v) => phononVoiceKey(v.name) === chosen)
      ? chosen
      : null;
  // Each row says what it is, "British (Phonon, HQ)", since the list is long
  // and the group titles scroll away (Mike, 2026-10-28).
  const phononSecondary = (v: PhononVoice) => {
    const label = `${v.accent} (Phonon, HQ)`;
    if (chosen !== phononVoiceKey(v.name) || load === null) return label;
    if (load.state === "loading") return load.fraction === null ? "Loading…" : `Downloading ${Math.round(load.fraction * 100)}%`;
    return load.state === "failed" ? "Couldn't download" : label;
  };

  // Only the name: the row has no room for more, and each voice's row in the
  // list says what it is. The list stays up after a pick, so voices can be
  // heard one after another.
  const device = voices.find((v) => voiceKey(v) === current);
  const phononChoice = PHONON_VOICES.find((v) => phononVoiceKey(v.name) === current);
  const automatic = phonon && usesPhonon(chosen) ? `Phonon, ${phononVoiceFor(null).name}` : "Device voice";
  const summary = device?.name ?? phononChoice?.name ?? "Automatic";

  return {
    summary,
    picker: (
      <section className="settings-section">
        <div className="settings-group">
          <div role="radiogroup" aria-label="Voice">
            <button type="button" role="radio" aria-checked={current === null} className="voice-option" onClick={() => choose(null)}>
              <span>Automatic</span>
              <span className="voice-option-lang">{automatic}</span>
            </button>
            {phonon && (
              <div role="group" aria-label="Phonon">
                <p className="voice-group-title" aria-hidden="true">
                  Phonon
                </p>
                {PHONON_VOICES.map((v) => (
                  <button
                    key={v.id}
                    type="button"
                    role="radio"
                    aria-checked={current === phononVoiceKey(v.name)}
                    className="voice-option"
                    onClick={() => choose(v)}
                  >
                    <span>{v.name}</span>
                    <span className="voice-option-lang">{phononSecondary(v)}</span>
                  </button>
                ))}
              </div>
            )}
            {voices.length > 0 && (
              <div role="group" aria-label="This device">
                <p className="voice-group-title" aria-hidden="true">
                  This device
                </p>
                {voices.map((v) => (
                  <button
                    key={voiceKey(v)}
                    type="button"
                    role="radio"
                    aria-checked={current === voiceKey(v)}
                    className="voice-option"
                    onClick={() => choose(v)}
                  >
                    <span>{v.name}</span>
                    <span className="voice-option-lang">{accentOf(v.lang)} (Device voice)</span>
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>
        <p className="settings-footer">
          {phonon
            ? "Phonon voices are made on this device; the others are the browser's own."
            : "How voice mode reads replies on this device. Tap a voice to hear it."}
        </p>
      </section>
    ),
  };
}

/** The other half of the update card (docs/tasks/whats-new.md): the card says
 * this list is here, and it is the way back from "Don't show this again". iOS
 * puts the list one level down; a sheet here opens it in place. */
function UpdatesSection({ whatsNew }: { whatsNew: NonNullable<SettingsProps["whatsNew"]> }) {
  const [open, setOpen] = useState(false);
  return (
    <section className="settings-section" aria-label="Updates">
      <h3 className="view-day">Updates</h3>
      <div className="settings-group">
        <button type="button" className="settings-action" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
          {open ? "Hide what's new" : "What's new"}
        </button>
        {open && (
          <ul className="whats-new-list settings-whats-new">
            {whatsNew.entries.length === 0 && <li className="whats-new-row whats-new-sub">Nothing yet.</li>}
            {whatsNew.entries.map((e) => (
              <ChangeRow key={e.id} entry={e} />
            ))}
          </ul>
        )}
        <div className="settings-row">
          <label className="settings-time">
            Show after updates
            <input
              type="checkbox"
              checked={!whatsNew.muted}
              onChange={(e) => {
                whatsNew.setMuted(!e.target.checked);
                // A card waiting behind this sheet must not appear the moment
                // it closes, just after being turned off.
                if (!e.target.checked) whatsNew.acknowledge();
              }}
            />
          </label>
        </div>
      </div>
      <p className="settings-footer">When Marlow is updated, what changed appears the next time you open the app.</p>
    </section>
  );
}

/** Web-only: a browser asks for push permission itself, from a user gesture.
 * Left out once it's on, when the row could do nothing (Mike, 2026-11-06).
 * Blocked or failed stays: that line is the only explanation there is. */
function NotificationsSection({ api }: { api: Api }) {
  const push = usePushState();
  if (push === "unsupported" || push === "on") return null;
  return (
    <section className="settings-section" aria-label="Notifications">
      <h3 className="view-day">Notifications</h3>
      <div className="settings-group">
        <button
          type="button"
          className="settings-action"
          disabled={push === "denied" || push === "failed"}
          onClick={() => void enablePush(api)}
        >
          {push === "denied"
            ? "Notifications are blocked in the browser"
            : push === "failed"
              ? "This browser's notifications can't be turned on"
              : "Turn on notifications"}
        </button>
      </div>
    </section>
  );
}

/** Server state, no device copy and no offline queue: the box is the only
 * source, so a failed save reverts rather than waiting (unlike face/effort,
 * lib/preferenceSync.ts). The section stays out until the box has answered,
 * because showing 8:00/23:00 before then could show a wrong time. */
function YourDaySection({ api }: { api: Api }) {
  const [good, setGood] = useState<DayTimes | null>(null);
  const [shown, setShown] = useState<DayTimes | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const ctl = new AbortController();
    // An `api` without the call (a test double) or a box too old to send the
    // fields just leaves the section out.
    Promise.resolve()
      .then(() => api.dayTimes(ctl.signal))
      .then((t) => {
        setGood(t);
        setShown(t);
      })
      .catch(() => {});
    return () => ctl.abort();
  }, [api]);

  if (!good || !shown) return null;

  // Posts only the changed field and shows whatever the box answers with, the
  // whole object. `refusal` words an HttpError; a transport failure is the
  // connection's fault either way.
  const save = (
    next: DayTimes,
    update: Parameters<Api["setDayTimes"]>[0],
    refusal: (e: HttpError) => string,
  ) => {
    setError(null);
    setShown(next);
    void api
      .setDayTimes(update)
      .then((t) => {
        setGood(t);
        setShown(t);
      })
      .catch((e: unknown) => {
        setShown(good);
        setError(e instanceof HttpError ? refusal(e) : DAY_OFFLINE);
      });
  };

  const change = (key: "dayStart" | "dayEnd", value: string) => {
    // A cleared input is mid-edit, not a time.
    if (!isClockTime(value) || value === shown[key]) return;
    save(
      { ...shown, [key]: value },
      key === "dayStart" ? { day_start: value } : { day_end: value },
      (e) => dayErrorMessage(e.detail),
    );
  };

  const changeCheckin = (hours: number) => {
    if (hours === shown.checkinEveryHours) return;
    save({ ...shown, checkinEveryHours: hours }, { checkin_every_hours: hours }, () => CHECKIN_SAVE_FAILED);
  };

  const changeRenudge = (on: boolean) => {
    if (on === shown.renudge) return;
    save({ ...shown, renudge: on }, { renudge: on }, () => CHECKIN_SAVE_FAILED);
  };

  return (
    <section className="settings-section" aria-label="Your day">
      <h3 className="view-day">Your day</h3>
      <div className="settings-group">
        <TimeRow label="Starts" value={shown.dayStart} onChange={(v) => change("dayStart", v)} />
        <TimeRow label="Ends" value={shown.dayEnd} onChange={(v) => change("dayEnd", v)} />
        {/* Absent from a box that predates check-ins. */}
        {shown.checkinEveryHours !== null && (
          <div className="settings-row">
            <label className="settings-time">
              Check-ins
              <select value={shown.checkinEveryHours} onChange={(e) => changeCheckin(Number(e.target.value))}>
                {CHECKIN_HOURS.map((h) => (
                  <option key={h} value={h}>
                    {checkinLabel(h)}
                  </option>
                ))}
              </select>
            </label>
          </div>
        )}
        {/* Absent from a box that predates to-do reminders. */}
        {shown.renudge != null && (
          <div className="settings-row">
            <label className="settings-time">
              Repeat reminders daily
              <input type="checkbox" checked={shown.renudge} onChange={(e) => changeRenudge(e.target.checked)} />
            </label>
          </div>
        )}
      </div>
      <p className="settings-footer">Marlow tidies up after your day ends, and checks your inbox during it.</p>
      {shown.renudge != null && (
        <p className="settings-footer">
          If you don't act on a reminder, it comes back a day later until it's done, dropped or snoozed.
        </p>
      )}
      {error && (
        <p className="settings-footer settings-error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}

/** The browser draws the time in the device's 12h/24h style; the value is
 * always 24h "HH:MM", which is what the box takes. */
function TimeRow({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
  return (
    <div className="settings-row">
      <label className="settings-time">
        {label}
        <input type="time" step={900} value={value} onChange={(e) => onChange(e.target.value)} />
      </label>
    </div>
  );
}
