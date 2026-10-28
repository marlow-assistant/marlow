import type { Face } from "../lib/faces";
import { FaceImage } from "./Face";
import type { ChatPresence } from "../lib/presence";

/** Marlow's face over the transcript, with a pill under it that says what the
 * turn in flight is doing (port of iOS `PresenceHeader`).
 *
 * **This replaced the old menu bar**, and took over its jobs. The avatar
 * answers "whose conversation is this". What is new is the working state. The
 * header does not scroll, so "Searching the web…" is visible from anywhere in
 * the transcript.
 *
 * The "Sonnet/Low" model chip that sat top-left (Mike, 2026-08-27) was removed
 * on 2026-10-12: low value, and the ideas lightbulb took its slot. Model and
 * effort are still set and shown in Settings.
 *
 * Tapping the avatar opens what Marlow has done and knows; Settings stays on
 * the gear (Mike, 2026-09-28).
 *
 * Not floating: it sits above the scroller, so the visible region starts under
 * it. A CSS gradient hanging off its bottom edge softens the cut that would
 * otherwise show where rows scroll out (the iOS header's fade). */
export function PresenceHeader({
  presence,
  face,
  ideasDot,
  onSettings,
  onAvatar,
  onIdeas,
}: {
  presence: ChatPresence;
  face: Face;
  /** A spotlight is open, or an idea is new since the panel was last opened. */
  ideasDot: boolean;
  onSettings: () => void;
  onAvatar: () => void;
  onIdeas: () => void;
}) {
  const busy = presence.kind === "active";
  return (
    <header className="presence">
      {/* Clears the pulsing ring (it extends 4px plus its 2.5px stroke past the
          avatar): any overlap clips the glow, which read as a bug on device. */}
      <div className="presence-centre">
        <button type="button" className="avatar" aria-label="Recent activity" onClick={onAvatar}>
          <span className="avatar-face">
            <FaceImage face={face} size={52} crop="header" />
          </span>
          {/* A slow breath on the ring rather than a spinner: the pill says
              what is happening, this only says that something is. Static
              (still visibly on) under prefers-reduced-motion. Absent while
              idle, so an idle chat animates nothing. */}
          {busy && <span className="avatar-ring" data-testid="avatar-ring" aria-hidden="true" />}
        </button>
        <div className="presence-pill">
          <span aria-live="polite" data-testid="presence">
            {busy ? presence.label : "Marlow"}
          </span>
        </div>
      </div>
      <div className="presence-side">
        {/* The lightbulb balances the gear on the other side, at the gear's
            size. The dot is decoration inside the button, so a screen reader
            hears "Ideas" and nothing else. */}
        <button type="button" className="bulb" aria-label="Ideas" onClick={onIdeas}>
          <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M9 18h6M10 21.5h4M12 2.5a6.5 6.5 0 0 0-3.9 11.7c.7.6 1.2 1.3 1.3 2.3h5.2c.1-1 .6-1.7 1.3-2.3A6.5 6.5 0 0 0 12 2.5z" />
          </svg>
          {ideasDot && <span className="bulb-dot" data-testid="ideas-dot" aria-hidden="true" />}
        </button>
        <button type="button" className="gear" aria-label="Settings" onClick={onSettings}>
          <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            {/* A gear: eight flat-topped teeth and a hub. The first version was a
                circle with eight rays, which read as a sun (Mike, 2026-10-02). */}
            <path d="M10.16 4.32 L10.64 1.89 L13.36 1.89 L13.84 4.32 L16.13 5.26 L18.19 3.89 L20.11 5.81 L18.74 7.87 L19.68 10.16 L22.11 10.64 L22.11 13.36 L19.68 13.84 L18.74 16.13 L20.11 18.19 L18.19 20.11 L16.13 18.74 L13.84 19.68 L13.36 22.11 L10.64 22.11 L10.16 19.68 L7.87 18.74 L5.81 20.11 L3.89 18.19 L5.26 16.13 L4.32 13.84 L1.89 13.36 L1.89 10.64 L4.32 10.16 L5.26 7.87 L3.89 5.81 L5.81 3.89 L7.87 5.26Z" />
            <circle cx="12" cy="12" r="3.2" />
          </svg>
        </button>
      </div>
    </header>
  );
}
