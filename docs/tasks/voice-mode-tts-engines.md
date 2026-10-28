# Voice mode: a neural voice on the phone

**Status:** shipped on iOS, merged 2026-10-21; the separate
voice-engine change was folded into it, since voice mode is unusable without a good voice.
Voice mode reads with **Phonon**, run on the phone's Neural Engine, and with
nothing else by choice. Heard on Mike's phone; 300 sentences in a row ran
clean there. iOS only, like voice mode itself.

## Why

On 2026-10-21 Mike found voice mode's Apple voice "ropy", even at Premium.
He asked for two leading on-device engines next to Apple's, picked in
Settings, with a one-off model download. After hearing them all the same
day, he cut it down to Phonon alone. Apple's voice was "basically unusable",
so voice mode needs Phonon downloaded before it opens.

## What it is

- **Phonon 31m** (Gradium, 2026-10-20, Apache-2.0). It's the Pocket TTS
  recipe, trained by the Kyutai founders' spin-off. The app offers its 25
  English voices, 13 of them British, plus the checkpoint's default.
- **Runtime:** FluidAudio's Core ML host (`PocketTtsSmallManager`, pinned to
  commit `c79c410`, since Phonon landed after v0.17.7). The FlowLM runs on
  the Neural Engine and the Mimi decoder in fp32 on the CPU. The pack comes
  from `FluidInference/pocket-tts-coreml`.
- **Minimum iOS raised to 18**, because the host needs MLState. Every
  Marlow phone runs 26.
- **Measured on Mike's iPhone 14 Pro, iOS 26.6** (`PhononSoakTests`, 300
  sentences):
  - RTF 0.082, so speech is made about 12× faster than it plays.
  - 0.1–0.5 s per sentence.
  - 4.3 s to load once the Neural Engine compile is cached.

## How it got here (2026-10-21)

1. **Kokoro-82M and Pocket TTS (Kyutai, January 2026), both on
   sherpa-onnx** (k2-fsa's ONNX Runtime wrapper, CPU).
   - Kokoro sounded good, though "pre-ElevenLabs".
   - Pocket was hissy and muddy. Temperature 0.3 instead of sherpa-onnx's
     0.7 cut the hiss. That's Kyutai's English default (kyutai-labs/pocket-tts
     #324).
   - But Charles still came out an American horror-trailer narrator, where
     Kyutai's demo Charles was clean and British.
   - The cause: sherpa-onnx only runs Kyutai's January model. From
     `english_2026-04` on, the configs set `insert_bos_before_voice`, which
     sherpa-onnx doesn't implement.
2. **Pocket TTS moved to FluidAudio's Core ML host,** running Kyutai's small
   `english_18m_refl_2026-10`, with Phonon on the same host beside it.
   - Mike ruled out a hand-written runtime for the newer graphs, and wanted
     the phone's accelerated hardware used.
   - He accepted the Core ML crash risk below.
   - Pocket became faster and less muddy, and even takes breaths.
   - Prosody varied by voice: one voice was bad, Charles "decent but
     slightly flat emotionally".
   - Phonon "clearly winning".
3. **Phonon only.** sherpa-onnx, Kokoro, Pocket TTS and the engine picker
   were removed. Kokoro's and Pocket's downloads (384 MB and 141 MB) are
   deleted at launch on phones that have them.

Rejected along the way:

- **FluidAudio's Core ML Kokoro.** Its `KokoroAne.md` (v0.17.7) says a BNNS
  segfault "on iOS … the 26.6 line still crashes (#844)", after 8–20
  synthesis calls in the report. It also says no iOS version or routing is
  shown safe for long sessions.
  - The crash is in Apple's `libBNNS`, which Core ML also uses for CPU
    segments, so Phonon's CPU-side Mimi decoder could meet it.
  - That's what `PhononSoakTests` is for. 300 calls passed. The reported
    crash came and went by the day, so a pass is evidence, not proof.
- **Kyutai's newest 100M Pocket models.** Neither sherpa-onnx nor any Core
  ML build runs them (FluidAudio's 100M pack is the January model).
- **Phonon 40m.** It's non-commercial (CC BY-NC 4.0), and has no Core ML
  build.
- **Kokoro via MLX** (`kokoro-ios`). GPU-only, so it can't run in the
  background.
- **Piper, Kitten, Matcha; Supertonic, Chatterbox, Orpheus.** Either a step
  below in quality, or larger or less proven on a phone.

## How it works

- **Voice mode** (`VoiceModeView`): until Phonon is downloaded, the mode is
  a download screen. It says what's needed (153 MB, once) and shows
  progress, Cancel and Try again. When the download finishes, the mode
  itself appears.
- **Preparing:** straight after the download, the store loads the model once
  (`compileForNeuralEngine`). That way iOS compiles it for the Neural Engine
  while the screen says "Preparing the voice…", rather than during the first
  sample or reply. FluidAudio measured that compile at 10–150 s; Core ML
  caches it.
- **Settings → Voice mode:**
  - Phonon's voice.
  - The download.
  - Once ready, **Play a sample** and **Delete** on one row. The sample is
    read a sentence at a time, so the first plays while the second is made.
  - The footer is one line linking to Phonon's model card, where its makers
    and licence are. Apache-2.0 asks for no credit line in the UI.
- **`NeuralSpeech`** fits the existing `SpeechOutput` seam, so
  `VoiceModeController` didn't change.
  - Sentences are synthesised one at a time on a serial queue, so the next
    one is made while the last plays.
  - Playback goes through one `AVAudioEngine` in voice mode's session.
  - A stop bumps a generation counter, and stale work and callbacks are
    dropped.
  - Apple's voice is the fallback, never a choice. If the model won't load
    or a sentence fails, every sentence from then on is Apple's, since the
    wrong voice beats silence in a car.
- **`PhononSynth`** drives FluidAudio's async API from that queue by
  blocking it, a GCD thread rather than a Swift cooperative one. It loads
  the pack from the app's own folder, so FluidAudio never downloads
  anything.
  - (`DictationSettings` still deletes `Application Support/FluidAudio` on
    launch, left over from the Parakeet spike. The voice doesn't use that
    folder.)
- **Downloads (`VoiceModelStore`):**
  - Exactly the files in the bundled `VoiceModels.json`, each pinned to a
    Hugging Face commit with its size. `scripts/voice_model_manifest.py`
    writes it, keeping what FluidAudio's own downloader keeps plus the
    offered voices. Rerun it to bump the model.
  - Files go into `phonon-<fingerprint>.partial`, and the folder is renamed
    when complete, so a folder without the suffix is always whole.
  - A cut-short download resumes at the file it was on.
  - Four files download at a time.
  - Stored in `Application Support/VoiceModels`, excluded from backup.
  - The screen stays on while downloading.

## Text Phonon reads oddly

Phonon was trained on American English. Its own text clean-up
(`PhononTextRewriter` in FluidAudio) handles currency, dates, URLs and some
number forms, but it does **not** expand unit abbreviations ("km/h" is only
kept in one piece), has no rule for "°C" or "%", and reads "$1" as "1
dollars".

- **Fixed:** British "-re" measures. Marlow writes Canadian English, and
  "centimetres" and "kilometres" sounded wrong; the "-er" spelling sounded
  right to Mike. `PhononSpelling.american` swaps metre/litre (any prefix),
  centre, theatre and fibre in the text sent to Phonon only; the chat keeps
  Marlow's spelling.
- **Suspected, not heard:** "-our" and "-ise" words (common in English
  text, so probably fine), "programme", "tyre", "grey", "kerb", and the
  symbols above. Don't swap "aluminium": it's said differently. Add a rule
  when one is heard wrong in real use; a probe recording of each is cheap.
- **Mitigated by the note:** spoken replies are asked for plain sentences,
  so Marlow spells units out rather than leaving abbreviations to Phonon.

## Licences

| Item | Licence |
|---|---|
| Phonon 31m weights and their Core ML conversion | Apache-2.0 |
| FluidAudio | Apache-2.0 |

Nothing is bundled: the app downloads the model when the user asks.

## Review (`/code-review high`, 2026-10-21)

Fixed across the passes:

- **Route change stalls the controller.** A route change could flush queued
  sentences without their "finished" callbacks, leaving voice mode in
  "speaking". The engine's live buffers are now tracked and reported
  finished.
- **Cancel race.** A Cancel landing just before a file started was lost.
- **Model folder key.** The folder is named by a fingerprint of the
  manifest, so a changed file list is a new download.
- **Screen lock mid-mode.** A download finishing re-enabled the idle timer
  under voice mode. Both now hold `ScreenAwake`.
- **Sample reloads.** The loaded voice is kept until Settings closes.
- **Silent failure.** A Core ML synthesis error now falls back to Apple's
  voice, instead of a silent "finished" sentence.
- **First compile.** It now happens while the screen shows "Preparing",
  instead of during the first sample or reply.

Left, until the phone shows they matter:

- **Silence before the first word.** The "still working" ticks stop when a
  sentence is handed to the voice. Phonon makes a sentence in 0.1–0.5 s on
  the phone, so this is likely moot.
- **A stop doesn't cancel a sentence already being synthesised.** It runs to
  the end before the next starts; at 0.5 s at most, likely moot.
- **A sample and voice mode each load their own model.**
- **Orphaned partial folder.** A delete during a download could leave a
  `.partial` folder. The UI can't do that: a download shows Cancel, not
  Delete.
- **The voice picker hitches for a moment** when picking (Mike's phone).
  Nothing heavy runs on a pick. Suspect the iOS 26 menu with 26 items.

## Not yet known (device checklist)

1. **Crashes in real use,** with playback and route changes, which the soak
   test doesn't exercise.
2. **Memory** over a long session.
3. **Sound over AirPods HFP.** The mixer resamples 24 kHz down to the call
   route.
4. **The "Preparing" wait** on a fresh download, timed.
