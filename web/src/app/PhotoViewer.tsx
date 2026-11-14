import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { ChatStore } from "../lib/chatStore";
import { useImageUrl } from "./hooks";

/** A message's photos full screen, opened by tapping one in its bubble (Mike,
 * 2026-11-14). It shows the copy that was sent, downscaled to 1568px on the
 * long edge: there is no larger one on the box or the phone.
 *
 * `keys` are the message's loaded photos only, so a grey square still
 * downloading is not a stop. The viewer holds the key it shows, not an
 * index, so a photo landing while it is open doesn't change the one on
 * screen. Arrows, ←/→ and a one-finger sideways swipe step through them;
 * Escape, a tap, the ✕ and Back close it.
 *
 * Back: on Android the system Back is the way out of anything full screen,
 * and with no history entry of its own it would leave the app. So opening
 * pushes one, Back pops it, and every other way of closing goes through
 * `history.back()` once, so the entry never outlives the viewer and nothing
 * before it is popped. */
export function PhotoViewer({
  store,
  keys,
  start,
  onClose,
}: {
  store: ChatStore;
  keys: string[];
  start: string;
  onClose: () => void;
}) {
  const [current, setCurrent] = useState(start);
  const found = keys.indexOf(current);
  const index = found >= 0 ? found : 0;
  const url = useImageUrl(store, keys[index]);
  const count = keys.length;
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  /** Whether our history entry is still on the stack. */
  const pushed = useRef(false);
  /** Set by the first close, so a second (a bubbled click) does nothing. */
  const closing = useRef(false);
  /** Whether the effect is mounted: StrictMode runs it, cleans up and runs it
   * again, and only an unmount that stays unmounted takes the entry. */
  const alive = useRef(false);
  const closeButton = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    alive.current = true;
    if (!pushed.current) {
      history.pushState({ ...(history.state ?? {}), photoViewer: true }, "");
      pushed.current = true;
    }
    const onPop = () => {
      pushed.current = false;
      closeRef.current();
    };
    window.addEventListener("popstate", onPop);
    const opener = document.activeElement as HTMLElement | null;
    closeButton.current?.focus();
    return () => {
      alive.current = false;
      window.removeEventListener("popstate", onPop);
      opener?.focus?.();
      // Unmounted some other way (the row went away): take the entry too,
      // unless this was StrictMode's rehearsal and the effect is back.
      setTimeout(() => {
        if (!alive.current && pushed.current) {
          pushed.current = false;
          history.back();
        }
      }, 0);
    };
  }, []);

  const close = () => {
    if (closing.current) return;
    closing.current = true;
    if (pushed.current) history.back(); // popstate closes
    else closeRef.current();
  };
  const step = (by: number) => {
    const next = Math.min(count - 1, Math.max(0, index + by));
    setCurrent(keys[next]);
  };
  const stepRef = useRef(step);
  stepRef.current = step;
  const closeFn = useRef(close);
  closeFn.current = close;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        closeFn.current();
      } else if (e.key === "ArrowLeft") stepRef.current(-1);
      else if (e.key === "ArrowRight") stepRef.current(1);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  /** Where a one-finger touch began; null for none, or once a second finger
   * makes it a pinch, which zooms and never steps. */
  const touchX = useRef<number | null>(null);

  return createPortal(
    <div
      className="photo-viewer"
      data-testid="photo-viewer"
      role="dialog"
      aria-modal="true"
      aria-label="Photo"
      onClick={close}
      onTouchStart={(e) => (touchX.current = e.touches.length === 1 ? e.touches[0].clientX : null)}
      onTouchEnd={(e) => {
        const from = touchX.current;
        touchX.current = null;
        if (e.touches.length > 0) return; // a finger is still down: a pinch
        const to = e.changedTouches[0]?.clientX;
        if (from == null || to == null || Math.abs(to - from) < 50) return;
        e.preventDefault(); // a swipe, not a tap: no click to close
        step(to < from ? 1 : -1);
      }}
    >
      {url ? <img src={url} alt={count > 1 ? `Photo ${index + 1} of ${count}` : "Photo"} /> : null}
      <button
        ref={closeButton}
        type="button"
        className="photo-viewer-close"
        aria-label="Close"
        onClick={(e) => {
          e.stopPropagation();
          close();
        }}
      >
        ✕
      </button>
      {count > 1 && (
        <>
          <div className="photo-viewer-count" aria-live="polite">
            {index + 1} of {count}
          </div>
          <button
            type="button"
            className="photo-viewer-step photo-viewer-prev"
            aria-label="Previous photo"
            disabled={index === 0}
            onClick={(e) => {
              e.stopPropagation();
              step(-1);
            }}
          >
            ‹
          </button>
          <button
            type="button"
            className="photo-viewer-step photo-viewer-next"
            aria-label="Next photo"
            disabled={index === count - 1}
            onClick={(e) => {
              e.stopPropagation();
              step(1);
            }}
          >
            ›
          </button>
        </>
      )}
    </div>,
    document.body,
  );
}
