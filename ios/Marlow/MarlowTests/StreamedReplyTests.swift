import Foundation
import Testing
@testable import Marlow

/// Typing the reply out as it is written (docs/tasks/streaming-reply-text.md).
///
/// The pacing struct is tested directly because its whole job is invisible from
/// the outside: every assertion about a store would pass against a version that
/// dumped each slab whole, which is the paragraph-at-a-time behaviour smoothing
/// exists to remove.
@MainActor
struct StreamedReplyTests {

    // MARK: - PartialTextPlaybackState

    @Test func revealsAGrowingPrefixOfWhatTheServerSent() {
        var state = PartialTextPlaybackState()
        state.ingest("hello there", ticksPerPoll: 4)

        let first = state.advance(jumpThreshold: 1_000)
        let second = state.advance(jumpThreshold: 1_000)

        #expect(first == "hel")
        // ceil(11/4) = 3 characters a tick, held steady rather than recomputed
        // from the shrinking backlog — see `stride`.
        #expect(second == "hello ")
        #expect("hello there".hasPrefix(try! #require(second)))
    }

    @Test func drainsTheBacklogWithinOnePollInterval() {
        /// The rule that keeps smoothing from becoming a latency tax: whatever
        /// has arrived is fully shown by the time the next poll's slab lands.
        var state = PartialTextPlaybackState()
        state.ingest(String(repeating: "x", count: 430), ticksPerPoll: 40)

        for _ in 0..<40 { _ = state.advance(jumpThreshold: 1_000) }

        #expect(state.backlog == 0)
    }

    @Test func returnsNilWithNothingLeftToReveal() {
        var state = PartialTextPlaybackState()
        state.ingest("hi", ticksPerPoll: 1)
        _ = state.advance(jumpThreshold: 1_000)

        #expect(state.advance(jumpThreshold: 1_000) == nil)
    }

    @Test func aLargeBacklogIsShownAtOnceRatherThanPerformed() {
        /// A phone back from a tunnel with the whole answer buffered should
        /// show the answer, not type it out for the user's benefit.
        var state = PartialTextPlaybackState()
        let essay = String(repeating: "y", count: 3_000)
        state.ingest(essay, ticksPerPoll: 40)

        #expect(state.advance(jumpThreshold: 2_000) == essay)
    }

    @Test func aLateJoinerShowsWhatItMissedRatherThanTypingIt() {
        /// The 2026-09-05 bug: open the app on an unread message and watch a
        /// reply that finished long ago type itself out from nothing. A
        /// playback state built for a turn already in progress has revealed
        /// nothing and receives the server's whole cumulative string, so
        /// pacing it is a replay, not streaming.
        var state = PartialTextPlaybackState(joinedLate: true)
        let alreadyWritten = String(repeating: "z", count: 300)
        state.ingest(alreadyWritten, ticksPerPoll: 40)

        // Well under `jumpThreshold`, so the existing large-backlog escape
        // hatch is not what is being exercised here.
        let firstTick = state.advance(jumpThreshold: 2_000)
        #expect(firstTick == alreadyWritten)
    }

    @Test func aLateJoinerStillTypesWhatArrivesAfterIt() {
        /// Catching up is a one-time concession to having arrived late, not a
        /// mode. Everything written from here on is text the user is watching
        /// arrive, and it paces like any other.
        var state = PartialTextPlaybackState(joinedLate: true)
        state.ingest(String(repeating: "z", count: 300), ticksPerPoll: 40)
        _ = state.advance(jumpThreshold: 2_000)

        state.ingest(String(repeating: "z", count: 300) + String(repeating: "a", count: 40), ticksPerPoll: 40)

        let next = state.advance(jumpThreshold: 2_000)
        #expect(next?.count == 301, "ceil(40/40) = one character a tick, as for any other slab")
    }

    @Test func aTurnWatchedFromTheStartIsStillTypedOut() {
        /// The guard on the fix: "first ingest" is ALSO true of a live turn's
        /// first text, and keying the catch-up on that instead of on having
        /// joined late would flatten the effect the whole feature exists for.
        var state = PartialTextPlaybackState()
        let firstSlab = String(repeating: "z", count: 300)
        state.ingest(firstSlab, ticksPerPoll: 40)

        let firstTick = state.advance(jumpThreshold: 2_000)
        #expect(firstTick != firstSlab)
    }

    @Test func aShorterPayloadNeverRewindsTheBubble() {
        /// The server's text only grows within a turn, so a shorter one means a
        /// coalesce or a restart. Rewinding mid-sentence is worse than briefly
        /// stale text that the authoritative `resultText` is about to replace.
        var state = PartialTextPlaybackState()
        state.ingest("the whole answer", ticksPerPoll: 1)
        _ = state.advance(jumpThreshold: 1_000)

        state.ingest("the", ticksPerPoll: 1)

        #expect(state.revealed == "the whole answer")
        #expect(state.backlog == 0)
    }

    @Test func repeatedIdenticalPayloadsAddNothing() {
        /// Every poll re-sends the whole string; that is the design, and it is
        /// why there is no cursor to keep. Re-ingesting must be a no-op.
        var state = PartialTextPlaybackState()
        state.ingest("same", ticksPerPoll: 1)
        state.ingest("same", ticksPerPoll: 1)

        #expect(state.backlog == 4)
    }

    // MARK: - Splitting at tool marks

    @Test func textIsBrokenWhereATooCallInterruptedIt() {
        /// The interleaved 11%: run together, these read as one garbled
        /// sentence — "I'll check your calendar.You have three meetings."
        let segments = PartialTextPlaybackState.segments(
            of: "I'll check your calendar.You have three meetings.", splitAt: [25]
        )

        #expect(segments == ["I'll check your calendar.", "You have three meetings."])
    }

    @Test func aReplyWithNoToolCallIsOnePiece() {
        #expect(
            PartialTextPlaybackState.segments(of: "just an answer", splitAt: []) == ["just an answer"]
        )
    }

    @Test func marksAreCountedInUnicodeScalarsLikeTheServerCounts() {
        /// The server measures `len(partial_text)` in Python, which counts code
        /// points. Splitting by Character would drift on the first emoji and
        /// put the break in the wrong place — here, one scalar early.
        let text = "done 👍 next"
        let mark = "done 👍 ".unicodeScalars.count

        #expect(PartialTextPlaybackState.segments(of: text, splitAt: [mark]) == ["done 👍 ", "next"])
    }

    @Test func marksAtTheEdgesAndDuplicatesAreIgnored() {
        /// A tool call before the first character or after the last one has
        /// nothing to split, and back-to-back calls land on one offset.
        let segments = PartialTextPlaybackState.segments(
            of: "abcdef", splitAt: [0, 3, 3, 6, 99, -1]
        )

        #expect(segments == ["abc", "def"])
    }

    @Test func onlyMarksTheTypingCursorHasReachedAreExposed() {
        /// A mark past what has been revealed would split text the user cannot
        /// see yet.
        var state = PartialTextPlaybackState()
        state.ingest("first part.second part.", marks: [12], ticksPerPoll: 4)
        _ = state.advance(jumpThreshold: 1_000)

        #expect(state.revealedMarks == [], "6 characters in, the mark at 12 is still ahead")

        for _ in 0..<4 { _ = state.advance(jumpThreshold: 1_000) }

        #expect(state.revealedMarks == [12])
    }

    // MARK: - Decoding

    @Test func aServerWithoutReplyStreamingDecodesAsEmpty() throws {
        let json = """
        {"turn_id": "t1", "status": "running", "source": "chat", "result_text": null,
         "session_id": null, "had_permission_denial": false, "error": null,
         "created_at": 0}
        """
        let turn = try JSONDecoder().decode(Turn.self, from: Data(json.utf8))

        #expect(turn.partialText == "")
        #expect(turn.partialTextMarks == [])
    }

    @Test func theStreamedTextAndItsToolMarksDecode() throws {
        let json = """
        {"turn_id": "t1", "status": "running", "source": "chat", "result_text": null,
         "session_id": null, "had_permission_denial": false, "error": null,
         "created_at": 0, "partial_text": "I'll check.", "partial_text_marks": [11]}
        """
        let turn = try JSONDecoder().decode(Turn.self, from: Data(json.utf8))

        #expect(turn.partialText == "I'll check.")
        #expect(turn.partialTextMarks == [11])
    }

    // MARK: - "Marlow is responding" (ChatHaptics.replyStarted)

    /// The queue behind the reply-started haptic
    /// (docs/tasks/ios-chat-ux-gaps.md, Mike 2026-09-11). Tested at the store
    /// because the haptic itself is a `UIImpactFeedbackGenerator` call with no
    /// observable result — what can actually be wrong is *when* and *how often*
    /// the store says a reply began, and that is all here.

    @Test func announcesAReplyStartingOnceTheFirstCharactersAreShown() async throws {
        let fake = FakeChatAPI()
        await fake.stubTurns([
            .success(.fixture(status: "running", partialText: "I'll check.")),
            .success(.fixture(status: "done", resultText: "I'll check. Done.")),
        ])
        let store = ChatStore(
            deviceToken: "tok", client: fake,
            persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        store.send(text: "ask")
        await awaitSettled(store)

        let placeholder = try requireCount(store.messages, 2)[1]
        #expect(store.startedAssistantReplies == [placeholder.id])
    }

    @Test func announcesAReplyStartingOnlyOnceHoweverMuchTextFollows() async throws {
        /// The latch, and the reason there is one. `startPartialTextPlayback`
        /// assigns `streamedText` on every tick — dozens of times across a real
        /// reply — so an unlatched announcement is not a rare double buzz, it is
        /// the phone vibrating continuously for the length of the answer.
        let fake = FakeChatAPI()
        await fake.stubTurns([
            .success(.fixture(status: "running", partialText: "one")),
            .success(.fixture(status: "running", partialText: "one two")),
            .success(.fixture(status: "running", partialText: "one two three")),
            .success(.fixture(status: "done", resultText: "one two three four")),
        ])
        let store = ChatStore(
            deviceToken: "tok", client: fake,
            persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        store.send(text: "ask")
        await awaitSettled(store)

        #expect(store.startedAssistantReplies.count == 1)
    }

    @Test func saysNothingForATurnThatNeverStreamsAnything() async throws {
        /// A turn that goes straight to `done` — a short answer, or a server
        /// that sent no partial text — has no "it started" moment distinct from
        /// its arrival, and firing both haptics back to back for one event is
        /// the thing that would make them meaningless.
        let fake = FakeChatAPI()
        await fake.stubTurns([.success(.fixture(status: "done", resultText: "yes"))])
        let store = ChatStore(
            deviceToken: "tok", client: fake,
            persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        store.send(text: "ask")
        await awaitSettled(store)

        #expect(try requireCount(store.messages, 2)[1].status == .done)
        #expect(store.startedAssistantReplies.isEmpty)
    }

    @Test func drainingTheStartedRepliesEmptiesThem() async throws {
        /// Same contract as `takeCompletedAssistantTurns`, and it matters for
        /// the same reason: `ChatView` reads this from an `onChange` that the
        /// poll loop re-fires several times a second, so a queue that is read
        /// without being emptied buzzes on every tick of the turn it announced.
        let fake = FakeChatAPI()
        await fake.stubTurns([
            .success(.fixture(status: "running", partialText: "hi")),
            .success(.fixture(status: "done", resultText: "hi there")),
        ])
        let store = ChatStore(
            deviceToken: "tok", client: fake,
            persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        store.send(text: "ask")
        await awaitSettled(store)

        #expect(store.takeStartedAssistantReplies().count == 1)
        #expect(store.startedAssistantReplies.isEmpty)
        #expect(store.takeStartedAssistantReplies().isEmpty)
    }

    // MARK: - The burst phase

    @Test func theBurstPollsBeforeTheFastPhaseWould() async throws {
        /// Everything worth showing happens in the first few seconds — CLI
        /// startup, the first tool call, the first text at 3.2s. `fastInterval`
        /// here is 10s, so a turn that reaches `.done` at all is proof the
        /// burst ran ahead of it.
        let fake = FakeChatAPI()
        await fake.stubTurns([
            .success(.fixture(status: "running", partialText: "th")),
            .success(.fixture(status: "done", resultText: "finished")),
        ])
        var policy = PollingPolicy.test()
        policy.burstDuration = .milliseconds(500)
        policy.fastInterval = 10_000_000_000

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: policy
        )
        defer { store.cancelActiveWork() }

        store.send(text: "ask")
        await awaitSettled(store)

        #expect(try requireCount(store.messages, 2)[1].status == .done)
    }

    @Test func theBurstIsBoundedByTheClockNotTheAttemptCount() async throws {
        /// The property that keeps a slow link from being made worse. Attempts
        /// alone would stretch: 12 of them at 500ms becomes 24 seconds of burst
        /// when each round trip costs 1.5s. Here the attempt bound is absurd,
        /// so only the deadline can end the phase — and it must, well short of
        /// exhausting it.
        let fake = FakeChatAPI()
        await fake.stubTurns([], fallback: .success(.fixture(status: "running")))
        var policy = PollingPolicy.test(fastAttempts: 1, slowAttempts: 1)
        policy.burstInterval = 2_000_000 // 2ms, so the count stays readable
        policy.burstDuration = .milliseconds(100)
        policy.burstAttempts = 1_000_000

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: policy
        )
        defer { store.cancelActiveWork() }

        store.send(text: "ask")
        await awaitSettled(store)

        let polls = await fake.getTurnCallCount
        #expect(polls < 1_000, "the deadline ended the burst, not the attempt cap (saw \(polls) polls)")
    }

    @Test func permissionsAreNotPolledDuringTheBurst() async throws {
        /// A permission request has the server's 55s window, so picking it up
        /// on the fast phase's first tick is invisible — and pairing every
        /// burst poll with a second round trip is the one part of this that
        /// would double request count for nothing.
        let fake = FakeChatAPI()
        await fake.stubTurns(
            [.success(.fixture(status: "running"))],
            fallback: .success(.fixture(status: "done", resultText: "ok"))
        )
        var policy = PollingPolicy.test()
        policy.burstDuration = .milliseconds(200)
        policy.fastInterval = 10_000_000_000

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: policy
        )
        defer { store.cancelActiveWork() }

        store.send(text: "ask")
        await awaitSettled(store)

        #expect(await fake.permissionsCallCount == 0, "the burst polls the turn only")
    }

    // MARK: - The store

    @Test func aRunningTurnShowsTheReplyAsItIsWritten() async throws {
        let fake = FakeChatAPI()
        await fake.stubTurns([
            .success(.fixture(status: "running", partialText: "the answer so far")),
            .success(.fixture(status: "done", resultText: "the answer, finished")),
        ])
        let reachedTerminalPoll = Gate()
        let holdTerminalPoll = Gate()
        await fake.setOnGetTurn { call in
            if call == 2 {
                await reachedTerminalPoll.open()
                await holdTerminalPoll.wait()
            }
        }

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(),
            policy: .test()
        )
        defer { store.cancelActiveWork() }

        store.send(text: "ask")
        await awaitOpened(reachedTerminalPoll, "the running poll should land first")
        #expect(
            await waitUntil { store.messages.last?.streamedText == "the answer so far" },
            "the reply should be readable while the turn is still running"
        )

        await holdTerminalPoll.open()
        await awaitSettled(store)
        let message = try requireCount(store.messages, 2)[1]
        // The correctness rule: `result_text` from the envelope wins. The
        // streamed copy is an approximation assembled from deltas and must
        // never be what a finished turn is left showing.
        #expect(message.status == .done)
        #expect(message.text == "the answer, finished")
        #expect(message.streamedText == nil)
    }

    @Test func aResumedTurnDoesNotInheritThePreviousLaunchesReply() async throws {
        /// A transcript written mid-turn carries the partial reply, and
        /// playback state is rebuilt empty on launch — so leaving it on screen
        /// means the first tick rewinds the bubble from a sentence back to a
        /// few characters. `reconcilePendingMessages` drops it for the same
        /// reason it drops the activity label.
        let persistURL = makeTempTranscriptURL()
        var onDisk = ChatMessage(
            id: UUID(), role: .assistant, text: "", createdAt: .now,
            turnId: "turn-1", status: .running, clientMsgId: UUID().uuidString
        )
        onDisk.streamedText = "Here are the three things I fou"
        onDisk.streamedTextMarks = [12]
        try writeTranscript([onDisk], to: persistURL)

        let fake = FakeChatAPI()
        await fake.stubTurns([.success(.fixture(status: "done", resultText: "finished"))])
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: persistURL, policy: .test()
        )
        defer { store.cancelActiveWork() }

        let resumed = try requireCount(store.messages, 1)[0]
        #expect(resumed.streamedText == nil, "the previous launch's half-written reply must not render")
        #expect(resumed.streamedTextMarks == nil)
    }

    @Test func aResumedTurnShowsTheReplySoFarWithoutRetypingIt() async throws {
        /// The store half of the catch-up rule. A real pacing budget is needed
        /// to tell the two behaviours apart, so this policy sets a poll
        /// interval and a tick rather than `.test()`'s zeros: paced, 300
        /// characters at `ticksPerPoll` 40 would take 40 ticks (2s) to appear;
        /// caught up, they land on the first one.
        /// The burst phase carries the poll, because `pollLoop` sleeps its
        /// interval BEFORE each request — a 2s fast interval alone would put
        /// the first poll past this test's whole deadline. A small non-zero
        /// burst interval rather than `.test()`'s zero, so the live burst
        /// paces instead of spinning hot against the fake.
        var policy = PollingPolicy.test(burstDuration: .seconds(2))
        policy.burstInterval = 10_000_000
        policy.fastInterval = 2_000_000_000
        policy.partialTextTick = 50_000_000

        let persistURL = makeTempTranscriptURL()
        let onDisk = ChatMessage(
            id: UUID(), role: .assistant, text: "", createdAt: .now,
            turnId: "turn-1", status: .running, clientMsgId: UUID().uuidString
        )
        try writeTranscript([onDisk], to: persistURL)

        let writtenWhileWeWereAway = String(repeating: "z", count: 300)
        let fake = FakeChatAPI()
        await fake.stubTurns(
            [.success(.fixture(status: "running", partialText: writtenWhileWeWereAway))],
            fallback: .success(.fixture(status: "running", partialText: writtenWhileWeWereAway))
        )
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: persistURL, policy: policy
        )
        defer { store.cancelActiveWork() }

        #expect(
            await waitUntil(timeout: .milliseconds(600)) {
                store.messages.last?.streamedText == writtenWhileWeWereAway
            },
            "a reply written before this launch should appear, not type itself out again"
        )
    }

    @Test func anErroredTurnDropsTheHalfWrittenReply() async throws {
        let fake = FakeChatAPI()
        await fake.stubTurns([
            .success(.fixture(status: "running", partialText: "I was saying")),
            .success(.fixture(status: "error", error: "failed")),
        ])
        let reachedTerminalPoll = Gate()
        let holdTerminalPoll = Gate()
        await fake.setOnGetTurn { call in
            if call == 2 {
                await reachedTerminalPoll.open()
                await holdTerminalPoll.wait()
            }
        }

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(),
            policy: .test()
        )
        defer { store.cancelActiveWork() }

        store.send(text: "ask")
        await awaitOpened(reachedTerminalPoll, "the running poll should land first")
        _ = await waitUntil { store.messages.last?.streamedText != nil }

        await holdTerminalPoll.open()
        await awaitSettled(store)
        let message = try requireCount(store.messages, 2)[1]
        #expect(message.status == .error)
        #expect(message.text == "failed")
        #expect(message.streamedText == nil, "half a sentence under an error message is worse than none")
    }
}
