import { useEffect, useState, type FormEvent } from "react";
import { Api, HttpError, TransportError } from "../lib/api";
import { DEFAULT_FACE } from "../lib/faces";
import { inviteFromHash, inviteFromText } from "../lib/pairLink";
import { FaceImage } from "./Face";

/** No server URL to enter: the client is served by the box it talks to. The
 * invite comes from the box (`deploy/pair-device.sh`), as a link, which is the
 * whole trust model: whoever can run it is on the box. Opening the link, or
 * scanning its QR with the camera, lands here with the invite in the URL's
 * fragment; it is taken out of the address bar at once, so it isn't left in
 * history or a screenshot. Pasting the link is the fallback. */
export function Pairing({ onPaired, notice }: { onPaired: (token: string) => void; notice?: string }) {
  const [name, setName] = useState(defaultDeviceName);
  const [fromLink, setFromLink] = useState(() => inviteFromHash(location.hash));
  const [pasted, setPasted] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const strip = () => {
      if (/(^|[#&])pair=/.test(location.hash)) {
        history.replaceState(null, "", location.pathname + location.search);
      }
    };
    strip();
    // A link opened in a tab already on this screen changes only the
    // fragment, which reloads nothing: take the invite from the event, or
    // the Pair button stays disabled with the invite sitting in the URL.
    const onHashChange = () => {
      const invite = inviteFromHash(location.hash);
      if (invite) {
        setFromLink(invite);
        setError(null);
      }
      strip();
    };
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  const invite = fromLink ?? inviteFromText(pasted);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy || !invite) return;
    setBusy(true);
    setError(null);
    try {
      const token = await new Api({ getToken: () => null }).pair(name.trim() || defaultDeviceName(), invite);
      onPaired(token);
    } catch (err) {
      // The box gives one answer for every refusal, so there is nothing finer
      // to say than: get another.
      setError(
        err instanceof TransportError
          ? "Can't reach Marlow."
          : err instanceof HttpError && err.status === 400
            ? "That link has expired or was already used. Get a new one."
            : "Pairing failed. Try again, or get a new link.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="pairing">
      {/* Who they are pairing with, large. Unpaired always means face 1:
          unpairing clears the stored choice. */}
      <span className="pairing-face">
        <FaceImage face={DEFAULT_FACE} size={120} crop="large" />
      </span>
      <h1>Pair with Marlow</h1>
      {notice && <p className="pairing-notice">{notice}</p>}
      <p className="pairing-help">
        {fromLink ? (
          "Name this device, then pair."
        ) : (
          <>
            Run <code>deploy/pair-device.sh</code> on the box, then open its link or scan its QR code with this device's
            camera. Links last ten minutes and work once.
          </>
        )}
      </p>
      <form onSubmit={submit}>
        <label>
          Device name
          <input
            name="device_name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            autoComplete="off"
            maxLength={64}
          />
        </label>
        {!fromLink && (
          <label>
            Pairing link
            <input
              name="link"
              value={pasted}
              onChange={(e) => setPasted(e.target.value)}
              autoComplete="off"
              autoCapitalize="off"
              spellCheck={false}
              placeholder="https://…/#pair=…"
              required
            />
          </label>
        )}
        {error && (
          <p className="pairing-error" role="alert">
            {error}
          </p>
        )}
        <button type="submit" className="primary" disabled={busy || !invite}>
          {busy ? "Pairing…" : "Pair"}
        </button>
      </form>
    </main>
  );
}

function defaultDeviceName(): string {
  const ua = navigator.userAgent;
  const os = /Android/.test(ua)
    ? "Android"
    : /Mac OS X/.test(ua)
      ? "Mac"
      : /Windows/.test(ua)
        ? "Windows"
        : /Linux/.test(ua)
          ? "Linux"
          : "Browser";
  const browser = /Edg\//.test(ua) ? "Edge" : /Firefox\//.test(ua) ? "Firefox" : /Chrome\//.test(ua) ? "Chrome" : /Safari\//.test(ua) ? "Safari" : "";
  return browser ? `${browser} on ${os}` : os;
}
