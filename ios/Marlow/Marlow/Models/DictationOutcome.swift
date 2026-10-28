import Foundation

/// What to do with a finished transcript — the one decision in dictation that
/// isn't `AVFoundation` plumbing, split out so it can be tested without a
/// microphone.
///
/// This used to be `DictationTranscript`, a stateful accumulator that stitched
/// a continuous transcript out of a streaming recognizer's fragments. That
/// whole apparatus is gone (2026-08-18): dictation now records to a file and
/// transcribes it once, so there are no fragments and nothing to stitch. What
/// survives is the noise check, which solved a real observed failure and is
/// unrelated to accumulation.
enum DictationOutcome: Equatable {
    case send(String)
    /// Nothing was transcribed at all.
    case nothingHeard
    /// Something was transcribed, but it looks like ambient noise.
    case noise

    /// Segments arrive as `SFTranscriptionSegment`, which has no public
    /// initializer, so this takes the one thing it needs from them — the
    /// confidence scores — rather than the segments themselves.
    /// `isSingleUtterance` is load-bearing, not a convenience. `confidences`
    /// now describes only the *last* result the recognizer produced, which
    /// after an endpoint is just the final utterance — so a 159-character
    /// dictation whose last utterance is two low-confidence words would be
    /// thrown away entirely. Anything the assembler banked puts the noise check
    /// out of scope, exactly as the old `committed.isEmpty` guard did.
    static func decide(
        text: String,
        confidences: [Float],
        isSingleUtterance: Bool
    ) -> DictationOutcome {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty { return .nothingHeard }
        if isSingleUtterance, isLikelyNoise(trimmed, confidences) { return .noise }
        return .send(trimmed)
    }

    /// Deliberately scoped to *very short* transcripts, and only there.
    ///
    /// This exists for one observed failure: ambient noise (a van's fan) with
    /// nobody speaking, guessed by the recognizer as the single word "no" and
    /// auto-sent. That is a one-word problem. An earlier version applied the
    /// check to every transcript by averaging confidence across all of it,
    /// which silently discarded a full minute of good dictation — on-device
    /// confidence scores run low and drift with room acoustics, so one noisy
    /// stretch drags the whole average under the bar and takes the rest of the
    /// transcript with it. Anything longer than a couple of words is evidence
    /// enough on its own that a person was talking; trust it.
    ///
    /// A segment's confidence is 0 when the recognizer didn't compute a score
    /// at all (a known on-device gap on some OS versions) rather than "zero
    /// confidence" — so only filter when at least one segment actually carries
    /// a computed score; otherwise there's no reliable signal.
    static func isLikelyNoise(_ text: String, _ confidences: [Float]) -> Bool {
        guard confidences.count <= 2 else { return false }
        guard let average = averageConfidence(confidences) else { return false }
        return average < 0.5
    }

    /// Average over segments carrying a computed score, or nil when none do.
    static func averageConfidence(_ confidences: [Float]) -> Float? {
        let scored = confidences.filter { $0 > 0 }
        guard !scored.isEmpty else { return nil }
        return scored.reduce(0, +) / Float(scored.count)
    }
}

extension String {
    var trimmed: String { trimmingCharacters(in: .whitespacesAndNewlines) }
}
