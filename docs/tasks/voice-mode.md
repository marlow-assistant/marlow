# Task — push-to-dictate button (voice-in, step one)

**Status:** ✅ **shipped, merged 2026-08-11; transcript-loss fixes
merged 2026-08-12.** Reopened 2026-08-16 and 2026-08-18 because dictation kept
losing text and Mike stopped using it; the streaming design was abandoned
2026-08-18 for "record first, transcribe once" (below). Confirmed on device
2026-08-18, and **Mike confirmed 2026-09-25 that dictation no longer drops text
in daily use**; the drop bug is closed. Transcription *quality* is a separate
question, owned by `ios-modern-on-device-dictation.md`. Captured 2026-08-01,
re-scoped 2026-08-10 to this one small step. **Shape:** client-side only, no
server change; `POST /v1/message` already takes already-STT'd text.

Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

## What this is

A dictation button next to send: a Claude-app-style push-to-dictate control, not
a hands-free mode. Tap to record, tap to stop, transcribe on-device. Spoken
replies and the hands-free loop are `voice-mode-hands-free.md`; they were kept
out of this step, which was not blocked on anything.

**Manual start/stop is the endpoint, full stop.** No VAD, no silence timeout;
Mike decides when the turn is over. `maxDuration` stays 180s, only so a
forgotten-open mic doesn't run forever.

## The STT choice — settled 2026-08-10: Apple, not whisper.cpp

`design.md` §STT assumed whisper.cpp, before the client existed. That means
bundling a model, C++ interop and build config in an app with no audio or speech
framework at all. Apple's on-device recognition satisfies the identical privacy
contract, *raw audio never leaves the device* (not "the transcriber must be
whisper"), with no bundling or interop. Flagged as a deliberate override of
`design.md`; don't silently re-adopt whisper.cpp without re-reading this.

**Settled 2026-09-30.** `ios-modern-on-device-dictation.md` answered it on
Mike's own audio: iOS 26 `SpeechTranscriber`, on-device. Parakeet TDT 0.6B v2
measured better but was abandoned as not worth a 465 MB download. That doc
considers and rejects the Whisper family for this experiment, so the override
still stands.

## Apple-server opt-in (2026-08-30, removed 2026-09-30)

Settings briefly exposed "Use Apple servers for speech recognition," off by
default, setting `requiresOnDeviceRecognition = false` (the flag allows or
prevents network use for the whole request; it is not a selective
low-confidence fallback). `requiresOnDeviceRecognition` had originally stayed
`true` because the point of on-device STT is that audio never leaves the device
(`design.md` §297). Device-tested 2026-08-30 and Mike left it on, but whether it
was *more accurate* was never measured. Dictation is on-device only again.

**One unexplained freeze, did not reproduce.** On the first launch of a fresh
install, the first tap on this toggle appeared to hang the app; the write landed
(read `on` after a force quit) and it behaved normally afterwards, and a clean
reinstall didn't reproduce it. Nothing in the write path explains it
(`@AppStorage` KVO is per-key). The suspicion is the first-launch pairing path,
where the notification-permission alert and the shared `.sheet(item:)` in
`ChatView` can collide over an open sheet. Left alone deliberately; that sheet
binding is load-bearing (`ios-permission-round-trip-ui.md` §4). If a settings
sheet ever wedges again, pause the debugger and read thread 1 before changing
anything.

**Noise filter and recognizer confidence.** `isLikelyNoise`'s `0.5` bar was
calibrated against *on-device* confidence scores, which run low; Apple's server
recognizer scores higher, so in server mode the fan-noise-as-"no" auto-send is
likelier to slip through. **Don't retune the threshold from theory**: log
confidences from a real device run first and set a second bar from those.

## What shipped (first version) and what real use added

- **Live level meter**, 24 bars, RMS mapped to dB rather than linear
  (`.measurement` session mode disables AGC, so linear clips or flatlines).
  Calibrated on-device: `-65dB`/`-35dB` brackets normal speaking at arm's
  length. Level updates moved off a per-buffer `Task {@MainActor}` (a race
  source, could write after teardown) to a lock-protected value polled at 50ms,
  in its own `LevelMeter: ObservableObject` so `ChatView.body` doesn't
  re-evaluate on every update.
- `taskHint = .dictation` and `contextualStrings = ["Marlow"]`: cheap tuning
  that leaves the privacy contract alone.
- **Noise-transcription filtering.** Fan noise was occasionally transcribed as
  a short word ("no") and auto-sent with no error. Fixed by averaging
  `SFTranscriptionSegment.confidence`, only when at least one segment reports a
  nonzero value (some devices always report `0.0`, meaning "not computed").
  **Rescoped 2026-08-11 after it ate a real dictation:** averaging over the
  whole transcript sank a minute of good speech in a noisy room under 0.5 and
  discarded it as "No speech heard." Now gated to **≤2 segments**, the
  one-word case it was written for; longer is trusted. Both discard paths
  `os_log` segment count and average confidence (never text).
- **"No speech heard" banner** separates silence from a genuine
  mid-recognition failure.
- Unrelated fix: cold-launch message list rendered blank until scrolled
  (`ScrollViewReader.scrollTo` in `.onAppear` raced `LazyVStack`'s first layout;
  deferred via `DispatchQueue.main.async`).

**Deliberately not done** (explicitly deferred by Mike, not oversight): meter
bars pulse rather than scroll ("try it on the phone" first), keyboard focus
restoration after dictation, VoiceOver labeling on `LevelMeterView`, named
constants for the magic numbers.

## Long-clip handling — decided 2026-08-12

Mike hit it on device: a minute-plus of real speech, banner, nothing sent.
Fixes in `DictationController`:

- **Never let an empty update overwrite a real transcript.** The handler
  assigned `bestTranscription.formattedString` unconditionally, so an empty
  revision (one can arrive with `isFinal`, or a straggling partial after it,
  since the `@MainActor` hops are unordered) wiped the dictation.
- **`stop()` tears down in three steps:** `stopCapturingAudio()` →
  `endAudio()` → `releaseAudioSession()`. It was deactivating the
  `AVAudioSession` under a live recognition task, which makes the task *fail*
  rather than deliver a final result, and that failure discarded everything.
  Moving `endAudio()` first alone was wrong the other way (a `/code-review
  medium` finding): the tap keeps calling `request.append` until removed, and
  appending after `endAudio()` can throw on the audio thread.
- **A recognizer that ends itself no longer strands a live mic.**
  `finishRecognition()` ran on any `isFinal` (including the audio-duration
  limit) without tearing down, so the dictation sent while the engine kept
  running, and tapping stop called `endAudio()` on a nil'd request and hung.
- **A recognizer failure sends whatever it heard**, through a `deliver()`
  shared with the clean-finish path, instead of "Dictation stopped
  unexpectedly."

## Lost text, and why the streaming design was abandoned — 2026-08-18

Three rounds of fixes didn't stop long dictations arriving truncated. The
2026-08-16 round (a `committed`/`pending` split, `isFinal` restarting the task,
a generation filter, a restart ceiling, a first-character heuristic guessing at
un-announced restarts) made it *better* and not *correct*: a ~1 minute
dictation arrived with only its last sentence, the same symptom on the branch
that had fixed it twice.

The root problem was structural. `SFSpeechAudioBufferRecognitionRequest` ends
its own task at utterance boundaries and at an internal audio-duration limit
and gives no reliable way to tell "this utterance is done" from "this dictation
is done." Everything above reassembled a transcript across that ambiguity;
every piece was a seam, and every bug was at a seam. **The deciding fact was
that partials were never displayed**: streaming's only benefit is live text on
screen, and the UI shows a level meter, not a transcript, so the complexity
bought nothing. Mike's call, 2026-08-18, explicitly accepting no-partials.

**What replaced it: record to a file, transcribe it once.** The tap writes to an
`AVAudioFile`; `stop()` closes it and runs one `SFSpeechURLRecognitionRequest`
with `shouldReportPartialResults = false`. `DictationTranscript` is gone and
`DictationController` lost about a third of its body. The stranded-dictation
watchdog was retired with it: `stop()` now awaits one call that returns or
throws, so there is no callback to fail to arrive.

**First device run, same day: truncated again, and it was the bridge.** 22.1s
recorded (4.2MB, full clip on disk) transcribed to 51 chars in 0.6s, a
*partial* hypothesis. `shouldReportPartialResults = false` doesn't stop a URL
request delivering hypotheses, and the `async` bridge resumed on the first
result of any kind as a defensive catch-all; the first ~5s was taken as the
answer and later results hit the single-resume guard. **Only `isFinal` settles
it now**; non-final results are banked so a task that dies early still delivers
its best hypothesis, and a watchdog (20s floor, scaled to clip length) keeps a
never-resuming continuation from hanging `isTranscribing`. The symptom matched
the streaming bug and the cause was unrelated: three lines of misplaced
caution.

**Second and third runs: the recognizer endpoints even on a file.** A 22.1s
clip returned its first ~5s; a 21.5s clip returned its tail (152 chars where
~13 chars/sec predicts ~280); a 6.3s clip was perfect. One utterance surviving
out of several. **The premise was half wrong**: a file removes seams from the
*audio*, but the URL request still endpoints at pauses and resets its
hypothesis, so the `isFinal` result covers only the last stretch of speech.
First result loses the ending; final result loses the beginning.

**`DictationAssembler` — accumulation again, but on a real signal.**
`SFTranscriptionSegment` carries `timestamp` and `duration`, so each result
states what audio it covers. A result whose first segment starts later than the
one it would replace *cannot* be a revision of it. That is the recognizer
telling us where a restart happened, unlike the deleted first-character
heuristic (a coin flip that deleted a minute of speech whenever two utterances
began with the same letter). Threshold 0.5s, just above revision jitter; a real
endpoint jumps by seconds. Results are collected synchronously under the
recognizer's lock and replayed into the assembler in order after recognition
finishes, **not** via a `Task { @MainActor }` hop per result, which is
unordered and could make a late early-hypothesis look like a restart. That
ordering hazard is the one piece of the streaming design worth keeping as a
lesson. About 60 lines of pure logic with 10 tests against observed failures;
the log prints assembled chars against the final result's chars, and the gap is
text endpointing would have thrown away.

**Measured on device, 2026-08-18:**

- **The ~1min limit does not reach file-based recognition.** 82.3s of audio
  gave 1075 chars: 13.1 chars/sec against the 13.2 baseline from a clean 6.3s
  clip. File-splitting is not built; the decision rule is retired.
- **Latency is a non-issue.** 2.8s to transcribe 82.3s (3.4% of realtime)
  behind a `Transcribing…` state with both buttons inert. Revisit only if a
  clip near 180s scales worse than linearly.
- **The assembler's value.** On that run the final result alone was 200 chars
  against 1075 assembled: 81% of the dictation lost by trusting `isFinal`. The
  24.5s run before it: 13 chars against 159.

**Punctuation, enabled 2026-08-18.** `addsPunctuation` defaults to false, hence
one unbroken run on the first long dictation. Claude reads unpunctuated text
fine; the argument is the *review* path, where Mike edits the transcript before
sending.

**Durable telemetry, 2026-08-18.** The only log lines on the loss path were
`.info`, memory-only and gone by the time anyone looked. All dictation
telemetry is now `.notice`/`.error`, which persist: `dictation started`,
`dictation stopped` (seconds, bytes), `dictation transcribed` (audio seconds →
chars, segments, average confidence, elapsed), `dictation failed to transcribe`
(error domain/code). Audio in versus text out identifies truncation. Never logs
transcript text. Console filter: subsystem `com.marlow.app`, category
`dictation`.

**Two endpoints, decided 2026-08-16 (Mike's call).** Auto-send with no review
was the other half of why the feature went unused: a mis-transcription was
unrecoverable and a truncated one invisible. **Mic** finalizes into the text
field for editing; **send** finalizes and sends. This also makes the review path
immune to the first-turn `canSend` race. Every other way a dictation can end
(length cap, backgrounding, recognizer failure) routes to review, so nothing
fires a turn Mike didn't ask for.

## Testing

`DictationOutcome` + `DictationOutcomeTests` cover the noise gate (the van-fan
"no") and the regression that made it dangerous: a long dictation with poor
average confidence must never be discarded. The old accumulation tests were
deleted with the mechanism.

**No real-audio integration test, still deliberate.** The Simulator reports
`supportsOnDeviceRecognition = false` (probed 2026-08-18), so `start()` bails at
its first guard, and a macOS target is out because `AVAudioSession` is iOS-only.
On a real device it is now worth doing (`xcodebuild test -destination
'platform=iOS,id=<device>'` with a recorded fixture). **Assert no content loss,
not exact text** (a distinctive phrase from the first ~10s and the last ~10s);
exact transcripts drift across OS versions. The fixture stays out of git, since
a recording of Mike's voice is personal content.

**Device smoke check after touching dictation** (the teardown paths aren't
unit-testable; steps 2–4 were never formally ticked off):

1. Speak ~90 seconds, pausing between sentences, tap **mic**. The whole
   transcript should land in the field (the pauses are the point). Note how
   long `Transcribing…` is up, and compare audio seconds to chars in
   `dictation transcribed`.
2. Speak a sentence, tap **send**: it sends without passing through the field.
3. Tap the mic and immediately stop: "No speech heard."
4. Type a few words, dictate, finish with mic: the dictation *appends*.
