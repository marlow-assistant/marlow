import XCTest
@testable import Marlow

/// "Read aloud" on a reply's long-press menu (docs/tasks/read-aloud.md).
@MainActor
final class MessageReaderTests: XCTestCase {
    private var speeches: [FakeSpeech] = []
    private var voice = "en_gb_Edwin"
    private var sessions: [String] = []

    private func make() -> MessageReader {
        MessageReader(
            voiceID: { [unowned self] in voice },
            makeSpeech: { [unowned self] in
                let s = FakeSpeech()
                speeches.append(s)
                return s
            },
            activate: { [unowned self] in sessions.append("on") },
            deactivate: { [unowned self] in sessions.append("off") }
        )
    }

    func testReadsSentenceBySentenceAsPlainSpeech() {
        let reader = make()
        let id = UUID()
        reader.read(id: id, text: "**Rain** at three. Bring a [coat](https://x.test).\n\n```\nls -la\n```")
        XCTAssertEqual(reader.readingID, id)
        XCTAssertEqual(sessions, ["on"])
        let spoken = speeches[0].spoken.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
        XCTAssertEqual(spoken.first, "Rain at three.")
        XCTAssertTrue(spoken.contains("Bring a coat."), "\(spoken)")
        XCTAssertFalse(spoken.joined().contains("ls -la"), "code is never read out")
        XCTAssertFalse(spoken.joined().contains("*"))
    }

    func testFinishingTheLastSentenceEndsTheRead() {
        let reader = make()
        reader.read(id: UUID(), text: "One. Two.")
        let speech = speeches[0]
        let count = speech.spoken.count
        XCTAssertGreaterThan(count, 1)
        for _ in 0..<(count - 1) { speech.finishOne() }
        XCTAssertNotNil(reader.readingID, "still reading until the last sentence ends")
        speech.finishOne()
        XCTAssertNil(reader.readingID)
        XCTAssertEqual(sessions, ["on", "off"])
    }

    func testStopCutsItOff() {
        let reader = make()
        reader.read(id: UUID(), text: "One. Two.")
        reader.stop()
        XCTAssertNil(reader.readingID)
        XCTAssertEqual(speeches[0].stops, 1)
        XCTAssertEqual(sessions, ["on", "off"])
    }

    func testStopWhenIdleTouchesNothing() {
        let reader = make()
        reader.stop()
        XCTAssertEqual(sessions, [])
    }

    func testReadingAnotherMessageReplacesTheFirst() {
        let reader = make()
        reader.read(id: UUID(), text: "First reply.")
        let second = UUID()
        reader.read(id: second, text: "Second reply.")
        XCTAssertEqual(reader.readingID, second)
        XCTAssertEqual(speeches.count, 1, "the loaded voice is kept between reads")
        XCTAssertEqual(speeches[0].stops, 1)
        XCTAssertEqual(speeches[0].spoken.last, "Second reply.")
        XCTAssertEqual(sessions, ["on"], "the session isn't bounced between replies")
        speeches[0].finishOne()
        XCTAssertNil(reader.readingID)
        XCTAssertEqual(sessions, ["on", "off"])
    }

    /// Dictation and voice mode have already taken the session by the time
    /// the reader hears of them: it must not deactivate it under them.
    func testHandOffStopsWithoutTouchingTheSession() {
        let reader = make()
        reader.read(id: UUID(), text: "One. Two.")
        reader.handOff()
        XCTAssertNil(reader.readingID)
        XCTAssertEqual(speeches[0].stops, 1)
        XCTAssertEqual(sessions, ["on"])
    }

    func testANewVoiceInSettingsLoadsAFreshSpeech() {
        let reader = make()
        reader.read(id: UUID(), text: "Hello.")
        reader.stop()
        voice = "en_us_Miles"
        reader.read(id: UUID(), text: "Hello again.")
        XCTAssertEqual(speeches.count, 2)
    }

    func testNothingToSayReadsNothing() {
        let reader = make()
        reader.read(id: UUID(), text: "  **  ")
        XCTAssertNil(reader.readingID)
        XCTAssertEqual(sessions, [])
    }

    func testReleaseStopsAndFreesTheVoice() {
        let reader = make()
        reader.read(id: UUID(), text: "Hello.")
        reader.release()
        XCTAssertNil(reader.readingID)
        reader.read(id: UUID(), text: "Hello.")
        XCTAssertEqual(speeches.count, 2)
    }
}
