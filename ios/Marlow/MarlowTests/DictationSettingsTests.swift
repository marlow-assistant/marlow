import Foundation
import Testing

@testable import Marlow

struct DictationSettingsTests {
    private func makeDefaults() -> (UserDefaults, String) {
        let name = "DictationSettingsTests-\(UUID().uuidString)"
        return (UserDefaults(suiteName: name)!, name)
    }

    @Test(arguments: [true, false])
    func retiringRemovesTheServerRecognitionKeyWhateverItHeld(usedServers: Bool) {
        let (defaults, name) = makeDefaults()
        defer { defaults.removePersistentDomain(forName: name) }
        defaults.set(usedServers, forKey: "useAppleServersForSpeechRecognition")
        defaults.set(812_462_158.0, forKey: "dictationCaptureExpiry")
        defaults.set("appleOnDevice", forKey: "dictationMode")
        defaults.set("low", forKey: "thinkingEffort")

        DictationSettings.retireLegacyKeys(in: defaults)
        DictationSettings.retireLegacyKeys(in: defaults)

        for key in ["useAppleServersForSpeechRecognition", "dictationCaptureExpiry", "dictationMode"] {
            #expect(defaults.object(forKey: key) == nil, "\(key) survived")
        }
        #expect(defaults.string(forKey: "thinkingEffort") == "low", "an unrelated key was touched")
    }

    /// Retained microphone audio must not outlive the UI that showed it.
    @Test func retiredFoldersAreRemovedAndNeighboursKept() throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("DictationSettingsTests-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let documents = root.appendingPathComponent("Documents", isDirectory: true)
        let support = root.appendingPathComponent("Application Support", isDirectory: true)
        let retired = DictationSettings.retiredDocumentsFolders.map { documents.appendingPathComponent($0) }
            + DictationSettings.retiredApplicationSupportFolders.map { support.appendingPathComponent($0) }
        let keep = [documents.appendingPathComponent("Other"), support.appendingPathComponent("Other")]
        for folder in retired + keep {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            try Data("x".utf8).write(to: folder.appendingPathComponent("clip.caf"))
        }

        DictationSettings.removeRetiredFolders(documents: documents, applicationSupport: support)
        DictationSettings.removeRetiredFolders(documents: documents, applicationSupport: support)

        for folder in retired { #expect(!FileManager.default.fileExists(atPath: folder.path), "\(folder.lastPathComponent) survived") }
        for folder in keep { #expect(FileManager.default.fileExists(atPath: folder.appendingPathComponent("clip.caf").path)) }
    }

    /// The literal names earlier builds used, or the cleanup silently misses
    /// them on a real phone.
    @Test func retiredNamesAreTheOnesEarlierBuildsUsed() {
        #expect(DictationSettings.retiredDocumentsFolders == ["DictationCorpus", "DictationSpikeInput", "DictationSpikeResults"])
        #expect(DictationSettings.retiredApplicationSupportFolders == ["FluidAudio"])
    }
}

@MainActor
struct AppleFileRecognizerRequestTests {
    /// The privacy boundary: no path may let Apple process audio on its
    /// servers.
    @Test func requestIsAlwaysOnDevice() {
        let request = AppleFileRecognizer.makeRequest(url: URL(fileURLWithPath: "/dev/null"))

        #expect(request.requiresOnDeviceRecognition)
        #expect(request.addsPunctuation)
        #expect(request.taskHint == .dictation)
        #expect(!request.shouldReportPartialResults)
    }
}

/// The table in docs/tasks/ios-modern-on-device-dictation.md ("Which engine …
/// runs"): SpeechTranscriber only when it can run right now, otherwise the old
/// recognizer — and never a wait.
struct AppleEngineSelectorTests {
    private func choose(_ analyzer: Bool, _ available: Bool, _ installed: Bool) -> AppleEngineSelector.Choice {
        AppleEngineSelector.choose(
            hasSpeechAnalyzer: analyzer, transcriberAvailable: available, assetInstalled: installed
        )
    }

    @Test func installedAssetOnACapablePhoneRunsSpeechTranscriber() {
        #expect(choose(true, true, true) == .speechTranscriber)
    }

    @Test func missingAssetRunsTheOldRecognizerAndKicksTheInstall() {
        #expect(choose(true, true, false) == .legacy(kickAssetInstall: true))
    }

    @Test(arguments: [true, false])
    func unavailableTranscriberRunsTheOldRecognizerWithNothingToInstall(installed: Bool) {
        #expect(choose(true, false, installed) == .legacy(kickAssetInstall: false))
    }

    @Test(arguments: [(true, true), (true, false), (false, true), (false, false)])
    func beforeIOS26ItIsAlwaysTheOldRecognizer(available: Bool, installed: Bool) {
        #expect(choose(false, available, installed) == .legacy(kickAssetInstall: false))
    }
}
