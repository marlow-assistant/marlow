# Task — voice mode (talk to Marlow, hear it answer)

**Status:** ✅ iOS shipped, merged 2026-10-21, with the Phonon voice
(`voice-mode-tts-engines.md`). In daily use on Mike's phone; on a second user's
phone from 2026-10-21. The web/Android half, first deferred by Mike
(2026-10-20: "This is for me, and I'm on iOS"), merged separately 2026-10-23 on
box-side transcription; see `voice-mode-android.md`.

Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

Built on push-to-dictate (`voice-mode.md`) and on reply streaming
(`streaming-reply-text.md`, 2026-09-03), which was the original blocker:
`Turn.partial_text` grows while a turn runs and the app fast-polls it, so speech
starts on the first sentence instead of after the whole reply.

## What it is

A mode Mike enters on purpose. The chat goes away and the screen becomes one big
button, a car-mode screen: usable at a glance, with a thumb. He talks, Marlow
answers out loud, he talks again. Leaving puts him back in the chat, where every
voice turn is already an ordinary bubble. For driving, walking the dog, anywhere
reading isn't available; AirPods are just a better mic and speaker.

## Decisions (Mike, 2026-10-20)

1. **Conscious entry, no auto-engage.** A voice-mode button in the composer; no
   headphone-connect trigger. The 2026-08 auto-engage ask is dropped, not
   deferred. When the field is empty and no photo is attached, send's slot shows
   the voice button (confirmed 2026-10-20); with something to send it is send.
2. **One big toggle button is the whole interface** (plus a small exit):
   press to record, press to stop, transcribe and send with no review; press
   while Marlow speaks to interrupt.
3. **The manual press is the endpoint** (next section).
4. **Replies are read out in full**, no spoken summary. Marlow is told the turn
   is spoken so it keeps answers short itself.
5. **Interrupting stops the phone's speech, not the turn.** `claude -p` can't be
   interrupted and there is no cancel route; the full text lands in the
   transcript. Only the TTS is cut.
6. **The voice screen is an overlay on `ChatView`**, not a separate
   conversation; turns use the same `POST /v1/message` and transcript.
7. **Record, transcribe once, send; no streaming of speech to the LLM** (below).
8. **AirPods stem press: not now.** Mike preferred the on-screen button.
9. **An interrupt also starts recording** (confirmed 2026-10-20). You interrupt
   to say something, and two presses double the work at that moment. The button
   then means one thing in every non-busy state: *stop whatever Marlow is doing
   out loud and listen to me*. Pressing while waiting does the same (the
   in-flight reply isn't spoken, it's in the transcript, and the new turn queues
   behind it).
10. **Permission requests: no spoken approval.** Accepting a spoken "yes" for
    tool calls by an agent with shell access is a trust-boundary change. The
    screen is up in this mode, so the existing approval card shows over the
    voice screen with a distinct tone; the request's details aren't spoken.

## The load-bearing constraint: never auto-endpoint

Mike pauses mid-sentence while thinking. VAD or a silence timeout would cut him
off and send a truncated turn, and turns are queued and serialized, so a bad one
isn't cheap to take back. The toggle makes this free: the turn ends when he
presses, never otherwise. **Do not add silence detection, even as a "safety
net."** The `maxDuration` (180s) guard for a forgotten-open mic stays; hitting it
sends nothing and says so.

**An earcon for "working."** A performative "one moment!" was rejected on
SOUL.md grounds (`START.md` known-gap #4); a short non-verbal tone is honest
because Marlow isn't *speaking* filler. It matters most in a car, where he isn't
looking.

## Why not stream input

Mike, 2026-10-20: record-transcribe-submit makes sense, "but if streaming
*were* possible we should consider it." Considered; not worth it:

- **`claude -p` takes a finished prompt.** Starting a turn speculatively and
  cancelling is out: one serialized queue, no cancel, burns Pro usage.
- **The most it could save is transcription time**, 2.8s for an 82s clip
  (`voice-mode.md`). Time-to-first-text is ~3s of CLI floor and the long tail is
  reply length (`reply-length-and-perceived-speed.md`); input streaming touches
  neither.
- **Live transcription is where text got lost** in 2026-08; reverting for ~1–2s
  is a bad trade.

The perceived-speed lever is output: speak the first sentence as soon as it
exists. Revisit only if stop-to-sent latency is what Mike feels.

## Telling Marlow it's voice mode

Mike, 2026-10-20: "be cautious of longer answers". Every word is read out,
so length is the listener's time.

**Per turn, not per session.** The CLI session continues across turns; a
one-time "you're in voice mode" would keep Marlow terse after Mike left.
`spoken: true` on `POST /v1/message` → `Turn.spoken` → `run_claude(spoken=)`,
which puts `SPOKEN_TURN_NOTE` (`server/turn_context.py`) on the line **directly
above the message**. Not inside the `[Context: ...]` block, as first planned:
that block returns "" on any failure and the note is the one thing voice mode
can't lose. It rides the prompt, not the cached system prompt, so it costs no
cache. The phone sends the fact; the server owns the wording.

It is a property of the turn, not the device: a spoken and a typed message can
sit in the queue together. **Coalescing treats a change in `spoken` as a
boundary**, like a change in effort, so a typed message is never read as spoken
or the reverse; consecutive spoken messages still merge (`_can_coalesce` fails
closed on an unclassified field).

**The note is the only place voice mode is described to Marlow.**
`identity/CLAUDE.md` had a "Voice mode" section too and the two drifted: each
length fix went into both and still lost to the general "few sentences" rules in
PREFERENCES.md and SOUL.md. Mike, 2026-10-21: drop the section, let the harness
say it at each spoken message. The note says it overrides the usual length and
style for that message only, which also tells Marlow the next typed message is
answered normally. Spelling, symbols and markdown are the phone's job.

**The phone still strips markdown before speaking** (`**`, `#`, backticks, URLs
read aloud are noise); a code block becomes "The code is in the chat."

## Speaking the reply

- **Engine: on-device only.** Replies draw on Mike's memory and email, and
  `push.py`'s contract is that content never touches third-party infrastructure.
  A cloud voice is a deliberate contract change, never a library swap.
  `AVSpeechSynthesizer` first; replaced by Phonon (`voice-mode-tts-engines.md`).
- **Chunk at sentence boundaries off `partial_text`.** Speak each complete
  sentence as it arrives; hold the trailing fragment until the next poll extends
  it or the turn finishes. A sentence ending in a terminator is spoken once a
  poll passes without it growing (the spike's fix, below). Tool-call marks
  (`partial_text_marks`) split sentences; `finish()` realigns on `result_text` by
  the last 40 chars spoken and says nothing rather than reread if it can't.
- **Audio session:** one `.playAndRecord` + HFP session for the mode's life (the
  spike below is why). `DictationController` has a `SessionRole`: in voice mode
  it records into that session and never deactivates it.
- **Interrupted replies in the transcript:** the bubble shows Marlow's full
  text. Marking where Mike stopped listening ("you stopped here") and telling the
  next turn's context the same is the **interrupt marker, not built.** It would
  stop Marlow referring back to a part Mike never heard.

## The device spike — Mike's iPhone + AirPods, 2026-10-20

A throwaway read `partial_text` aloud sentence by sentence (Music = `.playback`
vs Call = `.playAndRecord` with Bluetooth HFP). Prediction: text outruns speech
by >10× (~430 chars per 2s poll against ~15 chars/s). It answered both
questions. Times are from the first poll that saw the turn running.

| run | route | first text | first speech | stalls | speech ended after reply |
|---|---|---|---|---|---|
| short Q, default voice | Music/A2DP | 2.3s | 3.7s | 0 | 5.4s |
| short Q, Premium | Music/A2DP | 2.7s | 3.9s | 0 | 2.8s |
| calendar (tool first) | Music/A2DP | 5.3s | 5.3s | 0 | 14.1s |
| text → calendar → text | Call/HFP | 2.6s | 5.0s | 1 × 0.5s | 2.2s |

- **Polling is tight enough.** On the calendar turn speech finished 14s after
  the reply did. A tool call mid-reply cost one 0.5s stall, because the sentence
  before it covered most of the tool's runtime. Interrupt-cuts-TTS-only is right:
  by the second sentence there is usually nothing left of the turn to cancel.
- **HFP is acceptable (Mike).** Hence the single session: no category switch per
  press, the mic route stays live (keeps voice barge-in possible later). Session
  setup measured 0.0–0.1s either way.
- **Premium voice is a must.** The fallback (Samantha, default quality) was "the
  world's worst TTS"; Jamie (Premium) was usable. Siri voices aren't available
  to third-party apps (Mike downloaded one first and got the fallback).
- **The sentence-hold rule cost 1–2.5s on the first sentence** when it was the
  last text so far (rows 1, 2, 4; absent in row 3). Fixed by the
  unchanged-across-one-poll rule above.
- **Silence before the first word is 3–5s** (CLI floor plus a tool), where the
  "working" earcon belongs.
- **Voice quality was still the weak point (Mike, 2026-10-20):** even Premium is
  "quite low quality". The parked answer was an on-device neural TTS model
  before any cloud voice, trading a model download for the privacy contract
  intact.

## What the voice screen built (2026-10-20)

`VoiceModeController` (state machine idle → recording → transcribing → waiting →
speaking, following one reply at a time; an interrupt drops that reply's later
polls; terminal states are read off `ChatStore.messages`, and a reply that
vanishes counts as failed), `SentenceChunker`, `SpeechText.plain`,
`VoiceAudioSession`, `SystemSpeech` (stale delegate callbacks after a stop are
dropped so they can't end the next reply), `Earcons`, `VoiceModeView` (an overlay
*under* `PermissionOverlay`; keeps the screen awake; stops cleanly on
backgrounding or an audio interruption and waits for a press). Send path:
`ChatStore.sendSpoken(_:)`. Earcons are synthesised: listening, sent, failed; a
low "purr" (300 Hz, volume flutter) 0.8s after sending and every 3s while Marlow
works with nothing to say; a "tink-tink" when an approval card appears, every
10s in place of the purr until answered. Mike picked both by ear from rendered
options (2026-10-21). Unit-tested; the mic, synthesizer and AirPods route only
run on a device.

**Deliberately not in it:** background audio (`UIBackgroundModes: audio`; the
screen stays awake instead and locking the phone stops the mode; recording that
survives a lock has its own privacy-indicator questions); the interrupt
marker; voice barge-in; a voice picker; a failed spoken send retried manually
from the chat goes out as typed.

**`/code-review high` on the screen (2026-10-20):** 7 findings, 6 fixed (the view
built an audio engine on every `ChatView` render; the receive chime played over
the spoken reply; a code block spanning sentences was read out, now
`CodeFenceFilter`; a fast close-and-reopen let the old screen tear the new one
down; a reply failing mid-speech kept talking; doubled haptics). **Accepted:** a
press within a second of an interruption that caught "Transcribing…" finds the
recorder busy and quietly returns to idle; a second press works.

## On the phone

**First use (Mike, 2026-10-21):** "Seems decent so far." Interrupting worked.
The button was too big, so it was halved (half the screen's width, 130–210pt
tall). TTS was "ropy" even on Premium, the second time Mike said so, which
released the neural-TTS idea: settled next day on Phonon alone, on the Neural
Engine (`voice-mode-tts-engines.md`).

**Second day (2026-10-21), tuned the day it merged:**

- **Reply length.** A spoken "how much snow on Friday?" got four sentences.
  The note now says "a sentence or two, round numbers, overrides your usual
  length". `claude -p` runs on the box showed it doesn't clip answers that need
  more (steps, a story, route advice: 100–165 words).
- **The opening sentence.** "If you'll use a tool, say so first" was skipped in 3
  of 4 live tool turns (10–15s of silence). "Always start with one sentence" led
  in 9 of 9 test runs. The phone speaks it while the tools run, and the purr
  resumes after it.
- The tink-tink also plays on the iOS chat screen and the web client, so one
  sound means "approval needed" everywhere. Phonon read "centimetres" oddly, so
  the phone respells British measures for it.

## Open, parked (2026-10-21)

- **Model and effort for spoken turns.** Even a perfect opening sentence comes
  3–4s after the message reaches the box: Sonnet at medium effort thinks before
  writing. Mike: "it's possible Haiku in medium is better than Sonnet at Low but
  we don't know yet." Needs an A/B on real spoken questions, judged on quality
  as well as speed.
- **Silence during transcription.** Between the second press and the "sent" tone
  nothing plays. If it feels long, play "sent" at the press.
- **Weather answers recite every number**, even under the note. The lever is
  probably the weather tool's result (a short summary first), not more note
  wording. Only if it still bothers Mike.
- **Measuring reply length.** A script reporting spoken replies' word counts from
  box transcripts was proposed and declined for now.
- **The interrupt marker** (above), **voice barge-in**, **background audio.**
- **Not reviewed:** `/code-review high` didn't run on the late changes (the
  spelling swap, the note rewrite, the purr, the approval chime); each has tests
  seen to fail with the change reverted.
- Pre-tool text ("Let me check your calendar", then silence while the tool runs)
  is spoken; probably fine, even good, but no device verdict is recorded. Navigation prompts and calls: the screen
  stops and waits for a press rather than inheriting the framework default.
- **Device checks that stay worth running** after touching this (none run in the
  Simulator): a pause of several seconds mid-recording sends nothing; interrupt
  mid-reply stops speech and records; leaving the mode while listening or
  speaking stops both and sends nothing; locking the phone mid-reply stops
  speech; with AirPods, mic and speech both route to them at better than
  phone-call quality; an approval card appears over the voice screen with the
  tink-tink (seen working 2026-10-21); a typed turn after exit gets a normal
  length answer.
