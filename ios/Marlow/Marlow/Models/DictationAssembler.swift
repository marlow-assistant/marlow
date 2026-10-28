import Foundation

/// Assembles a full transcript out of the recognizer's successive results,
/// using **where in the audio each result sits** to tell a revision from a
/// restart.
///
/// **Yes, this is accumulation again.** It was deleted on 2026-08-18 along with
/// the streaming design, on the premise that recording to a file and running
/// recognition once would leave nothing to assemble. That premise was wrong,
/// and two device runs proved it: the recognizer endpoints at pauses even when
/// reading a complete file, so it resets its hypothesis mid-recording and the
/// result carrying `isFinal` describes only the *last* stretch of speech.
/// Taking the first result gave the first five seconds of a twenty-two second
/// clip; taking the final one gave the tail of a twenty-one second clip. The
/// audio has no seams. The recognizer's output does.
///
/// **What makes this different from the heuristic it replaces.** The old
/// `DictationTranscript` guessed at restarts by comparing first characters,
/// which is a coin flip that silently deleted a minute of speech whenever two
/// consecutive utterances happened to start with the same letter. Segment
/// timestamps are not a guess: they are the recognizer stating which part of
/// the audio it just transcribed. A result whose first segment starts *later*
/// than the one it would replace cannot be a revision of it — the earlier audio
/// is simply not in it.
struct DictationAssembler {
    /// One transcription segment, reduced to what this needs. Taken from
    /// `SFTranscriptionSegment`, which has no public initializer, so this can
    /// be constructed in tests.
    struct Segment: Equatable {
        let timestamp: TimeInterval
        let duration: TimeInterval

        init(timestamp: TimeInterval, duration: TimeInterval) {
            self.timestamp = timestamp
            self.duration = duration
        }
    }

    /// Utterances the recognizer has moved past. Append-only.
    private(set) var committed: [String] = []
    private var pending = ""
    private var pendingStart: TimeInterval?

    /// Resets observed — the count is the evidence for whether this type is
    /// earning its place, and gets logged when a dictation finishes.
    private(set) var restarts = 0

    /// A result's start may drift slightly between revisions of the same
    /// utterance. A genuine restart jumps forward by whole seconds — the gap is
    /// a pause long enough for the recognizer to endpoint — so the threshold
    /// only has to be larger than the jitter, not tuned.
    private static let restartThreshold: TimeInterval = 0.5

    var text: String {
        (committed + [pending])
            .filter { !$0.isEmpty }
            .joined(separator: " ")
    }

    mutating func accept(text: String, segments: [Segment]) {
        let incoming = text.trimmed
        // An empty revision is not evidence that the speech went away — taking
        // those at face value is what once turned real dictations into "No
        // speech heard."
        guard !incoming.isEmpty else { return }
        let start = segments.first?.timestamp ?? 0

        if let pendingStart, start > pendingStart + Self.restartThreshold {
            // This result begins after the one it would replace, so it cannot
            // contain that audio. Bank what we had.
            restarts += 1
            commit()
        }
        pending = incoming
        // Only lower the mark, never raise it within one utterance: a revision
        // that trims a leading word shouldn't look like a restart next time.
        self.pendingStart = min(pendingStart ?? start, start)
    }

    mutating func commit() {
        guard !pending.isEmpty else { return }
        committed.append(pending)
        pending = ""
        pendingStart = nil
    }
}
