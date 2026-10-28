import AVFoundation
import Foundation
import Testing
@testable import Marlow

// Voice mode (docs/tasks/voice-mode-hands-free.md). What a test can reach:
// sentence chunking, markdown-to-speech, the button's state machine, and the
// send path. What it can't — the mic, the synthesizer, the AirPods route —
// is the device checklist at the end of the task doc.

@Suite("Sentence chunker")
struct SentenceChunkerTests {
    @Test func aSentenceStillBeingWrittenIsHeld() {
        var chunker = SentenceChunker()
        #expect(chunker.ingest("You have three meet").isEmpty)
    }

    @Test func aSentenceFollowedByMoreTextIsSpokenAtOnce() {
        var chunker = SentenceChunker()
        let first = chunker.ingest("You have three meetings. The first")
        #expect(first.map { $0.trimmingCharacters(in: .whitespaces) } == ["You have three meetings."])
    }

    @Test func aTerminatedSentenceIsSpokenWhenThePollAfterItBringsNothingNew() {
        // The 1–2.5s first-sentence delay the device spike measured: a lone
        // sentence with nothing after it waited for the turn to end.
        var chunker = SentenceChunker()
        #expect(chunker.ingest("Yes, it's at noon.").isEmpty, "could still be '...noon.5' or similar")
        #expect(chunker.ingest("Yes, it's at noon.") == ["Yes, it's at noon."])
    }

    @Test func anUnterminatedSentenceIsHeldEvenWhenStable() {
        var chunker = SentenceChunker()
        _ = chunker.ingest("Checking your")
        #expect(chunker.ingest("Checking your").isEmpty)
    }

    @Test func textBeforeAToolCallIsSpokenWithoutPunctuation() {
        var chunker = SentenceChunker()
        let text = "Let me check your calendar"
        #expect(chunker.ingest(text + "You have", marks: [text.unicodeScalars.count]) == [text])
    }

    @Test func nothingIsSpokenTwice() {
        var chunker = SentenceChunker()
        let first = chunker.ingest("One. Two")
        let second = chunker.ingest("One. Two. Three")
        let last = chunker.finish("One. Two. Three.")
        #expect((first + second + last).map { $0.trimmingCharacters(in: .whitespaces) } == ["One.", "Two.", "Three."])
    }

    @Test func finishSpeaksTheTailFromTheAuthoritativeText() {
        var chunker = SentenceChunker()
        _ = chunker.ingest("Here's the plan. First")
        #expect(chunker.finish("Here's the plan. First, pack.").map { $0.trimmingCharacters(in: .whitespaces) } == ["First, pack."])
    }

    @Test func finishPicksUpAfterWhatWasSpokenWhenTheResultDiffers() {
        // `result_text` is reassembled server-side and needn't extend the
        // streamed text byte for byte; carry on after the last words spoken.
        var chunker = SentenceChunker()
        _ = chunker.ingest("I checked the calendar for tomorrow. More")
        let rest = chunker.finish("Okay. I checked the calendar for tomorrow. You're free.")
        #expect(rest.map { $0.trimmingCharacters(in: .whitespaces) } == ["You're free."])
    }

    @Test func finishSaysNothingRatherThanRereadWhenItCantFindItsPlace() {
        var chunker = SentenceChunker()
        _ = chunker.ingest("Something entirely different. ")
        #expect(chunker.finish("No overlap at all.").isEmpty)
    }
}

@Suite("Markdown to speech")
struct SpeechTextTests {
    @Test func emphasisAndHeadingsAreDropped() {
        #expect(SpeechText.plain("## Today\n**Three** meetings, *one* moved.") == "Today\nThree meetings, one moved.")
    }

    @Test func listMarkersGoButTheirTextStays() {
        #expect(SpeechText.plain("- milk\n2. eggs") == "milk\neggs")
    }

    @Test func linksReadAsTheirText() {
        #expect(SpeechText.plain("See [the forecast](https://example.com/x).") == "See the forecast.")
        #expect(SpeechText.plain("At https://example.com/x today") == "At a link today")
    }

    @Test func aCodeBlockSpanningSentencesIsNotReadOut() {
        var fences = CodeFenceFilter()
        let spoken = ["Run this. ```", "rm -rf build. ", "echo done. ", "``` Then reboot."]
            .map { SpeechText.plain(fences.filter($0)) }
            .joined(separator: " ")
        #expect(!spoken.contains("rm") && !spoken.contains("echo"))
        #expect(spoken.contains("The code is in the chat."))
        #expect(spoken.contains("Then reboot."), "text after the closing fence is kept")
    }

    @Test func aMarkdownOnlyFragmentIsEmpty() {
        #expect(SpeechText.plain("---").isEmpty)
    }
}

// MARK: - The button

@MainActor
final class FakeRecorder: VoiceRecorder {
    private(set) var begins = 0
    private(set) var finishes = 0
    private(set) var cancels = 0
    private var onResult: ((String?) -> Void)?

    func begin(onResult: @escaping (String?) -> Void) {
        begins += 1
        self.onResult = onResult
    }

    func finish() { finishes += 1 }
    func cancel() {
        cancels += 1
        onResult = nil
    }

    func deliver(_ text: String?) {
        let report = onResult
        onResult = nil
        report?(text)
    }
}

@MainActor
final class FakeSpeech: SpeechOutput {
    var onUtteranceFinished: (() -> Void)?
    private(set) var spoken: [String] = []
    private(set) var stops = 0
    private(set) var pending = 0

    func speak(_ text: String) {
        spoken.append(text)
        pending += 1
    }

    func stopSpeaking() {
        stops += 1
        pending = 0
    }

    func finishOne() {
        guard pending > 0 else { return }
        pending -= 1
        onUtteranceFinished?()
    }
}

@MainActor
@Suite("Voice mode button")
struct VoiceModeControllerTests {
    let recorder = FakeRecorder()
    let speech = FakeSpeech()
    let reply = UUID()

    private func make(sendResult: UUID? = nil) -> (VoiceModeController, Box) {
        let sent = Box()
        let id = sendResult ?? reply
        let controller = VoiceModeController(
            recorder: recorder, speech: speech,
            send: { text in sent.texts.append(text); return id },
            earcon: { sent.earcons.append($0) },
            firstWorkingTick: .seconds(3600),
            workingTickInterval: .seconds(3600)
        )
        return (controller, sent)
    }

    final class Box {
        var texts: [String] = []
        var earcons: [Earcons.Kind] = []
    }

    /// Talk, send, and get as far as waiting on Marlow.
    private func talk(_ controller: VoiceModeController, _ text: String = "what's on today") {
        controller.press()
        controller.press()
        recorder.deliver(text)
    }

    @Test func theFirstWorkingSoundComesSoonAfterSendingNotAFullIntervalLater() async throws {
        let sent = Box()
        let controller = VoiceModeController(
            recorder: recorder, speech: speech,
            send: { [reply] _ in reply },
            earcon: { sent.earcons.append($0) },
            firstWorkingTick: .milliseconds(20),
            workingTickInterval: .seconds(3600)
        )
        talk(controller)
        for _ in 0..<200 where !sent.earcons.contains(.working) {
            try await Task.sleep(for: .milliseconds(10))
        }
        #expect(sent.earcons == [.listening, .sent, .working])
    }

    @Test func pressTalksAndPressAgainSends() {
        let (controller, sent) = make()
        controller.press()
        #expect(controller.phase == .recording)
        #expect(recorder.begins == 1)

        controller.press()
        #expect(controller.phase == .transcribing)
        #expect(recorder.finishes == 1)

        recorder.deliver("what's on today")
        #expect(sent.texts == ["what's on today"])
        #expect(controller.phase == .waiting)
        #expect(sent.earcons == [.listening, .sent])
    }

    @Test func aPressWhileTranscribingDoesNothing() {
        let (controller, _) = make()
        controller.press()
        controller.press()
        controller.press()
        #expect(controller.phase == .transcribing)
        #expect(recorder.begins == 1)
    }

    @Test func nothingHeardGoesBackToIdleAndSendsNothing() {
        let (controller, sent) = make()
        controller.press()
        controller.press()
        recorder.deliver(nil)
        #expect(controller.phase == .idle)
        #expect(sent.texts.isEmpty)
    }

    @Test func theReplyIsSpokenAsItStreamsAndTheModeReturnsToIdle() {
        let (controller, _) = make()
        talk(controller)
        controller.replyProgress(reply, text: "You're free all morning. Lunch", marks: [])
        #expect(controller.phase == .speaking)
        #expect(speech.spoken == ["You're free all morning."])

        controller.replySettled(reply, text: "You're free all morning. Lunch is at noon.")
        #expect(speech.spoken == ["You're free all morning.", "Lunch is at noon."])
        #expect(controller.phase == .speaking, "still talking")

        speech.finishOne()
        speech.finishOne()
        #expect(controller.phase == .idle)
    }

    @Test func afterTheOpeningSentenceItPurrsAgainWhileMarlowKeepsWorking() async throws {
        let sent = Box()
        let controller = VoiceModeController(
            recorder: recorder, speech: speech,
            send: { [reply] _ in reply },
            earcon: { sent.earcons.append($0) },
            firstWorkingTick: .milliseconds(20),
            workingTickInterval: .seconds(3600)
        )
        talk(controller)
        controller.replyProgress(reply, text: "Checking your notes. ", marks: [21])
        #expect(speech.spoken == ["Checking your notes."])
        speech.finishOne()
        #expect(controller.phase == .waiting, "the tools are still running")
        for _ in 0..<200 where !sent.earcons.contains(.working) {
            try await Task.sleep(for: .milliseconds(10))
        }
        #expect(sent.earcons == [.listening, .sent, .working])

        controller.replySettled(reply, text: "Checking your notes. Nothing about games yet.")
        #expect(controller.phase == .speaking)
        speech.finishOne()
        #expect(controller.phase == .idle)
    }

    @Test func anApprovalCardChimesAndRemindsInsteadOfPurringUntilAnswered() async throws {
        let sent = Box()
        let controller = VoiceModeController(
            recorder: recorder, speech: speech,
            send: { [reply] _ in reply },
            earcon: { sent.earcons.append($0) },
            firstWorkingTick: .seconds(3600),
            workingTickInterval: .seconds(3600),
            approvalReminderInterval: .milliseconds(20)
        )
        talk(controller)
        controller.approvalChanged(pending: true)
        #expect(sent.earcons == [.listening, .sent, .approval], "at once, when the card appears")
        for _ in 0..<200 where sent.earcons.count < 4 {
            try await Task.sleep(for: .milliseconds(10))
        }
        #expect(sent.earcons.dropFirst(2).allSatisfy { $0 == .approval }, "reminders, not purrs")

        controller.approvalChanged(pending: false)
        let count = sent.earcons.count
        try await Task.sleep(for: .milliseconds(100))
        #expect(sent.earcons.count == count, "answered: no more chimes")
        #expect(controller.phase == .waiting)
    }

    @Test func noChimeLandsInARecording() {
        let sent = Box()
        let controller = VoiceModeController(
            recorder: recorder, speech: speech,
            send: { [reply] _ in reply },
            earcon: { sent.earcons.append($0) }
        )
        controller.press()
        controller.approvalChanged(pending: true)
        #expect(sent.earcons == [.listening])
    }

    @Test func interruptingStopsSpeechStartsListeningAndIgnoresTheRestOfThatReply() {
        let (controller, _) = make()
        talk(controller)
        controller.replyProgress(reply, text: "First thing. Second thing. Third", marks: [])
        #expect(speech.spoken.count == 2)

        controller.press()
        #expect(speech.stops == 1)
        #expect(controller.phase == .recording)
        #expect(recorder.begins == 2)

        controller.replyProgress(reply, text: "First thing. Second thing. Third thing. Fourth.", marks: [])
        controller.replySettled(reply, text: "First thing. Second thing. Third thing. Fourth.")
        #expect(speech.spoken.count == 2, "an interrupted reply stays interrupted")
        #expect(controller.phase == .recording)
    }

    @Test func pressingWhileMarlowIsStillThinkingSkipsThatReply() {
        let (controller, _) = make()
        talk(controller)
        controller.press()
        #expect(controller.phase == .recording)
        controller.replySettled(reply, text: "An answer you no longer want.")
        #expect(speech.spoken.isEmpty)
    }

    @Test func aFailedReplyReturnsToIdleWithATone() {
        let (controller, sent) = make()
        talk(controller)
        controller.replySettled(reply, text: nil)
        #expect(controller.phase == .idle)
        #expect(sent.earcons.last == .failed)
    }

    @Test func aReplyThatFailsMidSpeechStopsTheSpeech() {
        let (controller, _) = make()
        talk(controller)
        controller.replyProgress(reply, text: "One. Two. Three", marks: [])
        controller.replySettled(reply, text: nil)
        #expect(speech.stops == 1)
        #expect(controller.phase == .idle)
    }

    @Test func anEmptyReplyEndsWithoutSpeaking() {
        let (controller, _) = make()
        talk(controller)
        controller.replySettled(reply, text: "")
        #expect(controller.phase == .idle)
        #expect(speech.spoken.isEmpty)
    }

    @Test func markdownIsNotReadOut() {
        let (controller, _) = make()
        talk(controller)
        controller.replySettled(reply, text: "**Two** things.")
        #expect(speech.spoken == ["Two things."])
    }

    @Test func theReplysTerminalStateIsReadOffTheChat() {
        let (controller, _) = make()
        talk(controller)
        var message = ChatMessage(
            id: reply, role: .assistant, text: "", createdAt: Date(), turnId: "t1", status: .running,
            clientMsgId: nil, imageFilename: nil
        )
        controller.messagesChanged([message])
        #expect(controller.phase == .waiting)

        message.status = .done
        message.text = "Done."
        controller.messagesChanged([message])
        #expect(speech.spoken == ["Done."])
    }

    @Test func aReplyThatVanishesFromTheChatCountsAsFailed() {
        // Merged into a later turn, or lost to a server restart.
        let (controller, _) = make()
        talk(controller)
        controller.messagesChanged([])
        #expect(controller.phase == .idle)
    }

    @Test func leavingMidRecordingCancelsAndSendsNothing() {
        let (controller, sent) = make()
        controller.press()
        controller.stopAll()
        #expect(recorder.cancels == 1)
        #expect(controller.phase == .idle)
        recorder.deliver("too late")
        #expect(sent.texts.isEmpty)
    }

    @Test func leavingWhileSpeakingStopsTheSpeech() {
        let (controller, _) = make()
        talk(controller)
        controller.replyProgress(reply, text: "Something long. And more", marks: [])
        controller.stopAll()
        #expect(speech.stops == 1)
        #expect(controller.phase == .idle)
        #expect(recorder.cancels == 0, "nothing was recording")
    }
}

// MARK: - The send path

@MainActor
struct VoiceModeSendTests {
    @Test func aSpokenSendIsMarkedSpokenAndAnOrdinaryOneIsNot() async throws {
        let fake = FakeChatAPI()
        await fake.stubSend(.success(MessageResponse(turnId: "t1")))
        await fake.stubTurns([], fallback: .success(.fixture(turnId: "t1", status: "done", resultText: "Ok.")))
        let store = ChatStore(deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test())
        defer { store.cancelActiveWork() }

        let placeholder = try #require(store.sendSpoken("what's on today"))
        await awaitSettled(store)
        store.send(text: "thanks")
        await awaitSettled(store)

        let sent = try requireCount(await fake.sentMessages, 2)
        #expect(sent[0].spoken)
        #expect(!sent[1].spoken)
        #expect(store.messages.contains { $0.id == placeholder && $0.role == .assistant }, "the id names the reply's bubble")
    }

    @Test func theRawStreamIsHandedOnWhileTheReplyRuns() async throws {
        let fake = FakeChatAPI()
        await fake.stubSend(.success(MessageResponse(turnId: "t1")))
        await fake.stubTurns(
            [.success(.fixture(turnId: "t1", status: "running", partialText: "Half a", partialTextMarks: [3]))],
            fallback: .success(.fixture(turnId: "t1", status: "done", resultText: "Half a reply."))
        )
        let store = ChatStore(deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test())
        defer { store.cancelActiveWork() }
        var seen: [(UUID, String, [Int])] = []
        store.onReplyProgress = { seen.append(($0, $1, $2)) }

        let placeholder = try #require(store.sendSpoken("hi"))
        await awaitSettled(store)

        let first = try #require(seen.first)
        #expect(first.0 == placeholder)
        #expect(first.1 == "Half a")
        #expect(first.2 == [3])
    }
}

@MainActor
@Suite("Earcons")
struct EarconTests {
    /// The "still working" purr: 0.6 s, quiet, and silent at both
    /// ends so it fades in and out instead of clicking every few seconds.
    @Test func theWorkingCueIsAQuietPurrThatFadesInAndOut() {
        let samples = Earcons.samples(for: .working)
        #expect(abs(Double(samples.count) / 44_100 - 0.6) < 0.01)
        let peak = samples.map(abs).max() ?? 0
        #expect(peak > 0.1 && peak <= 0.2, "peak \(peak)")
        #expect(abs(samples.first ?? 1) < 0.001 && abs(samples.last ?? 1) < 0.001)
    }

    /// The approval tink-tink: louder than the purr, since it asks them to
    /// look, but short, and without a click at either end.
    @Test func theApprovalCueIsAShortClearTinkTink() {
        let samples = Earcons.samples(for: .approval)
        #expect(abs(Double(samples.count) / 44_100 - 0.59) < 0.01)
        let peak = samples.map(abs).max() ?? 0
        #expect(peak > 0.3 && peak <= 0.45, "peak \(peak)")
        #expect(abs(samples.first ?? 1) < 0.001 && abs(samples.last ?? 1) < 0.001)
    }

    @Test func everyCueBuildsAPlayableWav() throws {
        for kind in [Earcons.Kind.listening, .sent, .working, .approval, .failed] {
            #expect(try AVAudioPlayer(data: Earcons.wav(for: kind)).duration > 0.05, "\(kind)")
        }
    }
}
