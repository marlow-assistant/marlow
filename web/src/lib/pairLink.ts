/** The pairing link is `https://<box>/#pair=<invite>` (deploy/pair-device.sh).
 * The invite rides in the fragment because a browser never sends one: it stays
 * out of the box's access logs and off the Funnel path. */

// `secrets.token_urlsafe(32)` is 43 characters of base64url. Loose bounds, so
// a longer invite later still parses; this only has to reject junk early.
const INVITE = /^[A-Za-z0-9_-]{16,128}$/;

export function inviteFromHash(hash: string): string | null {
  const m = /^#?(?:.*&)?pair=([^&]+)/.exec(hash);
  return m && INVITE.test(m[1]) ? m[1] : null;
}

/** What a person pastes: the whole link, or just the invite. */
export function inviteFromText(text: string): string | null {
  const t = text.trim();
  if (INVITE.test(t)) return t;
  const i = t.indexOf("#");
  return i >= 0 ? inviteFromHash(t.slice(i)) : null;
}
