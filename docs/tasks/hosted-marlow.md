# Hosted Marlow: install the app, connect Claude Pro, done

**Status: FUTURE, not designed (Mike, 2026-10-17).** "A long way off,
because it would be Marlow running on my infra, but the current process is
still very much enthusiast-only."

## The gap

Today a new user needs an always-on Ubuntu box, Tailscale on the box and the
phone (with On Demand set up by hand on iOS), a terminal session to run `install.sh` and paste a Claude login code,
a Debug build over USB or TestFlight, and a pairing code typed with the right
host. The second instance's install (2026-10-17) took an afternoon with Mike and Claude
driving. The target is: install the app, tap to connect a Claude Pro account,
and Marlow is running.

## What it would take (first thoughts, not a plan)

- **Marlow runs on Mike's infra**, one confined instance per user. The
  second-instance work (`marlow-for-taylor.md`, `install.sh --instance`) is the
  seed of per-user isolation, but on a shared box the threat model changes:
  users are strangers to each other.
- **A public endpoint** (`funnel-ingress.md`), since nobody installs
  Tailscale for this.
- **The Claude login stays in Anthropic's own flow.** Anthropic's Claude
  Code legal page allows a platform hosting the unmodified CLI with each
  user signed in on their own subscription (design §7), with
  the operator under the Commercial Terms. Sign-in has to complete in
  Anthropic's flow, so an in-app login relay that carries the code through
  Marlow is ruled out. The app can at most hand the user Anthropic's link.
- **App Store distribution**, not Debug builds or TestFlight.

Reopen when the friends alpha shows how many people want Marlow but can't run
a box.
