import Foundation
import os
import Speech

/// Apple's recognizer run over one recorded file, configured exactly as
/// dictation runs it. Split out of `DictationController` (2026-09-26) so the
/// Parakeet spike's Apple arms called *this* code rather than a copy of it — a
/// comparison against a lookalike of production measures the lookalike. The
/// spike itself is kept at tag `dictation-spike-2026-09-29`, not on main. The
/// controller keeps everything around it: the recording, its deletion, the
/// outcome decision and the telemetry.
///
/// `SelectingAppleTranscriber` wraps this as the old-recognizer engine, and
/// keeps `SFSpeechRecognitionResult` from escaping it.
@MainActor
enum AppleFileRecognizer {
    struct Output {
        let result: Result<SFSpeechRecognitionResult, Error>
        /// The transcript rebuilt from every hypothesis the recognizer offered
        /// — see `DictationAssembler` for why one result is not enough.
        let assembledText: String
        let restarts: Int
    }

    /// Same subsystem and category as `DictationController`'s, so the
    /// watchdog's line lands where it always did.
    private static let log = Logger(subsystem: "com.marlow.app", category: "dictation")

    /// Runs recognition over the whole recording, once.
    ///
    /// `shouldReportPartialResults` is off on purpose — partials are what the
    /// old streaming design was assembled from, and nothing displays them. One
    /// request, one result, no assembly.
    static func recognize(
        url: URL,
        with recognizer: SFSpeechRecognizer,
        recordedSeconds: TimeInterval
    ) async -> Output {
        let (result, hypotheses) = await bridge(
            makeRequest(url: url), with: recognizer, recordedSeconds: recordedSeconds
        )

        var assembler = DictationAssembler()
        for hypothesis in hypotheses {
            assembler.accept(text: hypothesis.text, segments: hypothesis.segments)
        }
        assembler.commit()
        return Output(result: result, assembledText: assembler.text, restarts: assembler.restarts)
    }

    /// The request exactly as dictation sends it. Separate so a test can hold
    /// it to on-device without a recognizer or authorization.
    static func makeRequest(url: URL) -> SFSpeechURLRecognitionRequest {
        let request = SFSpeechURLRecognitionRequest(url: url)
        request.shouldReportPartialResults = false
        // Apple's flag is a privacy constraint, not a low-confidence fallback:
        // true prevents network use; false allows Apple to process the audio
        // on its servers. Always true, and not a parameter on purpose: the 2026-08-31
        // opt-in to server recognition was removed 2026-09-29 (it bought
        // nothing measurable). Nothing can pass false.
        request.requiresOnDeviceRecognition = true
        // This is exactly what it's for — biases the language model's
        // formatting/punctuation heuristics toward a single dictated
        // utterance rather than a search query or a yes/no confirmation.
        request.taskHint = .dictation
        // Off by default, which is why 82 seconds of dictation came back as one
        // unpunctuated run (device run, 2026-08-18). Claude reads it either way;
        // the argument for turning it on is the *review* path — the mic button
        // puts this text in the field for Mike to read and edit, and that is
        // much harder without sentence boundaries.
        request.addsPunctuation = true
        // Biases recognition toward the app's own name, which the generic
        // model has no reason to favor over homophones.
        request.contextualStrings = ["Marlow"]
        return request
    }

    /// Bridges the recognizer's callback to `async`.
    ///
    /// **Only `isFinal` ends this, and that is load-bearing.** The first
    /// version of this method also resumed on a non-final result, as a
    /// defensive catch-all for a callback shape that "shouldn't happen" — and
    /// that is precisely what broke it on device (2026-08-18):
    /// `shouldReportPartialResults = false` does *not* stop a URL request from
    /// delivering hypotheses. The first partial covered about five seconds of a
    /// twenty-two second recording, the catch-all took it as the answer, and
    /// the single-resume guard then discarded every later and more complete
    /// result. Same lost-text symptom as the streaming design, from three lines
    /// of misplaced caution.
    ///
    /// The partial is still worth keeping, just never worth *settling* on: if
    /// the task dies without ever reaching `isFinal`, delivering the best
    /// hypothesis so far beats delivering nothing.
    private static func bridge(
        _ request: SFSpeechURLRecognitionRequest,
        with recognizer: SFSpeechRecognizer,
        recordedSeconds: TimeInterval
    ) async -> (result: Result<SFSpeechRecognitionResult, Error>, hypotheses: [Hypothesis]) {
        let state = OSAllocatedUnfairLock(uncheckedState: RecognitionState())
        var watchdog: Task<Void, Never>?
        var task: SFSpeechRecognitionTask?
        defer {
            watchdog?.cancel()
            // Was `DictationController.finish()`'s job before the split; it
            // ran on the same main-actor turn that consumed this result, so
            // cancelling here instead changes nothing observable.
            task?.cancel()
        }

        let outcome: Result<SFSpeechRecognitionResult, Error>
        do {
            outcome = .success(try await withCheckedThrowingContinuation { continuation in
            // Resumes at most once. Everything below races: the recognizer's
            // callback, and the watchdog.
            @Sendable func settle(_ outcome: Result<SFSpeechRecognitionResult, Error>) {
                let isFirst = state.withLock { current -> Bool in
                    guard !current.settled else { return false }
                    current.settled = true
                    return true
                }
                guard isFirst else { return }
                continuation.resume(with: outcome)
            }

            /// Falls back to the best hypothesis seen, or the error if there
            /// was none.
            @Sendable func settleWithBestEffort(_ error: Error) {
                let best = state.withLock { $0.latest }
                if let best {
                    settle(.success(best))
                } else {
                    settle(.failure(error))
                }
            }

            // A continuation that never resumes is a permanent hang with
            // `isTranscribing` stuck true and the buttons inert — strictly
            // worse than losing the tail. Recognition of 22s of audio measured
            // 0.6s on device, so this is orders of magnitude of headroom and
            // should never fire in practice; if it does, the log says so.
            watchdog = Task { [recordedSeconds] in
                try? await Task.sleep(for: .seconds(max(20, recordedSeconds)))
                guard !Task.isCancelled else { return }
                Self.log.error("dictation transcription timed out waiting for a final result")
                settleWithBestEffort(CocoaError(.userCancelled))
            }

            task = recognizer.recognitionTask(with: request) { result, error in
                if let result {
                    state.withLock { $0.latest = result }
                    // Every result feeds the assembler, final or not: a result
                    // the recognizer later abandons is the *only* record of the
                    // audio it covered, so ignoring partials is what lost the
                    // opening of a dictation.
                    let transcription = result.bestTranscription
                    let segments = transcription.segments.map {
                        DictationAssembler.Segment(timestamp: $0.timestamp, duration: $0.duration)
                    }
                    let text = transcription.formattedString
                    // Collected synchronously under the lock rather than via a
                    // `Task { @MainActor }` hop per result. The hop would be
                    // *unordered*, so results could assemble out of sequence
                    // and a late-arriving early hypothesis would read as a
                    // restart — reintroducing the ordering hazard the streaming
                    // design carried. The assembler runs over this list, in
                    // order, once recognition has finished.
                    state.withLock { $0.hypotheses.append(Hypothesis(text: text, segments: segments)) }
                    // The only terminal success signal.
                    if result.isFinal { settle(.success(result)) }
                    return
                }
                if let error {
                    // A failure after real speech was already recognized should
                    // still deliver what was heard rather than throwing it away.
                    settleWithBestEffort(error)
                }
            }
            })
        } catch {
            outcome = .failure(error)
        }
        // Read after settling, so every result the recognizer offered is
        // present and in the order it arrived.
        return (outcome, state.withLock { $0.hypotheses })
    }

    /// Mutable state shared between the recognizer's callback and the
    /// watchdog. `uncheckedState` because `SFSpeechRecognitionResult` isn't
    /// `Sendable`; the lock is what makes the access safe.
    private struct RecognitionState {
        var settled = false
        var latest: SFSpeechRecognitionResult?
        var hypotheses: [Hypothesis] = []
    }

    /// One result the recognizer offered, reduced to what the assembler needs.
    private struct Hypothesis {
        let text: String
        let segments: [DictationAssembler.Segment]
    }
}
