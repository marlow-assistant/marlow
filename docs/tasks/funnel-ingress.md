# Funnel ingress — a box the phone reaches without Tailscale

**Status: BUILDING (2026-10-24).** Before inviting more
users. The reason is Mike's direction (2026-10-17): a hardened non-Tailscale
endpoint is the likely end state for phones, iOS too, and its priority rises
with each new user. Tailscale on a phone costs battery even with On Demand, is
one more thing to install, and On Demand is "not immediate", which can leave a
push banner generic. **Funnel is opt-in, the owner's choice per box (Mike,
2026-10-24, replacing "the default for invited boxes").** Tailscale on the
phone stays the default and the recommended setup: it keeps Marlow off the
internet. Funnel is for a phone that fights Tailscale (all three of the
Android user's connection failures were that), at the price of a box anyone
can open a TLS connection to. It doesn't remove Tailscale from the box, the
account or the tailnet, only from each phone. The hardening below comes first,
and is worth merging on the tailnet regardless: invites, revocation and the
request caps are the only way to cut off a lost phone. The research and alternatives are in
`phone-access-without-vpn.md`; this doc is the build.
**2026-10-24: items 1–6 built, merged 2026-10-25, Funnel still
off everywhere.** Not deployed to a box, nothing run on a device. Server, web and iOS code and
tests are in (`/v1/pair`, `/v1/device/unpair`, `deploy/pair-device.sh`,
`deploy/devices.sh`, `server/body_limit.py`, `/health` behind the token,
`tests/test_public_surface.py`, `deploy/funnel.sh` behind `update.sh
--funnel`). Never observed: the iOS QR scanner on a real camera, the web
fragment flow on Android Chrome, `--funnel` against a real tailnet, and the
Testing section below. Two findings:
- **The `claude -p` child could always reach loopback.** It runs as `marlow`,
  the same user as the server, and `egress.nft` (not live) accepts
  `127.0.0.0/8`. Rebinding `public_app` to loopback adds nothing: the child
  could already connect to the box's tailnet IP. It can't read
  `device_tokens.json` (a secret path), but that was not enough: the admin
  endpoints sat on the internal socket, which a turn can reach because the
  hook must, so one approved command could mint an invite, pair, and approve
  its own tool calls (review, 2026-10-24). They now have their own socket in
  `~/.marlow/admin/`, a secret path.
- **uvicorn's connection cap was a lockout.** A connection counts from the
  moment it opens and nothing timed it until its first response, so 64 sockets
  that never finished their headers took every slot (measured: 503 for the
  owner). Now each request gets ten seconds for its headers
  (`server/request_deadline.py`) and, without a valid token, ten for its body;
  the cap is 256. A determined flood still wins: with no client IP behind
  raw-TCP Funnel there is no per-client limit to apply.
- **iOS had no 401 handling** (the web does, WEB-8), so a device revoked from
  `devices.sh` would have shown server errors. Now `APIClient.onUnauthorized`
  reports the token a refused request carried, and `SessionStore` signs out
  only if it is still the current one, with a line on the pairing screen.
  Unit-tested; never seen on a phone (revoke one from `devices.sh` and watch).
- `deploy/deadmans/check.sh` now counts a 401 from `/health` as up, since the
  off-box host holds no token.

## Why

The connection problems the Android user hit all came from Tailscale running
on an Android phone, not from Marlow:

- 2026-10-02: Override DNS with no global nameserver took the phone's internet
  away whenever Tailscale was connected.
- YouTube stalls: an exit node, left selected, sent all the phone's traffic
  through an OVH VPS.
- 2026-10-11: lookups went around Tailscale's resolver, the `ts.net` name came
  back NXDOMAIN, and Chrome showed its own "offline copy" page. None of our
  code ran, so we couldn't explain it.

Each one looks to the user like Marlow is broken, and each fix was a phone
setting we found by remote debugging. A wrapper app wouldn't help: it uses the
same resolver and the same VPN. Funnel takes Tailscale off the phone. The
box's `ts.net` name resolves in public DNS to Tailscale's relay. The relay
carries raw TCP and can't read it. TLS still ends at uvicorn, with the cert we
already issue.

**Mike's box stays tailnet-only.** iOS + Tailscale behaves well for him, and
his box keeps the tailnet path covered by daily use. So Funnel is a per-box
setting, and the security work below ships to *every* box. The tailnet path
gets the same pairing and revocation, so there's one code path, not two.

## The decision this undoes

`CLAUDE.md`: "No public ingress — Tailscale only, device-token pairing." On a
Funnel box anyone can open a TLS connection to `public_app`, and the device
token is the only gate. That line in `CLAUDE.md` changes in the same PR as the
deploy switch: "public ingress only via per-box Funnel, after the hardening in
`docs/tasks/funnel-ingress.md`".

What a token is worth: it sends messages, reads the transcript and identity
files, and **approves tool calls**, the permission round-trip. A stolen token
on a Funnel box is close to a remote shell as `marlow`, reachable from
anywhere, until it's revoked. Most of what follows is about keeping tokens
from leaking and making them revocable.

## Work, in order

All of it is landed and deployed with Funnel off. Funnel is switched on last.

### 1. QR invite pairing (replaces the 6-digit code everywhere)

Today: `issue_code()` makes a 6-digit code. `/v1/pair/initiate` scans every
live challenge for a match, with no attempt limit, and returns a
`challenge_id`. `/v1/pair/complete` swaps that id for a token. Behind a
tailnet, a million guesses is fine. On the internet it isn't, and
`initiate`'s distinct errors ("expired" vs "no matching") act as an oracle.

New:
- `pairing-code.sh` (rename it: `pair-device.sh`) issues an **invite**:
  `secrets.token_urlsafe(32)`, single use, 10-minute TTL, stored as a hash. It
  prints a QR of `https://<box>:8443/#pair=<invite>` and the same link as text
  for a desktop.
- **The fragment, not a query string.** Browsers never send the fragment, so
  the invite stays out of access logs and the Funnel path. The web client
  reads `location.hash`, pairs, then `history.replaceState` drops it.
- One endpoint: `POST /v1/pair {invite, device_name}` → `{device_token}`.
  Redemption is atomic: mark it redeemed before writing the token. Every
  failure returns the same 400. Delete `initiate`/`complete` and the
  two-phase `Challenge`.
- **Android:** scanning the QR with the camera opens Chrome on the box and
  pairs in one tap. That's the user Funnel exists for.
- **iOS:** the camera would open Safari, which isn't supported, and universal
  links can't cover arbitrary `ts.net` hosts. So: an in-app scanner
  (VisionKit `DataScannerViewController`) that reads the same URL and
  splits it into address and invite, plus "paste link" as a fallback. One QR
  works for both clients.
- No guess budget needed: 256 bits can't be brute-forced, and a global
  lockout would let a stranger block pairing. Log issue/redeem/failure counts,
  never the invite.

Existing paired devices aren't affected. Pairing happens once.

### 2. Revocation

Unpair (`SettingsView.unpair()` on iOS, its web twin) clears the token on the
device only, and no route removes it from `device_tokens.json`.

- `POST /v1/device/unpair`: authenticated, revokes the calling token and
  clears its APNs/web-push registration. Both clients call it, then clear
  local state even when offline, and say so if the server didn't confirm.
- `internal_app` list/revoke plus `deploy/devices.sh` (list by name, paired_at,
  token fingerprint; revoke one) for a lost phone.
- Log a short fingerprint, never the token.

### 3. Bound unauthenticated work

`_decode_image()` checks size *after* JSON parse and base64 decode. A public
socket needs the cap earlier:
- ASGI middleware that rejects on `Content-Length` over ~9 MiB (6 MiB decoded
  image as base64 plus JSON; measure it, don't copy this number). It also
  counts bytes on chunked bodies and stops at the same cap. All of this
  before the route runs.
- uvicorn: `limit_concurrency`, a short `timeout_keep_alive`, default h11
  header limits.
- Raw-TCP Funnel hands uvicorn a loopback peer, so **there's no client IP**.
  Every limit is global. That's acceptable because nothing above needs per-IP
  accounting.

### 4. `/health` behind the token

The public route returns Claude auth state, queue depth, model, effort and git
branch/commit to anyone. It should require the token, and clients send
theirs. The watchdog already uses `internal_app`'s `/health`, which stays
open on loopback. Check `deploy/deadmans/check.sh` before changing it.

### 5. A test that pins the public surface

Every `public_app` route needs the token except `/v1/pair` and the static
client. A test enumerates `public_app.routes`. Each route
must depend on `require_device_token` or be on a short allowlist. A route
added later without auth fails the suite. This is what keeps steps 1–4 true.

### 6. The deploy switch

- `install.sh`/`update.sh --funnel` (stored in the box's env file so updates
  keep it): bind `public_app` to `127.0.0.1:8443`, then
  `tailscale funnel --bg --tcp=8443 tcp://127.0.0.1:8443`. `--no-funnel`:
  `tailscale funnel --tcp=8443 off`, then rebind to the tailscale IP. Write
  the order so a failed step leaves the box reachable over SSH.
- `app.py` warns when `public_bind_host` falls back to `127.0.0.1`. On a
  Funnel box loopback is intended, so the warning must tell the two cases
  apart.
- Prereq the script checks but can't do: a `funnel` nodeAttr in that tailnet's
  policy, scoped to the box, not the console's `autogroup:member` default.
  That goes in INSTALL.md as an admin-console step.
- The cert keeps coming from `tailscale cert` and `marlow-cert-renew.timer`.
  Raw TCP leaves it alone.

## Other security considerations

- **The name is already public.** Every `ts.net` cert is in Certificate
  Transparency logs, so scanners will find a Funnel box within hours. Steps
  1–5 deploy before Funnel goes on, not alongside.
- **XSS now has a payoff anywhere.** The web token lives in IndexedDB, readable
  by any script on the origin. Model output, which email can influence
  through prompt injection, is rendered in that origin. The CSP in
  `web_static.py` is the defence (`script-src 'self'; connect-src 'self'`, no
  inline): it stops injected script and stops a stolen token being sent off
  the origin. Re-read it in the security review and treat any loosening as a
  Funnel blocker.
- **The sandbox can reach loopback.** Moving `public_app` to `127.0.0.1` puts
  it where any user on the box can connect. It still needs a device token, but
  confirm in `boundaries.py`/`egress.nft` what a `claude -p` child can connect
  to on loopback before calling it unchanged. (`internal_app`, which can issue
  pairing invites, is on an owner-only Unix socket since 2026-10-16, not
  `:8464`.)
- **Static files are unauthenticated.** They reveal that the box runs Marlow
  and which build. That's acceptable. The client holds no secrets.
- **Funnel is beta, with fixed bandwidth limits.** Roll back with
  `--no-funnel`. Leave if availability fails without explanation, or if
  image sends hit the limits.

Review: `/code-review high` on each step, and `/security-review` on 1–6 as a
set before the switch.

## Clients — both or not done

QR pairing and revoke-on-unpair are user-facing. iOS (scanner, paste-link,
unpair call, health with token) and `web/` (fragment pairing, unpair call,
health with token) land together.

## Testing

Not on Mike's box: it stays tailnet-only, and his phone stays on Tailscale (Mike, 2026-10-24). A throwaway VM instead; the run below. Don't remote-debug a first run on the Android user's.

1. Steps 1–5 deployed, Funnel off. Pair a fresh device by QR on both clients.
   Unpair → old token returns 401. The suite passes and so do the on-device
   checks.
2. `--funnel` on Mike's box. **Questions to answer:**
   - Do tailnet clients still reach `<box>:8443` through Tailscale's proxy
     now that uvicorn has left the tailnet IP? Mike's iPhone with Tailscale
     on.
   - Does public DNS resolve the name to the relay? From a device off the
     tailnet: the iPhone with Tailscale off, and the Android emulator
     (`docs/android-emulator-testing.md`), which has no Tailscale.
3. Off-tailnet on both clients: send text and an image, get a tool approval
   in within the 55 s deadline, receive a push, reopen from cold.
4. Reboot the box: Funnel status and both health routes come back.
5. `--no-funnel`: Mike's box returns to tailnet-only and his phone still
   works.
6. Then the Android user's box: `update.sh --funnel`, add the nodeAttr, turn Tailscale off on
   the phone, re-pair by QR.

### VM run, 2026-10-24

A fresh Multipass Ubuntu 24.04 VM (2 GB) on Mike's Mac, on his tailnet,
`install.sh --ref worktree-funnel-ingress` (the feature branch, before the merge). Clients: the Claude
desktop browser pane (Chromium, standing in for Chrome), the iOS Simulator
running the app built from this branch, and curl. No real phone, no Android
emulator (see the off-tailnet row).

| Check | Result | Evidence |
|---|---|---|
| **`/proc/<pid>/root` bypass of the sandbox's hidden paths** | **Pass: not bypassable** | Replayed the exact bwrap argv captured from a live turn (`--dev-bind / / --tmpfs ~/.ssh --tmpfs ~/.marlow/{data,tls,admin} …`), with probes in place of `claude`, both as `marlow` and under the unit's `NoNewPrivileges`/`ProtectHome`/`ProtectSystem`/`PrivateTmp`: the server's `root`, `cwd`, fd links, `environ` and `maps`, and `admin.sock` via `/proc` all give EACCES. Only fd *numbers* are listable. Control: the same `ls` as `marlow` without bwrap lists `admin/ data/ tls/`, and a same-uid `sleep` can't be traversed from inside bwrap either. So it is bwrap's child user namespace (the kernel's cross-userns ptrace check), not AppArmor (`bwrap` profile is `unconfined` + `userns`). It holds while bwrap runs unprivileged; a setuid bwrap would lose it. |
| Hook on the same commands, run in-sandbox | Pass | `ls -la ~/.marlow/admin` and `curl --unix-socket ~/.marlow/admin/admin.sock …` are hard-denied as secret paths. `ls /proc/<pid>/root/home/marlow/.marlow/admin` is denied as "hook error -- failing closed": `realpath` raises `PermissionError` on `/proc/<pid>/root` inside the namespace. Fail-closed is right, but it's the error path. |
| Same probe from a real turn | Not run | The model refused both requests (`ls ~/.marlow/admin`, the `/proc` paths) as exfiltration. The argv replay above stands in for it. |
| `pair-device.sh` prints a link; web pairs by it | Pass | Link with `#pair=`, the fragment cleared from the address bar on load, "Chrome on Mac" paired. A link used 10 min 3 s after issue was refused with the right message. |
| iOS Simulator pairs by pasted link | Pass | Long-press → Paste → "Pairing with https://…:8443" → Pair → `device paired: 'iPhone 17 Pro'`. |
| iOS: a *typed* link | Not supported (by design) | Typed one character at a time (only the simulator does this), the link fails: `PairingView.swift:56` runs `PairingLink.parse` on every change and keeps the first success, and `isInvite` accepts 16–128 characters, so it locks onto the first 16 invite characters and the server refuses it as "expired". Scan and paste are the pairing paths, and both deliver the link whole; nobody types a 100-character link. An exact-43 pin was tried and reverted (Mike, 2026-10-24): it would make installed builds reject every link if the invite format ever changed, to protect a path nobody uses. Web parses at submit and accepts a typed link anyway. |
| Invite is single-use | Pass | Reusing a redeemed invite → 400. |
| Unpair on web → old token 401 | Pass | Token saved before Unpair: `/health` and `/v1/transcript` → 401; the page returns to pairing. |
| `devices.sh` list and revoke | Pass | Lists fingerprint, time, name. Revoke: 200 → 401. Unknown fingerprint: exit 1 with a message. Malformed: exit 2, usage. As another user: the admin dir is unreadable, curl to the socket gets 000. |
| `/health` without a token | Pass | 401. |
| `deadmans/check.sh` counts 401 as up | Pass | Against `:8443` → `healthy`. Against a dead port with threshold 0 → notify fired, state `alerted`. |
| Lockout: 256 idle TLS connections | Pass | t+0: 503 (expected while the cap is full). By t+15 s the server has closed all of them and a normal request gets 401. Same for 256 partial-header connections and for 400 idle. |
| **Funnel nodeAttr** | **Finding** | `tailscale funnel` on a box without the attribute prints a one-click link (`login.tailscale.com/f/funnel?node=…`) and **waits** instead of failing. The page's only option is "give Funnel access to all Members", i.e. `autogroup:member`, the scope INSTALL.md warns against. Fine for a one-box tailnet, broad on Mike's. Approved for this test. |
| **`update.sh --funnel` while waiting for that approval** | **Fail** | The server is already rebound to 127.0.0.1 when `tailscale funnel` blocks, so `:8443` is unreachable from the tailnet (000) until someone clicks. `funnel_nodeattr_help` never prints, because the CLI doesn't fail. Ctrl-C there skips the rollback. Fix: check Funnel permission before rebinding, or run the call with a timeout and roll back; print Tailscale's link rather than a policy snippet. **Fixed, uncommitted:** `funnel_preflight` reads the `funnel` capability from `tailscale status --json` (absent before approval, present after, on the VM) and refuses before anything changes, printing Tailscale's one-click link built from `Self.ID` (the same `?node=` the CLI printed) and the tag-scoped nodeAttr as the narrow option. The `tailscale funnel` call runs under `timeout 60`; on failure or timeout the rollback also turns off any half-made forward before the restart. Unit-tested; on the VM, on and off against real Tailscale pass. Re-run on the VM with the fix in place and the grant removed: the refusal printed before `== fetching origin`, with exit 1 and the same `?node=` link Tailscale's CLI prints; afterwards there was no drop-in, the serve config was `{}`, uvicorn was still on the tailnet IP (401), and the service hadn't restarted. (The console's JSON editor loaded empty in Firefox and worked in Chrome.) |
| `tailscale funnel --bg --tcp=8443 tcp://127.0.0.1:8443` as written | Pass | Exit 0. Status: `tcp://…:8443 (Funnel on)` → `tcp://127.0.0.1:8443`. tailscaled holds the tailnet IP's `:8443`, uvicorn holds `127.0.0.1:8443`. |
| Tailnet clients still reach `:8443` | Pass | From the Mac: 401. |
| Public DNS → relay | Pass | 1.1.1.1 and 8.8.8.8: `208.111.34.11`, `208.111.35.209`. |
| Off-tailnet reach | Pass, after a delay | curl `--resolve` to each relay IP over `en0`, with the real cert verified. `.35.209` worked at once. `.34.11` failed TLS (`SSL_ERROR_SYSCALL`) for about 3 minutes after enabling, and once more right after the reboot; tailscaled logged `Drop: … funnel-ingress-node … no rules matched` for each try. It cleared by itself both times. A client that draws that address in the window fails, so the docs could say "if it fails in the first few minutes, wait". Over the relay: pair 200, `/health` 200, `/v1/transcript` 200, a 2 MB unauthenticated `/v1/pair` → 413, revoke → 401. `GET /internal/*` returns the web app's `index.html`; `POST /internal/admin/invite` → 404. The Android emulator wasn't used: it resolves through the Mac's MagicDNS and rides the Mac's tailnet, so it isn't off-tailnet. |
| Reboot | Pass | Funnel status, both bindings, the tailnet's 401 and both relays' 401 came back with no action (one relay after about a minute). |
| `--no-funnel` | Pass, one leftover | Exit 0. Uvicorn is back on `100.x.y.z:8443`, the tailnet gets 401, both relays get 000. But `tailscale serve status --json` keeps `{"AllowFunnel": {"…:8443": true}}` and `funnel status` says "Funnel on … but no serve config". Nothing is served, but a later `serve` handler on `:8443` would be public at once. `tailscale funnel --https=8443 off` fails ("handler does not exist"); `tailscale funnel reset` clears it, and Marlow uses no other serve config. **Fixed, uncommitted:** after `--tcp=<port> off`, `funnel reset` runs when `AllowFunnel` is all that's left, and other serve config is left alone with a warning. On the VM: `{}` and "No serve config" afterwards. |
| Install notes (for the install feel-out run) | — | `install.sh` checks Tailscale before it prints the deploy key, so a friend without `tailscale up` hits two stops. A brand-new user's first screen is the "Marlow was updated" card listing every past change. |

Still needs a real phone: an iPhone with Tailscale off pairing over Funnel
(row 3 above: text, an image, an approval inside 55 s, push, cold reopen), the
iOS QR scanner on a camera, the web fragment flow in Android Chrome, and a
revoked iPhone falling back to the pairing screen.

What a client over the relay would add is small: raw-TCP Funnel carries the bytes
through untouched, so the phone sees the same name, certificate and server as on
the tailnet, and curl already did pair, health, transcript and revoke that way.
So the items above wait for the first box that turns Funnel on, most likely the
Android user's, rather than blocking the merge.
