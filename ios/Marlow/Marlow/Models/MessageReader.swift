import AVFoundation
import Foundation

/// "Read aloud" on a reply's long-press menu (docs/tasks/read-aloud.md): one
/// reply read in the voice picked in Settings, outside voice mode.
///
/// Read the way voice mode reads a reply: one `speak` per sentence, so the
/// first plays while the next is made, with code and markdown kept out. The
/// voice is Phonon once downloaded, Apple's until then (`VoiceChoice`), as
/// the Settings sample does: Mike chose that over hiding the item
/// (2026-11-11).
@MainActor
final class MessageReader: ObservableObject {
    /// The message being read, for the stop pill and the menu's label.
    @Published private(set) var readingID: UUID?

    /// What the loaded speech was built for. A change means a fresh one.
    private let voiceID: @MainActor () -> String
    private let makeSpeech: @MainActor () -> SpeechOutput
    private let activate: @MainActor () -> Void
    private let deactivate: @MainActor () -> Void
    /// Kept between reads of the same voice: loading Phonon takes seconds.
    private var loaded: (voice: String, speech: SpeechOutput)?
    private var left = 0

    init(
        voiceID: @escaping @MainActor () -> String = { MessageReader.speechKey() },
        makeSpeech: @escaping @MainActor () -> SpeechOutput = { @MainActor in VoiceChoice.speech() },
        activate: @escaping @MainActor () -> Void = { MessageReader.activateSession() },
        deactivate: @escaping @MainActor () -> Void = { MessageReader.deactivateSession() }
    ) {
        self.voiceID = voiceID
        self.makeSpeech = makeSpeech
        self.activate = activate
        self.deactivate = deactivate
    }

    /// The sentences of `text` worth hearing, as voice mode would say them.
    static func sentences(of text: String) -> [String] {
        var chunker = SentenceChunker()
        var fences = CodeFenceFilter()
        return chunker.finish(text)
            .map { SpeechText.plain(fences.filter($0)) }
            .filter { !$0.isEmpty }
    }

    /// The voice, and whether Phonon is downloaded: a reader built before
    /// the download would otherwise keep Apple's voice after it, and one
    /// built before a delete would keep a model whose pack is gone
    /// (`/code-review high`, 2026-11-11).
    static func speechKey() -> String {
        let downloaded = VoiceModelStore.shared.directory(for: .phonon) != nil
        return "\(VoicePreference.voice().id)|\(downloaded)"
    }

    func read(id: UUID, text: String) {
        let sentences = Self.sentences(of: text)
        guard !sentences.isEmpty else { return stop() }
        // Replacing one read with another keeps the session up: bouncing it
        // tells other apps to resume, then ducks them again.
        let wasReading = readingID != nil
        halt()
        let voice = voiceID()
        if loaded?.voice != voice {
            loaded = (voice, makeSpeech())
        }
        guard let speech = loaded?.speech else {
            if wasReading { deactivate() }
            return
        }
        if !wasReading { activate() }
        left = sentences.count
        readingID = id
        speech.onUtteranceFinished = { [weak self] in
            guard let self, self.readingID != nil else { return }
            self.left -= 1
            if self.left <= 0 { self.stop() }
        }
        sentences.forEach(speech.speak)
    }

    func stop() {
        guard readingID != nil else { return }
        halt()
        deactivate()
    }

    /// Stops for dictation or voice mode, which have already set the session
    /// up for themselves by the time this hears of them: deactivating it
    /// here would pull it out from under the recorder.
    func handOff() {
        halt()
    }

    private func halt() {
        guard readingID != nil else { return }
        readingID = nil
        left = 0
        loaded?.speech.stopSpeaking()
    }

    /// Stops and frees the voice's model: when the app goes to the
    /// background, so a loaded Phonon isn't held for nothing.
    func release() {
        stop()
        loaded = nil
    }

    /// `.playback`, so the ring/silent switch doesn't mute a reply someone
    /// asked to hear; `.spokenAudio` so other audio pauses rather than ducks.
    /// The Settings sample's session (`VoicePreview`).
    nonisolated static func activateSession() {
        let session = AVAudioSession.sharedInstance()
        try? session.setCategory(.playback, mode: .spokenAudio)
        try? session.setActive(true)
    }

    nonisolated static func deactivateSession() {
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }
}
