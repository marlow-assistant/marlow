# Direct-port ingress — "Internet-accessible port" in INSTALL.md

**Status: PARKED (2026-10-24).** Mike's direction: how a phone reaches the box
is the owner's choice, among three options: Tailscale on every device
(default, recommended); Tailscale Funnel (`funnel-ingress.md`); and an
internet-accessible port, open directly, for a cloud server or a home box with
a fixed address or dynamic DNS. INSTALL.md lists the port as "not built yet". Released by the first user
on a VPS, or behind a forwardable router, who wants it; don't build it ahead
of that.

## Why it is cheap now

Funnel already puts Marlow's own TLS port on the internet: the relay carries
raw TCP. So a direct port exposes the same surface Funnel does, and everything
`funnel-ingress.md` built for Funnel carries over unchanged: single-use invites in
the fragment, revocation, body caps, header and body deadlines, `/health`
behind the token, and the admin socket hidden from turns.

A direct port is better than Funnel in two ways. The server sees the real client IP, so a
per-IP connection limit becomes possible; Funnel can't have one, and a determined
flood still wins there. And it avoids Funnel's beta bandwidth limits, its three
fixed ports, and the relay delay of a few minutes seen when Funnel is turned on
(VM run, 2026-10-24).

## What it needs that Funnel gets from Tailscale

- **A certificate for a public name.** The `ts.net` name only resolves publicly
  under Funnel, so a direct port needs a domain the owner controls and ACME renewal: HTTP-01
  needs port 80 open, DNS-01 needs the registrar's API. A domain is one more
  signup and a yearly cost, which runs against "no new monthly cost". IP-address
  certificates would avoid the domain; check whether they're practical before
  building.
- **The bind.** `update.sh --public-port`-style flag, a drop-in like
  `30-funnel.conf`, binding `0.0.0.0` with the owner's certificate instead of
  the tailnet one.
- **A firewall step.** Only the Marlow port and SSH open (ufw).
- **The INSTALL.md section,** with the same plain trade-off as B, plus router
  port-forwarding for a home box. `phone-access-without-vpn.md` rejected direct
  *home* ingress (the home address becomes public, and ISP/NAT behaviour is
  fragile). That judgement is right for a default; offered as the owner's
  explicit choice, with the warning, it no longer blocks.

Tailscale on the box becomes optional with a direct port (admin can be plain SSH on a
VPS); whether install.sh should allow that is part of this work.
