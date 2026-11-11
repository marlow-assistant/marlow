# Read aloud: hear one reply on demand

**Status:** built on both clients (2026-11-11), on a branch. Unit tests on
both, and a Playwright run of the web menu and pill with a fake
`speechSynthesis`. **Not yet heard on a phone**: neither Phonon on Mike's
iPhone nor the web app on Android has read a reply this way.

## What it is

Press and hold a finished reply of Marlow's: the menu has **Read aloud**
under Copy. It reads that reply in the voice picked in Settings → Voice.
While it reads, a **Stop reading** pill floats above the composer, and the
same message's menu says Stop reading instead. Reading another reply
replaces the first. Error captions and Mike's own bubbles get Copy only.

Decided with Mike, 2026-11-11:
- **Stopping:** the pill, plus the menu item. Chosen over menu-only and
  tap-the-bubble.
- **iOS before Phonon is downloaded:** Apple's voice reads, as the Settings
  sample does (`VoiceChoice`). Hiding the item, or pointing to Settings, were
  the alternatives. Voice mode itself still won't open without Phonon.

## How

The reply is read the way voice mode reads one: `SentenceChunker.finish`
splits it into sentences, `CodeFenceFilter` keeps code out ("The code is in
the chat."), `SpeechText.plain` / `plainSpeech` strip the markdown, and each
sentence goes to its own `speak()`, so Phonon makes the next one while this
one plays.

- **iOS:** `MessageReader` (`Models/MessageReader.swift`), a `@StateObject`
  on `ChatView`. It keeps the loaded speech between reads of the same voice
  and builds a fresh one when Settings' voice changes. Session `.playback` /
  `.spokenAudio`, like the Settings sample, so the silent switch doesn't mute
  it. When dictation starts or voice mode opens it stops but leaves the
  session alone (`handOff`), because by then they have set the session up
  for themselves. Opening Settings stops it, so a voice sample can't play
  over it. It is released (model freed) when the app goes to the
  background, because the app has no background audio mode.
  **So locking the phone stops a read.** Listening to a long reply with the
  screen off would need the `audio` background mode. That's a product call
  for Mike, not done here.
- **Web:** `MessageReader` (`lib/messageReader.ts`), owned by `Chat` through
  `useMessageReader` (`app/ReadAloud.tsx`), reached by rows through
  `ReaderContext`. The speech is voice mode's: `PhononSpeech` over
  `BrowserSpeech`. It reads with the browser's voice until Phonon has
  loaded, then with Phonon, and Phonon is only fetched for someone who reads
  with it. The stored voice is read on every read, so a change in Settings
  applies to the next one. `unlock()` runs inside the tap, before anything is
  awaited, because iOS Safari speaks only after a speak in a user gesture.
  Once Phonon has been started it reads everything, so picking a device voice
  afterwards swaps in a fresh speech that never loaded it (`fits`). It stops
  when dictation starts, voice mode opens or Settings opens.

## Open

- Hear it on Mike's iPhone, both before and after Phonon is downloaded.
- Hear it on Android Chrome, including the first read, which starts the
  ~65 MB Phonon download and reads in Google's voice until it lands.
- iOS keeps its own Phonon in memory alongside Settings' preview while both
  are loaded. Not measured; the reader frees its model in the background.
