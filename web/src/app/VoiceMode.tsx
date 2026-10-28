import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import type { Api } from "../lib/api";
import { BrowserSpeech } from "../lib/browserSpeech";
import type { ChatStore } from "../lib/chatStore";
import { VoiceDictation } from "../lib/dictation";
import { Microphone } from "../lib/segmentRecorder";
import { loadPhonon, PhononSpeech, usesPhonon } from "../lib/phononSpeech";
import { KV_VOICE, kvGet } from "../lib/storage";
import { VoiceDebugLog } from "../lib/voiceDebug";
import { VoiceModeController, type VoicePhase } from "../lib/voiceMode";
import { playEarcon, unlockAudio } from "./feedback";
import { useChatState } from "./hooks";

/** Voice mode's screen (docs/tasks/voice-mode-android.md, after iOS
 * `VoiceModeView`): everything else goes away and one big button takes its
 * place, like a car-mode screen — usable at a glance, with a thumb. Press to
 * talk, press to send, press to interrupt. Leaving puts the user back in the
 * chat, where every voice turn is already an ordinary bubble: they went
 * through the same send and the same server transcript.
 *
 * An overlay inside Chat, under the approval card, so a request that arrives
 * mid-conversation still shows, with its tap, on this screen. It owns the
 * mode's lifetime: the microphone, the screen staying awake and the controller
 * all start when it mounts and end when it goes. */
export function VoiceMode({ store, api, onClose }: { store: ChatStore; api: Api; onClose: () => void }) {
  const [error, setError] = useState<string | null>(null);
  const [level, setLevel] = useState(0);

  // Built in the effect, not a memo, so each mount owns what it built and
  // tears down only that: StrictMode's mount-unmount-mount in development
  // otherwise closed the speech the second mount was still using.
  const [controller, setController] = useState<VoiceModeController | null>(null);
  // Outlives a StrictMode remount on purpose: it holds only timings.
  const debugLog = useRef(new VoiceDebugLog());
  const speechRef = useRef<PhononSpeech | null>(null);
  // The download's progress, 0 to 1, while there is one to show.
  const [download, setDownload] = useState<number | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const state = useChatState(store);

  useEffect(() => {
    // The microphone opens on entry and stays open, so a press records at
    // once and nothing from before it.
    const microphone = new Microphone();
    const log = debugLog.current;
    const browser = new BrowserSpeech();
    const speech = new PhononSpeech({
      fallback: browser,
      load: loadPhonon,
      makeContext: () => new AudioContext(),
      now: () => performance.now(),
    });
    speechRef.current = speech;
    speech.onSpeak = () => log.latest?.spoke();
    speech.onSynthesized = (ms, seconds) => log.latest?.synthesized(ms, seconds);
    speech.onLoad = (status) => setDownload(status.state === "loading" ? status.fraction : null);
    // The voice picked in Settings. Read long before the first reply, which
    // needs a press, a recording and a turn first. Phonon is only started,
    // and its model only fetched, for someone who reads with it; until then,
    // and for everyone else, replies are the browser's voice.
    void kvGet<string>(KV_VOICE)
      .catch(() => null)
      .then((v) => {
        const stored = v ?? null;
        browser.setChosenVoice(stored);
        if (usesPhonon(stored)) speech.start();
      });
    const recorder = new VoiceDictation(
      api,
      microphone,
      setError,
      (l, threshold) => setLevel(Math.min(1, l / Math.max(threshold * 4, 0.05))),
      log,
    );
    const voice = new VoiceModeController({
      recorder,
      speech,
      send: (text) => {
        const id = store.sendSpoken(text);
        if (id) log.latest?.sent();
        return id;
      },
      earcon: playEarcon,
    });
    setController(voice);
    void microphone.open().catch(() => {
      // Said on the first press, where it matters (micErrorText).
    });
    const offProgress = store.onReplyProgress((id, text, marks) => voice.replyProgress(id, text, marks));

    // A car-mode screen: it stays on while the mode is open. The lock goes
    // whenever the page is hidden, so it is asked for again on return.
    let wakeLock: WakeLockSentinel | null = null;
    let left = false;
    const holdAwake = () => {
      if (document.visibilityState !== "visible" || !("wakeLock" in navigator)) return;
      navigator.wakeLock.request("screen").then(
        (lock) => {
          if (left) void lock.release().catch(() => {});
          else wakeLock = lock;
        },
        () => {},
      );
    };
    holdAwake();

    // Nothing keeps listening or speaking in the background: a page can't,
    // and iOS stops there too. Stop cleanly, send nothing, and let go of the
    // mic so the phone's indicator goes off; the next press reopens it.
    const onHidden = () => {
      if (document.visibilityState === "visible") {
        holdAwake();
        return;
      }
      voice.stopAll();
      microphone.close();
    };
    document.addEventListener("visibilitychange", onHidden);
    window.addEventListener("pagehide", onHidden);

    return () => {
      left = true;
      voice.stopAll();
      offProgress();
      document.removeEventListener("visibilitychange", onHidden);
      window.removeEventListener("pagehide", onHidden);
      void wakeLock?.release().catch(() => {});
      speech.close();
      microphone.close();
    };
  }, [store, api]);

  const subscribe = useCallback((fn: () => void) => controller?.subscribe(fn) ?? (() => {}), [controller]);
  const phase = useSyncExternalStore(subscribe, () => controller?.getPhase() ?? "idle");
  useEffect(() => controller?.messagesChanged(state.messages), [controller, state.messages]);
  const approvalId = state.pendingPermission?.request_id ?? null;
  useEffect(() => controller?.approvalChanged(approvalId), [controller, approvalId]);

  const copyDebug = () => {
    const log = debugLog.current;
    log.voice = speechRef.current?.voiceName ?? log.voice;
    log.voices = speechRef.current?.englishVoices ?? log.voices;
    const text = log.format({ build: typeof __WEB_BUILD__ === "string" ? __WEB_BUILD__ : "dev" });
    const done = (label: string) => {
      setCopied(label);
      setTimeout(() => setCopied(null), 2000);
    };
    navigator.clipboard?.writeText(text).then(
      () => done("Copied"),
      () => done("Couldn't copy"),
    ) ?? done("Couldn't copy");
  };

  const press = () => {
    unlockAudio();
    speechRef.current?.unlock();
    controller?.press();
  };

  return (
    <div className="voice-mode" role="dialog" aria-label="Voice mode" data-testid="voice-mode">
      <button type="button" className="voice-close" aria-label="Leave voice mode" onClick={onClose}>
        ✕
      </button>
      <div className="voice-centre">
        <p className="voice-caption" aria-hidden="true">
          {CAPTION[phase]}
        </p>
        <button
          type="button"
          className={`voice-button voice-${phase}`}
          onClick={press}
          disabled={phase === "transcribing"}
          aria-label={LABEL[phase]}
          data-phase={phase}
        >
          <span className="voice-icon" aria-hidden="true">
            {ICON[phase]}
          </span>
          {phase === "recording" && <LevelMeter level={level} />}
        </button>
        {error ? (
          <p className="voice-notice" role="status">
            {error}
          </p>
        ) : (
          // Quiet, and only while there is a download to show. Nothing when it
          // fails: the debug report says why (it reads in the browser's voice).
          download !== null && (
            <p className="voice-notice" role="status">
              Downloading Marlow's voice… {Math.round(download * 100)}%
            </p>
          )
        )}
      </div>
      {/* For a user to paste back to whoever looks after their box: cuts,
          timings and the voice, never the words (lib/voiceDebug.ts). */}
      <button type="button" className="voice-debug" onClick={copyDebug} data-testid="voice-debug">
        {copied ?? "Copy debug info"}
      </button>
    </div>
  );
}

function LevelMeter({ level }: { level: number }) {
  const ref = useRef<HTMLSpanElement>(null);
  // Set through the CSSOM, not a style attribute: the CSP has no
  // 'unsafe-inline' for styles.
  useEffect(() => {
    ref.current?.style.setProperty("--level", String(level));
  }, [level]);
  return (
    <span className="voice-meter" aria-hidden="true">
      <span ref={ref} className="voice-meter-fill" />
    </span>
  );
}

const CAPTION: Record<VoicePhase, string> = {
  idle: "Tap to talk",
  recording: "Listening — tap to send",
  transcribing: "Transcribing…",
  waiting: "Marlow is thinking — tap to interrupt",
  speaking: "Tap to interrupt",
};

const LABEL: Record<VoicePhase, string> = {
  idle: "Talk to Marlow",
  recording: "Send",
  transcribing: "Transcribing",
  waiting: "Interrupt and talk",
  speaking: "Interrupt and talk",
};

const ICON: Record<VoicePhase, string> = {
  idle: "🎙",
  recording: "↑",
  transcribing: "…",
  waiting: "…",
  speaking: "🔊",
};
