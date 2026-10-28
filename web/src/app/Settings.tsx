import { useEffect, useMemo, useRef, useState } from "react";
import { HttpError, type Api } from "../lib/api";
import type { Effort } from "../lib/chatStore";
import {
  CHECKIN_HOURS,
  CHECKIN_SAVE_FAILED,
  checkinLabel,
  DAY_OFFLINE,
  dayErrorMessage,
  isClockTime,
  type DayTimes,
} from "../lib/dayFormat";
import type { Face } from "../lib/faces";
import type { LocationSharing, LocationStatus } from "../lib/location";
import { capitalizedModelName } from "../lib/modelName";
import { browserVoiceEngine, choosableVoices, previewVoice, voiceKey, type VoiceEngine } from "../lib/browserSpeech";
import { KV_TEXT_SIZE, KV_VOICE, kvDelete, kvGet, kvSet } from "../lib/storage";
import { applyTextSize, currentTextSize, TEXT_SIZES } from "../lib/textSize";
import { FaceGrid } from "./Face";
import { enablePush, usePushState } from "./push";
import type { WhatsNewModel } from "./useWhatsNew";
import { ChangeRow } from "./WhatsNew";

const LEVELS: { value: Effort; label: string }[] = [
  { value: "low", label: "Low" },
  { value: "medium", label: "Medium" },
  { value: "high", label: "High" },
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

/** Port of iOS `SettingsView`: Status, Intelligence, Build, Unpair, plus a
 * web-only Notifications section (iOS gets its permission from the OS). */
export function Settings(p: SettingsProps) {
  const [confirming, setConfirming] = useState(false);
  // Once per sheet: a new engine object each render would resubscribe the
  // Voice section's listener every time.
  const voiceEngine = useMemo(
    () => (p.voiceEngine === undefined ? browserVoiceEngine() : p.voiceEngine),
    [p.voiceEngine],
  );
  const connection = p.reauthNeeded ? "Needs reauthorization" : p.queuePaused ? "Paused" : "Connected";

  return (
    <div className="sheet" role="dialog" aria-label="Settings">
      <header className="sheet-header">
        <h2>Settings</h2>
        <button type="button" onClick={p.onClose}>
          Done
        </button>
      </header>
      <div className="sheet-body settings-body">
        <section className="settings-section" aria-label="Status">
          <h3 className="view-day">Status</h3>
          <dl className="settings-group">
            <Row label="Server" value={p.host ?? location.host} />
            <Row label="Connection" value={connection} />
            {p.token && <Row label="Device" value={p.token.slice(-6)} />}
          </dl>
        </section>

        {/* Shown large, as the avatar's own crop: monocle against glasses is
            part of the choice, and at the header's 52px it is hard to see.
            iOS puts this one level down (Settings > Appearance); a sheet here has
            the room to show the grid inline. No icon footer: a web app's
            home-screen icon is fixed by its manifest. */}
        <section className="settings-section" aria-label="Marlow">
          <h3 className="view-day">Marlow</h3>
          <FaceGrid face={p.face} onFace={p.onFace} />
        </section>

        <TextSizeSection />

        <VoiceSection engine={voiceEngine} />

        {/* Effort is a LATENCY dial on this workload, not a cost one: measured
            at ~2,900 thinking tokens against 5.8M cache reads in a day
            (docs/tasks/model-and-effort-config.md). The footer says so plainly
            rather than letting the control imply savings. */}
        <section className="settings-section" aria-label="Intelligence">
          <h3 className="view-day">Intelligence</h3>
          <div className="settings-group">
            <div className="segmented" role="radiogroup" aria-label="Thinking">
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
            {/* Read-only, in the same section so the header's "Sonnet/Low" is
                legible here. There is no model picker on purpose: switching
                models pays a full cold prefix read. */}
            <dl className="settings-rows">
              <Row label="Model" value={p.serverModel ? capitalizedModelName(p.serverModel) : "—"} />
            </dl>
          </div>
          <p className="settings-footer">
            Higher effort makes replies slower, not more expensive. Applies to your next message.
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

        {/* Both sides, deliberately adjacent and in the same format: the
            question this answers is "do these two agree?", which is exactly
            what a branch deploy makes worth asking. */}
        <section className="settings-section" aria-label="Build">
          <h3 className="view-day">Build</h3>
          <dl className="settings-group">
            <Row label="App" value={p.webBuild ?? __WEB_BUILD__} />
            <Row label="Server" value={p.serverBuild ?? "—"} />
          </dl>
        </section>

        <section className="settings-section">
          <div className="settings-group">
            <button type="button" className="settings-danger" onClick={() => setConfirming(true)}>
              Unpair
            </button>
          </div>
        </section>
      </div>

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

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="settings-row">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

/** Web-only: iOS text follows the system's Dynamic Type setting, which a
 * browser does not pass to a page (lib/textSize.ts). */
function TextSizeSection() {
  const [size, setSize] = useState(currentTextSize);
  return (
    <section className="settings-section" aria-label="Text size">
      <h3 className="view-day">Text size</h3>
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
                setSize(s);
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

/** Voice mode's reading voice, from the browser's own voices. The native app
 * has its own picker, over Phonon's neural voices
 * (docs/tasks/voice-mode-tts-engines.md); a browser can't run Phonon, so this
 * one lists `speechSynthesis` voices instead. On iOS Safari those are only the
 * basic built-in voices, all of which sounded bad to Mike (2026-10-25): this
 * picks the least bad there. On Android it is Google's voices. Kept on this
 * device only, like text size: the voices are this device's. Hidden until the
 * engine lists any, which on Android Chrome is seconds after load. */
function VoiceSection({ engine }: { engine: VoiceEngine | null }) {
  const [voices, setVoices] = useState(() => (engine ? choosableVoices(engine.synth.getVoices()) : []));
  const [chosen, setChosen] = useState<string | null>(null);
  // A tap before the stored choice has loaded wins over it.
  const tapped = useRef(false);

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

  if (!engine || voices.length === 0) return null;

  const choose = (voice: SpeechSynthesisVoice | null) => {
    tapped.current = true;
    setChosen(voice ? voiceKey(voice) : null);
    if (voice) {
      previewVoice(engine, voice);
      void kvSet(KV_VOICE, voiceKey(voice)).catch(() => {});
    } else {
      engine.synth.cancel();
      void kvDelete(KV_VOICE).catch(() => {});
    }
  };
  // A stored voice since uninstalled reads as Automatic, which is what
  // pickVoice falls back to.
  const current = voices.some((v) => voiceKey(v) === chosen) ? chosen : null;

  return (
    <section className="settings-section" aria-label="Voice">
      <h3 className="view-day">Voice</h3>
      <div className="settings-group" role="radiogroup" aria-label="Voice">
        <button type="button" role="radio" aria-checked={current === null} className="voice-option" onClick={() => choose(null)}>
          <span>Automatic</span>
          <span className="voice-option-lang" />
        </button>
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
            <span className="voice-option-lang">{v.lang}</span>
          </button>
        ))}
      </div>
      <p className="settings-footer">
How voice mode reads replies on this device. Tap a voice to hear it.</p>
    </section>
  );
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

/** Web-only: a browser asks for push permission itself, from a user gesture. */
function NotificationsSection({ api }: { api: Api }) {
  const push = usePushState();
  if (push === "unsupported") return null;
  return (
    <section className="settings-section" aria-label="Notifications">
      <h3 className="view-day">Notifications</h3>
      <div className="settings-group">
        <button
          type="button"
          className="settings-action"
          disabled={push === "on" || push === "denied" || push === "failed"}
          onClick={() => void enablePush(api)}
        >
          {push === "on"
            ? "Notifications are on"
            : push === "denied"
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
      </div>
      <p className="settings-footer">Marlow tidies up after your day ends, and checks your inbox during it.</p>
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
