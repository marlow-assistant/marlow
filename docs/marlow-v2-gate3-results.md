# Marlow v2 — Gate #3 Results (Server Skeleton)

**Date:** 2026-07-18
**Scope:** plan §2/§3/§6 — `server/` module skeleton, the tailnet + loopback
APIs, liveness artifacts, first-device pairing. Built and tested from Mike's
dev laptop (not the Marlow server itself — see §5 below for what that means
and doesn't mean).

## 1. What's built (`server/`)

- **`config.py`** — env-driven settings, no hardcoded paths. Public API bind
  host auto-detects the tailscale interface (`tailscale ip -4`); falls back
  to `127.0.0.1` with a loud startup warning rather than ever defaulting to
  `0.0.0.0` (no-public-ingress is a hard constraint, not a preference).
- **`boundaries.py`** — the §5.1 concrete paths (verified in gate #2:
  `.credentials.json`, `~/.ssh`, `.env` files) plus argument-shape regexes
  (`find -exec/-delete`, `rm -rf`, `git push --force`) for gate #4's hook to
  use — data only, no enforcement here.
- **`log.py`** — rotating file + stream logging with field-targeted secret
  redaction (`accessToken`, `refreshToken`, `device_token`, `Bearer ...`).
- **`invoke.py`** — the only module that shells out to `claude -p`. Scrubbed
  env (strips `CLAUDE_CODE_*`/`CLAUDECODE`/`AI_AGENT`), `--strict-mcp-config`,
  stdin from `/dev/null`, `--continue`/`--resume` session routing, JSON
  parsing, `permission_denials`-array detection (not exit code, per
  gate1-results §1). Explicitly does **not** implement a speculative
  `--settings <path>` flag for cron-vs-iOS policy swapping — that CLI
  behavior was never validated in gate #1 and guessing at it would violate
  the plan's own "don't guess" rule; that's gate #4/#5 work.
- **`queue.py`** — single asyncio work queue, one `claude -p` in flight at a
  time. Turn store with `client_msg_id` idempotency and a `delivered` flag so
  `/v1/pending` hands back each ready turn exactly once (see bug note below).
- **`auth_monitor.py`** — reads the gate #1-confirmed credential shape,
  watches `refreshTokenExpiresAt`. `reauth_needed` is derived from the
  timestamp itself, not from an observed failure signature — that signature
  is still gate #1's one open sub-item.
- **`pairing.py`** — device pairing + token store. First-device bootstrap
  (plan §8, resolved this gate): an admin-issued out-of-band code via a
  loopback-only endpoint (see below), not a standalone script (see bug note).
- **`push.py`** — stubbed per Mike's call (Apple Developer account/cert is a
  separate long-lead track); real interface, log-only body.
- **`health.py`** — `/health` shape from §3.1. `oldest_pending_permission_age`
  is always `null` until gate #4 builds `permissions.py`.
- **`app.py`** — two ASGI apps: `public_app` (tailnet, client-facing) and
  `internal_app` (loopback-only). Permission-round-trip endpoints exist and
  return `501` — kept in the contract now so it doesn't shift shape later.

## 2. Real bugs found by actually running it (not just import-checking)

Ran the server against a fake `claude` binary and drove the full pairing →
message → pending flow with curl, then converted that into a pytest suite.
Two design bugs surfaced this way that a syntax check would have missed:

1. **`/v1/pending` was backwards.** First draft returned still-in-flight
   turns; the spec (§3.1) means it to return *ready* turns after a push wake.
   Fixed: `queue.py` now tracks a `delivered` flag; `ready_undelivered_turns()`
   returns each completed turn exactly once.
2. **`WorkQueue`'s `asyncio.Queue` was constructed at import time**, before
   any event loop exists. Fine for the one-process production case, but it
   meant the queue silently bound to whichever event loop first touched it —
   surfaced as `RuntimeError: bound to a different event loop` the moment a
   second server lifecycle ran in the same process (every test). Fixed by
   creating the queue inside `start()`, which always runs in the loop that
   will actually serve requests — also makes the singleton properly
   restartable, which it wasn't before.
3. **The pairing admin CLI was disconnected from the running server.** First
   draft was a `python -m server.pairing issue-code` script — but
   `PairingStore`'s challenge table is in-memory, so a separate process
   issuing a code would never share it with the process the app actually
   talks to. Moved to a loopback-only endpoint
   (`POST /internal/admin/pairing-code`) on the already-running server
   instead.

## 3. Test suite (`tests/`, 34 tests, all passing)

Added per Mike's ask — replaces ad-hoc curl smoke-testing with something
repeatable. `tests/conftest.py` points the whole `server` package at an
isolated temp sandbox (fake `claude` binary, fake credentials file, temp
data/log dirs) *before* any `server.*` import, since `config.py` builds its
settings singleton from `os.environ` at import time.

- `test_boundaries.py` — secret-path/arg-shape matchers.
- `test_auth_monitor.py` — missing/corrupt credentials, expired refresh
  token, pre-warn window.
- `test_pairing.py` — happy path, wrong code, double-redeem, expired
  challenge, token persistence across store instances.
- `test_invoke.py` — env scrubbing, JSON parsing, permission-denial
  detection, missing-binary and non-JSON-stdout error paths.
- `test_queue.py` — single/serial invocation, `client_msg_id` idempotency,
  the `ready_undelivered_turns` fix above.
- `test_app.py` — full FastAPI `TestClient` integration: pairing → message →
  turn fetch → pending, 401 without a token, 501 stubs for gate #4.

Run: `pip install -r server/requirements-dev.txt && pytest tests/`

## 4. Deploy artifacts (`deploy/`) — **written, not installed anywhere**

- `marlow-server.service` — new dedicated `marlow` user (per Mike's call),
  systemd hardening (`ProtectHome=read-only` etc.) with explicit
  `ReadWritePaths` for the credential write-back path (gate1-results §6) —
  caught in review that `ProtectHome=read-only` would otherwise silently
  break the token auto-refresh.
- `watchdog.sh` — hits the *internal* (loopback) `/health`, not the tailnet
  one, so a tailscale outage doesn't get misread as a process wedge. Added
  `internal_get_health` to `app.py` to make that possible.
- `deadmans/` — must run on a different host; `notify.sh` is a stub (logs to
  stderr/syslog only) since the actual alert channel isn't decided.
- `egress.nft` + `resolve-egress-ips.sh` — nftables allowlist keyed off
  `boundaries.EGRESS_ALLOWLIST`, refreshed periodically since hostnames
  don't resolve inside nft rulesets. **Untested against real interfaces.**

**Update (2026-07-18, post-review):** the cross-user sharing this section
originally flagged as open is resolved — Marlow v2 is standalone, not a
shared instance with `mike`. `marlow` gets its own `git clone` of this repo
(a one-time seeded copy of `CLAUDE.md`/`SOUL.md`/`USER.md`/`memory/`, which
then diverges independently) and its own interactive `claude` login (its own
`~/.claude/.credentials.json`, no cross-user write-back). This also resolves
plan §8's "Discord's fate": v1 (tmux + Discord) is retired once v2 is proven,
not kept as a permanent parallel fallback — so the two Marlows diverging is a
transition period, not a permanent state. `deploy/README.md` §0,
`marlow-server.service`, and the new `deploy/update.sh` (dev-laptop-codes-it /
Marlow-box-runs-it update path: fetch, fast-forward-only merge, reinstall
deps, restart, verify `/health`) all reflect this now.

## 5. What "built on the dev laptop" means here

Everything above was written, syntax-checked, and exercised against a fake
`claude` binary (proves the plumbing: routing, auth, queue serialization,
pairing) — not against a live Pro-auth `claude -p` or the real server
environment (tailscale, the `marlow` user, real credentials). Nothing in
`deploy/` has been applied anywhere. Gate #3's Mike-checklist items (approve
the first real device pairing, confirm the dead-man's switch actually texts
on `kill`) still need to happen on the actual server.

## Next

Resolve deploy/README.md §0 (user/credential access), then actually install
on the server: create the `marlow` user, decide the working-tree/credentials
access story, install the systemd unit + watchdog, set up the dead-man's
switch on a different host, verify `egress.nft` against real interface names.
Gate #4 (permission round-trip: `permissions.py`, `hooks/pretooluse.py`) is
the next build gate — its endpoints already exist as `501` stubs here so the
contract doesn't move.
