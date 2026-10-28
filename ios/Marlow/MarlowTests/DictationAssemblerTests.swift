import Testing

@testable import Marlow

/// Every case here is a dictation that actually lost text on Mike's phone.
///
/// This is the coverage that was missing through three rounds of fixes: the
/// logic that decides what a recognizer result *means* now lives in a plain
/// struct that takes timestamps rather than `SFTranscriptionSegment`, so it
/// runs in the Simulator — which cannot do on-device recognition at all
/// (`supportsOnDeviceRecognition == false`, probed 2026-08-18) and so could
/// never have caught these through the controller.
struct DictationAssemblerTests {
    private func segments(from start: Double, count: Int = 3) -> [DictationAssembler.Segment] {
        (0..<count).map {
            DictationAssembler.Segment(timestamp: start + Double($0) * 0.4, duration: 0.4)
        }
    }

    /// A clip short enough that the recognizer never endpoints — the 6.3s case
    /// that worked on device. The assembler must be a no-op here.
    @Test func aSingleUtteranceIsPassedThroughUnchanged() {
        var assembler = DictationAssembler()
        assembler.accept(text: "hello", segments: segments(from: 0))
        assembler.accept(text: "hello there", segments: segments(from: 0))
        assembler.commit()
        #expect(assembler.text == "hello there")
        #expect(assembler.restarts == 0)
    }

    /// The 2026-08-18 failure: the recognizer endpoints at a pause and the
    /// result carrying `isFinal` describes only the last stretch of speech.
    /// Taking that result alone gave the tail of a 21.5s clip.
    @Test func aRestartBanksTheEarlierUtteranceInsteadOfLosingIt() {
        var assembler = DictationAssembler()
        assembler.accept(text: "the first thing I said", segments: segments(from: 0))
        assembler.accept(text: "and the second thing", segments: segments(from: 8.0))
        assembler.commit()
        #expect(assembler.text == "the first thing I said and the second thing")
        #expect(assembler.restarts == 1)
    }

    /// The failure mode of the heuristic this replaces: two consecutive
    /// utterances starting with the same letter. The old first-character check
    /// read that as a revision and deleted everything before it. Timestamps
    /// don't care what the words are.
    @Test func aRestartIsDetectedEvenWhenBothUtterancesStartAlike() {
        var assembler = DictationAssembler()
        assembler.accept(text: "I think we should go", segments: segments(from: 0))
        assembler.accept(text: "I wonder about that", segments: segments(from: 9.0))
        assembler.commit()
        #expect(assembler.text == "I think we should go I wonder about that")
    }

    /// And its mirror: a genuine revision that changes the first character must
    /// *not* be banked as a new utterance, or the transcript duplicates.
    @Test func aRevisionThatChangesTheFirstWordIsNotARestart() {
        var assembler = DictationAssembler()
        assembler.accept(text: "Hi there", segments: segments(from: 0))
        assembler.accept(text: "High there everyone", segments: segments(from: 0))
        assembler.commit()
        #expect(assembler.text == "High there everyone")
        #expect(assembler.restarts == 0)
    }

    @Test func severalRestartsAllSurvive() {
        var assembler = DictationAssembler()
        assembler.accept(text: "one", segments: segments(from: 0))
        assembler.accept(text: "two", segments: segments(from: 5))
        assembler.accept(text: "three", segments: segments(from: 10))
        assembler.accept(text: "four", segments: segments(from: 15))
        assembler.commit()
        #expect(assembler.text == "one two three four")
        #expect(assembler.restarts == 3)
    }

    /// Small drift between revisions of one utterance is not a restart —
    /// otherwise every refinement would duplicate the utterance.
    @Test func subSecondDriftIsRevisionNotRestart() {
        var assembler = DictationAssembler()
        assembler.accept(text: "hello there", segments: segments(from: 1.0))
        assembler.accept(text: "hello there friend", segments: segments(from: 1.3))
        assembler.commit()
        #expect(assembler.text == "hello there friend")
        #expect(assembler.restarts == 0)
    }

    /// The recognizer sends empty revisions, including alongside `isFinal`.
    /// Taking one at face value is what once turned a real dictation into
    /// "No speech heard."
    @Test func anEmptyRevisionNeverWipesWhatWasHeard() {
        var assembler = DictationAssembler()
        assembler.accept(text: "something real", segments: segments(from: 0))
        assembler.accept(text: "", segments: [])
        assembler.accept(text: "   ", segments: [])
        assembler.commit()
        #expect(assembler.text == "something real")
    }

    /// A result with no segments carries no position, so it must be treated as
    /// a revision — the safe direction, since a wrong restart only duplicates
    /// while a wrong revision deletes.
    @Test func aResultWithoutSegmentsIsTreatedAsARevision() {
        var assembler = DictationAssembler()
        assembler.accept(text: "first", segments: segments(from: 0))
        assembler.accept(text: "first and more", segments: [])
        assembler.commit()
        #expect(assembler.text == "first and more")
        #expect(assembler.restarts == 0)
    }

    @Test func nothingHeardYieldsEmptyText() {
        var assembler = DictationAssembler()
        assembler.commit()
        #expect(assembler.text == "")
        #expect(assembler.restarts == 0)
    }

    /// Committing twice must not duplicate the last utterance — `transcribe`
    /// commits after the loop, and the failure path used to commit again.
    @Test func committingTwiceIsIdempotent() {
        var assembler = DictationAssembler()
        assembler.accept(text: "only once", segments: segments(from: 0))
        assembler.commit()
        assembler.commit()
        #expect(assembler.text == "only once")
    }
}
