import { useEffect, useRef, useState, type ChangeEvent, type ClipboardEvent, type KeyboardEvent, type RefObject } from "react";
import { useMenuDismiss } from "./CopyMenu";
import type { Api } from "../lib/api";
import type { ChatStore } from "../lib/chatStore";
import { Dictation, PartialTranscriptError, transcribeErrorText } from "../lib/dictation";
import { downscaleToJpeg, ImageUnsupportedError, MAX_IMAGES } from "../lib/imageDownscale";
import { Microphone } from "../lib/segmentRecorder";
import { unlockAudio } from "./feedback";

/** Speech to text through the box (docs/tasks/voice-mode-android.md). Null
 * when the box or this browser can't. */
export interface ComposerVoice {
  api: Api;
  /** The mic: dictate into the draft. */
  dictation: boolean;
  /** The voice mode button, in send's slot when there is nothing to send. */
  voiceMode: boolean;
  onVoiceMode: () => void;
  /** Recording or transcribing, so the chat holds its own sounds. */
  onDictating: (on: boolean) => void;
}

interface StagedImage {
  id: string;
  blob: Blob;
  url: string;
}

type Dictating = "idle" | "recording" | "transcribing";

const MAX_LINES = 5;

/** Section 16: iOS has no keyboard-send rule. On a device with a fine pointer
 * (a desktop) Enter sends and Shift-Enter is a newline, the usual convention;
 * Enter during IME composition is ignored. On a touch-first device Enter is a
 * newline, as on iOS, because a soft keyboard's Return is where people put
 * line breaks and the send button is right there. */
function enterSends(): boolean {
  return typeof matchMedia === "function" && matchMedia("(pointer: fine)").matches;
}

/** docs/tasks/ios-image-input.md step 2, web half. A plain `accept="image/*"`
 * input was assumed to offer Camera in Android Chrome's chooser; on a current
 * phone it opens the system photo picker, which has none (seen on the Android
 * user's phone, 2026-10-23). So a touch device gets a menu with a separate
 * `capture` input. A desktop keeps the one-click file dialog: it ignores
 * `capture`, and "Take photo" would open the same dialog again. */
function offersCamera(): boolean {
  return typeof matchMedia === "function" && matchMedia("(pointer: coarse)").matches;
}

function AttachMenu({
  wrapRef,
  onCamera,
  onLibrary,
  onClose,
}: {
  wrapRef: RefObject<HTMLDivElement | null>;
  onCamera: () => void;
  onLibrary: () => void;
  onClose: () => void;
}) {
  // Dismissal watches the wrapper, not just the menu, so a tap on "+" itself
  // toggles the menu shut instead of closing it and reopening it.
  useMenuDismiss(wrapRef, onClose, { anchored: true });
  return (
    <div className="copy-menu attach-menu" role="menu" data-testid="attach-menu">
      <button type="button" role="menuitem" onClick={onCamera}>
        <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M3 8.5A1.5 1.5 0 0 1 4.5 7H7l1.5-2.5h7L17 7h2.5A1.5 1.5 0 0 1 21 8.5v9a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 17.5z" />
          <circle cx="12" cy="13" r="3.5" />
        </svg>
        Take photo
      </button>
      <button type="button" role="menuitem" onClick={onLibrary}>
        <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <rect x="3" y="6" width="14" height="12" rx="1.5" />
          <path d="M7 6V4.5A1.5 1.5 0 0 1 8.5 3H19.5A1.5 1.5 0 0 1 21 4.5v10a1.5 1.5 0 0 1-1.5 1.5H17" />
          <circle cx="7.5" cy="10" r="1.2" />
          <path d="m3.5 17 4.5-4.5 3.5 3.5 2-2 3.5 3" />
        </svg>
        Choose photo
      </button>
    </div>
  );
}

export function Composer({ store, voice = null }: { store: ChatStore; voice?: ComposerVoice | null }) {
  const [text, setText] = useState("");
  const [dictating, setDictating] = useState<Dictating>("idle");
  const dictationRef = useRef<{ dictation: Dictation; microphone: Microphone } | null>(null);
  const dictatingRef = useRef<Dictating>("idle");
  dictatingRef.current = dictating;
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [images, setImages] = useState<StagedImage[]>([]);
  const imagesRef = useRef<StagedImage[]>([]);
  imagesRef.current = images;
  const [imageError, setImageError] = useState<string | null>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const cameraRef = useRef<HTMLInputElement>(null);
  const attachWrapRef = useRef<HTMLDivElement>(null);
  const [attachMenuOpen, setAttachMenuOpen] = useState(false);

  // CMP-8: grows from one to five lines, then scrolls.
  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    const line = parseFloat(getComputedStyle(ta).lineHeight) || 20;
    const border = ta.offsetHeight - ta.clientHeight;
    const pad = border + parseFloat(getComputedStyle(ta).paddingTop) * 2;
    const max = line * MAX_LINES + pad;
    // The box is border-box but scrollHeight excludes the border: without adding
    // it back, N lines overflowed their own box by the border width (found by the
    // CMP-8 e2e test: five lines "scrolled", and the sixth grew the field 2px).
    const needed = ta.scrollHeight + border;
    ta.style.height = `${Math.min(needed, max)}px`;
    ta.style.overflowY = needed > max ? "auto" : "hidden";
  }, [text]);

  // Object URLs are revoked when a thumbnail is removed or sent, and here
  // when the composer goes away; not on every list change, because a refused
  // send puts the same thumbnails back.
  useEffect(() => () => imagesRef.current.forEach((i) => URL.revokeObjectURL(i.url)), []);

  const canSend = (text.trim() !== "" || images.length > 0) && dictating === "idle";

  const onDictating = voice?.onDictating;
  useEffect(() => onDictating?.(dictating !== "idle"), [dictating, onDictating]);

  // Leaving the chat drops a recording; hiding the page ends it and keeps
  // what was said, since a phone stops a hidden page's microphone anyway.
  // Only while recording: one already transcribing is finishing by itself.
  useEffect(
    () => () => {
      stopDictation();
      if (noticeTimer.current) clearTimeout(noticeTimer.current);
    },
    [],
  );
  const finishRef = useRef<() => Promise<void>>(async () => {});
  finishRef.current = finishDictation;
  useEffect(() => {
    const onHidden = () => {
      if (document.visibilityState === "hidden" && dictatingRef.current === "recording") void finishRef.current();
    };
    document.addEventListener("visibilitychange", onHidden);
    return () => document.removeEventListener("visibilitychange", onHidden);
  }, []);

  function stopDictation() {
    dictationRef.current?.dictation.cancel();
    dictationRef.current?.microphone.close();
    dictationRef.current = null;
  }

  async function startDictation() {
    if (!voice) return;
    unlockAudio();
    const microphone = new Microphone();
    const dictation = new Dictation(voice.api, microphone);
    dictationRef.current = { dictation, microphone };
    setDictating("recording");
    try {
      await dictation.begin();
    } catch (e) {
      if (dictationRef.current?.dictation !== dictation) return;
      stopDictation();
      setDictating("idle");
      showNotice(transcribeErrorText(e), 4000);
    }
  }

  async function finishDictation() {
    const current = dictationRef.current;
    if (!current || dictatingRef.current !== "recording") return;
    dictatingRef.current = "transcribing";
    setDictating("transcribing");
    const insert = (heard: string) => {
      if (heard) setText((t) => (t.trim() ? `${t.trimEnd()} ${heard}` : heard));
    };
    try {
      const heard = await current.dictation.finish();
      if (dictationRef.current !== current) return;
      insert(heard);
    } catch (e) {
      if (dictationRef.current !== current) return;
      // What was heard before a segment failed still goes in the draft, where
      // it can be read before it is sent; voice mode, which sends at once,
      // drops it instead.
      if (e instanceof PartialTranscriptError) insert(e.heard.trim());
      showNotice(transcribeErrorText(e), 4000);
    } finally {
      if (dictationRef.current === current) {
        current.microphone.close();
        dictationRef.current = null;
        setDictating("idle");
        taRef.current?.focus();
      }
    }
  }

  /** One notice line, shared by the photo and the mic, on one timer so an
   * older message's clear can't cut a newer one short. */
  function showNotice(message: string | null, ms = 3000) {
    if (noticeTimer.current) clearTimeout(noticeTimer.current);
    noticeTimer.current = null;
    setImageError(message);
    if (message) noticeTimer.current = setTimeout(() => setImageError(null), ms);
  }

  /** Appends, up to MAX_IMAGES. Over the cap the first ones that fit are
   * kept and a notice says the rest were left out. */
  async function attach(files: (File | Blob)[]) {
    showNotice(null);
    const room = Math.max(0, MAX_IMAGES - imagesRef.current.length);
    // Kept so a per-file failure below doesn't replace it: the user must still
    // learn the rest were left out.
    let capNote = "";
    if (files.length > room) {
      capNote =
        room === 0
          ? `You can attach up to ${MAX_IMAGES} photos.`
          : `Only the first ${room} added: up to ${MAX_IMAGES} photos per message.`;
      showNotice(capNote, 4000);
    }
    for (const file of files.slice(0, room)) {
      try {
        const blob = await downscaleToJpeg(file);
        const url = URL.createObjectURL(blob);
        const accepted = imagesRef.current.length < MAX_IMAGES;
        if (!accepted) {
          URL.revokeObjectURL(url);
          break;
        }
        // The ref moves on at once, so a second pick landing before React
        // renders still sees this one when it counts.
        imagesRef.current = [...imagesRef.current, { id: crypto.randomUUID(), blob, url }];
        setImages(imagesRef.current);
      } catch (e) {
        const why = e instanceof ImageUnsupportedError ? e.message : "Couldn't read that image.";
        showNotice(capNote ? `${why} ${capNote}` : why, capNote ? 4000 : 3000);
      }
    }
  }

  function removeImage(id: string) {
    const gone = imagesRef.current.find((i) => i.id === id);
    if (gone) URL.revokeObjectURL(gone.url);
    imagesRef.current = imagesRef.current.filter((i) => i.id !== id);
    setImages(imagesRef.current);
  }

  async function send() {
    if (!canSend) return;
    unlockAudio();
    // CMP-1: read and clear text and images in one step; put both back if the
    // send refuses.
    const draft = text;
    const staged = imagesRef.current;
    setText("");
    imagesRef.current = [];
    setImages([]);
    const ok = await store.send(draft, staged.map((i) => i.blob));
    if (ok) staged.forEach((i) => URL.revokeObjectURL(i.url));
    else {
      setText(draft);
      const merged = [...staged, ...imagesRef.current];
      // Photos added while the send was in flight can push past the cap: drop
      // the newest, free their blob URLs, and say so.
      merged.slice(MAX_IMAGES).forEach((i) => URL.revokeObjectURL(i.url));
      if (merged.length > MAX_IMAGES) showNotice(`Only ${MAX_IMAGES} photos fit; the last ones were removed.`, 4000);
      imagesRef.current = merged.slice(0, MAX_IMAGES);
      setImages(imagesRef.current);
    }
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key !== "Enter" || e.shiftKey || e.nativeEvent.isComposing || e.keyCode === 229) return;
    if (!enterSends()) return;
    e.preventDefault();
    void send();
  }

  /** Both file inputs, the library and the camera, land here. The camera
   * input has no `multiple`, so it hands over one file. */
  function onPicked(e: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(e.target.files ?? []);
    if (files.length > 0) void attach(files);
    e.target.value = "";
  }

  function onPaste(e: ClipboardEvent<HTMLTextAreaElement>) {
    // CMP-5: the clipboard is read only when the user pastes.
    const files = Array.from(e.clipboardData.files).filter((f) => f.type.startsWith("image/"));
    if (files.length > 0) {
      e.preventDefault();
      void attach(files);
    }
  }

  return (
    <div className="composer">
      {imageError && <div className="notice">{imageError}</div>}
      {images.length > 0 && (
        <div className="preview-row" data-testid="preview-row">
          {images.map((image) => (
            <div className="preview" data-testid="preview" key={image.id}>
              <img src={image.url} alt="Attached" width={44} height={44} />
              <button
                type="button"
                className="preview-remove"
                aria-label="Remove photo"
                onClick={() => removeImage(image.id)}
              >
                ×
              </button>
            </div>
          ))}
        </div>
      )}
      <div className="composer-row">
        <div className="attach-wrap" ref={attachWrapRef}>
          <button
            type="button"
            className="attach"
            aria-label="Add a photo"
            aria-haspopup={offersCamera() ? "menu" : undefined}
            aria-expanded={offersCamera() ? attachMenuOpen : undefined}
            onClick={() => (offersCamera() ? setAttachMenuOpen((o) => !o) : fileRef.current?.click())}
          >
            +
          </button>
          {attachMenuOpen && (
            <AttachMenu
              wrapRef={attachWrapRef}
              onCamera={() => {
                setAttachMenuOpen(false);
                cameraRef.current?.click();
              }}
              onLibrary={() => {
                setAttachMenuOpen(false);
                fileRef.current?.click();
              }}
              onClose={() => setAttachMenuOpen(false)}
            />
          )}
        </div>
        <input ref={fileRef} type="file" accept="image/*" multiple hidden data-testid="file-input" onChange={onPicked} />
        <input
          ref={cameraRef}
          type="file"
          accept="image/*"
          capture="environment"
          hidden
          data-testid="camera-input"
          onChange={onPicked}
        />
        <textarea
          ref={taRef}
          rows={1}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
          placeholder={PLACEHOLDER[dictating]}
          aria-label="Message"
          enterKeyHint={enterSends() ? "send" : "enter"}
          readOnly={dictating !== "idle"}
        />
        {voice?.dictation && (
          <button
            type="button"
            className={`mic mic-${dictating}`}
            aria-label={dictating === "recording" ? "Stop dictating" : "Dictate"}
            aria-pressed={dictating === "recording"}
            disabled={dictating === "transcribing"}
            onClick={() => void (dictating === "idle" ? startDictation() : finishDictation())}
            data-testid="mic"
          >
            {dictating === "transcribing" ? <span className="spinner" aria-hidden="true" /> : "🎙"}
          </button>
        )}
        {canSend ? (
          <button type="button" className="send" aria-label="Send" onClick={() => void send()}>
            ↑
          </button>
        ) : (
          voice?.voiceMode &&
          dictating === "idle" && (
            <button
              type="button"
              className="send voice-open"
              aria-label="Voice mode"
              title="Talk to Marlow and hear the replies read aloud"
              onClick={() => {
                unlockAudio();
                voice.onVoiceMode();
              }}
              data-testid="voice-open"
            >
              〰
            </button>
          )
        )}
      </div>
    </div>
  );
}

const PLACEHOLDER: Record<Dictating, string> = {
  idle: "Message Marlow",
  recording: "Listening…",
  transcribing: "Transcribing…",
};
