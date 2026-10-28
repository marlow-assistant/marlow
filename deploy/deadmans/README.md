# Dead-man's switch — install on a DIFFERENT host

`check.sh` curls the Marlow server's `/health` over the tailnet and alerts if
it's been unreachable longer than `MARLOW_DEADMANS_THRESHOLD_HOURS` (default
2h). It must run somewhere other than the Marlow server — e.g. Mike's laptop,
another always-on box on the tailnet, or a small cloud VM — since the point
is surviving the case where the server itself is down.

`/health` needs a device token on the public listener, and this host holds none on
purpose, so `check.sh` counts a `401` as up: the server answered through the
tailnet, which is what it watches. No answer, or a `5xx`, is down.

## Install

1. Copy `check.sh` and `notify.sh` to the other host.
2. `chmod +x check.sh notify.sh`
3. **Replace `notify.sh`'s body** with a real alert mechanism — it currently
   only logs to stderr/syslog, which nobody will see. Not decided in the plan
   which channel to use (SMS? email? something else?) — that's a Mike call.
4. Add a cron entry:
   ```
   */15 * * * * MARLOW_DEADMANS_HEALTH_URL=https://marlow.<tailnet>.ts.net:8443/health /path/to/check.sh >> /var/log/marlow-deadmans.log 2>&1
   ```
   Use the tailnet **role name**, never a `100.x` address — the deploy target
   is `marlow.<tailnet>.ts.net` (see `docs/marlow-v2-START.md`). The
   public listener is TLS-only as of deploy/README.md §8b — `http://` here
   will fail against a real deploy.

## Untested

This has not been run against a live server — there's no server to check
against yet. Verify it actually fires by `kill`ing the marlow-server process
once gate #3 is deployed (per the plan's own gate #3 Mike-checklist item).
