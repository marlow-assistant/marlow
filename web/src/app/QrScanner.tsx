import jsQR from "jsqr";
import { useEffect, useRef } from "react";
import { inviteFromText } from "../lib/pairLink";

/** True where a page can ask for the camera at all (secure context, API present). */
export function canScan(): boolean {
  return typeof navigator !== "undefined" && !!navigator.mediaDevices?.getUserMedia;
}

const FRAME_MS = 150;
// jsQR's cost grows with pixels and a QR on a phone screen is large in frame,
// so a downscaled copy decodes as well and keeps a low-end phone responsive.
const MAX_SIDE = 640;

/** Opens the rear camera and reads frames until one holds a pairing link, then
 * hands over the invite. The home-screen app on iOS can't be reached by the
 * system camera's "open in browser" (no URL handoff to a web app, and its
 * storage is separate from the browser's), so it has to read the code itself.
 * `BarcodeDetector` is absent from WebKit, hence jsQR. A code that isn't ours
 * is ignored and the camera keeps looking. */
export function QrScanner({ onInvite, onError }: { onInvite: (invite: string) => void; onError: () => void }) {
  const video = useRef<HTMLVideoElement>(null);
  // Held in refs so a parent re-render doesn't restart the camera.
  const cb = useRef({ onInvite, onError });
  cb.current = { onInvite, onError };

  useEffect(() => {
    let stopped = false;
    let stream: MediaStream | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const canvas = document.createElement("canvas");

    const stop = () => {
      stopped = true;
      clearTimeout(timer);
      stream?.getTracks().forEach((t) => t.stop());
    };

    const tick = () => {
      if (stopped) return;
      const v = video.current;
      const w = v?.videoWidth ?? 0;
      const h = v?.videoHeight ?? 0;
      if (v && w && h) {
        const scale = Math.min(1, MAX_SIDE / Math.max(w, h));
        canvas.width = Math.round(w * scale);
        canvas.height = Math.round(h * scale);
        const ctx = canvas.getContext("2d", { willReadFrequently: true });
        if (ctx) {
          ctx.drawImage(v, 0, 0, canvas.width, canvas.height);
          const img = ctx.getImageData(0, 0, canvas.width, canvas.height);
          const code = jsQR(img.data, img.width, img.height);
          const invite = code ? inviteFromText(code.data) : null;
          if (invite) {
            stop();
            cb.current.onInvite(invite);
            return;
          }
        }
      }
      timer = setTimeout(tick, FRAME_MS);
    };

    navigator.mediaDevices
      .getUserMedia({ video: { facingMode: "environment" } })
      .then(async (s) => {
        if (stopped) {
          s.getTracks().forEach((t) => t.stop());
          return;
        }
        stream = s;
        const v = video.current;
        if (v) {
          v.srcObject = s;
          await v.play();
        }
        tick();
      })
      .catch(() => {
        if (!stopped) {
          stop();
          cb.current.onError();
        }
      });

    return stop;
  }, []);

  // playsInline + muted: without them iOS opens its fullscreen player.
  return <video ref={video} className="pairing-scan" playsInline muted aria-label="Camera view for scanning the pairing code" />;
}
