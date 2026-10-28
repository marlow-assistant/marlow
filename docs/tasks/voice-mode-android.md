# Task — voice mode for Android (the web client)

**Status:** steps 4–5 built, plus a voice-mode debug report, **merged
2026-10-23**. Tested on Mike's box from desktop Chrome and the Android
emulator; every other box installs speech on its next `update.sh`. Sent to the
Android user the same day with what to look out for (misheard names, the phone's
voice setting, a screen reader talking over Marlow); **waiting on their first
debug report.** Web only by design (iOS has its own on-device voice mode,
shipped 2026-10-21, `voice-mode-hands-free.md`, `voice-mode-tts-engines.md`).
The phone is a recent Pixel, so Google's text-to-speech engine is the default
and Chrome should offer Google's voices.

Compressed 2026-10-26: the build plan, specs and test lists are cut; git history has the full version.

**Verified in a cloud session** (no mic, no emulator, no phone): the real 110M
through the whole engine, on its sample speech and on a real earnings call with
car and café noise mixed in (below); a file desktop Chromium's own
MediaRecorder recorded going through PyAV, whole and cut, word-perfect; the full
browser path against the dev server with the real model (a 31 s recording cut at
pauses into 3 segments, joined in order with no words lost, sent `spoken: true`,
reply read out 0.9 s after release, fake speech synthesis);
`deploy/install-voice.sh` against the real model archive and venv.

**Verified on Mike's box, 2026-10-23** (`update.sh` installed the packages and
model; speed check 0.79 s for 15 s of speech):
- **Desktop Chrome on a Mac, Mike speaking:** dictation, long dictation, voice
  mode, interrupting a reply, and leaving mid-recording (sends nothing). Debug
  report, two utterances of 25 and 29 s: each pause-cut once near 14 s, the box
  0.54–0.73 s per segment, round trip 0.6–0.84 s, release to text 0.6–0.76 s.
  Release to first speech ~5.3 s, of which ~4.6 s is Marlow's turn, as on iOS:
  speech to text needs no more speed work.
- **Android emulator** (Pixel image, Chrome 156, the Mac's mic fed from its
  speakers): Chrome on Android's recordings decode and transcribe; the first mic
  prompt; voice mode end to end with Google's on-device en-GB voice; the debug
  report. Android's recording log (`dumpsys audio`) shows the mic held for exactly
  as long as voice mode is open and let go on close, held only while dictating,
  and let go at once when Home is pressed mid-dictation, with what was said kept
  in the draft. The **installed PWA** runs standalone, reuses the site's mic
  grant without a second prompt, and dictates correctly.
- Seen in passing: Chrome on a Mac followed macOS's input to an iPhone across the
  room (Continuity), which looked like a slow start and a cut-off last word. Not
  a code problem; worth one line if the Android user hears it with a Bluetooth
  headset.

**Still open:**
1. The 110M on **real phone-mic speech**: a car, an accent.
2. A **denied mic** (the notice). Not tried.
3. **Actual audio output on a phone**: Google's voice quality, the earcons'
   volume, the interrupted-as-end rule and the `onend` watchdog on a real engine,
   the Screen Wake Lock holding. The emulator played one reply; the Pixel is the
   real test. macOS's default voice (Daniel) is wooden; better ones are downloads
   in Spoken Content, not a code change.
4. The pause cutter on a real phone in a real room, a car above all (Mike's quiet
   room sat at the 0.01 threshold floor).
5. The clipboard on a real phone ("Copy debug info"; the report itself is checked).
6. TalkBack.

### Names heard wrong (parked, 2026-10-23)

The 110M heard "Marlow" as "Arlo" in Mike's first test. Not built, per the bias
against unmeasured fixes; it waits for the Android user to feel it. First thing
to try: sherpa-onnx **hotwords** (a boosted word list), which needs
`modified_beam_search` instead of greedy decoding; unverified for this NeMo
transducer, and the speed check must be rerun with it on. Fallback: a small
server-side replace for a few known names ("Arlo" → "Marlow"), cheap but blind
to context.

### Setting a box up

`update.sh` and `install.sh` do it by default (`deploy/install-voice.sh`, Mike
2026-10-22: default to useful): pip install `server/requirements-voice.txt`,
download the 110M into `~/.marlow/data/models/` against a pinned SHA-256, time 15
s of speech and warn above 2 s but install anyway; a slow figure is measured
again on the next update. A failure (no network, no
wheels) never fails the deploy; the mic just stays off. Only ever the 110M: the
0.6B is too big to put on every box unasked (Mike, 2026-10-22); a box that wants
it installs it by hand and sets `MARLOW_STT_MODEL`, and `update.sh` then leaves
speech alone. `touch ~/.marlow/data/models/OFF` keeps it off. Without the
packages or model, `GET /v1/transcribe` says no and the web client shows no mic.
Opt-in real-model test: `MARLOW_STT_TEST_MODEL_DIR=<model folder> .venv/bin/pytest
-q tests/test_transcribe.py`.

### What was built

`server/transcribe.py` and `POST /v1/transcribe?utterance_id=&seq=&final=` with
the segment as the raw body (no multipart, no base64). WebM/Ogg Opus only, ≤1
MiB, ≤35 s; one segment at a time box-wide on its own worker thread; in order per
utterance, a retried segment answered from what was already heard; an utterance
belongs to the device token that started it. The model loads on first use and
stays; the first segment ever transcribed leaves `~/.marlow/data/stt_has_been_used`
and from then on every start loads the model in the background (Mike,
2026-10-22). The pause cutter (`web/src/lib/pauseCutter.ts`): cut at a ≥500 ms
pause once a segment passes 12 s; from 22 s any ≥150 ms word gap; at 25 s
regardless (a recorder can't cut "at the quietest moment of the last 3 s", which
is in the past, so the last 3 s take any word gap instead). The composer's mic
dictates into the draft; voice mode opens from send's slot when there is nothing
to send, as on iOS. A failed later segment keeps what was heard in the draft;
voice mode sends nothing instead. The test pages and benchmark scripts behind the
results were throwaway and are not in the repo; each section says what it
measured.

## The answer: the web client can do it, no native app

An installed PWA on Android Chrome has everything voice mode uses as long as the
screen stays on: `getUserMedia` + `MediaRecorder`; the Screen Wake Lock API;
Web Audio earcons; `PermissionCard` over the mode; the chunker ported over
`partial_text`; `speechSynthesis` for text to speech. What a PWA can't do is keep
recording or speaking with the screen locked or the app in the background; iOS
voice mode stops on backgrounding too, so that's parity. A Capacitor shell is
only needed if that changes, or if box-side transcription proves too slow and
on-device Parakeet is the fallback (path B).

## Speech to text: the decision that came first

Same decision as web dictation, parked 2026-10-02 when the Android user found
FUTO Voice Input good enough (`portable-web-client.md`, "PWA first, Capacitor only
if needed"). Voice mode forces it: its screen records itself, so a keyboard's
voice typing (FUTO, Gboard) can't help.

- **A: transcribe on the box (chosen).** Record a whole utterance with
  `MediaRecorder`, upload it, run Parakeet (via sherpa-onnx, int8, CPU), return
  the text. Whole-file, like iOS, which is why iOS dictation is good. One server
  path serves web dictation and voice mode, desktop included. Audio leaves the
  phone only to the user's own box, over Tailscale.
- **B: Capacitor shell, Parakeet on the phone.** An APK to distribute and a plugin
  to maintain. Needed only if the real-speech ear test (Still open, item 1) fails; the VPS
  speed rerun passed.
- **C: Chrome's `SpeechRecognition`.** Streams and ends on pauses (the problem
  `portable-web-client.md` describes), quality varies by phone, and it may send
  audio to Google, the one client that wouldn't keep voice data on the device.
  Rejected.

### The measurement: is the box fast enough?

`scripts/parakeet-timing.sh`, for a box owner to run once (no sudo, ~480 MB
download into `/tmp`, deleted after); it prints CPU, memory, load time and
transcription time after the speaker stops for 5/15/30/60 s of speech. "Fast
enough" is the wait after release, added to Marlow's own 3–4 s. iOS on-device
takes about a second. Rough bar: **a 15 s utterance in under ~2 s** matches iOS;
up to ~4 s is usable in voice mode, where the purr covers the wait, but sluggish
for typed dictation.

| Box | CPU | Cores | 15 s speech | 60 s speech | Peak memory |
|---|---|---|---|---|---|
| Mike's (2015 MacBook Air, Ubuntu), 2026-10-21 | i7-5650U, 2.2 GHz | 4 threads (2 cores) | 2.3 s | 12.1 s | 1.6 GB |
| The Android user's VPS, 2026-10-21 | Haswell (virtual, no TSX) | 2 | 3.5 s (worst 4.2) | 15.3 s | 1.8 GB of 3.7 GB |
| The Android user's VPS, **110M**, 2026-10-22 | same | 2 | 1.09 s (worst 1.11) | 4.39 s (worst 5.62) | 1.1 GB (after the 60 s decode) |

The first two rows are the 0.6B. Mike's ten-year-old laptop CPU lands right on
the bar (~0.15–0.2× real time); both transcripts of the sample clip were
word-perfect, model load 3.7–6 s once. On the VPS the 0.6B was under the ~4 s line
but over the ~2 s bar, and **memory was the blocker**: kept loaded it holds ~1.8
GB on a 3.7 GB box with 2.9 GB free, beside the server and every `claude -p` run;
loading per request adds 6 s to every utterance; and transcription competes with a
running turn for the same two cores.

### Memory is set by utterance length, and a smaller model fixes the rest

Measured 2026-10-21 on Mike's box (4 threads, 7.8 GB), from a separate shell
account so Marlow was untouched. Fresh process per figure, sherpa-onnx 1.13.8,
int8. The VPS's 1.8 GB "peak" was mostly a 60 s decode on top of the loaded
model, not the weights. Resident memory after a decode of N seconds (MB):

| Model | After load | 5 s | 15 s | 30 s | 60 s |
|---|---|---|---|---|---|
| Parakeet TDT 0.6B v2 | 802 | 835 | 897 | 999 | 1210 |
| Parakeet TDT 110M | 220 | 256 | 318 | 416 | 614 |

Each extra second costs ~7–13 MB on either model and the arenas aren't given
back, so **capping each segment bounds memory.** These are first-decode figures.
**Steady state, 110M, a 30 s utterance decoded 40 times in one process:** 496 MB
after the first decode, **683 MB from the second on, flat through the fortieth.**
No leak, but plan on ~0.7 GB at a 30 s cap; the cutter's typical 10–25 s segments
sit below that. The 0.6B wasn't rerun in steady state; expect it well above its
first-decode ~1.0 GB. Memory ratios transfer well between machines (same
architecture and runtime), speed less so.

| | 0.6B | 110M | Moonshine base |
|---|---|---|---|
| Load | 3.1 s | 1.5 s | 1.5 s |
| Resident after load | 803 MB | 221 MB | 375 MB |
| 15 s of speech | 2.4 s | 0.8 s | 1.7 s |
| 60 s of speech | 11.8 s | 4.0 s | 20.6 s |
| WER, 12 sentences | 5.1% | 1.9% | 9.5% |

The 12 sentences are Marlow-style requests (names, times, "Tailscale", "numpy")
synthesized with macOS `say`, so the audio is clean and flatters every model. The
0.6B's "errors" are formatting only ("9.30" for "nine thirty"). The 110M spells
numbers out and made three real slips: "replied" for "reply", "oak milk" for "oat
milk", "Numpi" for "numpy". Moonshine mangled the grocery sentence. The 110M will
likely trail the 0.6B on noisy audio.

**Predicted for the Android user's VPS** (110M): ~0.7 GB steady at the 30 s cap
and ~1.2 s for 15 s of speech. **Confirmed 2026-10-22:** 0.30 / 1.09 / 1.95 / 4.39
s for 5 / 15 / 30 / 60 s (0.07× real time, faster than predicted), load 2.4 s,
peak 1.1 GB after the 60 s decode, which the 30 s cap never reaches; sample
transcript word-perfect. Mike's box on the same script: 224 MB idle, 0.24 / 0.68 /
1.49 / 3.52 s, 276 / 425 / 614 / 991 MB after. `scripts/parakeet-timing.sh` now
defaults to the 110M (`MODEL=sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8` for the
larger).

**Decision:** path A with the 110M, a 30 s segment cap, the model kept loaded,
the 0.6B as a config option.

## Recording: one Opus file per segment, cut at a pause

Decided 2026-10-21, to hide transcription latency: once a recording passes ~10–15
s, cut at the first pause and upload that segment while the user keeps talking.
On release only the last segment is left, so a 40 s monologue waits about as long
as a 5 s one.

- **Raw PCM rejected** (~2 MB a minute): Opus is a tenth of that.
- **Per-segment `MediaRecorder`** (`audio/webm;codecs=opus`, 24 kbps, ~3 KB/s).
  Timeslice chunks aren't independently decodable (only the first carries the
  header), so each segment is its own recorder: stop the old one and start a new
  one on the same live stream, inside the silence.
- **Pause detection:** an `AnalyserNode` polled every ~50 ms, an energy threshold
  over an adaptive noise floor. No worklet, no VAD model; Silero only if a real
  phone shows the threshold misfiring. A hard cut at ~25 s backs it up and
  enforces the server's 30 s cap from the client side.
- **Upload:** `utterance_id` + `seq` + `final`; the server transcribes one at a
  time, in order; the client joins the texts.
- **Server decode:** PyAV is a pip wheel that bundles ffmpeg's libraries (no apt
  package), **~128 MB on disk** (27 MB package + 101 MB libraries).
- **Opus smoke test, 2026-10-21:** the 12 test clips encoded by an independent
  ffmpeg/libopus (48 kHz mono, 24 kbps, WebM, as Chrome does), decoded with PyAV
  to 16 kHz, transcribed by the 110M. Decoded lengths match the WAV exactly; WER
  1.9% (WAV) vs 2.5% (Opus), one word dropped in one clip. Opus is not the risk. A
  file Chrome itself recorded (its muxer omits duration and cues; PyAV streams
  those) was checked 2026-10-22 with desktop Chromium: right length, word-perfect;
  Chrome on Android later passed on the emulator.

**Restart gap, measured on the Android emulator (Chrome 149), 2026-10-21.** A 12 s
recording cut every 2 s (5 cuts), mic fed from the Mac:

| | Decoded | Shortfall vs wall time |
|---|---|---|
| Uncut baseline | 11.94 s | 87 ms |
| Stop, then start | 11.88 s | 143 ms |
| Start new, then stop old (overlap) | 11.88 s | 142 ms |

About 11 ms lost per cut, noise against a ≥500 ms pause; the overlap trick buys
nothing, so stop-then-start. Each cut call took 1–3 ms. With a continuous 440 Hz
tone the level was continuous across every seam; the first 20 ms of each new
segment was ~25% quieter (encoder warm-up), inside the silence. 6 segments came
to 30–37 KB.

**Unexplained:** the very first baseline run, right after granting the mic
permission, decoded 15.2 s from 12 s of wall time: audio from before the recorder
started. It didn't repeat on a rerun. If it happens on a phone, the first segment
contains speech from before the press. Guard: open the mic when voice mode opens,
keep the stream, and trim the first segment to the press time. The emulator's mic
is the Mac's audio, not phone hardware; the stream stays open across cuts so
hardware warm-up shouldn't matter, but a real phone hasn't confirmed it.

## The pause cutter and the 110M on real speech (cloud run, 2026-10-22)

Cut short when its credits ran out: **one call, 8 utterance-length clips (20 s to
2 min, 13.8 min in all), 110M only.** A first read, not a verdict.

**Data.** Clips cut from a real earnings call (Rev's `earnings21`, call 4386541, from
`github.com/revdotcom/speech-datasets`:
spontaneous speech over a phone line, with a reference transcript), each starting
and ending in a pause. Real noise from Microsoft's MS-SNSD (`github.com/microsoft/MS-SNSD`,
`noise_train/Car_1.wav`,
`Cafe_1.wav`) mixed in at the speech level minus 10 or 5 dB. LibriSpeech wasn't
available (openslr, Hugging Face, archive.org were blocked from the container).

**Method.** Each clip is one press: a fresh cutter, cut where a Python port of
`pauseCutter.ts` cuts it, fed the RMS an `AnalyserNode` gives (2048 samples at 48
kHz, every 50 ms). Segments encoded as WebM/Opus 24 kbps, decoded by
`ParakeetEngine.decode`, transcribed by the 110M, texts joined. The same clip
whole (which the server never does) is the floor cutting can't beat; a third
version is cut blindly every 25 s. Error rate uses Whisper's English normalizer
against the reference aligned to the model's timed words.

| | Whole clip | Pause cuts | Blind every 25 s | Cuts in silence | Blind cuts in silence |
|---|---|---|---|---|---|
| Clean | 7.4% | 6.8% | 8.0% | 47 of 51 | 12 of 28 |
| Car, 10 dB | 7.7% | 6.9% | 8.2% | 43 of 51 | 12 of 28 |
| Café, 10 dB | 8.7% | 9.9% | 10.0% | 40 of 54 | 12 of 28 |
| Car, 5 dB | 7.4% | 7.2% | 8.0% | 37 of 56 | 12 of 28 |

"In silence": the clean source within ±30 ms of the cut is 20 dB under the speech
level, judged on the clean audio even for noisy rows.

- **The simple cutter works.** Cutting costs nothing measurable against the whole
  clip in clean speech and car noise; café babble costs about a point. Blind 25 s
  cuts are worse everywhere. Of 212 cuts, 208 were pause cuts, 3 used the 22–25 s
  word-gap rule and 1 was a hard 25 s cut (car, 10 dB). The failure flagged in
  advance, never finding a pause in a car and falling back to the hard cut, didn't
  happen. Segments ran ~12–16 s.
- **Noise moves cuts toward words:** in silence 92% of the time clean, 66% in car
  at 5 dB. The error rate barely moves, so the misses land in short gaps the model
  rides over. Café babble, being speech-like, is where the cutter is weakest. If a
  fix is ever needed: tune the threshold first, Silero only if tuning can't do it.
- **The 110M handles noise well:** 7–9% everywhere on phone-line speech; most
  errors are names and figures, which is what to listen for in the ear test.
- **Not trusted:** a mid-word measure from the model's own word timings disagreed
  with the energy one (33 of 51 clean cuts "mid-word" where energy calls 47
  silent); a transducer's token durations run on into the pause, and capping them
  at 240 ms didn't fix it. The error rate is the judge and it agrees with energy.
- **Not done:** the other two calls, and the real browser path. Three 8-minute
  Chromium runs of the real `SegmentRecorder` (fake mic fed from this call, clean,
  car and café; Chrome's noise suppression and gain control in the loop) each
  produced 31 segments, in line with the port (~15 s each), but where those cuts
  landed wasn't analysed.

### If cutting ever needs to be smarter

Mike's idea, 2026-10-22: rather than the first pause past 12 s, pick the best one
in the 15–25 s window (at 25 s look back 10 s and cut at the quietest point). Not
possible with today's recorder: by the time a better pause is known to be behind
us it is inside the running file, and Opus in WebM can't be trimmed on the phone
without re-encoding. Two ways: **cut on the box (the one with legs)**, one
`MediaRecorder` per press uploading timeslice chunks every second, which the
server decodes as one continuing stream, so it can look back as far as it likes
with a proper VAD (sherpa-onnx ships Silero) and transcribes while the speaker
keeps talking; the phone loses the cutter and the ~11 ms gaps, the endpoint
becomes "append to this utterance" with a streaming decoder per open utterance.
Or **cut on the phone** from `AudioWorklet` PCM, each piece encoded with WebCodecs
and muxed by hand: more client code for the same result. A cheaper step first:
lower the bar over time (~700 ms at 12 s sliding to 150 ms by 22 s). **Decision
(Mike, 2026-10-22): keep the simple cutter and get real feedback from the Android
user first.**

### The voice-mode debug report

Mike's idea, 2026-10-22, built the same day: **"Copy debug info"** at the foot of
the voice screen copies a plain-text report of the last five utterances for the
Android user to paste back. Per utterance: each segment's length, why it was cut
(`pause`, `word gap`, `hard`, `release`) and the cutter's threshold then; each
upload's size, round trip, the box's own decode-and-transcribe time
(`server_seconds`) and retries; release-to-text with a word count;
release-to-first-speech; the voice `speechSynthesis` chose, and whether it runs on
the device. **Never the words.** Round trip minus box time is the network plus the
queue for the box's one worker. `web/src/lib/voiceDebug.ts`; in memory only, gone
when voice mode closes.

## Text to speech on Android

**Tried on the emulator 2026-10-21** (Pixel_10 image with Google Play, Chrome 149,
Google Speech Services). Mike's verdict after installing a better English voice in
system settings: decent, with minor interference he put down to the emulator's
audio path. Measured with a throwaway page:

- **Voices:** Chrome lists one voice per locale (en_GB, en_AU, en_IN, en_NG,
  en_US), all `local`. Which voice a locale uses is chosen in Android's Settings →
  Text-to-speech, not by the page. The list is **empty at page load and fills ~6 s
  later**: wait for `onvoiceschanged`.
- **Sentence by sentence:** gaps between queued utterances were 74, 92 and 113 ms,
  with one 490 ms outlier (an arm64 emulator on a fast Mac; a mid-range phone may
  be slower).
- **Stopping:** `cancel()` returned in 0 ms and speech stopped at once; a cancel
  then a new utterance began 130 ms later, clean.
- **Quirk:** a cancelled utterance fires `onerror` with `interrupted`, **not**
  `onend`. A chunker that waits on `onend` hangs on barge-in.
- `onend` fired every time in the short runs; keep a watchdog anyway, Chrome
  Android is known for dropping it.
- **Not tested:** the installed PWA (only a Chrome tab), a real phone, and the
  engine on the Android user's phone, which may default to the maker's own with
  different voices. Ask what phone they have.

Options: **`speechSynthesis` with Google's voices** (no download, far better than
iOS's built-in ones, which Mike found unusable; chosen), or **Phonon in the
browser**, which would need an ONNX export on onnxruntime-web/WebGPU. sherpa-onnx
can't run Phonon (it lacks `insert_bos_before_voice`; `voice-mode-tts-engines.md`)
and nobody has checked a mid-range phone could run it at real time. Only if the
Google voice fails the ear test.

**iPhone Safari (2026-10-25).** The web app on an iPhone gets only Apple's basic
built-in voices: Safari 18.7 listed Samantha, Daniel, Karen and the rest, no
Enhanced or Premium, and every one sounded bad to Mike. Web Settings now has a
voice picker (Voice section, `choosableVoices`) so the least bad can be chosen,
and Android gets the same picker over Google's voices. Phonon for the web, in the
browser or made on the box and streamed, was **parked** (Mike, 2026-10-25); see
the next paragraph.

**Phonon in the browser (2026-10-28).** Mike ran the `phonon-tts` demo on his
iPhone and it sounded great, so web voice mode can now read with Phonon 31m
(`web/src/lib/phononSpeech.ts`): the package in a worker, CPU, q8, **one thread**
(the page isn't cross-origin isolated, so no threads). The model comes from
Hugging Face (~65 MB, pinned by the package) and is cached in the Cache API; the
CSP allows `huggingface.co` and `*.hf.co` for it. **The default everywhere**
(Automatic in Settings → Voice): first planned for iPhone only, but the Android
user found Google's voices poor too (Mike, 2026-10-28). A device voice someone
picked earlier stays theirs until they choose Automatic. Settings offers the
native app's 25 voices from the pinned `gradium/phonon-voices` revision
(`web/src/lib/phononVoices.ts`), with Automatic = Alfie (Mike). Until it loads, or
if it can't (no SIMD, offline), each utterance falls back to `speechSynthesis`.
**Not yet heard** on Mike's iPhone through Marlow, or on an Android phone, and the
real-time factor on a phone is unmeasured (the debug report now records it).

## The server side and the iOS port

`POST /v1/message` takes `spoken: true` and the server adds `SPOKEN_TURN_NOTE`
(short replies, an opening sentence before any tool); the web client only sends
the flag. Nothing about voice mode is in Marlow's instructions
(`voice-mode-hands-free.md`, "The note is the only place voice mode is described to
Marlow"). The iOS code was the spec: the button's phases with **no silence
endpointing** (a press while waiting or speaking stops speech and starts
listening; the turn is never cancelled), the sentence chunker including the
sentence before a tool call, `CodeFenceFilter`, `SpeechText.plain`, the earcons
(the purr isn't ported; the tink-tink already existed in `feedback.ts`), and
leaving the mode (close, backgrounding or an audio interruption stops speech and
recording and sends nothing). The Phonon download screens aren't needed on web.
The remaining step is the ear test on the Android user's real phone, with the
engine it actually uses.
