# Task — modern on-device dictation

**Status: ✅ DONE 2026-09-30.** Dictation runs iOS 26's
`SpeechTranscriber` on-device, with the old recognizer as fallback. Parakeet
was measured better but abandoned. The developer corpus capture is removed.

**Why it was done:** Marlow's dictation was usable, but it visibly lagged the
in-app dictation of the big chat assistants (Mike, 2026-09-29).

## How it works now

The engine is chosen at each dictation, never by the user
(`AppleEngineSelector`, a pure function):

| iOS 26+ | `SpeechTranscriber.isAvailable` | en-US asset installed | runs |
|---|---|---|---|
| yes | yes | yes | `SpeechTranscriber` (`.transcription`, `SpeechAnalyzer(inputAudioFile:)`) |
| yes | yes | no | old recognizer, and the asset install is kicked |
| yes | no | — | old recognizer |
| no | — | — | old recognizer |

- **Record first, transcribe the whole file once.** Streaming lost text at
  the seams (see `DictationController`'s type comment).
- **Never waits for the asset.** The OS owns the download. The app reserves
  the asset (best effort) and installs it at launch and on every foreground.
- **Always on-device.** `AppleFileRecognizer` hard-codes
  `requiresOnDeviceRecognition = true` with no parameter to change it.
  SpeechTranscriber works in airplane mode (checked on the phone). Settings
  shows one row plus "Dictation runs on this iPhone. Audio never leaves it."
- **A watchdog on both engines.** A transcription that never returns settles
  after max(20 s, recording length) with whatever was heard, because a hang
  leaves the mic button inert.
- **The noise gate is the old recognizer's only.** It keys on per-segment
  confidence, which SpeechTranscriber does not return. It exists for fan
  noise heard as "no" and auto-sent. If SpeechTranscriber ever does that,
  the preset with confidence attributes is the lever.
- **Testable without a microphone:** `DictationTranscriptionPipeline` +
  `AppleTranscribing` hold the outcome rules (noise gate, salvage on
  failure).
- **Telemetry** names the engine that ran (`appleSpeechTranscriber` /
  `appleLegacy`).
- **On launch, `DictationSettings` deletes what earlier builds left behind.**
  That covers the retired settings keys (including the earlier "Use Apple
  servers" toggle), the capture's retained audio, the spike's folders and ~965 MB of
  Parakeet models. Nothing needs a reinstall.

**Automated coverage:**
- **Engine selector:** every row of the table above.
- **Request:** always on-device.
- **Noise gate:** it fires on one low-confidence word, not after a restart,
  and never on SpeechTranscriber output.
- **Failures:** salvaged text is delivered; with nothing salvaged, the
  outcome is "nothing heard".
- **Cleanup:** it removes every retired key and folder, and leaves the
  folder's neighbours alone.

The SpeechAnalyzer glue and its watchdog need a real device, so they have
no unit tests.

**Not separately observed on the phone:** a clip near the 180 s cap, and
backgrounding mid-recording.

## Why this engine

Mike's iPhone 14 Pro (iOS 26.6), 68 real dictations plus 3 spliced long
clips, 4357 reference words:

| engine | WER | <15 s clips | 172 s clip took | |
|---|---|---|---|---|
| old `SFSpeechRecognizer`, on-device | 5.7% | 8.0% | 8.5 s | misses place names |
| old, Apple servers | 5.7% | 8.0% | 45 s | same words; removed |
| **`SpeechTranscriber`** | **3.5%** | 3.7% | 2.3 s | **shipped**, no app download |
| Parakeet v2 (FluidAudio) | 2.6% | 2.2% | 1.9 s | best, but 465 MB and warm-up machinery |
| Parakeet v3 | 3.0% | 4.7% | 2.3 s | worse than v2 |
| `DictationTranscriber` | 5.9% | 8.5% | — | the old model behind the new API |

- **The deciding judgement:** SpeechTranscriber closed most of the gap at
  zero install cost. A morning's use was "great" (Mike), so Parakeet's
  remaining ~0.9 pt was not worth a 470 MB download plus readiness states
  and fallbacks.
- **The references are "silver":** Whisper large-v3 served as a fifth
  hypothesis, and they were adjudicated blind. They can only contain words
  one of the original five engines produced, so SpeechTranscriber's score is
  pessimistic, by about 0.1–0.2 pt when read by eye.
- **The same model on phone and Mac:** 69 of 71 SpeechTranscriber
  transcripts were identical, so a Mac run is a valid cheap first test for
  a future Apple model.
- **Other alternatives were ruled out:**
  - Parakeet Unified doesn't load on an A16 (FluidAudio #828).
  - TDT-CTC 110M was only a resource fallback.
  - The Whisper ports have no batch path as good as FluidAudio's.

## If this is reopened

- **Parakeet:**
  - **Code:** tag `parakeet-scaffolding-2026-09-30` has the readiness routing
    (`DictationRouter`), the engine protocol, the fallback and wait-for-warm
    paths, and fake-engine tests.
  - **Package:** the FluidAudio 0.15.6 pin was added with the scaffolding.
  - **Engine and harness:** tag `dictation-spike-2026-09-29`.
  - **Design:** readiness states, a staged install, a cellular download
    warning, and falling back to Apple while it warms. It is in an earlier
    revision of this doc.
  - **Known unknowns:** an app reinstall colds its CoreML cache (22 s first
    load), and its memory doubled between builds for no known reason.
- **Measuring a new engine:**
  - **Corpus:** `~/Documents/DictationCorpus-*` on the Mac, the only copies,
    never in the repo.
  - **Results and references:** `~/Documents/DictationSpike-{results,silver,whisper,spliced}`.
  - **Harness and scorer:** the spike tag (`scripts/dictation_spike.py`).
  - **SpeechAnalyzer arm and Mac tool:** branch `speechanalyzer-spike`
    (`scripts/speech_analyzer_mac.swift`).
- **Corpus capture:** tag `dictation-corpus-capture-2026-09-30`. If it is
  restored, keep its guard rails:
  - default off, and visibly on while running;
  - the preference is the expiry date;
  - excluded from backup, deleted on any failure;
  - audio moved before its sidecar is written;
  - delete-all needs confirmation.

  One bug was parked: about 2% of clips were a bare 4096-byte header.

**Parked:** "Marlow" is misspelt 2 times in 70, so a post-transcription
replace if it grates. A SpeechTranscriber noise gate only if the failure is
seen.
