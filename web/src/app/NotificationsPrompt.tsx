import { useState } from "react";
import type { Api } from "../lib/api";
import { enablePush, usePushState } from "./push";

/** WEB-9: notifications are how Marlow reaches you when the app is closed, so
 * the chat asks for them itself instead of leaving it to Settings. On the
 * Android emulator (Mike, 2026-10-02) they were never turned on in the
 * installed app, and replies arrived silently with nothing to say why.
 *
 * A browser only lets a page ask from a tap, so this is a card with a button,
 * not a prompt on load. "Not now" hides it until the next launch; it comes back
 * until notifications are on. */
export function NotificationsPrompt({ api }: { api: Api }) {
  const push = usePushState();
  const [dismissed, setDismissed] = useState(false);
  const [asking, setAsking] = useState(false);
  if (dismissed || push === "on" || push === "unsupported") return null;

  const text =
    push === "denied"
      ? "Notifications are blocked, so Marlow can't reach you while the app is closed. Allow them in this site's settings in the browser."
      : push === "failed"
        ? "This browser's notifications can't be turned on, so Marlow can't reach you while the app is closed."
        : "Turn on notifications so Marlow can reach you while the app is closed.";

  return (
    <div className="notify-prompt" role="region" aria-label="Notifications" data-testid="notify-prompt">
      <p>{text}</p>
      <div className="notify-prompt-buttons">
        <button type="button" className="link-button" onClick={() => setDismissed(true)}>
          Not now
        </button>
        {push === "off" && (
          <button
            type="button"
            className="primary"
            disabled={asking}
            onClick={() => {
              setAsking(true);
              void enablePush(api).finally(() => setAsking(false));
            }}
          >
            Turn on
          </button>
        )}
      </div>
    </div>
  );
}
