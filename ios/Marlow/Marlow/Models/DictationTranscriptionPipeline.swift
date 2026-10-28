import Foundation

/// How a dictation resolved: the outcome, which engine ran, and the raw
/// numbers behind the controller's telemetry.
struct DictationResolution: Equatable {
    var outcome: DictationOutcome
    var engine: DictationEngine
    /// Time spent inside the engine.
    var elapsedSeconds: TimeInterval
    var transcription: AppleTranscription
}

/// The part of a dictation that runs after the recording file is closed:
/// transcribe, then decide the outcome. Split out of `DictationController` so
/// the outcome rules — the noise gate, salvage on failure — are testable
/// without a microphone or a real recognizer.
@MainActor
final class DictationTranscriptionPipeline {
    private let apple: AppleTranscribing

    init(apple: AppleTranscribing) {
        self.apple = apple
    }

    func run(url: URL, recordedSeconds: TimeInterval) async -> DictationResolution {
        let startedAt = Date()
        let transcription = await apple.transcribe(url: url, recordedSeconds: recordedSeconds)
        let elapsed = Date().timeIntervalSince(startedAt)

        let outcome: DictationOutcome
        if transcription.failure == nil {
            outcome = DictationOutcome.decide(
                text: transcription.assembledText,
                confidences: transcription.confidences,
                isSingleUtterance: transcription.restarts == 0
            )
        } else {
            // A failure partway through should still deliver what was heard.
            outcome = DictationOutcome.decide(
                text: transcription.assembledText,
                confidences: [],
                isSingleUtterance: false
            )
        }
        return DictationResolution(
            outcome: outcome,
            engine: transcription.engine,
            elapsedSeconds: elapsed,
            transcription: transcription
        )
    }
}
