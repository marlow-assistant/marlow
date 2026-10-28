import Foundation

/// The cleanup of what earlier dictation builds left behind. There are no
/// dictation settings: the engine is Apple's, on-device, picked per phone by
/// `AppleEngineSelector`. The statement that audio stays on the phone lives
/// in the Speech Recognition permission prompt (`project.yml`), the one place
/// every user reads it — Settings no longer has a dictation section.
///
/// There was briefly a stored mode (Apple / Parakeet) — Parakeet was measured
/// and abandoned 2026-09-30 in favour of iOS 26's SpeechTranscriber; its
/// scaffolding is at tag `parakeet-scaffolding-2026-09-30`
/// (docs/tasks/ios-modern-on-device-dictation.md).
enum DictationSettings {
    /// Settings keys no build reads any more. Deleted on launch; each is a
    /// no-op once gone.
    /// - The 2026-08-31 opt-in to Apple's server recognition, removed 2026-09-29: it
    ///   matched on-device word for word on 67 of 68 clips at twice the
    ///   latency, so it bought nothing and sent audio off the phone. `true`
    ///   users (Mike was one) lose nothing and gain privacy.
    /// - The developer corpus capture's expiry (2026-09-01 to 09-30).
    /// - The Apple/Parakeet mode, which only a branch build ever wrote.
    static let retiredKeys = [
        "useAppleServersForSpeechRecognition",
        "dictationCaptureExpiry",
        "dictationMode",
    ]

    /// Folders earlier builds left in the app container, relative to
    /// Documents and Application Support. Removed so nothing needs a delete
    /// and reinstall (Mike, 2026-09-30):
    /// - `DictationCorpus`: the developer capture's retained **microphone
    ///   audio**. With its Settings rows and Files sharing gone, anything left
    ///   here would be invisible and permanent — the one outcome the capture's
    ///   guard rails existed to prevent.
    /// - `DictationSpikeInput` / `DictationSpikeResults`: the Parakeet spike
    ///   harness's copies of that corpus, and its transcripts.
    /// - `FluidAudio`: the spike's Parakeet models, ~965 MB.
    static let retiredDocumentsFolders = ["DictationCorpus", "DictationSpikeInput", "DictationSpikeResults"]
    static let retiredApplicationSupportFolders = ["FluidAudio"]

    static func retireLegacyKeys(in defaults: UserDefaults = .standard) {
        for key in retiredKeys { defaults.removeObject(forKey: key) }
    }

    /// Deletes the retired folders. Idempotent; a missing folder is not an
    /// error. File deletion, so callers run it off the main thread.
    static func removeRetiredFolders(
        documents: URL = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0],
        applicationSupport: URL = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
    ) {
        let targets = retiredDocumentsFolders.map { documents.appendingPathComponent($0, isDirectory: true) }
            + retiredApplicationSupportFolders.map { applicationSupport.appendingPathComponent($0, isDirectory: true) }
        for url in targets where FileManager.default.fileExists(atPath: url.path) {
            try? FileManager.default.removeItem(at: url)
        }
    }
}
