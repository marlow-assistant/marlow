# Task — Marlow loses to other AI assistants on small questions

**Status: TIME-TO-FIRST-WORD READ 2026-10-09; fix merged the same day
and live.** Waiting on lookups dominates; CLI startup does
not (table under "Read 2026-10-09"). The fix is an identity rule: write a
useful opening before the first tool call, only when there is one. Next:
re-read `first_text_seconds` on tool turns after a week.

## The symptom

Mike reported (2026-09-11) reaching for other AI assistants and claude.ai for anything
quick, despite Marlow holding all his context. His own framing was that the
stock apps "recall enough details" to be equivalently useful, and he assumed
the cause was either raw model speed or a dumber-but-faster tier winning on
responsiveness.

## What the telemetry said

Attended (`source=ios`) turns since 2026-09-01, n=89, from
`~/.marlow/logs/usage.jsonl`:

| Output tokens | Turns | Median duration |
|---|---|---|
| <200 | 9 | **4.4s** |
| 200-600 | 23 | 11.5s |
| 600-1200 | 25 | 20.4s |
| 1200+ | 32 | **31.8s** |

Median reply **935 tokens**, p90 **2,683**. Median turn **19.2s**, p90 57.1s.
64% of turns exceed 600 tokens.

**Duration tracks reply length almost exactly. It is not the box, the model or
the network.** Config was already the fast setting (`sonnet` / effort `low`,
`server/config.py:243`), and thinking fires on 78% of turns at only ~220 median
tokens — a couple of seconds, not twenty. A short Marlow reply already lands in
**4.4s**, competitive with what Mike was defecting to.

So: **Marlow is not slow, Marlow is long.** A rival assistant's instant mode was not winning on
intelligence or infrastructure. It was answering small questions at their own
size.

## The second-order finding, which is the more important one

Verbosity was *burying* the differentiator. The one sentence in a 935-token
reply that could only come from Marlow's context — where the van is, what the
board holds, what was decided last week — sat in paragraph three. A generically
good answer in 4s beats a specifically good answer in 20s whose specific part is
hidden, which is why the drift happened even on turns where Marlow was right.

**Corollary for positioning:** do not sell "it knows you, so it is worth the
wait". Rival assistants' memory is good enough now that *knowing him* is no longer the
moat, and nobody believes a wait-longer pitch at the moment they reach for the
phone. The moat is **knowing right now** — today's board, today's plan, his
actual location. That has to arrive first and short.

## What shipped

`## Response style` in the live identity gained a `### Length — answer at the
size you were asked` subsection (default to a few sentences; short is not
generic; depth means precision not volume), and **lost** the bullet
`- Technical depth is welcome; don't simplify unless asked.` — an unbounded
licence that was probably a live contributor.

## The paragraph Mike held back, kept here on purpose

Drafted as the closing paragraph of that subsection and **cut on his call
2026-09-11** to keep the per-turn identity cost down. Bring it back only if the
measurement says the wording underperformed — the reason to prefer it is that a
rule carrying its own measured cause survives a later session softening it into
a style preference:

> **Why (measured 2026-09-11):** median attended reply was 935 tokens, p90
> 2,683, and duration tracks length almost exactly — under 200 tokens finishes
> in 4.4s, over 1,200 takes 31.8s. Mike had started reaching for other AI assistants for
> anything quick. Not because it knew more: because it answered small questions
> small.

## How we will know (re-run 2026-09-18)

```
ssh marlow@marlow 'python3 - <<PY
import json, statistics as st
rows=[json.loads(l) for l in open("/home/marlow/.marlow/logs/usage.jsonl")]
rec=[r for r in rows if r.get("source")=="ios" and not r.get("is_error")
     and r.get("ts","")>="2026-09-11"]
out=sorted(r.get("output_tokens",0) or 0 for r in rec)
d=sorted(r["duration_seconds"] for r in rec)
print("n", len(rec))
print("output p50/p90", out[len(out)//2], out[int(len(out)*.9)])
print("duration p50/p90", round(d[len(d)//2],1), round(d[int(len(d)*.9)],1))
PY'
```

Baseline to beat: output p50 **935** / p90 **2,683**; duration p50 **19.2s** /
p90 **57.1s**. The bucket table above predicts that an output p50 near 250
tokens puts duration p50 around **6s**.

**Judge the goal, not the metric.** Shorter replies are not the point — a drop
in output tokens with Mike still defecting to other AI assistants is a failure, not a
success. The question is whether he stops reaching past Marlow for quick
things, and his report is the real gate. The numbers only say whether the
wording changed behaviour at all.

**Watch for the over-correction:** the morning brief and the weekly digest are
*supposed* to be substantial. They run from `cron/*.md` job prompts that ask for
named sections, so they should hold — but a thin Friday digest is this change's
most likely casualty and the first place to look.

## Re-run 2026-09-25

140 attended turns since 2026-09-11. Output p50/p90 **722 / 2,570** (was
935 / 2,683); duration p50/p90 **16.9s / 43.1s** (was 19.2s / 57.1s). The
median moved, the tail barely did, and it is nowhere near the ~250 / ~6s the
bucket table predicted. Mike's call: length is fine now.

But duration is time to the *last* word, and reply text streams — so it was
never what he feels. Split by what the turn did:

| turn | share | duration p50 | output p50 |
|---|---|---|---|
| no tools, reply <300 tok | 11% | **4.8s** | 107 |
| no tools | 31% | 9.9s | 507 |
| 1-2 tool steps | 46% | 16.4s | 585 |
| 3+ tool steps | 24% | 28.0s | 1,298 |

Small untooled answers are already as fast as the rivals. 69% of turns reach for a
tool, and the suspicion (unmeasured) is that Marlow looks things up *before*
writing a word, so the first word waits on every lookup. Two smaller
contributors: CLI startup (0.7–2.4s, `turn-activity-streaming.md`) and
sentence-sized deltas vs a rival assistant's per-token animation.

**Instrumented, not yet read:** `usage.jsonl` now carries
`first_output_seconds` (CLI startup floor) and `first_text_seconds` (first
reply delta at the server; the app's poll interval adds on top). After a week,
which one dominates picks the fix — answer-then-check prompting, a warm
process, or rendering. Do not build any of them before that read.

## Read 2026-10-09: waiting on lookups dominates

232 attended turns, 2026-09-25 to 2026-10-09, bucketed by `num_turns`
(1 = no tools):

| turn | share | startup p50 | first text p50 / p90 | duration p50 |
|---|---|---|---|---|
| no tools | 37% | 1.9s | **5.0s** / 11.2s | 7.9s |
| 1-2 tool steps | 43% | 1.8s | **12.5s** / 21.3s | 13.8s |
| 3+ tool steps | 20% | 1.9s | **23.8s** / 43.6s | 26.9s |

On tool turns the first word lands ~90% of the way through: Marlow does every
lookup before writing anything. Startup is a flat ~1.9s, so a warm process is
worth little, and rendering is noise against 12-24s.

**The fix, and why it is shaped this way.** Mike, on the rival-style
"something before the tool": it is often fancy filler, and the rival then
*erases* it for a final answer, so text he read disappears. So the rule
(`identity/CLAUDE.md`, "Answer before you look things up") is confidence-gated:
write an opening only when it would stand alone if the tools added nothing,
never a placeholder, and a lookup that contradicts it gets an explicit
correction. Nothing is erased here: `_whole_reply` (`server/invoke.py`) has
reassembled pre-tool text since the 2026-09-04 Twillingate truncation.
Memory reads for a known thread count as "the lookup is the answer", so the
rule cannot override "read the entry before you respond" with a stale guess.

**How to judge it.** `first_text_seconds` on tool turns drops only where an
opening was written, so the shape after a week says how often it fires and
what it buys. Filler, and how often corrections appear, is Mike's call by feel.
Frequent corrections would mean the rule is doing harm.

## Cache writes — MEASURED AND CLOSED 2026-09-11, do not reopen

This was ranked #2 on the theory that a 24k-per-turn cache write was eating the
3.22s time-to-first-text. **It measured as zero latency. The lead is dead; the
twenty seconds is reply length and nothing else.**

Hold agentic loop depth (`num_turns`) *and* output length fixed, and turns that
write a lot of cache are not slower than turns that write almost none:

| loop depth | `cache_creation` < 8k | `cache_creation` >= 20k |
|---|---|---|
| 2 tool steps | 16.0s (out 679) | 11.0s (out 549) |
| 3 tool steps | 14.5s (out 559) | 13.6s (out 542) |

Normalised the same way: **21.2 s per 1k output tokens** for small-write turns,
**21.5** for big-write turns. Not a small effect, an absent one.

**The trap that produced the false lead, worth remembering.** The first cut held
only *output length* constant and showed 16s -> 22s, which read as a clean 6s
tax. It was confounding: the turns that write 35k of cache are the tool-heavy
turns, and the tool calls are what cost the six seconds. Controlling for
`num_turns` collapses the gap entirely. `num_turns` only began recording
2026-09-02, so the controlled comparison rests on n=14 vs n=32 — small, but both
framings agree and the effect is absent rather than merely small.

**Two corrections to how the number was read.**

- **The identity block is exonerated.** `identity_hash` is constant across every
  turn of all ten sessions since 2026-09-01 — zero variation. The "prefix may not
  be stable" hypothesis is disproved; `identity.py`'s `_session_cache` does
  exactly what `cache-friendly-identity-injection.md` specified. Don't re-litigate
  that design from this direction.
- **24,186 was a misleading median.** The distribution is bimodal, not a flat
  tax: p10 **2,103**, p50 24,186, p90 **54,557**. A quarter of attended turns
  write under 5k (near-perfect cache hits) and 17% write over 40k. There is no
  steady 24k overhead to remove — there are cheap turns and expensive turns.

**One anomaly survives, as a cost note only — not worth a project.** Three
consecutive turns on 2026-09-01 (06:16, 06:18, 06:19, session `2bedd03f`) wrote
51,765 -> 54,557 -> 57,256 while `cache_read` sat flat near 78k, on gaps of
128s/82s/35s, so not TTL expiry. The CLI transcript for that window shows each
turn generated only ~7k *characters*: one thinking block, some text, one
`tool_use` with a 277-byte result. Writing 50k+ to record that is
disproportionate, and the monotonic growth suggests a mid-prefix block being
rewritten each turn rather than read. Unexplained, and deliberately left that
way — on a Pro subscription this burns rate limit, not wall clock and not
dollars. Reopen only if rate-limit exhaustion becomes a felt symptom.
