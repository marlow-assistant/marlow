# Task — one working failure-alert channel

**Status: DECISION DEFERRED, deliberately.** Opened 2026-08-09 while building
piece 4's pusher, because the pusher needed a way to report and the survey turned
up an uncomfortable answer.

## The finding

**v2 has exactly one outbound alert channel that reaches Mike today: a turn posted
to `/internal/cron/message`, delivered by the iOS app.** Everything else built for
this purpose terminates in a log file or a stub:

- `deploy/watchdog.sh` — probes `/health`, and on no answer restarts the unit. It
  is a *liveness prober*, not a reporter: no notion of reading a sentinel, and no
  channel. Its output goes to `watchdog.log`. Easy to mistake for the answer,
  because "we have a watchdog" is true and irrelevant.
- `deploy/deadmans/notify.sh` — the right shape and an explicit stub: *"replace
  with a real out-of-band notification... Not decided in the plan which mechanism
  Mike wants."* It echoes to stderr and `logger`.
- `deploy/deadmans/README.md` — still marked **Untested**, on the grounds that
  there was no live server to check against. There is one now.
- systemd `Restart=always` and cron's `MAILTO=""` — neither reports anything to a
  human by design.

So "log it and exit non-zero" and "write a sentinel for the watchdog" are, today,
the same outcome: a line nobody reads.

## The split that makes this tractable

The two failure classes have genuinely different requirements, and only one of
them is blocked:

| Class | Server state | Channel | Status |
|---|---|---|---|
| A job/pusher/tool failed | **up** | `/internal/cron/message` → turn → phone | works now |
| The server is down or wedged | **down** | must be out-of-band, off-box | blocked on this decision |

Class A does not need this task. A rejected push says nothing about whether
marlow-server is running, so routing it through the server is sound rather than
expedient — which is why `deploy/identity-push.py` does exactly that, once per
day per failure (`client_msg_id` dedup), and why it does **not** write a sentinel
for a consumer that does not exist.

Class B is what `deadmans` is for, and it cannot use the working channel by
definition.

## The decision, when it's wanted

One question: **what does `notify.sh` do?** It runs on a *different* host, so it
may use anything that host can reach — and must not depend on Marlow, the box, or
the tailnet being healthy. Candidates, cheapest first: an SMTP relay to Mike's
email; a transactional SMS API; a push service (ntfy/Pushover); a Slack or Discord
webhook. Nothing here needs to be clever, and it should not be a second Marlow.

Not in scope: replacing the class-A channel. It works, and adding a second path
for failures that already reach the phone is machinery for its own sake.

## Decision rule — when this stops being deferrable

Mike's call, 2026-08-09: **deadmans is "far future" compared to getting Marlow v2
working and useful.** So this stays parked until one of:

1. **A class-B failure actually costs something.** The box wedges or dies and Mike
   finds out by noticing Marlow is quiet. That is the signal the switch was
   supposed to beat, and it converts this from theory to a measured gap.
2. **v2 carries something with a deadline.** Once a cron job is load-bearing for a
   real commitment — a morning brief he plans around — silent death gets expensive
   rather than annoying.
3. **Something else needs a class-B report.** A second consumer means the stub is
   now blocking two things, and the cost of deciding is amortised.

Until then the honest position is: class-A failures reach the phone, class-B
failures do not, and that is a known, accepted gap rather than an oversight. Do
not build a sentinel-file convention, a general alerting abstraction, or a
watchdog change in anticipation — see the bias-against-speculative-machinery rule.
The sentinel is about three lines whenever a consumer exists, and its format
should be designed around that consumer's needs, not guessed at now.

## Reference

`docs/tasks/marlow-working-dir-git-policy.md` piece 4 (the pusher that motivated
this), `deploy/README.md` §7a (the pusher's reporting behaviour) and §8 (the
dead-man's switch install), plan §6/§10.2 (watchdog).
