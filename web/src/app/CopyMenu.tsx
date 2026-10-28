import { useEffect, useRef, useState, type MouseEvent, type ReactNode, type RefObject } from "react";
import { createPortal } from "react-dom";

/** RND-4: whole-message Copy, the way iOS offers it: press and hold the bubble
 * (or right-click it, or the context-menu key on it) for a one-item menu.
 * Replaced a "Copy" link beside every reply (Mike, 2026-10-02, on the Android
 * emulator). Android Chrome and Firefox both surface a long press as a
 * `contextmenu` event, so one handler covers touch, mouse and keyboard.
 *
 * A reader who has selected part of the text gets the browser's own menu
 * instead, so copying a sentence still works on desktop. */
export function Copyable({ text, className, children }: { text: string; className: string; children: ReactNode }) {
  const [at, setAt] = useState<{ x: number; y: number } | null>(null);
  const onContextMenu = (e: MouseEvent<HTMLDivElement>) => {
    if (at) {
      e.preventDefault();
      return;
    }
    const sel = window.getSelection();
    if (sel && !sel.isCollapsed && sel.anchorNode && e.currentTarget.contains(sel.anchorNode)) return;
    e.preventDefault();
    let x = e.clientX;
    let y = e.clientY;
    if (x === 0 && y === 0) {
      // The context-menu key: no pointer, so anchor to the bubble.
      const r = e.currentTarget.getBoundingClientRect();
      x = r.left + 16;
      y = r.top + 16;
    }
    setAt({ x, y });
  };
  return (
    <div className={`${className} copyable`} tabIndex={0} onContextMenu={onContextMenu}>
      {children}
      {at && <CopyMenu text={text} at={at} onClose={() => setAt(null)} />}
    </div>
  );
}

const MENU_WIDTH = 132;
const MENU_HEIGHT = 44;
const MARGIN = 8;

/** How a small menu goes away: a press outside `ref` or Escape, and, for a
 * menu placed at fixed coordinates, a scroll or a resize that would leave it
 * floating where the thing it belonged to used to be. Focuses the first menu
 * item on open (not the first button: the photo menu watches a wrapper that
 * also holds "+"). Shared by the Copy menu and the composer's photo menu.
 *
 * `anchored`: the menu is positioned in CSS against its control, so it moves
 * with it. It must not close on resize or scroll: with
 * `interactive-widget=resizes-content`, opening the photo menu while the soft
 * keyboard is up moves focus off the textarea, the keyboard closes, and the
 * page resizes and scrolls under the menu that just opened. */
export function useMenuDismiss(
  ref: RefObject<HTMLElement | null>,
  onClose: () => void,
  { anchored = false }: { anchored?: boolean } = {},
) {
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    ref.current?.querySelector<HTMLElement>("[role=menuitem]")?.focus({ preventScroll: true });
    const away = (e: Event) => {
      if (!ref.current?.contains(e.target as Node)) close.current();
    };
    const dismiss = () => close.current();
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") close.current();
    };
    document.addEventListener("pointerdown", away, true);
    if (!anchored) {
      // Capture: the transcript scroller's scroll does not bubble to document.
      document.addEventListener("scroll", dismiss, true);
      window.addEventListener("resize", dismiss);
    }
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("pointerdown", away, true);
      document.removeEventListener("scroll", dismiss, true);
      window.removeEventListener("resize", dismiss);
      document.removeEventListener("keydown", key);
    };
  }, [ref, anchored]);
}

function CopyMenu({ text, at, onClose }: { text: string; at: { x: number; y: number }; onClose: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  const [copied, setCopied] = useState(false);
  useMenuDismiss(ref, onClose);

  // Above the finger, so the hand does not cover it; below if there is no room.
  const left = Math.max(MARGIN, Math.min(at.x - MENU_WIDTH / 2, window.innerWidth - MENU_WIDTH - MARGIN));
  const above = at.y - MENU_HEIGHT - 24;
  const top = above >= MARGIN ? above : Math.min(at.y + 24, window.innerHeight - MENU_HEIGHT - MARGIN);

  return createPortal(
    <div className="copy-menu" role="menu" ref={ref} style={{ left, top }} data-testid="copy-menu">
      <button
        type="button"
        role="menuitem"
        disabled={copied}
        onClick={() => {
          void navigator.clipboard
            ?.writeText(text)
            .then(() => {
              setCopied(true);
              setTimeout(() => close.current(), 700);
            })
            .catch(() => close.current());
        }}
      >
        {copied ? "Copied" : "Copy"}
      </button>
    </div>,
    document.body,
  );
}
