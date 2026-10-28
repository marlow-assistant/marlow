# Recommendation — phone access without a persistent VPN

**Status: PARKED — VPN On Demand is enabled and working (2026-08-27).**
Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

**Superseded for Android boxes by `funnel-ingress.md` (2026-10-11),** a
per-box Funnel opt-in; Mike's box stays tailnet-only.
**2026-10-24:** the owner chooses among Tailscale on every device (default),
Tailscale Funnel, and an internet-accessible port (parked in
`direct-port-ingress.md`); see `deploy/INSTALL.md`, "How your phone reaches
Marlow". The Funnel recommendation below is not being pursued. This doc is
the original research and the reviewer's verdict, kept because it stays
correct if On Demand stops being enough.

**Android (2026-10-10):** Android has no On Demand. The Android user's
complaint was not battery but a video app stalling while Tailscale was
connected (upstream `tailscale/tailscale#20519`, closed without a fix; CGNAT/QUIC,
cause unknown), so they toggled Tailscale daily to get Marlow updates.
Excluding that app in the Android app's Settings → Split tunneling fixed it in
a quick test. That is the answer to "Android user needs Tailscale off for app
X": exclude the app, not public ingress. Reopen Funnel only if exclusion stops
working.

Written 2026-08-27 for a second AI to review, and reviewed the same day. It
changes no server, client, deployment or tailnet policy, and not the hard
constraint in `CLAUDE.md`. Enabling any option below would change an
authentication boundary, so it needs Mike's explicit approval and a security
review.

## The proposal

The problem reported: on poor cellular service the phone's persistent VPN
repeatedly tries to stay reachable and drains the battery. The proposal: keep
the development laptop and the Marlow laptop on Tailscale, take Tailscale off
the phone's normal data path, and expose only Marlow's client API over
ordinary HTTPS through **Tailscale Funnel**, but not before pairing,
revocation, health and abuse controls were hardened. SSH, deployment and
development access stay on the tailnet.

The shape: a raw TCP Funnel (`marlow.<tailnet>.ts.net:8443` → loopback
`8443` → `public_app` with its existing TLS certificate). Raw TCP was
preferred over terminating TLS at a third-party edge, so the certificate and
the uvicorn TLS boundary stay intact. The plaintext `internal_app` stays on
its owner-only Unix socket and is never a Funnel target. The iOS endpoint
would not change, but that has to be proven from a phone with Tailscale off,
not from a tailnet member.

Funnel publishes one node service to non-members via a relay that carries an
encrypted TCP proxy and cannot read the content. It is on all plans but still
beta, uses only ports 443, 8443 or 10000, and has non-configurable bandwidth
limits, acceptable for one user's text and downscaled images.

A read-only check of the deployed node on 2026-08-27: Tailscale 1.102.2 (Funnel
needs 1.38.3), no serve config, and Marlow listening on the tailnet interface,
not loopback. So the cutover was never configuration-only: the listener would
have to move to loopback in a deliberately ordered switch (loopback before
Funnel is an outage, Funnel before loopback fails).

## The trust-boundary change

Today an attacker needs tailnet membership and an application credential. With
Funnel anyone on the Internet can reach the client API and the bearer token
becomes the whole boundary. The token itself is strong
(`secrets.token_urlsafe(32)`, iOS Keychain `AfterFirstUnlockThisDeviceOnly`).
The gaps are lifecycle and the unauthenticated surfaces around it.

### Blocker 1 — pairing is publicly brute-forceable

`PairingStore.issue_code()` makes a six-digit code valid for ten minutes;
`/v1/pair/initiate` has no failure budget, per-challenge counter or throttle.
A million-value credential is fine behind a tailnet and not on a public
endpoint. Minimum: one live challenge at a time, a small global failure budget
independent of source IP (five, then invalidate), the ten-minute expiry and
single redemption kept, one uniform failure response, and logs that never
contain the code. The budget can't rely on client IP: carrier NAT and IPv6
privacy addresses make it too broad or unstable, and raw TCP forwarding may
not pass the original address. A high-entropy QR/deep-link invitation would be
stronger; it wasn't the minimum because it adds iOS UI and delivery scope.

### Blocker 2 — Unpair does not revoke the server credential

iOS Unpair deletes the Keychain item, but no server route removes the entry
from `device_tokens.json`, so a copied token stays valid forever. Minimum: an
authenticated self-revoke route; Unpair attempts it, then clears the local
credential even offline, and the UI is honest if revocation wasn't confirmed;
a loopback-only admin list/revoke for a lost phone; clear the APNs token on
revoke; log a token fingerprint, never the token. For one personal device, a
strong long-lived token with reliable revocation beats short expiry that can
strand the phone.

### Blocker 3 — public `/health` discloses operational state

It needs no token and returns Claude auth state, queue depth and pause state,
oldest permission age, model, effort and git branch/commit. Minimum: require a
device token on the public route, keep the duplicate `internal_app` route open
on loopback for the watchdog, send the token from iOS, and audit
`deploy/deadmans/check.sh` if it ever runs off-box. Don't hand an off-box
monitor a full device token that can send messages and approve tools.

### Blocker 4 — bound unauthenticated work before parsing

On a public socket, invalid clients can burn connections and send bodies that
are parsed before the 401. The decoded-image ceiling in `_decode_image()` is
post-parse, not an ingress limit. Minimum: a body ceiling enforced before
JSON/base64 parsing (room for the 6 MiB decoded image, about 8 MiB encoded,
plus bounded JSON overhead; choose and test the exact figure), bounds on
header size, request duration and in-flight requests, a modest global rate
limit on failed auth and pairing, and no echo of tokens, codes or oversized
error content into logs. Middleware is simpler than a loopback gateway but
must reject before buffering. Volumetric DDoS protection is not realistic in
this process; the goal is bounded application work.

### Operational gates (if ever reopened)

Scope the `funnel` node attribute narrowly (the CLI default can grant it to every
`autogroup:member`); write an explicit cutover and SSH rollback sequence;
verify `funnel status` after a reboot (`--bg` should persist); test the real
certificate chain from a non-tailnet phone with no ATS exception; keep the
watchdog on `internal_app` so a Funnel failure isn't read as a wedged Marlow;
keep access logs to method/path/status; record an exit trigger for the beta
dependency (repeated unexplained failures, bandwidth limits hurting normal
use, a policy change). Acceptance would be on a physical phone with Tailscale
off, including APNs wake while terminated and locked, a permission answered
inside the 55-second deadline, pairing lockout and recovery, and an old token
returning 401 after Unpair. Battery needs an empirical comparison on the same
phone and poor signal: always-on Tailscale, On Demand, and Tailscale off with
Funnel. One anecdotal interval is not a percentage.

## Alternatives considered

| Option | Phone VPN | Origin exposure | Content trust | Operational cost | Recommendation |
| --- | --- | --- | --- | --- | --- |
| Tailscale VPN On Demand | Only when rules trigger | None | Existing Tailscale model | Very low | Immediate experiment, not the durable answer |
| Tailscale Funnel | None | Home IP hidden; one public service | Funnel relay cannot read content | Low | **Preferred after hardening** |
| Cloudflare Tunnel + Access | None | Home IP hidden; outbound connector | Cloudflare terminates and inspects HTTPS at its edge by default | Medium | Best fallback if edge controls outweigh content-trust cost |
| VPS TCP relay/reverse tunnel | None | VPS is public; home IP can remain hidden | Can preserve end-to-end TLS | High | Only if Funnel proves unsuitable |
| Router port-forward + DDNS | None | Home address and port directly public | End-to-end TLS possible | Medium/high and fragile | Reject |
| Discord/Signal/APNs as full transport | None | No Marlow HTTP endpoint | Third-party message transport | High product/protocol change | Degraded fallback only |

- **VPN On Demand.** The iOS client can choose Always/Never/Do Nothing for
  cellular and trigger on a matching `*.ts.net` hostname. `Do Nothing` plus
  hostname matching avoids an always-on tunnel while keeping opportunistic
  access; `Never` makes Marlow unavailable on cellular. It might not fix poor
  service if APNs fetches or active use keep retriggering the tunnel.
- **Cloudflare Tunnel.** Outbound-only, with Access service-token headers, WAF
  and rate limits in front. Costs: a Cloudflare-controlled hostname, another
  daemon and credential, iOS changes to attach Access credentials. Decisive
  against it for private conversation content: Cloudflare decrypts proxied
  HTTPS at its edge by default. That is a different content-trust decision, not
  another brand of the same tunnel. It becomes the preferred fallback if
  Funnel's beta reliability or missing abuse controls cause a measured
  problem and Mike accepts the edge trade.
- **VPS relay.** Preserves end-to-end TLS and gives full control of limits and
  logs, but is a paid public host to patch, monitor and recover. Build it only
  in response to a concrete Funnel failure.
- **Direct public ingress.** Reveals the home address, depends on ISP/NAT,
  delivers hostile traffic straight to the residence, and adds certificate and
  firewall machinery with no extra authentication layer. Existing bearer tokens
  don't make it attractive.
- **A messaging channel instead of HTTPS.** Discord was a workable v1
  transport and APNs already reaches the phone, but making either the primary
  channel changes the privacy model and means rebuilding polling, images,
  sessions, idempotency and approvals around messaging semantics. An emergency
  or dead-man channel, not an answer to a battery problem.

## Reviewer response (2026-08-27)

A second session reviewed this document against the code. Recorded so it
doesn't have to be re-derived.

**The research checks out.** Every load-bearing claim was verified:
`PairingStore.initiate()` loops over every live challenge matching a six-digit
code with no counter or throttle; `SettingsView.unpair()` clears only the
Keychain and `server/app.py` had no revoke route; `health.get_health()` returns
the state above and was the only `public_app` route with no token;
`_decode_image()` measures `len(data)` after `base64.b64decode`; the public
bind host is the tailscale interface. The alternatives table is fair, and the
Cloudflare content-trust point is the right reason to prefer Funnel for
private conversation content.

**What the document got wrong.**

- *It never measured the thing it was solving.* It blamed the battery cost
  entirely on the Tailscale network extension, but the iOS client's own polling
  is a candidate: `PollingPolicy` in `ChatStore.swift` polls `/health` every
  15s while foregrounded and `/v1/turn` every 2s for roughly five minutes
  during a turn. On poor cellular that cadence can hold the radio in a
  high-power state by itself, and removing Tailscale wouldn't touch it.
- *It buries its cheapest answer.* VPN On Demand is zero-code and reversible,
  but got one subsection while Funnel got the other 300 lines.
- *Funnel breaks a stated hard constraint.* `CLAUDE.md` said "No public
  ingress — Tailscale only." That constraint, not the four security blockers,
  is the actual decision.

**What happened.** Mike enabled VPN On Demand on 2026-08-27, and it works. The
same settings went on Taylor's phone on 2026-10-17 (On Demand, "Do Nothing",
MagicDNS hostname) and should be part of every iPhone's onboarding
(`friends-alpha.md`, pairing papercuts). It cost nothing, changed no
authentication boundary, and is reversible from the phone's settings. So
Funnel is parked, not rejected. Reopen only if On Demand proves insufficient
over real use on poor service, and with battery evidence, because without a
measurement the polling cadence is an equally plausible cause and Funnel would
not fix it.

**The blockers split two ways.** Two are bugs today, independent of transport:
Blocker 2 (a token that survives Unpair forever is wrong on a tailnet too) and
Blocker 4 (cheap, correct on any transport). The other two are costs only
Funnel would impose: Blocker 1 (a million-value code is defensible behind a
tailnet) and Blocker 3 (the disclosure matters only once the route is
Internet-visible, and authenticating it has a real cost in
`deploy/deadmans/check.sh`). Don't pay for blockers 1 and 3 on behalf of a
transport that hasn't been chosen.

**Still open** (the reviewer's questions `funnel-ingress.md` does not answer):

- What source address reaches uvicorn through raw TCP Funnel, and can it be
  trusted? The design must stay correct if the answer is "none."
- Does the firewall allow Funnel's relay traffic and certificate renewal
  without widening unrelated egress?
- What is the exact rollback order that restores direct tailnet access without
  leaving a public Funnel pointed at the wrong listener?

## Decision boundary

This document is not permission to enable Funnel, change the bind host, edit
tailnet policy, install a proxy, alter iOS networking, or weaken the repo's
no-public-ingress constraint. Its recommendation becomes a project only after
Mike chooses the direction. Until then the deployed design stays
Tailscale-only.
