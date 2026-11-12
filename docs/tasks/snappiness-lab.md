# Task — a snappiness lab, and the first strategies to try in it

**Status: idea, 2026-11-12.** Nothing built. Mike wants Marlow to feel
snappier now that the model is selectable, and wants to try strategies on his
Mac by feel instead of shipping each one to a box and his phone.

## Why now

The 2026-10-09 read (`reply-length-and-perceived-speed.md`): on tool turns
the first word lands at 12–24s median, about 90% of the way through the turn.
CLI startup is a flat ~1.9s. The wait is the lookups, not the model or the
transport. The model picker adds a new axis (Sonnet/Low, Opus/Low) that has
never been measured for first-word time, and every comparison so far has
cost a box deploy plus a phone build.

## The lab: what to build

The server already runs locally: `scripts/dev-server.sh` gives a sandbox
under `.dev/` (gitignored), plain HTTP on loopback, and a fake `claude`
(`scripts/dev/fake_claude.py`) driven by hashtags. The web client in desktop
Chrome has voice mode, and the iOS Simulator reaches the Mac's localhost with
no forwarding. So the "phone" is already free. What's missing is
turns that *feel* like real ones. Two modes, cheapest first:

1. **Replayed timing, no model.** Teach the fake CLI a latency profile taken
   from the measured shape: the startup floor, N tool steps of a few seconds
   each, then the first text, with sentence-sized deltas at a realistic
   cadence. Add an optional pre-tool opening sentence. This is enough to
   judge UX strategies (an earcon, a spoken status line, the sentence-hold
   rule, how the activity row reads) by ear and eye. It's deterministic, so
   A/B comparisons are fair, it costs no plan usage, and it runs in CI-like
   isolation. Most of the felt-speed questions are here.
2. **The real CLI on a scratch identity.** `dev-server.sh --real`: the real
   `claude` binary, `identity/` copied in as the template, a synthetic
   USER.md and memory, and the model and effort from the sandbox's
   preferences. It uses Mike's subscription and his account's connectors, so
   lookups are real and so is their latency. Box content stays in `.dev/`
   and never enters the repo. Mike runs it from a clean shell: a builder
   session must never nest `claude -p`. This mode is for prompt and model
   strategies, where only a real model answers the question.

**A bench on top of mode 2:** a fixed set of a dozen synthetic prompts shaped
like real traffic (no-tool, one lookup, multi-lookup, voice), run against a
matrix of variants (model × effort × identity rule), with a table of
`first_output_seconds`, `first_text_seconds` and total from the sandbox's
`usage.jsonl`. Feel picks the winner, and the bench says whether the feel
generalizes. Keep it small: the prompts and the table, nothing scored.

Not in scope: a model-judged quality score, a second server process, and any
change to the box's runtime path.

## Strategies queued for the lab

- **A spoken status line on voice turns (the first one to try).** Before the
  first tool call on a voice turn, Marlow says one short sentence naming
  what it's about to check ("Checking your calendar and the ferry times").
  In a car, 12–24s of silence feels broken. The text rule "Answer before you
  look things up" bans placeholders, and that stays for written replies,
  which Mike likes filler-free. The voice version differs from filler
  because it names the specific lookups: never "Let me check", never
  "Good question". It stays in the transcript and nothing is erased. It is
  one identity bullet gated on the voice-turn marker
  (`voice-mode-hands-free.md`, "Telling Marlow it's voice mode"). Check
  whether the "working" earcon was ever built, since the two overlap.
- **Sonnet/Low vs Opus/Low on first text**, on tool and no-tool turns.
- **Lookups in parallel**: prompting for independent tool calls in one step
  instead of a chain.
- **Spoken reply length**: the voice cap, by feel.

## Considered and rejected: a second fast model for the first reply

A rival assistant shows a quick "here's what I'll do" paragraph, then a
fuller answer, and folds the first away. The public evidence (2026-11-12)
says that's one model writing a preamble before its tool calls, not a
separate model. A research design does pair a tiny "talker" model (sub-200ms
replies) with a large reasoner whose answer it weaves in. Production voice
agents mostly use one model that speaks a specific interim line while a tool
runs, and one vendor warns that generic filler games the latency metric: what
counts is time to the first *relevant* words.

A second model doesn't fit Marlow:

- **One work queue serializes every `claude -p` call** (CLAUDE.md), so a fast
  call can't overlap the real one.
- **Each call pays the ~1.9s CLI floor** and spends plan usage.
- **A call without the conversation can only produce generic filler**, and
  giving it the conversation means a second cold cache.
- **It brings back erasing text the user already read**, which Mike rejected
  on 2026-10-09.

The status line gets the same felt benefit from the model that is already
running.
