import AVFoundation
import Foundation
import os
import Speech

/// The engine that actually ran — the Apple mode runs `SpeechTranscriber` or
/// the old recognizer depending on the phone (`AppleEngineSelector`).
enum DictationEngine: String, Sendable {
    /// iOS 26's SpeechAnalyzer model.
    case appleSpeechTranscriber
    /// `SFSpeechRecognizer`, on-device.
    case appleLegacy
}

/// What Apple's recognizer produced for one file, reduced to the plain values
/// dictation uses. `SFSpeechRecognitionResult` has no public initializer, so
/// it stays inside `SpeechAppleTranscriber` — which is also what lets tests
/// hand the pipeline a canned result.
struct AppleTranscription: Sendable, Equatable {
    struct Failure: Sendable, Equatable {
        let domain: String
        let code: Int
    }

    /// The transcript rebuilt from every hypothesis — see `DictationAssembler`.
    /// On failure this is what was salvaged before the recognizer died.
    let assembledText: String
    /// Segment confidences of the last result; empty on failure.
    let confidences: [Float]
    let restarts: Int
    /// Characters in the final result alone (trimmed), the number to compare
    /// against `assembledText` to see what endpointing would have dropped.
    let finalCharacterCount: Int
    let failure: Failure?
    /// Which Apple engine produced this. `SpeechTranscriber` reports no
    /// confidences, so `DictationOutcome`'s noise gate — keyed on them — is
    /// inert for it by construction. The gate exists for one failure observed
    /// on the old recognizer (fan noise heard as "no", auto-sent); nothing
    /// like it has been seen from SpeechTranscriber. If it appears, the preset
    /// with confidence attributes is the lever.
    var engine: DictationEngine = .appleLegacy
}

@MainActor
protocol AppleTranscribing {
    func transcribe(url: URL, recordedSeconds: TimeInterval) async -> AppleTranscription
}

/// The production conformer: `AppleFileRecognizer` on the device's recognizer.
@MainActor
final class SpeechAppleTranscriber: AppleTranscribing {
    private let recognizer: SFSpeechRecognizer?

    init(recognizer: SFSpeechRecognizer?) {
        self.recognizer = recognizer
    }

    func transcribe(url: URL, recordedSeconds: TimeInterval) async -> AppleTranscription {
        // Unreachable in practice: `DictationController.start()` refuses to
        // record without a recognizer. Reported as a failure rather than a
        // crash so a future caller can't hang a dictation.
        guard let recognizer else {
            return AppleTranscription(
                assembledText: "", confidences: [], restarts: 0, finalCharacterCount: 0,
                failure: .init(domain: "com.marlow.dictation", code: -1)
            )
        }
        let recognition = await AppleFileRecognizer.recognize(
            url: url,
            with: recognizer,
            recordedSeconds: recordedSeconds
        )
        switch recognition.result {
        case .success(let recognized):
            let transcription = recognized.bestTranscription
            return AppleTranscription(
                // Deliberately *not* `bestTranscription.formattedString`.
                // That is only the last stretch of speech whenever the
                // recognizer endpointed at a pause — see DictationAssembler.
                assembledText: recognition.assembledText,
                confidences: transcription.segments.map(\.confidence),
                restarts: recognition.restarts,
                finalCharacterCount: transcription.formattedString.trimmed.count,
                failure: nil
            )
        case .failure(let error):
            let nsError = error as NSError
            return AppleTranscription(
                assembledText: recognition.assembledText, confidences: [],
                restarts: recognition.restarts, finalCharacterCount: 0,
                failure: .init(domain: nsError.domain, code: nsError.code)
            )
        }
    }
}

/// Which Apple engine a dictation runs — the table in
/// docs/tasks/ios-modern-on-device-dictation.md ("Which engine … runs"), as a
/// pure function so every row is tested.
///
/// **Never waits for the asset.** Until the OS has installed SpeechTranscriber's
/// model, the old recognizer answers — it is what every dictation used before
/// this, so it is a fine answer for the first one or two — and the install is
/// kicked so the next dictation gets the new model.
enum AppleEngineSelector {
    enum Choice: Equatable {
        case speechTranscriber
        case legacy(kickAssetInstall: Bool)
    }

    static func choose(hasSpeechAnalyzer: Bool, transcriberAvailable: Bool, assetInstalled: Bool) -> Choice {
        guard hasSpeechAnalyzer, transcriberAvailable else { return .legacy(kickAssetInstall: false) }
        return assetInstalled ? .speechTranscriber : .legacy(kickAssetInstall: true)
    }
}

/// SpeechTranscriber's model is an OS-managed asset: the system downloads and
/// stores it, not the app. On the 14 Pro it installed itself on first use
/// (2026-09-30); this only moves that download earlier and asks the OS to keep
/// it. Safe to call repeatedly — each call is a status check once installed.
@available(iOS 26, *)
enum SpeechTranscriberAssets {
    private static let log = Logger(subsystem: "com.marlow.app", category: "dictation")

    static func locale() async -> Locale? {
        await SpeechTranscriber.supportedLocale(equivalentTo: Locale(identifier: "en-US"))
    }

    static func module(_ locale: Locale) -> SpeechTranscriber {
        // `.transcription`: finalized text only. Record-first has no use for
        // volatile results, alternatives or timing.
        SpeechTranscriber(locale: locale, preset: .transcription)
    }

    static func isInstalled() async -> Bool {
        guard SpeechTranscriber.isAvailable, let locale = await locale() else { return false }
        return await AssetInventory.status(forModules: [module(locale)]) == .installed
    }

    static func ensureInstalled() async {
        guard SpeechTranscriber.isAvailable, let locale = await locale() else { return }
        // Best effort, and deliberately outside the download's `do`: asks the
        // OS not to reclaim the asset, but the download does not need it (the
        // spike installed without reserving). A reserve error must never
        // stop the install, or every dictation stays on the old recognizer.
        do {
            _ = try await AssetInventory.reserve(locale: locale)
        } catch {
            let nsError = error as NSError
            log.error("dictation: SpeechTranscriber reserve failed: \(nsError.domain, privacy: .public) \(nsError.code)")
        }
        do {
            if let request = try await AssetInventory.assetInstallationRequest(supporting: [module(locale)]) {
                log.notice("dictation: installing SpeechTranscriber asset")
                try await request.downloadAndInstall()
                log.notice("dictation: SpeechTranscriber asset installed")
            }
        } catch {
            let nsError = error as NSError
            log.error("dictation: SpeechTranscriber asset install failed: \(nsError.domain, privacy: .public) \(nsError.code)")
        }
    }
}

/// SpeechAnalyzer over the whole recorded file, once — the same record-first
/// shape as the old path, so no streaming or assembly. Scored 3.5% WER against
/// the old recognizer's 5.7% on Mike's corpus, on the phone
/// (docs/tasks/ios-modern-on-device-dictation.md, "Evidence").
@available(iOS 26, *)
@MainActor
final class SpeechAnalyzerTranscriber {
    private static let log = Logger(subsystem: "com.marlow.app", category: "dictation")

    /// Resolves exactly once: with the analyzer's result, or — if it never
    /// finishes — with whatever was heard so far when the watchdog fires. Same
    /// reasoning as the old path's watchdog (`AppleFileRecognizer.bridge`): a
    /// transcription that never returns leaves `isTranscribing` stuck true and
    /// the buttons inert, strictly worse than losing the tail. At ~2.3 s for a
    /// 172 s clip on the 14 Pro, the timeout is headroom, not a budget.
    func transcribe(url: URL, recordedSeconds: TimeInterval) async -> AppleTranscription {
        let pieces = OSAllocatedUnfairLock(initialState: [String]())
        let settled = OSAllocatedUnfairLock(initialState: false)
        var work: Task<Void, Never>?
        var watchdog: Task<Void, Never>?
        let transcription = await withCheckedContinuation { (continuation: CheckedContinuation<AppleTranscription, Never>) in
            @Sendable func settle(_ failure: Error?) {
                let first = settled.withLock { done -> Bool in
                    guard !done else { return false }
                    done = true
                    return true
                }
                guard first else { return }
                continuation.resume(returning: Self.output(pieces, failure))
            }
            work = Task { @MainActor in
                do {
                    try await Self.run(url: url, into: pieces)
                    settle(nil)
                } catch {
                    settle(error)
                }
            }
            watchdog = Task {
                try? await Task.sleep(for: .seconds(max(20, recordedSeconds)))
                guard !Task.isCancelled else { return }
                Self.log.error("dictation: SpeechTranscriber timed out waiting for a final result")
                settle(CocoaError(.userCancelled))
            }
        }
        watchdog?.cancel()
        work?.cancel()
        return transcription
    }

    nonisolated private static func output(_ pieces: OSAllocatedUnfairLock<[String]>, _ failure: Error?) -> AppleTranscription {
        let text = pieces.withLock { $0.joined() }.trimmed
        let nsError = failure.map { $0 as NSError }
        return AppleTranscription(
            assembledText: text, confidences: [], restarts: 0,
            finalCharacterCount: text.count,
            failure: nsError.map { .init(domain: $0.domain, code: $0.code) },
            engine: .appleSpeechTranscriber
        )
    }

    private static func run(url: URL, into pieces: OSAllocatedUnfairLock<[String]>) async throws {
        guard let locale = await SpeechTranscriberAssets.locale() else {
            throw CocoaError(.featureUnsupported)
        }
        let module = SpeechTranscriberAssets.module(locale)
        let analyzer = SpeechAnalyzer(modules: [module])
        // Listening before feeding, or early results have nowhere to go.
        // Pieces are kept as they arrive so a failure (or the watchdog)
        // partway through still delivers what was heard, as the old path does.
        let collector = Task {
            for try await result in module.results {
                let text = String(result.text.characters)
                pieces.withLock { $0.append(text) }
            }
        }
        do {
            let file = try AVAudioFile(forReading: url)
            if let end = try await analyzer.analyzeSequence(from: file) {
                try await analyzer.finalizeAndFinish(through: end)
            } else {
                await analyzer.cancelAndFinishNow()
            }
            try await collector.value
        } catch {
            collector.cancel()
            await analyzer.cancelAndFinishNow()
            throw error
        }
    }
}

/// The production `AppleTranscribing`: asks `AppleEngineSelector` per
/// dictation, then runs SpeechTranscriber or the old recognizer. Always
/// on-device either way; neither path can reach Apple's servers.
@MainActor
final class SelectingAppleTranscriber: AppleTranscribing {
    private let legacy: SpeechAppleTranscriber

    init(recognizer: SFSpeechRecognizer?) {
        legacy = SpeechAppleTranscriber(recognizer: recognizer)
    }

    func transcribe(url: URL, recordedSeconds: TimeInterval) async -> AppleTranscription {
        if #available(iOS 26, *) {
            let choice = AppleEngineSelector.choose(
                hasSpeechAnalyzer: true,
                transcriberAvailable: SpeechTranscriber.isAvailable,
                assetInstalled: await SpeechTranscriberAssets.isInstalled()
            )
            switch choice {
            case .speechTranscriber:
                return await SpeechAnalyzerTranscriber().transcribe(url: url, recordedSeconds: recordedSeconds)
            case .legacy(let kick):
                if kick { Task.detached { await SpeechTranscriberAssets.ensureInstalled() } }
            }
        }
        return await legacy.transcribe(url: url, recordedSeconds: recordedSeconds)
    }
}
