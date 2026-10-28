import Foundation
import Testing

@testable import Marlow

/// What survives the move to record-then-transcribe.
///
/// The old `DictationTranscriptTests` covered accumulating a transcript across
/// streaming fragments — committing utterances, guessing at restart boundaries,
/// surviving empty revisions. None of that exists any more: dictation records
/// to a file and transcribes it once, so there are no fragments. Those tests
/// were deleted with the logic they described rather than rewritten, which is
/// the honest bookkeeping — they were testing a mechanism, and the mechanism is
/// what was wrong.
///
/// What is left is the noise check, which came from a real observed failure and
/// never had anything to do with accumulation.
struct DictationOutcomeTests {
    @Test func emptyTranscriptIsNothingHeard() {
        #expect(DictationOutcome.decide(text: "", confidences: [], isSingleUtterance: true) == .nothingHeard)
    }

    @Test func whitespaceOnlyTranscriptIsNothingHeard() {
        #expect(DictationOutcome.decide(text: "   \n ", confidences: [], isSingleUtterance: true) == .nothingHeard)
    }

    @Test func transcriptIsTrimmedBeforeSending() {
        #expect(
            DictationOutcome.decide(text: "  hello there  ", confidences: [0.9], isSingleUtterance: true)
                == .send("hello there")
        )
    }

    /// The van-fan case: one or two words, low confidence, nobody speaking.
    @Test func shortLowConfidenceTranscriptIsNoise() {
        #expect(DictationOutcome.decide(text: "no", confidences: [0.2], isSingleUtterance: true) == .noise)
    }

    @Test func shortHighConfidenceTranscriptIsSent() {
        #expect(DictationOutcome.decide(text: "yes", confidences: [0.9], isSingleUtterance: true) == .send("yes"))
    }

    /// The regression that made the check dangerous in the first place: a long
    /// dictation with mediocre average confidence must never be discarded.
    @Test func longTranscriptIsNeverNoiseHoweverLowTheConfidence() {
        let confidences = [Float](repeating: 0.05, count: 40)
        let text = "this is a long dictation that happens to score badly throughout"
        #expect(DictationOutcome.decide(text: text, confidences: confidences, isSingleUtterance: true) == .send(text))
    }

    /// Three segments is already out of scope — the check is for one- and
    /// two-word transcripts only.
    @Test func threeSegmentsIsOutOfNoiseScope() {
        #expect(DictationOutcome.isLikelyNoise("a b c", [0.1, 0.1, 0.1]) == false)
    }

    /// A confidence of 0 means "not computed" on some OS versions, not "no
    /// confidence" — with no scored segment there is no signal to act on.
    @Test func allZeroConfidencesAreNotEvidenceOfNoise() {
        #expect(DictationOutcome.isLikelyNoise("no", [0, 0]) == false)
        #expect(DictationOutcome.decide(text: "no", confidences: [0, 0], isSingleUtterance: true) == .send("no"))
    }

    /// The bug this guard exists for: a long assembled transcript whose *last*
    /// utterance is two low-confidence words. `confidences` describes only that
    /// last fragment, so without the guard the whole dictation is discarded.
    @Test func aMultiUtteranceTranscriptIsNeverNoiseHoweverBadItsLastFragment() {
        #expect(
            DictationOutcome.decide(
                text: "a hundred and fifty nine characters of perfectly good speech",
                confidences: [0.1, 0.1],
                isSingleUtterance: false
            ) == .send("a hundred and fifty nine characters of perfectly good speech")
        )
    }

    @Test func averageIgnoresUncomputedScores() throws {
        let average = try #require(DictationOutcome.averageConfidence([0, 0.8, 0, 0.6]))
        #expect(abs(average - 0.7) < 0.0001)
        #expect(DictationOutcome.averageConfidence([]) == nil)
        #expect(DictationOutcome.averageConfidence([0, 0]) == nil)
    }
}
