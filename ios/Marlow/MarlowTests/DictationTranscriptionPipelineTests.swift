import Foundation
import Testing

@testable import Marlow

@MainActor
private final class FakeAppleTranscriber: AppleTranscribing {
    var result: AppleTranscription
    private(set) var calls = 0

    init(_ result: AppleTranscription? = nil) {
        self.result = result ?? Self.confident("apple text")
    }

    func transcribe(url: URL, recordedSeconds: TimeInterval) async -> AppleTranscription {
        calls += 1
        return result
    }

    static func confident(_ text: String) -> AppleTranscription {
        AppleTranscription(
            assembledText: text, confidences: [0.9, 0.9, 0.9], restarts: 0,
            finalCharacterCount: text.count, failure: nil
        )
    }
}

@MainActor
struct DictationTranscriptionPipelineTests {
    private let url = URL(fileURLWithPath: "/dev/null")

    @Test func deliversTheTranscriptAndTheEngineThatRan() async {
        let pipeline = DictationTranscriptionPipeline(apple: FakeAppleTranscriber())

        let resolution = await pipeline.run(url: url, recordedSeconds: 5)

        #expect(resolution.outcome == .send("apple text"))
        #expect(resolution.engine == .appleLegacy)
        #expect(resolution.transcription.failure == nil)
    }

    @Test func appleModeStillGatesASingleLowConfidenceWordAsNoise() async {
        let apple = FakeAppleTranscriber(AppleTranscription(
            assembledText: "no", confidences: [0.2], restarts: 0, finalCharacterCount: 2, failure: nil
        ))
        let pipeline = DictationTranscriptionPipeline(apple: apple)

        let resolution = await pipeline.run(url: url, recordedSeconds: 5)

        #expect(resolution.outcome == .noise)
    }

    @Test func appleModeDeliversSalvagedTextWhenRecognitionFails() async {
        let apple = FakeAppleTranscriber(AppleTranscription(
            assembledText: " what I got ", confidences: [], restarts: 1, finalCharacterCount: 0,
            failure: .init(domain: "kAFAssistantErrorDomain", code: 203)
        ))
        let pipeline = DictationTranscriptionPipeline(apple: apple)

        let resolution = await pipeline.run(url: url, recordedSeconds: 5)

        #expect(resolution.outcome == .send("what I got"))
        #expect(resolution.transcription.failure == .init(domain: "kAFAssistantErrorDomain", code: 203))
    }

    @Test func appleModeFailureWithNothingSalvagedIsNothingHeard() async {
        let apple = FakeAppleTranscriber(AppleTranscription(
            assembledText: "", confidences: [], restarts: 0, finalCharacterCount: 0,
            failure: .init(domain: "d", code: 1)
        ))
        let pipeline = DictationTranscriptionPipeline(apple: apple)

        let resolution = await pipeline.run(url: url, recordedSeconds: 5)

        #expect(resolution.outcome == .nothingHeard)
    }

    /// The gate is scoped to a single utterance: a restart puts it out of scope.
    @Test func appleModeNoiseGateDoesNotApplyAfterARestart() async {
        let apple = FakeAppleTranscriber(AppleTranscription(
            assembledText: "no", confidences: [0.2], restarts: 1, finalCharacterCount: 2, failure: nil
        ))
        let pipeline = DictationTranscriptionPipeline(apple: apple)

        let resolution = await pipeline.run(url: url, recordedSeconds: 5)

        #expect(resolution.outcome == .send("no"))
    }

    /// SpeechTranscriber returns no confidences, so the old recognizer's
    /// one-word noise gate cannot fire on it — a real "no" is delivered — and
    /// the engine that ran is what telemetry sees.
    @Test func speechTranscriberOutputIsDeliveredWithoutTheNoiseGate() async {
        let apple = FakeAppleTranscriber(AppleTranscription(
            assembledText: "no", confidences: [], restarts: 0, finalCharacterCount: 2,
            failure: nil, engine: .appleSpeechTranscriber
        ))
        let pipeline = DictationTranscriptionPipeline(apple: apple)

        let resolution = await pipeline.run(url: url, recordedSeconds: 1)

        #expect(resolution.outcome == .send("no"))
        #expect(resolution.engine == .appleSpeechTranscriber)
    }
}
