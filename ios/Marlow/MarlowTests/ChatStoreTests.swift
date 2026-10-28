import Foundation
import Testing
import UIKit
@testable import Marlow

/// The chat state machine. These cover the four lifecycle bugs `/code-review`
/// found by hand on the first chat screen, 2026-07-31 (leaked poll task past Unpair, permanent-vs-transient
/// error handling, no path back from the poll timeout, a stuck placeholder
/// across a force-quit) so a future change to `ChatStore` has to break a test
/// to reintroduce one.
@MainActor
struct ChatStoreTests {

    // MARK: - Send -> poll -> terminal

    @Test func sendPollsUntilDoneAndShowsTheResult() async throws {
        let fake = FakeChatAPI()
        await fake.stubSend(.success(MessageResponse(turnId: "turn-42")))
        await fake.stubTurns([
            .success(.fixture(status: "pending")),
            .success(.fixture(status: "running")),
            .success(.fixture(status: "done", resultText: "Two emails need you.")),
        ])

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        store.send(text: "  check my email  ")
        await awaitSettled(store)

        let messages = try requireCount(store.messages, 2)
        #expect(messages[0].role == .user)
        #expect(messages[0].text == "check my email", "the draft is trimmed before sending")
        #expect(messages[1].role == .assistant)
        #expect(messages[1].status == .done)
        #expect(messages[1].text == "Two emails need you.")
        #expect(messages[1].turnId == "turn-42")

        let sent = try requireCount(await fake.sentMessages, 1)
        #expect(sent[0].text == "check my email")
        #expect(sent[0].deviceToken == "tok")
        #expect(sent[0].clientMsgId == messages[0].id.uuidString)

        let polls = try requireCount(await fake.getTurnCalls, 3)
        #expect(polls.allSatisfy { $0.turnId == "turn-42" })
    }

    @Test func terminalErrorStatusSurfacesTheServersMessage() async throws {
        let fake = FakeChatAPI()
        await fake.stubTurns([.success(.fixture(status: "error", error: "claude exited 1"))])

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        store.send(text: "hi")
        await awaitSettled(store)

        let messages = try requireCount(store.messages, 2)
        #expect(messages[1].status == .error)
        #expect(messages[1].text == "claude exited 1")
        #expect(await fake.getTurnCallCount == 1, "a terminal status stops the loop")
    }

    @Test func mergedRemovesOnlyTheAssistantPlaceholderAndStopsPolling() async throws {
        let fake = FakeChatAPI()
        await fake.stubSend(.success(MessageResponse(turnId: "absorbed")))
        await fake.stubTurns([.success(.fixture(turnId: "absorbed", status: "merged"))])

        let persistURL = makeTempTranscriptURL()
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: persistURL, policy: .test()
        )
        defer { store.cancelActiveWork() }

        store.send(text: "and Thursday too")
        await awaitSettled(store)

        let messages = try requireCount(store.messages, 1)
        #expect(messages[0].role == .user)
        #expect(messages[0].text == "and Thursday too")
        #expect(try requireCount(readTranscript(at: persistURL), 1)[0].role == .user)
        #expect(await fake.getTurnCallCount == 1, "merged is terminal, not another running state")
        #expect(store.completedAssistantTurns.isEmpty, "an absorbed turn is not a new arrival")
    }

    @Test func doneClearsTheCurrentActivityLabel() async throws {
        let fake = FakeChatAPI()
        await fake.stubTurns([
            .success(.fixture(status: "running", activities: [
                TurnActivity(kind: "tool_start", tool: "Read"),
                TurnActivity(kind: "tool_start", tool: "Bash"),
                TurnActivity(kind: "tool_start", tool: "WebSearch"),
            ])),
            .success(.fixture(status: "done", resultText: "finished")),
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
            policy: .test(activityDwell: 10_000_000_000)
        )
        defer { store.cancelActiveWork() }

        store.send(text: "read it")
        await awaitOpened(reachedTerminalPoll, "the running activity should land before the terminal poll")
        #expect(try requireCount(store.messages, 2)[1].activityLabel == "Reading a file…")

        await holdTerminalPoll.open()
        await awaitSettled(store)
        let message = try requireCount(store.messages, 2)[1]
        #expect(message.status == .done)
        #expect(message.activityLabel == nil, "terminal status drops the two labels still queued behind Read")
    }

    @Test func errorClearsTheCurrentActivityLabel() async throws {
        let fake = FakeChatAPI()
        await fake.stubTurns([
            .success(.fixture(status: "running", activities: [TurnActivity(kind: "tool_start", tool: "Bash")])),
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
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        store.send(text: "run it")
        await awaitOpened(reachedTerminalPoll, "the running activity should land before the terminal poll")
        #expect(try requireCount(store.messages, 2)[1].activityLabel == "Running a command…")

        await holdTerminalPoll.open()
        await awaitSettled(store)
        let message = try requireCount(store.messages, 2)[1]
        #expect(message.status == .error)
        #expect(message.activityLabel == nil)
    }

    @Test func aFailedPollDoesNotDiscardAlreadyQueuedActivity() async throws {
        let fake = FakeChatAPI()
        await fake.stubTurns([
            .success(.fixture(status: "running", activities: [TurnActivity(kind: "tool_start", tool: "Read")])),
            .failure(URLError(.networkConnectionLost)),
            .success(.fixture(status: "done", resultText: "finished")),
        ])
        let reachedFinalPoll = Gate()
        let holdFinalPoll = Gate()
        await fake.setOnGetTurn { call in
            if call == 3 {
                await reachedFinalPoll.open()
                await holdFinalPoll.wait()
            }
        }

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        store.send(text: "read it")
        await awaitOpened(reachedFinalPoll, "polling should continue after the transient failure")
        let inFlight = try requireCount(store.messages, 2)[1]
        #expect(inFlight.status == .running)
        #expect(inFlight.activityLabel == "Reading a file…", "received events keep playing through a fetch failure")

        await holdFinalPoll.open()
        await awaitSettled(store)
    }

    @Test func cumulativeActivityOnlyQueuesNewEventsAndCoalescesLabels() throws {
        var state = ActivityPlaybackState()
        let first = [
            TurnActivity(kind: "tool_start", tool: "Read"),
            TurnActivity(kind: "tool_end", tool: nil),
            TurnActivity(kind: "text", tool: nil),
        ]

        // `text` carries its own label ("Writing…") as of reply streaming, so
        // it no longer coalesces into the tool_end "Thinking…" ahead of it.
        state.ingest(first)
        #expect(state.pendingLabels == ["Reading a file…", "Thinking…", "Writing…"])

        state.ingest(first)
        #expect(
            state.pendingLabels == ["Reading a file…", "Thinking…", "Writing…"],
            "a repeated cumulative poll adds nothing"
        )

        // Four labels now exceed the three-deep skip-ahead cap, so the oldest
        // is dropped — the same catch-up behaviour the next test pins.
        state.ingest(first + [TurnActivity(kind: "tool_start", tool: "Bash")])
        #expect(state.pendingLabels == ["Thinking…", "Writing…", "Running a command…"])
    }

    @Test func activityBacklogSkipsAheadToTheLastThreeLabels() {
        var state = ActivityPlaybackState()
        state.ingest([
            TurnActivity(kind: "tool_start", tool: "Read"),
            TurnActivity(kind: "tool_end", tool: nil),
            TurnActivity(kind: "tool_start", tool: "Bash"),
            TurnActivity(kind: "tool_end", tool: nil),
            TurnActivity(kind: "tool_start", tool: "WebSearch"),
        ])

        #expect(state.pendingLabels == ["Running a command…", "Thinking…", "Searching the web…"])
    }

    @Test func activityCursorContinuesWhenTheServersCappedWindowSlides() {
        var state = ActivityPlaybackState()
        let initial = (0..<64).map {
            TurnActivity(kind: "tool_start", tool: $0.isMultiple(of: 2) ? "Read" : "Bash", at: Double($0))
        }
        state.ingest(initial)
        while state.nextLabel() != nil {}

        let newest = TurnActivity(kind: "tool_end", tool: nil, at: 64)
        state.ingest(Array(initial.dropFirst()) + [newest])

        #expect(state.pendingLabels == ["Thinking…"], "the event appended after cap rollover must not be lost")
    }

    @Test func productionActivityLabelsDwellForSevenHundredMilliseconds() {
        #expect(PollingPolicy.default.activityDwell == 700_000_000)
    }

    @Test func cancellingWorkClearsALabelWhoseQueueAlreadyDrained() async throws {
        let fake = FakeChatAPI()
        let running = Turn.fixture(status: "running", activities: [TurnActivity(kind: "tool_start", tool: "Read")])
        await fake.stubTurns([.success(running)], fallback: .success(running))
        let reachedThirdPoll = Gate()
        let holdThirdPoll = Gate()
        await fake.setOnGetTurn { call in
            if call == 3 {
                await reachedThirdPoll.open()
                await holdThirdPoll.wait()
            }
        }

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(),
            policy: .test(activityDwell: 0)
        )
        defer { store.cancelActiveWork() }

        store.send(text: "read it")
        await awaitOpened(reachedThirdPoll, "polling should reach a third call")
        // Dwell is 0, so the one coalesced label is shown and the playback task
        // unwinds on its own, removing itself from `activityPlaybackTasks`.
        // That now-empty dictionary is exactly what the clear-up used to key
        // off, which is why this case needs its own test.
        #expect(
            await waitUntil { store.messages.last?.activityLabel == "Reading a file…" },
            "the label should have been shown before the queue drained"
        )

        store.cancelActiveWork()
        await holdThirdPoll.open()
        #expect(
            try requireCount(store.messages, 2)[1].activityLabel == nil,
            "a drained queue still leaves a label to clear"
        )
    }

    @Test func stallingDropsTheLabelAndTheSlowPhaseNeverPacesAnother() async throws {
        let fake = FakeChatAPI()
        let running = Turn.fixture(status: "running", activities: [TurnActivity(kind: "tool_start", tool: "Read")])
        await fake.stubTurns([.success(running), .success(running)], fallback: .success(running))
        let reachedSlowPoll = Gate()
        let holdSlowPoll = Gate()
        await fake.setOnGetTurn { call in
            if call == 3 {
                await reachedSlowPoll.open()
                await holdSlowPoll.wait()
            }
        }

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(),
            policy: .test(fastAttempts: 2, activityDwell: 0)
        )
        defer { store.cancelActiveWork() }

        store.send(text: "read it")
        await awaitOpened(reachedSlowPoll, "the fast phase should exhaust into the slow phase")
        let stalled = try requireCount(store.messages, 2)[1]
        #expect(stalled.status == .stalled)
        #expect(stalled.activityLabel == nil, "the stall transition drops what the fast phase left")

        await holdSlowPoll.open()
        await awaitSettled(store)
        #expect(
            try requireCount(store.messages, 2)[1].activityLabel == nil,
            "the slow phase never queues a label `.stalled` cannot render"
        )
    }

    @Test func emptyDraftIsIgnored() async {
        let fake = FakeChatAPI()
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        store.send(text: "   \n  ")
        await awaitSettled(store)

        #expect(store.messages.isEmpty)
        #expect(await fake.sentMessages.isEmpty)
    }

    @Test func aFailedPostMarksThePlaceholderInError() async throws {
        let fake = FakeChatAPI()
        await fake.stubSend(.failure(APIError.server(status: 500, detail: "boom")))

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        store.send(text: "hi")
        await awaitSettled(store)

        let messages = try requireCount(store.messages, 2)
        #expect(messages[1].status == .error)
        #expect(messages[1].text.contains("500"))
        #expect(await fake.getTurnCallCount == 0, "never polls a turn that was never created")
    }

    @Test func aNonTransientSendFailureFailsWithoutRetrying() async throws {
        let fake = FakeChatAPI()
        await fake.stubSend(.failure(APIError.server(status: 400, detail: "bad request")))

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        store.send(text: "hi")
        await awaitSettled(store)

        let messages = try requireCount(store.messages, 2)
        #expect(messages[1].status == .error)
        #expect(await fake.sentMessages.count == 1, "a rejected request is not worth retrying")
    }

    // MARK: - Flaky send recovery

    @Test func aDroppedConnectionOnSendRetriesUntilItLands() async throws {
        let fake = FakeChatAPI()
        await fake.stubSendScript(
            [.failure(URLError(.networkConnectionLost)), .failure(URLError(.timedOut))],
            fallback: .success(MessageResponse(turnId: "turn-42"))
        )
        await fake.stubTurns([.success(.fixture(turnId: "turn-42", status: "done", resultText: "made it"))])

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        store.send(text: "hi from a tunnel")
        await awaitSettled(store)

        let messages = try requireCount(store.messages, 2)
        #expect(messages[1].status == .done, "the third attempt should have gone through")
        #expect(messages[1].text == "made it")
        #expect(await fake.sentMessages.count == 3, "two drops, then the send that landed")
    }

    @Test func aDroppedConnectionThatNeverRecoversEventuallyFailsWithARetryOption() async throws {
        let fake = FakeChatAPI()
        await fake.stubSend(.failure(URLError(.notConnectedToInternet)))

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test(sendMaxAttempts: 3)
        )
        defer { store.cancelActiveWork() }

        store.send(text: "hi")
        await awaitSettled(store)

        let messages = try requireCount(store.messages, 2)
        #expect(messages[1].status == .error)
        #expect(await fake.sentMessages.count == 3, "gives up only once the retry budget is spent")
        #expect(messages[1].clientMsgId != nil, "so the retry action in the UI has something to resend")
    }

    @Test func aSendThatNeverReturnsTimesOutAsTransientAndRetries() async throws {
        let fake = FakeChatAPI()
        let firstAttemptCancelled = Gate()
        await fake.setOnSend { callIndex in
            guard callIndex == 1 else { return }
            do {
                try await Task.sleep(for: .seconds(60))
                Issue.record("the first request should have been cancelled at its deadline")
            } catch is CancellationError {
                await firstAttemptCancelled.open()
            } catch {
                Issue.record("unexpected error while parking the first request: \(error)")
            }
        }
        await fake.stubTurns([.success(.fixture(status: "done", resultText: "made it"))])

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(),
            policy: .test(sendMaxAttempts: 2, sendAttemptTimeout: 0.01)
        )
        defer { store.cancelActiveWork() }

        store.send(text: "hi from a dead zone")
        await awaitSettled(store)

        await awaitOpened(firstAttemptCancelled, "the wall-clock deadline should cancel the hung request")
        let messages = try requireCount(store.messages, 2)
        #expect(messages[1].status == .done, "URLError.timedOut must take the existing transient retry path")
        #expect(messages[1].text == "made it")
        #expect(await fake.sentMessages.count == 2, "the deadline must retry rather than surface CancellationError")
    }

    @Test func anImageSendUsesTheSameTwentySecondAttemptTimeout() async throws {
        let fake = FakeChatAPI()
        await fake.stubTurns([.success(.fixture(status: "done", resultText: "ok"))])
        var policy = PollingPolicy.test()
        policy.sendAttemptTimeout = PollingPolicy.default.sendAttemptTimeout

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(),
            attachmentsDirectoryURL: makeTempAttachmentsDirectoryURL(), policy: policy
        )
        defer { store.cancelActiveWork() }

        store.send(text: "what's this?", images: [Data([1, 2, 3])])
        await awaitSettled(store)

        let sent = try requireCount(await fake.sentMessages, 1)
        #expect(!sent[0].imagesBase64.isEmpty, "sanity: this exercised the image path")
        #expect(sent[0].timeout == 20, "text and downscaled-image sends share Mike's settled 20s bound")
    }

    @Test func aSuccessfulSendCancelsItsDeadlineTask() async throws {
        let fake = FakeChatAPI()
        await fake.stubTurns([.success(.fixture(status: "done", resultText: "ok"))])
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(),
            policy: .test(sendAttemptTimeout: 5)
        )
        defer { store.cancelActiveWork() }

        store.send(text: "hi")

        let settled = await store.awaitPendingWork(timeout: .seconds(1))
        #expect(settled, "the successful request must cancel the 5s deadline instead of leaving its Task alive")
        let messages = try requireCount(store.messages, 2)
        #expect(messages[1].status == .done)
    }

    @Test func retryResendsAFailedTurnAndReachesTheSameOutcomeAsANormalSend() async throws {
        let fake = FakeChatAPI()
        await fake.stubSend(.failure(URLError(.notConnectedToInternet)))

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test(sendMaxAttempts: 1)
        )
        defer { store.cancelActiveWork() }

        store.send(text: "still offline")
        await awaitSettled(store)
        let failed = try requireCount(store.messages, 2)
        #expect(failed[1].status == .error)

        await fake.stubSend(.success(MessageResponse(turnId: "turn-99")))
        await fake.stubTurns([.success(.fixture(turnId: "turn-99", status: "done", resultText: "back online"))])

        store.retry(failed[1].id)
        await awaitSettled(store)

        let messages = try requireCount(store.messages, 2)
        #expect(messages[0].text == "still offline", "the original user message is untouched")
        #expect(messages[1].status == .done)
        #expect(messages[1].text == "back online")
        let sent = try requireCount(await fake.sentMessages, 2)
        #expect(sent[1].clientMsgId == messages[0].id.uuidString, "resent under the same client_msg_id")
    }

    @Test func retryOnAnythingOtherThanAFailedMessageDoesNothing() async throws {
        let fake = FakeChatAPI()
        await fake.stubSend(.success(MessageResponse(turnId: "turn-1")))
        let gate = Gate()
        let entered = Gate()
        await fake.setOnGetTurn { _ in
            await entered.open()
            await gate.wait()
        }
        await fake.stubTurns([.success(.fixture(status: "running"))], fallback: .success(.fixture(status: "running")))

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer {
            store.cancelActiveWork()
            Task { await gate.open() } // unblock the parked poll so its Task can actually exit
        }

        store.send(text: "hi")
        await awaitOpened(entered, "should be parked mid-poll, still .running")

        let before = store.messages
        #expect(before[1].status == .running)
        store.retry(before[1].id) // not .error — must be a no-op
        #expect(store.messages == before)
    }

    /// Regression test for a `/code-review medium` finding on the resilient-sends change (2026-08-08):
    /// `submit()`'s retry loop swallowed cancellation during its backoff
    /// sleep (`try? await Task.sleep(...)` discards `CancellationError`) and
    /// looped straight back into another real network call — contradicting
    /// `cancelActiveWork()`'s own documented guarantee of "reliably stops
    /// outbound calls." This pins the fix: a cancel that lands mid-backoff
    /// must prevent the next attempt from ever firing.
    @Test func cancelActiveWorkDuringSendBackoffPreventsTheNextAttempt() async throws {
        let fake = FakeChatAPI()
        await fake.stubSendScript(
            [.failure(URLError(.networkConnectionLost))],
            fallback: .success(MessageResponse(turnId: "turn-1"))
        )
        let firstAttemptStarted = Gate()
        let releaseFirstAttempt = Gate()
        let secondAttemptStarted = Gate()
        await fake.setOnSend { callIndex in
            if callIndex == 1 {
                await firstAttemptStarted.open()
                await releaseFirstAttempt.wait()
            } else if callIndex == 2 {
                await secondAttemptStarted.open()
            }
        }

        // Long enough backoff to reliably land a cancel inside the sleep
        // rather than racing it — the point is to land strictly *after* the
        // catch block's own (pre-existing, already-correct) isCancelled
        // check, which is what distinguishes this from that check.
        var policy = PollingPolicy.test()
        policy.sendBaseInterval = 300_000_000
        policy.sendMaxInterval = 300_000_000

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: policy
        )
        defer { store.cancelActiveWork() }

        store.send(text: "hi")
        await awaitOpened(firstAttemptStarted, "first attempt should have started")
        await releaseFirstAttempt.open() // let it fail and clear its own isCancelled check

        try await Task.sleep(for: .milliseconds(30)) // let the (synchronous) catch block run, landing us in the sleep
        store.cancelActiveWork()

        let openedInTime = await secondAttemptStarted.waitOpened(timeout: .milliseconds(600))
        #expect(!openedInTime, "cancelling mid-backoff must stop the retry loop before it fires again")
        #expect(await fake.sentMessages.count == 1, "only the already in-flight first attempt should have gone out")
    }

    // MARK: - Permanent vs. transient

    @Test func aPermanent404StopsPollingImmediately() async throws {
        let fake = FakeChatAPI()
        await fake.stubTurns([], fallback: .failure(APIError.server(status: 404, detail: nil)))

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test(fastAttempts: 50)
        )
        defer { store.cancelActiveWork() }

        store.send(text: "hi")
        await awaitSettled(store)

        #expect(await fake.getTurnCallCount == 1, "a 404 must not burn the whole poll budget")
        let messages = try requireCount(store.messages, 2)
        #expect(messages[1].status == .error)
        #expect(messages[1].text.contains("lost track"))
    }

    @Test func transientFailuresKeepPollingUntilTheAnswerArrives() async throws {
        let fake = FakeChatAPI()
        await fake.stubTurns([
            .failure(URLError(.timedOut)),
            .failure(URLError(.networkConnectionLost)),
            .failure(APIError.server(status: 503, detail: "restarting")),
            .success(.fixture(status: "done", resultText: "late but here")),
        ])

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        store.send(text: "hi")
        await awaitSettled(store)

        #expect(await fake.getTurnCallCount == 4)
        let messages = try requireCount(store.messages, 2)
        #expect(messages[1].status == .done)
        #expect(messages[1].text == "late but here")
    }

    @Test func transientFailuresGiveUpAtTheAttemptCap() async throws {
        let fake = FakeChatAPI()
        await fake.stubTurns([], fallback: .failure(URLError(.timedOut)))

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(),
            policy: .test(fastAttempts: 3, slowAttempts: 2)
        )
        defer { store.cancelActiveWork() }

        store.send(text: "hi")
        await awaitSettled(store)

        #expect(await fake.getTurnCallCount == 5, "both phases exhaust, then it stops")
        let messages = try requireCount(store.messages, 2)
        #expect(messages[1].status == .error, "only now is it a real, terminal error")
        #expect(messages[1].text.contains("never answered"))
    }

    // MARK: - Fast -> slow phase handoff

    @Test func aLateDoneAfterTheFastPhaseStillUpgradesTheMessage() async throws {
        let persistURL = makeTempTranscriptURL()
        let fake = FakeChatAPI()
        let recorder = Recorder()
        await fake.stubTurns([
            .success(.fixture(status: "running")),
            .success(.fixture(status: "running")),
            .success(.fixture(status: "done", resultText: "took a while, but: done")),
        ])
        // Snapshot the persisted transcript at the top of each poll. Call 3 is
        // the first of the slow phase, so its snapshot shows what the user was
        // left looking at once the fast window closed.
        await fake.setOnGetTurn { _ in
            await recorder.record(readTranscript(at: persistURL))
        }

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: persistURL,
            policy: .test(fastAttempts: 2, slowAttempts: 5)
        )
        defer { store.cancelActiveWork() }

        store.send(text: "trawl the web for me")
        await awaitSettled(store)

        let snapshots = try requireCount(await recorder.transcripts, 3)
        let afterFastPhase = try requireCount(snapshots[2], 2)[1]
        #expect(afterFastPhase.status == .stalled, "the fast window closing is visible, not a silent spinner")
        #expect(afterFastPhase.text.contains("still checking in the background"))
        #expect(afterFastPhase.turnId != nil, "keeps the turn_id, so a relaunch can resume it")

        let messages = try requireCount(store.messages, 2)
        #expect(messages[1].status == .done, "the late answer replaces the interim error")
        #expect(messages[1].text == "took a while, but: done")
    }

    // MARK: - Cancellation

    @Test func cancelActiveWorkStopsMutationMidFlight() async throws {
        let fake = FakeChatAPI()
        let gate = Gate()
        let entered = Gate()
        await fake.stubTurns([.success(.fixture(status: "done", resultText: "should never land"))])
        // Block inside the first getTurn so the cancel lands while a call is
        // genuinely in flight — the exact window the Unpair bug lived in.
        await fake.setOnGetTurn { _ in
            await entered.open()
            await gate.wait()
        }

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test()
        )

        store.send(text: "hi")
        await awaitOpened(entered, "send() should have reached the first getTurn")

        store.cancelActiveWork()
        await gate.open()
        await awaitSettled(store)

        let messages = try requireCount(store.messages, 2)
        #expect(messages[1].status == .running, "a cancelled poll must not write its result")
        #expect(messages[1].text.isEmpty)
    }

    @Test func cancelActiveWorkStopsFurtherPolling() async throws {
        let fake = FakeChatAPI()
        await fake.stubTurns([], fallback: .success(.fixture(status: "running")))

        // A real (if tiny) interval, not zero: with a zero interval the loop
        // would burn all 5000 attempts before the test could cancel anything,
        // and there'd be no window to cancel *in*.
        let policy = PollingPolicy(
            fastInterval: 1_000_000, fastAttempts: 5000,
            slowInterval: 1_000_000, slowAttempts: 5000,
            healthInterval: 1_000_000_000_000
        )
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: policy
        )

        store.send(text: "hi")
        // Wait for polling to genuinely be under way. Cancelling before the
        // first poll would make every assertion below vacuously true — the
        // test has to prove it stopped something that was actually running.
        #expect(
            await waitUntil { await fake.getTurnCallCount >= 3 },
            "polling should have started before we cancel it"
        )

        store.cancelActiveWork()
        await awaitSettled(store)

        let countAfterCancel = await fake.getTurnCallCount
        #expect(countAfterCancel >= 3, "sanity: polling really had started")
        #expect(countAfterCancel < 5000, "cancelled well before the attempt cap")

        try await Task.sleep(for: .milliseconds(50))
        #expect(await fake.getTurnCallCount == countAfterCancel, "no polling continues past cancellation")
    }

    // MARK: - Reconciliation at launch

    @Test func aRunningMessageOnDiskResumesPolling() async throws {
        let persistURL = makeTempTranscriptURL()
        let user = ChatMessage(
            id: UUID(), role: .user, text: "what's up", createdAt: Date(), turnId: nil,
            status: .done, clientMsgId: nil
        )
        let placeholder = ChatMessage(
            id: UUID(), role: .assistant, text: "", createdAt: Date(), turnId: "turn-9",
            status: .running, clientMsgId: user.id.uuidString
        )
        try writeTranscript([user, placeholder], to: persistURL)

        let fake = FakeChatAPI()
        await fake.stubTurns([.success(.fixture(status: "done", resultText: "recovered"))])

        let store = ChatStore(deviceToken: "tok", client: fake, persistURL: persistURL, policy: .test())
        defer { store.cancelActiveWork() }
        await awaitSettled(store)

        let polls = try requireCount(await fake.getTurnCalls, 1)
        #expect(polls[0].turnId == "turn-9", "resumes the stored turn rather than resending")
        #expect(await fake.sentMessages.isEmpty)
        let messages = try requireCount(store.messages, 2)
        #expect(messages[1].status == .done)
        #expect(messages[1].text == "recovered")
    }

    @Test func aTurnMergedWhileClosedReconcilesToTheUserMessageAlone() async throws {
        let persistURL = makeTempTranscriptURL()
        let user = ChatMessage(
            id: UUID(), role: .user, text: "one more thing", createdAt: Date(), turnId: nil,
            status: .done, clientMsgId: nil
        )
        let placeholder = ChatMessage(
            id: UUID(), role: .assistant, text: "", createdAt: Date(), turnId: "absorbed",
            status: .running, clientMsgId: user.id.uuidString
        )
        try writeTranscript([user, placeholder], to: persistURL)

        let fake = FakeChatAPI()
        await fake.stubTurns([.success(.fixture(turnId: "absorbed", status: "merged"))])
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: persistURL, policy: .test()
        )
        defer { store.cancelActiveWork() }
        await awaitSettled(store)

        let messages = try requireCount(store.messages, 1)
        #expect(messages[0].id == user.id)
        #expect(try requireCount(readTranscript(at: persistURL), 1)[0].id == user.id)
        #expect(await fake.sentMessages.isEmpty, "reconciliation polls the known turn; it does not resend")
        #expect(store.completedAssistantTurns.isEmpty)
    }

    @Test func aResumedMessageDropsTheActivityLabelItWasPersistedWith() async throws {
        let persistURL = makeTempTranscriptURL()
        let user = ChatMessage(
            id: UUID(), role: .user, text: "what's on today", createdAt: Date(), turnId: nil,
            status: .done, clientMsgId: nil
        )
        var placeholder = ChatMessage(
            id: UUID(), role: .assistant, text: "", createdAt: Date(), turnId: "turn-9",
            status: .running, clientMsgId: user.id.uuidString
        )
        // What a mid-turn persist() from some other path leaves on disk.
        placeholder.activityLabel = "Checking your calendar…"
        try writeTranscript([user, placeholder], to: persistURL)

        let fake = FakeChatAPI()
        await fake.stubTurns([.success(.fixture(status: "running", activities: []))])

        let store = ChatStore(deviceToken: "tok", client: fake, persistURL: persistURL, policy: .test())
        defer { store.cancelActiveWork() }

        // Read before awaiting anything: `reconcilePendingMessages` runs inside
        // `init`, so this is the state the first frame renders — the poll Task
        // has had no suspension point to start on yet.
        let messages = try requireCount(store.messages, 2)
        #expect(messages[1].status == .running)
        #expect(
            messages[1].activityLabel == nil,
            "a label from a previous launch must not narrate the resumed turn"
        )
    }

    @Test func aRunningMessageWithNoTurnIdCannotBeRecovered() async throws {
        let persistURL = makeTempTranscriptURL()
        let placeholder = ChatMessage(
            id: UUID(), role: .assistant, text: "", createdAt: Date(), turnId: nil,
            status: .running, clientMsgId: nil
        )
        try writeTranscript([placeholder], to: persistURL)

        let fake = FakeChatAPI()
        let store = ChatStore(deviceToken: "tok", client: fake, persistURL: persistURL, policy: .test())
        defer { store.cancelActiveWork() }
        await awaitSettled(store)

        let messages = try requireCount(store.messages, 1)
        #expect(messages[0].status == .error)
        #expect(messages[0].text.contains("Interrupted before Marlow replied"))
        #expect(await fake.getTurnCallCount == 0)
        let persisted = try requireCount(readTranscript(at: persistURL), 1)
        #expect(persisted[0].status == .error, "the fallback is persisted, not just in memory")
    }

    /// The bug this file's review found: the interim post-fast-phase state used
    /// to be persisted as `.error`, which reconciliation treats as terminal —
    /// so a force-quit during the slow phase stranded the message on disk
    /// showing "still checking in the background" while nothing checked, with a
    /// perfectly good turn_id sitting right there.
    @Test func aStalledMessageOnDiskResumesPolling() async throws {
        let persistURL = makeTempTranscriptURL()
        let user = ChatMessage(
            id: UUID(), role: .user, text: "trawl the web", createdAt: Date(), turnId: nil,
            status: .done, clientMsgId: nil
        )
        let placeholder = ChatMessage(
            id: UUID(), role: .assistant,
            text: "Marlow hasn't answered yet — still checking in the background.",
            createdAt: Date(), turnId: "turn-slow", status: .stalled, clientMsgId: user.id.uuidString
        )
        try writeTranscript([user, placeholder], to: persistURL)

        let fake = FakeChatAPI()
        await fake.stubTurns([.success(.fixture(status: "done", resultText: "the late answer"))])

        let store = ChatStore(deviceToken: "tok", client: fake, persistURL: persistURL, policy: .test())
        defer { store.cancelActiveWork() }
        await awaitSettled(store)

        let polls = try requireCount(await fake.getTurnCalls, 1)
        #expect(polls[0].turnId == "turn-slow", "resumes the stalled turn instead of stranding it")
        #expect(await fake.sentMessages.isEmpty, "it has a turn_id — resume, don't resend")
        let messages = try requireCount(store.messages, 2)
        #expect(messages[1].status == .done)
        #expect(messages[1].text == "the late answer")
    }

    @Test func aStalledMessageWithNoTurnIdCannotBeRecovered() async throws {
        let persistURL = makeTempTranscriptURL()
        let placeholder = ChatMessage(
            id: UUID(), role: .assistant, text: "", createdAt: Date(), turnId: nil,
            status: .stalled, clientMsgId: nil
        )
        try writeTranscript([placeholder], to: persistURL)

        let fake = FakeChatAPI()
        let store = ChatStore(deviceToken: "tok", client: fake, persistURL: persistURL, policy: .test())
        defer { store.cancelActiveWork() }
        await awaitSettled(store)

        let messages = try requireCount(store.messages, 1)
        #expect(messages[0].status == .error)
        #expect(await fake.getTurnCallCount == 0)
    }

    @Test func aSendingMessageOnDiskIsResentUnderTheSameClientMsgId() async throws {
        let persistURL = makeTempTranscriptURL()
        let user = ChatMessage(
            id: UUID(), role: .user, text: "did it land?", createdAt: Date(), turnId: nil,
            status: .done, clientMsgId: nil
        )
        let placeholder = ChatMessage(
            id: UUID(), role: .assistant, text: "", createdAt: Date(), turnId: nil,
            status: .sending, clientMsgId: user.id.uuidString
        )
        try writeTranscript([user, placeholder], to: persistURL)

        let fake = FakeChatAPI()
        await fake.stubSend(.success(MessageResponse(turnId: "turn-dedup")))
        await fake.stubTurns([.success(.fixture(status: "done", resultText: "it landed"))])

        let store = ChatStore(deviceToken: "tok", client: fake, persistURL: persistURL, policy: .test())
        defer { store.cancelActiveWork() }
        await awaitSettled(store)

        let sent = try requireCount(await fake.sentMessages, 1)
        #expect(sent[0].text == "did it land?", "resends the paired user message's text")
        #expect(
            sent[0].clientMsgId == user.id.uuidString,
            "same client_msg_id so the server dedupes if the original POST did land"
        )
        let messages = try requireCount(store.messages, 2)
        #expect(messages[1].status == .done)
        #expect(messages[1].text == "it landed")
    }

    @Test func aSendingMessageWithNoPairedUserMessageCannotBeRecovered() async throws {
        let persistURL = makeTempTranscriptURL()
        let placeholder = ChatMessage(
            id: UUID(), role: .assistant, text: "", createdAt: Date(), turnId: nil,
            status: .sending, clientMsgId: UUID().uuidString // points at nothing
        )
        try writeTranscript([placeholder], to: persistURL)

        let fake = FakeChatAPI()
        let store = ChatStore(deviceToken: "tok", client: fake, persistURL: persistURL, policy: .test())
        defer { store.cancelActiveWork() }
        await awaitSettled(store)

        let messages = try requireCount(store.messages, 1)
        #expect(messages[0].status == .error)
        #expect(messages[0].text.contains("Interrupted before sending"))
        #expect(await fake.sentMessages.isEmpty)
    }

    // MARK: - Image attachments (docs/tasks/ios-image-input.md)

    @Test func sendWithImageWritesTheAttachmentAndPostsItBase64Encoded() async throws {
        let fake = FakeChatAPI()
        await fake.stubSend(.success(MessageResponse(turnId: "turn-img")))
        await fake.stubTurns([.success(.fixture(turnId: "turn-img", status: "done", resultText: "a cat"))])

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(),
            attachmentsDirectoryURL: makeTempAttachmentsDirectoryURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        let imageData = Data([0xFF, 0xD8, 0xFF, 0xD9]) // not a real JPEG — send() never inspects the bytes
        store.send(text: "what's in this?", images: [imageData])
        await awaitSettled(store)

        let messages = try requireCount(store.messages, 2)
        #expect(messages[0].role == .user)
        let filename = try #require(messages[0].imageFilenames?.first, "the user message records where the bytes landed")
        #expect(filename == "\(messages[0].id.uuidString)-0.jpg", "keyed by client_msg_id plus the image's index")
        #expect(store.loadAttachment(filename: filename) == imageData, "the exact bytes handed to send() are what's on disk")

        let sent = try requireCount(await fake.sentMessages, 1)
        #expect(sent[0].imagesBase64 == [imageData.base64EncodedString()])
    }

    @Test func sendWithSeveralImagesSavesEachAndSendsThemInOrder() async throws {
        let fake = FakeChatAPI()
        await fake.stubSend(.success(MessageResponse(turnId: "turn-multi")))
        await fake.stubTurns([.success(.fixture(turnId: "turn-multi", status: "done", resultText: "three cats"))])
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(),
            attachmentsDirectoryURL: makeTempAttachmentsDirectoryURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        let images = [Data([1]), Data([2, 2]), Data([3, 3, 3])]
        store.send(text: "compare", images: images)
        await awaitSettled(store)

        let user = try #require(store.messages.first)
        let filenames = try #require(user.imageFilenames)
        #expect(filenames == (0..<3).map { "\(user.id.uuidString)-\($0).jpg" })
        #expect(filenames.map { store.loadAttachment(filename: $0) } == images)
        let sent = try requireCount(await fake.sentMessages, 1)
        #expect(sent[0].imagesBase64 == images.map { $0.base64EncodedString() })
    }

    @Test func retryResendsEverySavedImage() async throws {
        let fake = FakeChatAPI()
        await fake.stubSend(.failure(URLError(.notConnectedToInternet)))
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(),
            attachmentsDirectoryURL: makeTempAttachmentsDirectoryURL(), policy: .test(sendMaxAttempts: 1)
        )
        defer { store.cancelActiveWork() }

        let images = [Data([1, 1]), Data([2, 2]), Data([3, 3])]
        store.send(text: "offline", images: images)
        await awaitSettled(store)
        let failed = try requireCount(store.messages, 2)
        await fake.stubSend(.success(MessageResponse(turnId: "turn-r")))
        await fake.stubTurns([.success(.fixture(turnId: "turn-r", status: "done", resultText: "ok"))])

        store.retry(failed[1].id)
        await awaitSettled(store)

        let sent = try requireCount(await fake.sentMessages, 2)
        #expect(sent[1].imagesBase64 == images.map { $0.base64EncodedString() })
        #expect(sent[1].clientMsgId == sent[0].clientMsgId)
    }

    /// CMP-4/CMP-6: a caption with only some of its images is the failure the
    /// whole design refuses, so one missing file sends the text alone, and says so.
    @Test func retryWithOneImageFileMissingSendsTextOnlyAndSaysSo() async throws {
        let fake = FakeChatAPI()
        await fake.stubSend(.failure(URLError(.notConnectedToInternet)))
        let attachmentsDir = makeTempAttachmentsDirectoryURL()
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(),
            attachmentsDirectoryURL: attachmentsDir, policy: .test(sendMaxAttempts: 1)
        )
        defer { store.cancelActiveWork() }

        store.send(text: "offline", images: [Data([1]), Data([2]), Data([3])])
        await awaitSettled(store)
        let failed = try requireCount(store.messages, 2)
        let middle = try #require(store.messages[0].imageFilenames?[1])
        try FileManager.default.removeItem(at: attachmentsDir.appendingPathComponent(middle))
        await fake.stubSend(.success(MessageResponse(turnId: "turn-m")))
        await fake.stubTurns([.success(.fixture(turnId: "turn-m", status: "done", resultText: "ok"))])

        store.retry(failed[1].id)
        await awaitSettled(store)

        let sent = try requireCount(await fake.sentMessages, 2)
        #expect(sent[1].imagesBase64.isEmpty, "never the caption with two of its three images")
        #expect(store.permissionNotice != nil)
    }

    @Test func aFailedWriteOfAnyImageSendsTextOnly() async throws {
        let fake = FakeChatAPI()
        await fake.stubSend(.success(MessageResponse(turnId: "turn-1")))
        await fake.stubTurns([.success(.fixture(status: "done", resultText: "ok"))])
        let blockedPath = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("MarlowTests-blocked-\(UUID().uuidString)")
        try Data().write(to: blockedPath)
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(),
            attachmentsDirectoryURL: blockedPath, policy: .test()
        )
        defer { store.cancelActiveWork() }

        #expect(store.send(text: "two", images: [Data([1]), Data([2])]))
        await awaitSettled(store)

        #expect(store.messages[0].allImageFilenames.isEmpty)
        let sent = try requireCount(await fake.sentMessages, 1)
        #expect(sent[0].imagesBase64.isEmpty)
        #expect(store.permissionNotice != nil)
    }

    @Test func sendWithoutImageCarriesNoImageFields() async throws {
        let fake = FakeChatAPI()
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(),
            attachmentsDirectoryURL: makeTempAttachmentsDirectoryURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        store.send(text: "just text")
        await awaitSettled(store)

        #expect(store.messages[0].allImageFilenames.isEmpty)
        let sent = try requireCount(await fake.sentMessages, 1)
        #expect(sent[0].imagesBase64.isEmpty)
    }

    /// `/code-review medium` finding: the photo/paste buttons were otherwise
    /// unreachable, since nothing could satisfy both "picked an image" and
    /// "typed a caption."
    @Test func sendWithAnImageAndNoCaptionStillSends() async throws {
        let fake = FakeChatAPI()
        await fake.stubSend(.success(MessageResponse(turnId: "turn-img-only")))
        await fake.stubTurns([.success(.fixture(turnId: "turn-img-only", status: "done", resultText: "a cat"))])

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(),
            attachmentsDirectoryURL: makeTempAttachmentsDirectoryURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        let sent = store.send(text: "   ", images: [Data([0xFF, 0xD8, 0xFF, 0xD9])])
        #expect(sent, "an image with a blank/empty caption must still be sendable")
        await awaitSettled(store)

        let messages = try requireCount(store.messages, 2)
        #expect(messages[0].text == "", "the blank caption is trimmed to empty, not sent as whitespace")
        #expect(!messages[0].allImageFilenames.isEmpty)
    }

    @Test func sendWithNoTextAndNoImageIsStillRejected() async {
        let fake = FakeChatAPI()
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        let sent = store.send(text: "   ")
        #expect(!sent)
        #expect(store.messages.isEmpty)
    }

    /// `/code-review medium` finding: `saveAttachment`'s write failure used to
    /// be swallowed with `try?` while the filename was recorded anyway, so a
    /// later retry silently resent text-only with no image and no
    /// explanation. The fix keeps what's sent and what's saved-for-retry in
    /// agreement: a failed write means send text-only immediately too.
    @Test func aFailedAttachmentWriteSendsTextOnlyWithANotice() async throws {
        let fake = FakeChatAPI()
        await fake.stubSend(.success(MessageResponse(turnId: "turn-1")))
        await fake.stubTurns([.success(.fixture(status: "done", resultText: "ok"))])

        // A regular file sitting where the attachments directory should be —
        // FileManager.createDirectory(at:) throws against an existing
        // non-directory path, so every write through it fails deterministically.
        let blockedPath = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("MarlowTests-blocked-\(UUID().uuidString)")
        try Data().write(to: blockedPath)

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(),
            attachmentsDirectoryURL: blockedPath, policy: .test()
        )
        defer { store.cancelActiveWork() }

        let sent = store.send(text: "what's this?", images: [Data([1, 2, 3])])
        #expect(sent, "the message itself still goes out, as text")
        await awaitSettled(store)

        let messages = try requireCount(store.messages, 2)
        #expect(messages[0].allImageFilenames.isEmpty, "a failed write must not be recorded as a successful attachment")
        let sentMessages = try requireCount(await fake.sentMessages, 1)
        #expect(sentMessages[0].imagesBase64.isEmpty, "text-only, matching what was actually saved for retry")
        #expect(store.permissionNotice != nil, "the user is told the photo wasn't attached")
    }

    /// `/code-review medium` finding: `MessageRow` re-reads and re-decodes an
    /// attached image from disk on every SwiftUI re-render (every poll tick
    /// while a turn is in flight). `loadedImage` is the fix — same identity
    /// back for the same filename means the second call didn't touch disk.
    @Test func loadedImageCachesTheDecodedResult() async throws {
        let fake = FakeChatAPI()
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(),
            attachmentsDirectoryURL: makeTempAttachmentsDirectoryURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        let tinyImage = UIGraphicsImageRenderer(size: CGSize(width: 2, height: 2)).image { ctx in
            UIColor.green.setFill()
            ctx.fill(CGRect(x: 0, y: 0, width: 2, height: 2))
        }
        let realJPEG = try #require(tinyImage.jpegData(compressionQuality: 0.8))
        store.send(text: "look", images: [realJPEG])
        await awaitSettled(store)
        let filename = try #require(store.messages[0].imageFilenames?.first)

        let first = try #require(store.loadedImage(filename: filename))
        let second = try #require(store.loadedImage(filename: filename))
        #expect(first === second, "the second call should be served from cache, not a fresh disk read + decode")
    }

    @Test func retryResendsTheSameImageBytes() async throws {
        let fake = FakeChatAPI()
        await fake.stubSend(.failure(URLError(.notConnectedToInternet)))

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(),
            attachmentsDirectoryURL: makeTempAttachmentsDirectoryURL(),
            policy: .test(sendMaxAttempts: 1)
        )
        defer { store.cancelActiveWork() }

        let imageData = Data([1, 2, 3, 4])
        store.send(text: "still offline", images: [imageData])
        await awaitSettled(store)
        let failed = try requireCount(store.messages, 2)
        #expect(failed[1].status == .error)

        await fake.stubSend(.success(MessageResponse(turnId: "turn-99")))
        await fake.stubTurns([.success(.fixture(turnId: "turn-99", status: "done", resultText: "back online"))])

        store.retry(failed[1].id)
        await awaitSettled(store)

        let sent = try requireCount(await fake.sentMessages, 2)
        #expect(
            sent[1].imagesBase64 == [imageData.base64EncodedString()],
            "retry() reads the same bytes back off disk by the paired user message's imageFilenames"
        )
    }

    @Test func aSendingMessageWithAnImageOnDiskIsResentWithTheImage() async throws {
        let persistURL = makeTempTranscriptURL()
        let attachmentsDir = makeTempAttachmentsDirectoryURL()
        let user = ChatMessage(
            id: UUID(), role: .user, text: "did it land?", createdAt: Date(), turnId: nil,
            status: .done, clientMsgId: nil
        )
        let imageData = Data([9, 9, 9])
        let filename = "\(user.id.uuidString).jpg"
        try imageData.write(to: attachmentsDir.appendingPathComponent(filename))
        var userWithImage = user
        userWithImage.imageFilename = filename
        let placeholder = ChatMessage(
            id: UUID(), role: .assistant, text: "", createdAt: Date(), turnId: nil,
            status: .sending, clientMsgId: user.id.uuidString
        )
        try writeTranscript([userWithImage, placeholder], to: persistURL)

        let fake = FakeChatAPI()
        await fake.stubSend(.success(MessageResponse(turnId: "turn-dedup-img")))
        await fake.stubTurns([.success(.fixture(status: "done", resultText: "it landed"))])

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: persistURL,
            attachmentsDirectoryURL: attachmentsDir, policy: .test()
        )
        defer { store.cancelActiveWork() }
        await awaitSettled(store)

        let sent = try requireCount(await fake.sentMessages, 1)
        #expect(sent[0].imagesBase64 == [imageData.base64EncodedString()])
    }

    @Test func aSendingMessageWithSeveralImagesOnDiskIsResentWithAllOfThem() async throws {
        let persistURL = makeTempTranscriptURL()
        let attachmentsDir = makeTempAttachmentsDirectoryURL()
        var user = ChatMessage(
            id: UUID(), role: .user, text: "did they land?", createdAt: Date(), turnId: nil,
            status: .done, clientMsgId: nil
        )
        let blobs = [Data([4]), Data([5, 5])]
        let names = (0..<2).map { "\(user.id.uuidString)-\($0).jpg" }
        for (name, blob) in zip(names, blobs) { try blob.write(to: attachmentsDir.appendingPathComponent(name)) }
        user.imageFilenames = names
        let placeholder = ChatMessage(
            id: UUID(), role: .assistant, text: "", createdAt: Date(), turnId: nil,
            status: .sending, clientMsgId: user.id.uuidString
        )
        try writeTranscript([user, placeholder], to: persistURL)
        let fake = FakeChatAPI()
        await fake.stubSend(.success(MessageResponse(turnId: "turn-dedup-multi")))
        await fake.stubTurns([.success(.fixture(status: "done", resultText: "yes"))])

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: persistURL,
            attachmentsDirectoryURL: attachmentsDir, policy: .test()
        )
        defer { store.cancelActiveWork() }
        await awaitSettled(store)

        let sent = try requireCount(await fake.sentMessages, 1)
        #expect(sent[0].imagesBase64 == blobs.map { $0.base64EncodedString() })
    }

    @Test func terminalMessagesOnDiskAreLeftAlone() async throws {
        let persistURL = makeTempTranscriptURL()
        let done = ChatMessage(
            id: UUID(), role: .assistant, text: "old answer", createdAt: Date(), turnId: "turn-old",
            status: .done, clientMsgId: nil
        )
        let errored = ChatMessage(
            id: UUID(), role: .assistant, text: "old failure", createdAt: Date(), turnId: nil,
            status: .error, clientMsgId: nil
        )
        try writeTranscript([done, errored], to: persistURL)

        let fake = FakeChatAPI()
        let store = ChatStore(deviceToken: "tok", client: fake, persistURL: persistURL, policy: .test())
        defer { store.cancelActiveWork() }
        await awaitSettled(store)

        #expect(store.messages == [done, errored])
        #expect(await fake.getTurnCallCount == 0)
        #expect(await fake.sentMessages.isEmpty)
    }

    // MARK: - Persistence

    @Test func theTranscriptSurvivesARestart() async throws {
        let persistURL = makeTempTranscriptURL()
        let fake = FakeChatAPI()
        await fake.stubTurns([.success(.fixture(status: "done", resultText: "persisted"))])

        let first = ChatStore(deviceToken: "tok", client: fake, persistURL: persistURL, policy: .test())
        first.send(text: "remember this")
        await awaitSettled(first)
        let expected = first.messages
        first.cancelActiveWork()

        let second = ChatStore(deviceToken: "tok", client: FakeChatAPI(), persistURL: persistURL, policy: .test())
        defer { second.cancelActiveWork() }
        await awaitSettled(second)

        #expect(second.messages == expected)
        #expect(second.messages.count == 2)
    }

    @Test func aCorruptTranscriptLoadsAsEmptyRatherThanCrashing() async throws {
        let persistURL = makeTempTranscriptURL()
        try Data("not json at all".utf8).write(to: persistURL)

        let store = ChatStore(
            deviceToken: "tok", client: FakeChatAPI(), persistURL: persistURL, policy: .test()
        )
        defer { store.cancelActiveWork() }

        #expect(store.messages.isEmpty)
    }

    /// A downgrade to a build that predates a status shouldn't cost the user
    /// their history: the whole array decodes at once, so an unknown status
    /// must degrade that one row rather than throw the transcript away.
    @Test func aStatusFromANewerBuildDoesNotWipeTheTranscript() async throws {
        let persistURL = makeTempTranscriptURL()
        let json = """
        {"latestSeq":null,"messages":[\
        {"id":"\(UUID().uuidString)","role":"user","text":"kept","createdAt":0,\
        "status":"done"},
         {"id":"\(UUID().uuidString)","role":"assistant","text":"from the future",\
        "createdAt":0,"status":"teleporting"}]}
        """
        try Data(json.utf8).write(to: persistURL)

        let store = ChatStore(
            deviceToken: "tok", client: FakeChatAPI(), persistURL: persistURL, policy: .test()
        )
        defer { store.cancelActiveWork() }

        let messages = try requireCount(store.messages, 2)
        #expect(messages[0].text == "kept", "the readable rows survive")
        #expect(messages[1].status == .error, "the unknown status degrades to a terminal error")
    }

    // MARK: - Tool-permission round trip

    @Test func aPendingRequestAppearingMidPollPublishesIt() async throws {
        let fake = FakeChatAPI()
        await fake.stubSend(.success(MessageResponse(turnId: "turn-1")))
        await fake.stubTurns([.success(.fixture(status: "running")), .success(.fixture(status: "done"))])
        await fake.stubPermissions([
            .success(PermissionsResponse(permissionRequests: [])),
            .success(PermissionsResponse(permissionRequests: [.fixture(requestId: "req-1", tool: "Bash")])),
        ])

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        store.send(text: "hi")
        await awaitSettled(store)

        let pending = await MainActor.run { store.pendingPermission }
        #expect(pending?.requestId == "req-1")
        #expect(pending?.tool == "Bash")
    }

    @Test func answeringPostsTheDecisionAndClearsTheSheetImmediately() async throws {
        let fake = FakeChatAPI()
        await fake.stubSend(.success(MessageResponse(turnId: "turn-1")))
        await fake.stubTurns([], fallback: .success(.fixture(status: "running")))
        // Realistically the server stops returning a request once it's
        // resolved, but the fake's fallback keeps repeating it — so the poll
        // loop is pinned via the getTurn hook while we answer and assert,
        // otherwise a same-tick re-fetch could put it right back before the
        // assertion runs (fastInterval is 0 in .test()).
        await fake.stubPermissions([], fallback: .success(PermissionsResponse(permissionRequests: [.fixture(requestId: "req-9")])))
        await fake.stubAnswerPermission(.success(AnswerPermissionResponse(accepted: true)))

        let entered = Gate()
        let hold = Gate()
        await fake.setOnGetTurn { call in
            if call == 1 {
                await entered.open()
                await hold.wait()
            }
        }

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(),
            policy: .test(fastAttempts: 5000)
        )
        defer { store.cancelActiveWork() }

        store.send(text: "hi")
        await awaitOpened(entered, "should have reached the first getTurn")
        let appeared = await MainActor.run { store.pendingPermission != nil }
        #expect(appeared, "the sheet should have appeared")

        await store.answerPermission(decision: "allow")

        let calls = try requireCount(await fake.answerPermissionCalls, 1)
        #expect(calls[0].requestId == "req-9")
        #expect(calls[0].decision == "allow")
        let pending = await MainActor.run { store.pendingPermission }
        #expect(pending == nil, "clears immediately rather than waiting for the next poll")

        await hold.open()
    }

    /// "Allow similar for this reply" is the same round trip as allow with a
    /// different decision string; the store must pass it through untouched and
    /// clear the card the same way.
    @Test func allowSimilarPostsItsDecisionAndClearsTheCard() async throws {
        let fake = FakeChatAPI()
        await fake.stubSend(.success(MessageResponse(turnId: "turn-1")))
        await fake.stubTurns([], fallback: .success(.fixture(status: "running")))
        await fake.stubPermissions([], fallback: .success(PermissionsResponse(permissionRequests: [
            .fixture(requestId: "req-9", summaryJSON: #"{"title": "Run grep", "similar": {"label": "grep commands"}}"#)
        ])))
        await fake.stubAnswerPermission(.success(AnswerPermissionResponse(accepted: true)))

        let entered = Gate()
        let hold = Gate()
        await fake.setOnGetTurn { call in
            if call == 1 {
                await entered.open()
                await hold.wait()
            }
        }

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(),
            policy: .test(fastAttempts: 5000)
        )
        defer { store.cancelActiveWork() }

        store.send(text: "hi")
        await awaitOpened(entered, "should have reached the first getTurn")
        let appeared = await MainActor.run { store.pendingPermission?.summary?.similar?.label }
        #expect(appeared == "grep commands", "the summary should survive the poll")

        await store.answerPermission(decision: "allow_similar")

        let calls = try requireCount(await fake.answerPermissionCalls, 1)
        #expect(calls[0].requestId == "req-9")
        #expect(calls[0].decision == "allow_similar")
        let pending = await MainActor.run { store.pendingPermission }
        #expect(pending == nil, "clears like allow does")

        await hold.open()
    }

    @Test func anAlreadyResolvedAnswerShowsATooLateNoticeAndStillClears() async throws {
        let fake = FakeChatAPI()
        await fake.stubSend(.success(MessageResponse(turnId: "turn-1")))
        await fake.stubTurns([], fallback: .success(.fixture(status: "running")))
        await fake.stubPermissions([], fallback: .success(PermissionsResponse(permissionRequests: [.fixture(requestId: "req-9")])))
        await fake.stubAnswerPermission(.success(AnswerPermissionResponse(accepted: false)))

        let entered = Gate()
        let hold = Gate()
        await fake.setOnGetTurn { call in
            if call == 1 {
                await entered.open()
                await hold.wait()
            }
        }

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(),
            policy: .test(fastAttempts: 5000)
        )
        defer { store.cancelActiveWork() }

        store.send(text: "hi")
        await awaitOpened(entered, "should have reached the first getTurn")
        let appeared = await MainActor.run { store.pendingPermission != nil }
        #expect(appeared, "the sheet should have appeared")

        await store.answerPermission(decision: "deny")

        let pending = await MainActor.run { store.pendingPermission }
        #expect(pending == nil)
        let notice = await MainActor.run { store.permissionNotice }
        #expect(notice != nil, "accepted: false is 'too late', not an error")

        await hold.open()
    }

    @Test func aRequestThatDisappearsFromThePollClearsTheSheet() async throws {
        let fake = FakeChatAPI()
        await fake.stubSend(.success(MessageResponse(turnId: "turn-1")))
        await fake.stubTurns([], fallback: .success(.fixture(status: "running")))
        await fake.stubPermissions([
            .success(PermissionsResponse(permissionRequests: [.fixture(requestId: "req-1")])),
        ], fallback: .success(PermissionsResponse(permissionRequests: [])))

        // With zero-delay intervals the loop can consume the single scripted
        // appearance and its empty follow-up before a bare waitUntil ever
        // observes the interim state, so pin the loop right after the first
        // checkPermissions() call (which runs just before getTurn each tick)
        // to make the appearance observable before it's cleared again.
        let entered = Gate()
        let hold = Gate()
        await fake.setOnGetTurn { call in
            if call == 1 {
                await entered.open()
                await hold.wait()
            }
        }

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(),
            policy: .test(fastAttempts: 5000)
        )
        defer { store.cancelActiveWork() }

        store.send(text: "hi")
        await awaitOpened(entered, "should have reached the first getTurn")
        let pending = await MainActor.run { store.pendingPermission }
        #expect(pending?.requestId == "req-1", "the sheet should have appeared")

        await hold.open()

        let cleared = await waitUntil { await MainActor.run { store.pendingPermission == nil } }
        #expect(cleared, "the server resolved it (timeout or answered elsewhere); the sheet must follow")
    }

    @Test func aFailedPermissionsFetchDoesNotDisturbTurnPolling() async throws {
        let fake = FakeChatAPI()
        await fake.stubSend(.success(MessageResponse(turnId: "turn-42")))
        await fake.stubTurns([
            .success(.fixture(status: "pending")),
            .success(.fixture(status: "running")),
            .success(.fixture(status: "done", resultText: "still works")),
        ])
        await fake.stubPermissions([], fallback: .failure(APIError.server(status: 500, detail: "boom")))

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        store.send(text: "hi")
        await awaitSettled(store)

        let messages = try requireCount(store.messages, 2)
        #expect(messages[1].status == .done)
        #expect(messages[1].text == "still works")
        let pending = await MainActor.run { store.pendingPermission }
        #expect(pending == nil, "a failed permissions fetch must not surface anything")
    }

    @Test func anAlreadyExpiredRequestIsNeverPublished() async throws {
        // Simulates a slow locked-phone unlock: by the time the poll fetches
        // it, the request's own expires_at has already passed. Publishing it
        // anyway would show the sheet with no `false -> true` transition left
        // for its countdown to notice and self-clear on.
        let fake = FakeChatAPI()
        await fake.stubSend(.success(MessageResponse(turnId: "turn-1")))
        await fake.stubTurns([.success(.fixture(status: "running")), .success(.fixture(status: "done"))])
        let past = Date().timeIntervalSince1970 - 120
        await fake.stubPermissions([
            .success(PermissionsResponse(permissionRequests: [
                .fixture(requestId: "req-stale", createdAt: past, expiresAt: past + 55),
            ])),
        ])

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        store.send(text: "hi")
        await awaitSettled(store)

        let pending = await MainActor.run { store.pendingPermission }
        #expect(pending == nil, "an already-expired request must never reach the sheet")
    }

    @Test func aStaleInFlightFetchDoesNotResurrectAnAnsweredRequest() async throws {
        // The interleaving the generation guard exists for: a checkPermissions()
        // GET is mid-flight when the user answers the currently-shown request.
        // The GET's stale snapshot (still containing that request) must not
        // overwrite the `nil` the answer already set.
        let fake = FakeChatAPI()
        await fake.stubSend(.success(MessageResponse(turnId: "turn-1")))
        await fake.stubTurns([], fallback: .success(.fixture(status: "running")))
        // Exactly 3 fast-phase attempts (below), so exactly 3 checkPermissions
        // calls happen and none more — call 3's empty response is what a real
        // server would return once the request is gone, isolating call 2's
        // stale resurrection attempt as the only thing that could flip
        // `pendingPermission` back after the answer clears it.
        await fake.stubPermissions([
            .success(PermissionsResponse(permissionRequests: [.fixture(requestId: "req-1")])),
            .success(PermissionsResponse(permissionRequests: [.fixture(requestId: "req-1")])),
        ], fallback: .success(PermissionsResponse(permissionRequests: [])))
        await fake.stubAnswerPermission(.success(AnswerPermissionResponse(accepted: true)))

        let enteredSecondFetch = Gate()
        let holdSecondFetch = Gate()
        await fake.setOnPermissions { call in
            if call == 2 {
                await enteredSecondFetch.open()
                await holdSecondFetch.wait()
            }
        }

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(),
            policy: .test(fastAttempts: 3)
        )
        defer { store.cancelActiveWork() }

        store.send(text: "hi")
        let appeared = await waitUntil { await MainActor.run { store.pendingPermission != nil } }
        #expect(appeared, "the sheet should have appeared")

        // Wait until a second checkPermissions() is in flight (holding its
        // stale-req-1 response) before answering — this is the exact window
        // the race can happen in.
        await awaitOpened(enteredSecondFetch, "a second permissions fetch should be in flight")
        await store.answerPermission(decision: "allow")
        let clearedRightAfterAnswering = await MainActor.run { store.pendingPermission }
        #expect(clearedRightAfterAnswering == nil, "answering clears the sheet immediately")

        // Release the stale fetch; it must not resurrect the request.
        await holdSecondFetch.open()
        let stayedClear = await waitUntil(timeout: .seconds(2)) { await MainActor.run { store.pendingPermission != nil } }
        #expect(!stayedClear, "a stale in-flight fetch must not resurrect an already-answered request")
    }

    @Test func slowPhasePollingSkipsPermissionChecks() async throws {
        // A permission request tied to a stalled turn is already dead (the
        // server's t_hook_seconds window closed minutes before the slow phase
        // even starts) — checkPermissions() must not keep polling for it.
        // Zero-delay test intervals mean the whole fast->slow sequence runs to
        // completion near-instantly, so this asserts on the final call counts
        // rather than trying to catch the interim `.stalled` state.
        let fake = FakeChatAPI()
        await fake.stubSend(.success(MessageResponse(turnId: "turn-1")))
        await fake.stubTurns([], fallback: .success(.fixture(status: "running")))

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(),
            policy: .test(fastAttempts: 1, slowAttempts: 5)
        )
        defer { store.cancelActiveWork() }

        store.send(text: "hi")
        await awaitSettled(store)

        #expect(await fake.getTurnCallCount == 6, "1 fast-phase tick + 5 slow-phase ticks")
        let permissionCalls = await fake.permissionsCallCount
        #expect(permissionCalls == 1, "only the single fast-phase tick should have checked permissions")
    }

    // MARK: - Health banners

    @Test func reauthNeededRaisesTheBanner() async throws {
        let fake = FakeChatAPI()
        await fake.stubHealth(.success(.fixture(status: "reauth_needed")))

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        #expect(await waitUntil { await MainActor.run { store.reauthNeeded } })
        #expect(store.queuePaused == false)
    }

    @Test func missingCredentialsRaisesTheReauthBanner() async throws {
        let fake = FakeChatAPI()
        // What the server actually sends when the credentials file is gone or
        // unparseable: status "error", not "reauth_needed", and no prewarn
        // (there is no expiry timestamp left to compare against). Keying the
        // banner on the status string left this case showing nothing at all,
        // while the server's auth watcher pushed "session has expired".
        await fake.stubHealth(.success(.fixture(status: "error", authOk: false)))

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        #expect(await waitUntil { await MainActor.run { store.reauthNeeded } })
    }

    @Test func reauthPrewarnRaisesItsOwnBanner() async throws {
        let fake = FakeChatAPI()
        await fake.stubHealth(.success(.fixture(status: "ok", reauthPrewarn: true)))

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        #expect(await waitUntil { await MainActor.run { store.reauthPrewarn } })
        // Still working — the ask is "reauth today", not "everything stopped".
        #expect(store.reauthNeeded == false)
    }

    @Test func reauthNeededSuppressesThePrewarnBanner() async throws {
        let fake = FakeChatAPI()
        await fake.stubHealth(.success(.fixture(status: "reauth_needed", reauthPrewarn: true)))

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        // The server reports prewarn as true past the deadline too. Stacking
        // both banners says the same thing twice, once too gently.
        #expect(await waitUntil { await MainActor.run { store.reauthNeeded } })
        #expect(store.reauthPrewarn == false)
    }

    @Test func queuePausedRaisesTheBanner() async throws {
        let fake = FakeChatAPI()
        await fake.stubHealth(.success(.fixture(queuePaused: true)))

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        #expect(await waitUntil { await MainActor.run { store.queuePaused } })
        #expect(store.reauthNeeded == false)
    }

    @Test func anUnreachableHealthEndpointLeavesBannersDown() async throws {
        let fake = FakeChatAPI()
        await fake.stubHealth(.failure(URLError(.cannotConnectToHost)))

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        try await Task.sleep(for: .milliseconds(50))
        #expect(store.reauthNeeded == false, "a failed poll must not be read as a state change")
        #expect(store.queuePaused == false)
    }


    // MARK: - Transcript sync (docs/tasks/ios-server-transcript.md)

    /// A row stamped a few minutes ago, so the launch window and the 30-day
    /// cutoff both treat it as recent.
    private func recent(_ minutesAgo: Double = 5) -> Double {
        Date().timeIntervalSince1970 - minutesAgo * 60
    }

    private func cronRow(
        _ seq: Int, turn: String, text: String = "Morning.", createdAt: Double? = nil
    ) -> TranscriptRow {
        TranscriptRow(
            seq: seq, id: UUID().uuidString, role: .assistant, source: "cron", turnId: turn,
            text: text, status: .done, createdAt: createdAt ?? recent(), answers: []
        )
    }

    private func page(_ rows: [TranscriptRow], latest: Int? = nil, hasMore: Bool = false) -> Result<TranscriptPage, Error> {
        .success(TranscriptPage(rows: rows, latestSeq: latest ?? (rows.map(\.seq).max() ?? 0), hasMore: hasMore))
    }

    /// The regression test for the bug that actually shipped with
    /// `/v1/pending`. The merge rules are tested on their own; this drives the
    /// wire. Every other test of the rules would have passed against a build
    /// where nothing on the health tick ever asked for the transcript, and the
    /// briefing was never even requested.
    @Test func theHealthTickSyncsTheTranscriptWithoutBeingAsked() async throws {
        let fake = FakeChatAPI()
        await fake.stubTranscript([page([cronRow(1, turn: "cron-1", text: "Morning.")])])

        // Nothing is sent, nothing is polled — the store is only opened.
        let store = ChatStore(
            deviceToken: "tok", client: fake,
            persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        #expect(await waitUntil { await MainActor.run { store.messages.count == 1 } },
                "the health tick must fetch the transcript on its own")
        #expect(store.messages.first?.turnId == "cron-1")
        #expect(store.messages.first?.seq == 1)
    }

    @Test func theFirstSyncFetchesTheNewestPageAndShowsItOldestFirst() async throws {
        let url = makeTempTranscriptURL()
        let fake = FakeChatAPI()
        let user = TranscriptRow(seq: 8, id: UUID().uuidString, role: .user, turnId: "t8", text: "hi", createdAt: recent(9))
        let reply = TranscriptRow(
            seq: 9, id: UUID().uuidString, role: .assistant, turnId: "t8", text: "hello", status: .done,
            createdAt: recent(8), answers: ["t8"]
        )
        // Newest first, as the server sends a page with neither cursor.
        await fake.stubTranscript([page([reply, user], latest: 9, hasMore: true)])

        let store = ChatStore(deviceToken: "tok", client: fake, persistURL: url, policy: .test())
        defer { store.cancelActiveWork() }
        #expect(await waitUntil { await MainActor.run { store.messages.count == 2 } })

        let call = try requireCount(await fake.transcriptCalls.map { [$0.after, $0.before, $0.limit] }, 1)
        #expect(call[0] == [nil, nil, 200], "no cursor at all, and a page of 200")
        #expect(store.messages.map(\.role) == [.user, .assistant])
        #expect(store.messages.map(\.seq) == [8, 9])
        #expect(store.completedAssistantTurns.isEmpty,
                "history is not news: a fresh install must not sound once per row")
        #expect(store.hasEarlierMessages, "the page said there is more behind it")

        let cache = try #require(readCache(at: url))
        #expect(cache.latestSeq == 9)
        #expect(cache.hasOlderOnServer == true)
    }

    @Test func laterSyncsAskPastTheCursorAndFollowHasMore() async throws {
        let url = makeTempTranscriptURL()
        let held = ChatMessage(
            id: UUID(), role: .assistant, text: "cached", createdAt: Date(), turnId: "t5",
            status: .done, clientMsgId: nil, seq: 5
        )
        try writeTranscript([held], latestSeq: 5, to: url)
        let fake = FakeChatAPI()
        // Page one stops at its limit: `latest_seq` is 8, but the cursor must
        // move to the last row it carried (7), or row 8 is skipped for good.
        await fake.stubTranscript([
            page([cronRow(6, turn: "c6"), cronRow(7, turn: "c7")], latest: 8, hasMore: true),
            page([cronRow(8, turn: "c8")], latest: 8),
        ])

        let store = ChatStore(deviceToken: "tok", client: fake, persistURL: url, policy: .test())
        defer { store.cancelActiveWork() }
        #expect(await waitUntil { await MainActor.run { store.messages.count == 4 } })

        let calls = await fake.transcriptCalls.map { [$0.after, $0.before, $0.limit] }
        #expect(calls == [[5, nil, 200], [7, nil, 200]])
        #expect(store.messages.compactMap(\.seq) == [5, 6, 7, 8])
        #expect(store.completedAssistantTurns == ["c6", "c7", "c8"], "arrivals are announced, oldest first")
        #expect(readCache(at: url)?.latestSeq == 8)
    }

    @Test func aPageThatMakesNoProgressDoesNotSpinTheSync() async throws {
        let url = makeTempTranscriptURL()
        try writeTranscript([], latestSeq: 5, to: url)
        let fake = FakeChatAPI()
        // `has_more` with nothing in the page: a misbehaving server.
        await fake.stubTranscript([page([], latest: 9, hasMore: true)])

        let store = ChatStore(deviceToken: "tok", client: fake, persistURL: url, policy: .test())
        defer { store.cancelActiveWork() }
        #expect(await waitUntil { await fake.transcriptCallCount >= 1 })
        try await Task.sleep(for: .milliseconds(50))

        #expect(await fake.transcriptCallCount == 1)
    }

    @Test func theSyncRunsBeforeHealthOnTheTick() async throws {
        // After a push wake the transcript is what the user is waiting for.
        let fake = FakeChatAPI()
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        #expect(await waitUntil { await fake.callOrder.count >= 2 })
        #expect(Array(await fake.callOrder.prefix(2)) == ["transcript", "health"])
    }

    /// docs/tasks/ios-notification-open-and-content-swap.md half A: a warm
    /// resume lands mid-`healthInterval` sleep, so without an explicit
    /// foreground trigger a cron turn that finished while backgrounded only
    /// shows up on the *next* tick. `.test()`'s `healthInterval` is long
    /// enough that this test would time out waiting on the tick, so a pass
    /// here can only be `handleDidBecomeActive()` restarting the loop itself.
    @Test func becomingActiveSyncsWithoutWaitingForTheHealthTick() async throws {
        let fake = FakeChatAPI()
        // The first response (the store's own initial tick at `init`) is
        // empty; the briefing only appears on the second fetch, which only
        // `handleDidBecomeActive()` can trigger before the next tick.
        await fake.stubTranscript([page([]), page([cronRow(1, turn: "cron-2", text: "Digest.")])])

        let store = ChatStore(
            deviceToken: "tok", client: fake,
            persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        #expect(await waitUntil { await fake.transcriptCallCount >= 1 },
                "the initial tick should have made its one (empty) fetch")
        #expect(store.messages.isEmpty)

        store.handleDidBecomeActive()

        #expect(await waitUntil { await MainActor.run { store.messages.count == 1 } },
                "becoming active must sync immediately, not wait for the next tick")
        #expect(store.messages.first?.turnId == "cron-2")
    }

    /// Regression test for a bug caught in `/code-review medium` on the
    /// foreground trigger: an earlier version tracked the foreground fetch under
    /// a fixed key in `activeTasks`, and overwriting a dictionary entry does not
    /// cancel the `Task` it replaces — a rapid foreground/background flap
    /// orphaned the first fetch's `Task`, and its own cleanup then deleted
    /// the *second* call's `activeTasks` entry instead of its own.
    /// `handleDidBecomeActive()` now cancels-and-restarts the single
    /// `healthTask` instead, so two rapid calls converge cleanly on the
    /// second's result rather than corrupting any bookkeeping.
    @Test func rapidDidBecomeActiveCallsConvergeCleanly() async throws {
        let fake = FakeChatAPI()
        await fake.stubTranscript([page([]), page([cronRow(1, turn: "cron-3", text: "Digest.")])])

        let store = ChatStore(
            deviceToken: "tok", client: fake,
            persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        #expect(await waitUntil { await fake.transcriptCallCount >= 1 },
                "the initial tick should have made its one (empty) fetch")

        store.handleDidBecomeActive()
        store.handleDidBecomeActive()

        #expect(await waitUntil { await MainActor.run { store.messages.count == 1 } },
                "two rapid calls should still land the eventual row, not corrupt or drop it")
        #expect(store.messages.first?.turnId == "cron-3")

        // Torn down cleanly: cancelActiveWork() must be able to stop
        // whichever task is current without hanging or crashing.
        store.cancelActiveWork()
    }

    /// The tap on a push cancels the health loop and starts its successor in
    /// the same turn, which can be while the old loop's sync is still in the
    /// air. A successor that skipped because "one is already running" would
    /// drop the sync the tap exists to trigger — and then wait a full
    /// `healthInterval` for the next.
    @Test func aSyncStartedWhileAnotherIsUnwindingStillRuns() async throws {
        let fake = FakeChatAPI()
        let reached = Gate()
        let release = Gate()
        await fake.setOnTranscript { call in
            guard call == 1 else { return }
            await reached.open()
            await release.wait()
        }
        await fake.stubTranscript([page([]), page([cronRow(1, turn: "cron-4", text: "Digest.")])])

        let store = ChatStore(
            deviceToken: "tok", client: fake,
            persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }
        await awaitOpened(reached, "the launch sync should be in the air")

        store.handleDidBecomeActive()
        await release.open()

        #expect(await waitUntil { await MainActor.run { store.messages.count == 1 } },
                "the successor's sync must wait for its predecessor, not be skipped by it")
        #expect(await fake.transcriptCallCount == 2)
    }

    /// A transcript read consumes nothing, so unlike the `/v1/pending` drain it
    /// replaced there is nothing to rescue when the store is cancelled mid-fetch:
    /// the response is dropped, the cursor stays put, and the next sync asks
    /// again.
    @Test func aSyncCancelledMidFlightAppliesNothingAndLosesNothing() async throws {
        let url = makeTempTranscriptURL()
        let fake = FakeChatAPI()
        await fake.stubTranscript([page([cronRow(1, turn: "cron-1")])])
        let box = StoreBox()
        await fake.setOnTranscript { _ in
            await MainActor.run { box.store?.cancelActiveWork() }
        }
        let store = ChatStore(deviceToken: "tok", client: fake, persistURL: url, policy: .test())
        box.store = store
        #expect(await waitUntil { await fake.transcriptCallCount >= 1 })
        try await Task.sleep(for: .milliseconds(50))
        #expect(store.messages.isEmpty, "a cancelled store must not publish into a screen it left")

        // The next launch asks past the same (absent) cursor and gets the row.
        await fake.setOnTranscript { _ in }
        await fake.stubTranscript([page([cronRow(1, turn: "cron-1")])])
        let next = ChatStore(deviceToken: "tok", client: fake, persistURL: url, policy: .test())
        defer { next.cancelActiveWork() }
        #expect(await waitUntil { await MainActor.run { next.messages.count == 1 } })
    }

    @Test func aSyncedRowIsPersistedImmediately() async throws {
        let url = makeTempTranscriptURL()
        let fake = FakeChatAPI()
        await fake.stubTranscript([page([cronRow(1, turn: "cron-1")])])
        let store = ChatStore(deviceToken: "tok", client: fake, persistURL: url, policy: .test())
        defer { store.cancelActiveWork() }
        #expect(await waitUntil { await MainActor.run { store.messages.count == 1 } })

        let cache = try #require(readCache(at: url))
        #expect(cache.messages.count == 1, "not written until some later save")
        #expect(cache.messages[0].turnId == "cron-1")
        #expect(cache.messages[0].seq == 1)
        #expect(cache.latestSeq == 1)
    }

    @Test func anOwnSendIsConfirmedByItsRowWithoutDuplicating() async throws {
        let fake = FakeChatAPI()
        await fake.stubSend(.success(MessageResponse(turnId: "turn-7")))
        await fake.stubTurns([.success(.fixture(turnId: "turn-7", status: "done", resultText: "Done."))])
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }
        store.send(text: "hello")
        await awaitSettled(store)
        #expect(await waitUntil { await fake.callOrder.contains("health") }, "the launch tick is over")
        let before = store.messages
        _ = store.takeCompletedAssistantTurns()

        let user = try #require(before.first)
        await fake.stubTranscript([page([
            TranscriptRow(
                seq: 1, id: UUID().uuidString, role: .user, turnId: "turn-7",
                clientMsgId: user.id.uuidString, text: "hello", createdAt: recent()
            ),
            TranscriptRow(
                seq: 2, id: UUID().uuidString, role: .assistant, turnId: "turn-7", text: "Done.",
                status: .done, createdAt: recent(), answers: ["turn-7"]
            ),
        ])])
        store.handleDidBecomeActive()

        #expect(await waitUntil { await MainActor.run { store.messages.last?.seq == 2 } })
        #expect(store.messages.map(\.id) == before.map(\.id), "both bubbles keep their local ids")
        #expect(store.messages.map(\.seq) == [1, 2])
        #expect(store.completedAssistantTurns.isEmpty,
                "the poll already sounded this reply; its row must not sound it again")
    }

    /// A reply sent from another device: its user row has no answer yet, so
    /// the phone makes a placeholder, polls the turn, and streams it.
    @Test func aTurnStartedElsewhereStreamsThroughAPlaceholderAndIsNotSoundedTwice() async throws {
        let fake = FakeChatAPI()
        let asked = TranscriptRow(
            seq: 1, id: UUID().uuidString, role: .user, turnId: "t-desk", text: "from the desktop",
            createdAt: recent()
        )
        await fake.stubTranscript([page([asked])])
        await fake.stubTurns([
            .success(.fixture(turnId: "t-desk", status: "running", partialText: "Working on")),
            .success(.fixture(turnId: "t-desk", status: "done", resultText: "All done.")),
        ])
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        #expect(await waitUntil { await MainActor.run { store.messages.count == 2 } })
        await awaitSettled(store)
        #expect(await fake.getTurnCalls.map(\.turnId).first == "t-desk", "the phone polls the other device's turn")
        let placeholder = try #require(store.messages.last)
        #expect(placeholder.role == .assistant)
        #expect(placeholder.status == .done)
        #expect(placeholder.text == "All done.")
        #expect(placeholder.seq == nil, "still local until its row arrives")
        #expect(store.takeCompletedAssistantTurns() == ["t-desk"])

        // Then the row arrives: it resolves the placeholder in place.
        await fake.stubTranscript([page([TranscriptRow(
            seq: 2, id: UUID().uuidString, role: .assistant, turnId: "t-desk", text: "All done.",
            status: .done, createdAt: recent(), answers: ["t-desk"]
        )])])
        store.handleDidBecomeActive()
        #expect(await waitUntil { await MainActor.run { store.messages.last?.seq == 2 } })

        #expect(store.messages.count == 2)
        #expect(store.messages.last?.id == placeholder.id, "same bubble, same id")
        #expect(store.completedAssistantTurns.isEmpty, "one reply, one sound")
    }

    /// The user row's own reply arrived in the same sync: nothing to poll.
    @Test func aTurnAlreadyAnsweredWhenItIsSyncedStartsNoPoll() async throws {
        let fake = FakeChatAPI()
        await fake.stubTranscript([page([
            TranscriptRow(seq: 1, id: UUID().uuidString, role: .user, turnId: "t1", text: "q", createdAt: recent(2)),
            TranscriptRow(
                seq: 2, id: UUID().uuidString, role: .assistant, turnId: "t1", text: "a",
                status: .done, createdAt: recent(1), answers: ["t1"]
            ),
        ])])
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        #expect(await waitUntil { await MainActor.run { store.messages.count == 2 } })
        await awaitSettled(store)
        #expect(await fake.getTurnCallCount == 0)
    }

    /// An error row confirms the placeholder and gives it a `seq`. Retry must
    /// make it local-only again, or the retry's reply row (turn B, answering the
    /// original A) cannot find it and lands as a second bubble while the
    /// placeholder's own poll also completes: two replies, two sounds
    /// (review, 2026-10-01).
    @Test func aRetryAfterAnErrorRowStillGetsOneReplyAndOneSound() async throws {
        let fake = FakeChatAPI()
        await fake.stubSend(.success(MessageResponse(turnId: "A")))
        await fake.stubTurns([.success(.fixture(turnId: "A", status: "error", error: "boom"))])
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }
        store.send(text: "hello")
        await awaitSettled(store)
        #expect(await waitUntil { await fake.callOrder.contains("health") }, "the launch tick is over")
        let user = try #require(store.messages.first)
        let placeholder = try #require(store.messages.last)
        #expect(placeholder.status == .error)

        // The server's error row for turn A confirms the placeholder.
        await fake.stubTranscript([page([
            TranscriptRow(
                seq: 1, id: UUID().uuidString, role: .user, turnId: "A",
                clientMsgId: user.id.uuidString, text: "hello", createdAt: recent(3)
            ),
            TranscriptRow(
                seq: 2, id: UUID().uuidString, role: .assistant, turnId: "A", text: "boom",
                status: .error, createdAt: recent(2), answers: ["A"]
            ),
        ])])
        store.handleDidBecomeActive()
        #expect(await waitUntil { await MainActor.run { store.messages.last?.seq == 2 } })

        // Retry: the poll for the new turn B is held in the air.
        let reachedPoll = Gate()
        let releasePoll = Gate()
        await fake.setOnGetTurn { call in
            guard call == 2 else { return }
            await reachedPoll.open()
            await releasePoll.wait()
        }
        await fake.stubSend(.success(MessageResponse(turnId: "B")))
        await fake.stubTurns([.success(.fixture(turnId: "B", status: "done", resultText: "Fixed."))])
        store.retry(placeholder.id)
        await awaitOpened(reachedPoll, "the retry's poll should be in flight")
        #expect(store.messages.last?.seq == nil, "a retried placeholder is local-only again")

        // The retry's reply row arrives while that poll is still waiting.
        await fake.stubTranscript([page([TranscriptRow(
            seq: 3, id: UUID().uuidString, role: .assistant, turnId: "B", text: "Fixed.",
            status: .done, createdAt: recent(), answers: ["A"]
        )])])
        store.handleDidBecomeActive()
        #expect(await waitUntil { await MainActor.run { store.messages.last?.seq == 3 } })
        await releasePoll.open()
        await awaitSettled(store)

        #expect(store.messages.filter { $0.role == .assistant }.count == 1, "one reply, not two bubbles")
        #expect(store.messages.last?.id == placeholder.id)
        #expect(store.messages.last?.text == "Fixed.")
        #expect(store.takeCompletedAssistantTurns() == ["B"], "one sound")
    }

    // MARK: Transcript sync: reachability

    @Test func aSyncThatCannotReachTheServerSaysSo() async throws {
        let fake = FakeChatAPI()
        await fake.stubTranscript([.failure(URLError(.notConnectedToInternet))])
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        #expect(await waitUntil { store.connectionActivity == .unreachable },
                "a healthy /health does not excuse a transcript that never arrived")
    }

    @Test func aTranscriptServerErrorIsNotCalledUnreachable() async throws {
        let fake = FakeChatAPI()
        await fake.stubTranscript([.failure(APIError.server(status: 500, detail: "boom"))])
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        #expect(await waitUntil { await fake.transcriptCallCount >= 1 })
        try await Task.sleep(for: .milliseconds(30))
        #expect(store.connectionActivity == .idle, "a 500 is proof the network is fine and the box is not")
    }

    // MARK: Transcript sync: attachments

    @Test func aForeignImageIsDownloadedAndShownThroughImageFilename() async throws {
        let attachmentsDir = makeTempAttachmentsDirectoryURL()
        let attachmentId = UUID().uuidString
        let bytes = Data([1, 2, 3, 4])
        let fake = FakeChatAPI()
        await fake.stubAttachment(id: attachmentId, .success(bytes))
        await fake.stubTranscript([page([TranscriptRow(
            seq: 1, id: UUID().uuidString, role: .user, turnId: "t1", text: "look",
            createdAt: recent(), attachments: [.init(id: attachmentId, mime: "image/jpeg")]
        ), TranscriptRow(
            seq: 2, id: UUID().uuidString, role: .assistant, turnId: "t1", text: "nice",
            status: .done, createdAt: recent(), answers: ["t1"]
        )])])

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(),
            attachmentsDirectoryURL: attachmentsDir, policy: .test()
        )
        defer { store.cancelActiveWork() }
        #expect(await waitUntil { await MainActor.run { store.messages.first?.imageFilenames != nil } })

        let message = try #require(store.messages.first)
        #expect(message.imageFilenames == ["\(attachmentId).jpg"])
        #expect(message.attachmentIds == [attachmentId])
        #expect(store.loadAttachment(filename: "\(attachmentId).jpg") == bytes)
        #expect(await fake.attachmentCalls == [attachmentId])
    }

    @Test func aFailedImageDownloadIsRetriedAtTheNextLaunchNotEveryTick() async throws {
        let url = makeTempTranscriptURL()
        let attachmentsDir = makeTempAttachmentsDirectoryURL()
        let attachmentId = UUID().uuidString
        let fake = FakeChatAPI()
        await fake.stubAttachment(id: attachmentId, .failure(URLError(.notConnectedToInternet)))
        await fake.stubTranscript([page([TranscriptRow(
            seq: 1, id: UUID().uuidString, role: .user, turnId: "t1", text: "look",
            createdAt: recent(), attachments: [.init(id: attachmentId, mime: "image/jpeg")]
        ), TranscriptRow(
            seq: 2, id: UUID().uuidString, role: .assistant, turnId: "t1", text: "nice",
            status: .done, createdAt: recent(), answers: ["t1"]
        )])])
        let first = ChatStore(
            deviceToken: "tok", client: fake, persistURL: url,
            attachmentsDirectoryURL: attachmentsDir, policy: .test()
        )
        #expect(await waitUntil { await fake.attachmentCalls.count >= 1 })
        await awaitSettled(first)
        #expect(first.messages.first?.allImageFilenames.isEmpty == true)
        #expect(first.messages.first?.attachmentIds == [attachmentId], "the id is kept, so the picture is not lost")
        first.cancelActiveWork()

        await fake.stubAttachment(id: attachmentId, .success(Data([7])))
        let second = ChatStore(
            deviceToken: "tok", client: fake, persistURL: url,
            attachmentsDirectoryURL: attachmentsDir, policy: .test()
        )
        defer { second.cancelActiveWork() }

        #expect(await waitUntil { await MainActor.run { second.messages.first?.imageFilenames != nil } })
    }

    /// A row with several images (sent from another device) downloads all of
    /// them, and the message lists them in row order.
    @Test func everyImageOfARowIsDownloadedInRowOrder() async throws {
        let attachmentsDir = makeTempAttachmentsDirectoryURL()
        let ids = (0..<3).map { _ in UUID().uuidString }
        let fake = FakeChatAPI()
        for (i, id) in ids.enumerated() { await fake.stubAttachment(id: id, .success(Data([UInt8(i)]))) }
        await fake.stubTranscript([page([TranscriptRow(
            seq: 1, id: UUID().uuidString, role: .user, turnId: "t1", text: "look",
            createdAt: recent(), attachments: ids.map { .init(id: $0, mime: "image/jpeg") }
        ), TranscriptRow(
            seq: 2, id: UUID().uuidString, role: .assistant, turnId: "t1", text: "nice",
            status: .done, createdAt: recent(), answers: ["t1"]
        )])])

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(),
            attachmentsDirectoryURL: attachmentsDir, policy: .test()
        )
        defer { store.cancelActiveWork() }
        let expected = ids.map { "\($0).jpg" }
        #expect(await waitUntil { await MainActor.run { store.messages.first?.imageFilenames == expected } })
        #expect(store.messages.first?.attachmentIds == ids)
        #expect(ids.enumerated().allSatisfy { store.loadAttachment(filename: "\($1).jpg") == Data([UInt8($0)]) })
    }

    /// A cache written before multi-image messages holds one `imageFilename`;
    /// it must still decode, render and resend.
    @Test func aCachedSingleImageMessageStillDecodesThroughTheLegacyField() throws {
        var old = ChatMessage(
            id: UUID(), role: .user, text: "old photo", createdAt: Date(), turnId: nil, status: .done,
            clientMsgId: nil, imageFilename: "legacy.jpg"
        )
        old.attachmentId = UUID().uuidString
        let decoded = try JSONDecoder().decode(ChatMessage.self, from: JSONEncoder().encode(old))
        #expect(decoded.allImageFilenames == ["legacy.jpg"])
        #expect(decoded.allAttachmentIds == [old.attachmentId!])
        #expect(decoded.imageFilenames == nil)
    }

    @Test func gridColumnsForTwoToSixImages() {
        #expect((1...6).map(ChatMessage.gridColumns(forImageCount:)) == [1, 2, 3, 2, 3, 3])
    }

    // MARK: Transcript cache

    @Test func theOldPhoneLocalTranscriptIsDeletedAndNotMigrated() async throws {
        let url = makeTempTranscriptURL()
        let legacy = url.deletingLastPathComponent().appendingPathComponent("chat-transcript.json")
        let old = ChatMessage(
            id: UUID(), role: .user, text: "an old message", createdAt: Date(), turnId: nil,
            status: .done, clientMsgId: nil
        )
        try JSONEncoder().encode([old]).write(to: legacy)

        let store = ChatStore(deviceToken: "tok", client: FakeChatAPI(), persistURL: url, policy: .test())
        defer { store.cancelActiveWork() }

        #expect(store.messages.isEmpty, "Mike's call, 2026-10-01: phone-local history may be lost")
        #expect(!FileManager.default.fileExists(atPath: legacy.path))
    }

    @Test func theAttachmentsDirectoryIsLeftAloneWhenTheOldCacheGoes() async throws {
        // This phone's own images live there, and the outbox needs them.
        let url = makeTempTranscriptURL()
        let legacy = url.deletingLastPathComponent().appendingPathComponent("chat-transcript.json")
        try Data("[]".utf8).write(to: legacy)
        let attachmentsDir = makeTempAttachmentsDirectoryURL()
        let image = attachmentsDir.appendingPathComponent("keep-me.jpg")
        try Data([1]).write(to: image)

        let store = ChatStore(
            deviceToken: "tok", client: FakeChatAPI(), persistURL: url,
            attachmentsDirectoryURL: attachmentsDir, policy: .test()
        )
        defer { store.cancelActiveWork() }

        #expect(FileManager.default.fileExists(atPath: image.path))
    }

    @Test func attachmentFilesNoKeptMessageReferencesAreDeletedAtLaunch() async throws {
        let url = makeTempTranscriptURL()
        let attachmentsDir = makeTempAttachmentsDirectoryURL()
        let past = Date().addingTimeInterval(-3600)
        func write(_ name: String, modified: Date? = nil) throws {
            let file = attachmentsDir.appendingPathComponent(name)
            try Data([1]).write(to: file)
            try FileManager.default.setAttributes(
                [.modificationDate: modified ?? past], ofItemAtPath: file.path
            )
        }
        let pendingDownload = UUID().uuidString
        try write("kept-by-filename.jpg")
        try write("\(pendingDownload).jpg")
        try write("orphan-from-the-old-transcript.jpg")
        try write("belongs-to-a-dropped-message.jpg")
        // Stands in for a file the first send or download writes while the
        // prune is running: its mtime is after the store's launch.
        try write("written-just-now.jpg", modified: Date().addingTimeInterval(3600))

        let now = Date()
        var kept = ChatMessage(
            id: UUID(), role: .user, text: "photo", createdAt: now, turnId: nil, status: .done,
            clientMsgId: nil, imageFilename: "kept-by-filename.jpg"
        )
        kept.seq = 2
        var downloading = ChatMessage(
            id: UUID(), role: .user, text: "photo", createdAt: now, turnId: nil, status: .done,
            clientMsgId: nil, seq: 3, attachmentId: pendingDownload
        )
        downloading.imageFilename = nil
        let dropped = ChatMessage(
            id: UUID(), role: .user, text: "old photo",
            createdAt: now.addingTimeInterval(-31 * 24 * 60 * 60), turnId: nil, status: .done,
            clientMsgId: nil, imageFilename: "belongs-to-a-dropped-message.jpg", seq: 1
        )
        try writeTranscript([dropped, kept, downloading], latestSeq: 3, to: url)

        let fake = FakeChatAPI()
        // The download in flight at launch must keep failing, so its file stays
        // a plain "referenced by attachmentId" case.
        await fake.stubAttachment(id: pendingDownload, .failure(URLError(.notConnectedToInternet)))
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: url,
            attachmentsDirectoryURL: attachmentsDir, policy: .test()
        )
        defer { store.cancelActiveWork() }

        func exists(_ name: String) -> Bool {
            FileManager.default.fileExists(atPath: attachmentsDir.appendingPathComponent(name).path)
        }
        #expect(await waitUntil { !exists("orphan-from-the-old-transcript.jpg") && !exists("belongs-to-a-dropped-message.jpg") },
                "files no kept message references are removed")
        #expect(exists("kept-by-filename.jpg"))
        #expect(exists("\(pendingDownload).jpg"), "a pending download's file is referenced by its attachment id")
        #expect(exists("written-just-now.jpg"), "a file newer than this launch is never pruned")
    }

    @Test func anUnreadableCacheStartsEmptyAndBootstraps() async throws {
        let url = makeTempTranscriptURL()
        try Data("not json".utf8).write(to: url)
        let fake = FakeChatAPI()
        await fake.stubTranscript([page([cronRow(3, turn: "c3")])])

        let store = ChatStore(deviceToken: "tok", client: fake, persistURL: url, policy: .test())
        defer { store.cancelActiveWork() }

        #expect(await waitUntil { await MainActor.run { store.messages.count == 1 } })
        let call = try requireCount(await fake.transcriptCalls.map { [$0.after, $0.before] }, 1)
        #expect(call[0] == [nil, nil])
    }

    @Test func anEmptyCacheReadsAsLoadingUntilTheFirstFetchLands() async throws {
        let url = makeTempTranscriptURL()
        let fake = FakeChatAPI()
        // The first fetch fails (unreachable): still loading, not "nothing
        // here". The second gets an empty server through.
        await fake.stubTranscript([.failure(URLError(.notConnectedToInternet))])
        let store = ChatStore(deviceToken: "tok", client: fake, persistURL: url, policy: .test())
        defer { store.cancelActiveWork() }
        let seen = LockedFlags()
        await fake.setOnTranscript { _ in
            seen.append(await MainActor.run { store.awaitingFirstFetch })
        }

        // The test policy ticks health once, so foregrounding is the retry.
        #expect(await waitUntil { await fake.transcriptCallCount >= 1 })
        await MainActor.run { store.handleDidBecomeActive() }
        #expect(await waitUntil { await fake.transcriptCallCount >= 2 })
        #expect(await waitUntil { await MainActor.run { !store.awaitingFirstFetch } })
        // Every fetch the hook saw in flight began with the flag still set:
        // the failed one did not clear it.
        #expect(!seen.values.isEmpty && seen.values.allSatisfy { $0 })
    }

    @Test func aCachedTranscriptIsNeverLoading() async throws {
        let url = makeTempTranscriptURL()
        try writeTranscript([], latestSeq: 5, to: url)
        let store = ChatStore(deviceToken: "tok", client: FakeChatAPI(), persistURL: url, policy: .test())
        defer { store.cancelActiveWork() }
        #expect(await MainActor.run { !store.awaitingFirstFetch })
    }

    @Test func serverRowsOlderThanThirtyDaysAreDroppedOnLoadButLocalOnesNever() async throws {
        let url = makeTempTranscriptURL()
        let now = Date()
        let tooOld = now.addingTimeInterval(-31 * 24 * 60 * 60)
        func message(_ text: String, at date: Date, seq: Int?) -> ChatMessage {
            ChatMessage(
                id: UUID(), role: .assistant, text: text, createdAt: date, turnId: nil,
                status: .done, clientMsgId: nil, seq: seq
            )
        }
        try writeTranscript([
            message("old server row", at: tooOld, seq: 1),
            message("old local-only", at: tooOld, seq: nil),
            message("recent server row", at: now.addingTimeInterval(-60), seq: 2),
        ], latestSeq: 2, to: url)

        let store = ChatStore(deviceToken: "tok", client: FakeChatAPI(), persistURL: url, policy: .test())
        defer { store.cancelActiveWork() }

        #expect(store.messages.map(\.text) == ["old local-only", "recent server row"])
    }

    // MARK: Show earlier

    private func serverMessages(seqs: ClosedRange<Int>) -> [ChatMessage] {
        seqs.map { seq in
            ChatMessage(
                id: UUID(), role: .assistant, text: "row \(seq)",
                createdAt: Date().addingTimeInterval(-Double(100 - seq) * 60), turnId: "t\(seq)",
                status: .done, clientMsgId: nil, seq: seq
            )
        }
    }

    @Test func showEarlierFetchesTheServersPageBeforeTheOldestRowOnceTheLocalOnesRunOut() async throws {
        let url = makeTempTranscriptURL()
        let cache = ChatStore.TranscriptCache(
            latestSeq: 60, hasOlderOnServer: true, messages: serverMessages(seqs: 51...60)
        )
        try JSONEncoder().encode(cache).write(to: url)
        let fake = FakeChatAPI()
        // Newest first, as a `before` page arrives.
        let older = serverMessages(seqs: 41...50).reversed().map { message in
            TranscriptRow(
                seq: message.seq!, id: message.id.uuidString, role: .assistant, turnId: message.turnId ?? "",
                text: message.text, status: .done, createdAt: message.createdAt.timeIntervalSince1970,
                answers: []
            )
        }
        await fake.stubTranscript([page([]), page(Array(older), latest: 60, hasMore: false)])

        let store = ChatStore(deviceToken: "tok", client: fake, persistURL: url, policy: .test())
        defer { store.cancelActiveWork() }
        #expect(await waitUntil { await fake.transcriptCallCount >= 1 })
        await awaitSettled(store)

        #expect(store.hasEarlierMessages, "the server has more, so the button stays up")
        store.showEarlierMessages()
        await awaitSettled(store)

        let calls = await fake.transcriptCalls.map { [$0.after, $0.before, $0.limit] }
        #expect(calls.last == [nil, 51, 200], "before the oldest row held, one page")
        #expect(store.messages.compactMap(\.seq) == Array(41...60), "prepended, oldest first")
        #expect(store.completedAssistantTurns.isEmpty, "scrollback is not an arrival")
        #expect(readCache(at: url)?.hasOlderOnServer == false)

        // The server is out of older rows: the button goes once the rows
        // already held have all been revealed, and no further fetch happens.
        while store.hasEarlierMessages { store.showEarlierMessages(); await awaitSettled(store) }
        #expect(store.visibleMessages.count == 20)
        #expect(await fake.transcriptCalls.filter { $0.before != nil }.count == 1)
    }

    @Test func showEarlierWithNothingMoreOnTheServerFetchesNothing() async throws {
        let url = makeTempTranscriptURL()
        try writeTranscript(serverMessages(seqs: 1...5), latestSeq: 5, to: url)
        let fake = FakeChatAPI()
        let store = ChatStore(deviceToken: "tok", client: fake, persistURL: url, policy: .test())
        defer { store.cancelActiveWork() }
        #expect(await waitUntil { await fake.transcriptCallCount >= 1 })

        #expect(!store.hasEarlierMessages)
        store.showEarlierMessages()
        await awaitSettled(store)

        #expect(await fake.transcriptCalls.filter { $0.before != nil }.isEmpty)
    }

    // MARK: - Completed-turn signal (ChatView's receive sound)

    @Test func aCompletedPolledTurnIsQueuedOnceAndDrains() async throws {
        let fake = FakeChatAPI()
        await fake.stubSend(.success(MessageResponse(turnId: "turn-42")))
        await fake.stubTurns([
            .success(.fixture(status: "running")),
            .success(.fixture(status: "done", resultText: "Done.")),
        ])

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        store.send(text: "hello")
        await awaitSettled(store)

        #expect(store.completedAssistantTurns == ["turn-42"])
        #expect(store.takeCompletedAssistantTurns() == ["turn-42"])
        #expect(store.completedAssistantTurns.isEmpty, "a drained completion must not sound twice")
        #expect(store.takeCompletedAssistantTurns().isEmpty)
    }

    @Test func aFailedTurnIsNotQueuedAsACompletion() async throws {
        let fake = FakeChatAPI()
        await fake.stubSend(.success(MessageResponse(turnId: "turn-42")))
        await fake.stubTurns([.success(.fixture(status: "error", error: "boom"))])

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        store.send(text: "hello")
        await awaitSettled(store)

        #expect(store.completedAssistantTurns.isEmpty, "the receive sound is for a reply that landed")
    }

    @Test func aSyncedRowIsQueuedOnlyWhenItSucceeded() async throws {
        let fake = FakeChatAPI()
        let failed = TranscriptRow(
            seq: 2, id: UUID().uuidString, role: .assistant, source: "cron", turnId: "cron-2",
            text: "boom", status: .error, createdAt: recent(), answers: []
        )
        // Past the bootstrap, which is deliberately silent: arrivals announce.
        let url = makeTempTranscriptURL()
        try writeTranscript([], latestSeq: 0, to: url)
        await fake.stubTranscript([page([cronRow(1, turn: "cron-1"), failed])])

        let store = ChatStore(deviceToken: "tok", client: fake, persistURL: url, policy: .test())
        defer { store.cancelActiveWork() }
        #expect(await waitUntil { await MainActor.run { store.messages.count == 2 } })

        #expect(store.completedAssistantTurns == ["cron-1"])
    }

    // MARK: - The fetch indicator

    /// Tapping "Marlow has something for you" on a poor connection opened a
    /// transcript with nothing new in it and no sign anything was in flight
    /// (Mike, 2026-09-07). The fetch passes no timeout, so that silence can
    /// run to URLSession's 60s default.

    @Test func aSlowFetchSaysSomethingIsHappening() async throws {
        let fake = FakeChatAPI()
        let reachedSync = Gate()
        let releaseSync = Gate()
        await fake.setOnTranscript { _ in
            await reachedSync.open()
            await releaseSync.wait()
        }
        var policy = PollingPolicy.test()
        policy.connectionNoticeDelay = 0

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: policy
        )
        defer { store.cancelActiveWork() }

        await awaitOpened(reachedSync, "the first pass should reach /v1/transcript")
        #expect(
            await waitUntil { store.connectionActivity == .checking },
            "a fetch the user is implicitly waiting on should show itself"
        )

        await releaseSync.open()
        #expect(
            await waitUntil { store.connectionActivity == .idle },
            "and stop showing itself the moment the fetch lands"
        )
    }

    @Test func aHealthyLinkNeverFlashesTheIndicator() async throws {
        /// `.test()`'s notice delay is effectively never, standing in for a
        /// round trip that beats the real 600ms. A wifi round trip is ~30ms,
        /// two orders of magnitude inside it — the indicator is for waiting,
        /// and a flicker on every foreground would be worse than nothing.
        let fake = FakeChatAPI()
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        #expect(await waitUntil { await fake.transcriptCallCount >= 1 })
        #expect(store.connectionActivity == .idle)
    }

    @Test func aPassThatCannotReachTheServerSaysSo() async throws {
        /// A spinner that vanishes having shown nothing is the blank screen
        /// this feature exists to fix, arrived at more slowly. A failed pass
        /// keeps the row and says what happened.
        let fake = FakeChatAPI()
        await fake.stubHealth(.failure(URLError(.notConnectedToInternet)))

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        #expect(await waitUntil { store.connectionActivity == .unreachable })
    }

    @Test func aServerThatAnswersBadlyIsNotCalledUnreachable() async throws {
        /// "Can't reach Marlow" is a claim about the network. A 500 out of
        /// `/health` is proof of the opposite — the tailnet is fine and the
        /// box is not — and sending Mike to look at his connection for a fault
        /// on the box is worse than saying nothing.
        let fake = FakeChatAPI()
        await fake.stubHealth(.failure(APIError.server(status: 500, detail: "boom")))

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        #expect(await waitUntil { await fake.transcriptCallCount >= 1 })
        #expect(store.connectionActivity == .idle)
    }

    @Test func thumbingTheNotificationStartsAnAnnouncedPass() async throws {
        /// The path Mike actually takes: the app is already open behind the
        /// notification, so nothing new launches — `handleDidBecomeActive()`
        /// is the whole of the client's reaction to the tap, and it has to be
        /// as talkative as a cold launch.
        let fake = FakeChatAPI()
        var policy = PollingPolicy.test()
        policy.connectionNoticeDelay = 0

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: policy
        )
        defer { store.cancelActiveWork() }
        #expect(await waitUntil { await fake.transcriptCallCount >= 1 }, "the launch pass")

        let reachedSecondPass = Gate()
        let releaseSecondPass = Gate()
        await fake.setOnTranscript { call in
            guard call >= 2 else { return }
            await reachedSecondPass.open()
            await releaseSecondPass.wait()
        }

        store.handleDidBecomeActive()

        await awaitOpened(reachedSecondPass, "becoming active should refetch")
        #expect(
            await waitUntil { store.connectionActivity == .checking },
            "a fetch behind a notification tap is exactly the one worth showing"
        )
        await releaseSecondPass.open()
    }

    @Test func recoveringFromAnUnreachableServerClearsTheIndicator() async throws {
        let fake = FakeChatAPI()
        await fake.stubHealth(.failure(URLError(.notConnectedToInternet)))

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }
        #expect(await waitUntil { store.connectionActivity == .unreachable })

        await fake.stubHealth(.success(.fixture()))
        store.handleDidBecomeActive()

        #expect(
            await waitUntil { store.connectionActivity == .idle },
            "the sticky failure must not outlive the connection coming back"
        )
    }

    // MARK: - Transcript window

    private func dailyTranscript(count: Int) -> [ChatMessage] {
        let now = Date()
        return (0..<count).map { i in
            ChatMessage(
                id: UUID(), role: .assistant, text: "day \(i)",
                createdAt: now.addingTimeInterval(-Double(count - i) * 24 * 60 * 60),
                turnId: "t\(i)", status: .done, clientMsgId: nil
            )
        }
    }

    /// The window is a view over the transcript, never a pruning of it: the
    /// local file is the only durable copy (ios-transcript-scaling.md).
    @Test func aLongTranscriptLoadsWindowedButWhole() async throws {
        let persistURL = makeTempTranscriptURL()
        try writeTranscript(dailyTranscript(count: 100), to: persistURL)

        let store = ChatStore(deviceToken: "tok", client: FakeChatAPI(), persistURL: persistURL, policy: .test())
        defer { store.cancelActiveWork() }

        #expect(store.messages.count == 100)
        #expect(store.visibleMessages.count == TranscriptWindow.minimumCount)
        #expect(store.visibleMessages.last == store.messages.last)
        #expect(store.hasEarlierMessages)

        while store.hasEarlierMessages { store.showEarlierMessages() }
        #expect(store.visibleMessages.count == 100)
        #expect(readTranscript(at: persistURL).count == 100)
    }

    /// A cron reply that reached the phone days late (phone off) is older than
    /// the launch window. Mike was notified about it, so it must not sit hidden
    /// behind "show earlier".
    @Test func aLateSyncedRowOlderThanTheWindowIsStillShown() async throws {
        let persistURL = makeTempTranscriptURL()
        // Local-only rows (no `seq`), so the 30-day cutoff leaves all 100 for
        // the window to work over.
        try writeTranscript(dailyTranscript(count: 100), latestSeq: 5, to: persistURL)
        let fake = FakeChatAPI()
        let store = ChatStore(deviceToken: "tok", client: fake, persistURL: persistURL, policy: .test())
        defer { store.cancelActiveWork() }
        // The launch tick must be over before the page is scripted: the fake
        // hands a page out once, and a tick cancelled by the foreground
        // trigger below would drop one it had already been given.
        #expect(await waitUntil { await fake.callOrder.contains("health") })
        let oldest = try #require(store.visibleMessages.first).createdAt
        #expect(store.hasEarlierMessages)

        await fake.stubTranscript([page([cronRow(
            6, turn: "cron-late", text: "Late digest.",
            createdAt: oldest.addingTimeInterval(-60 * 60).timeIntervalSince1970
        )])])
        store.handleDidBecomeActive()

        #expect(await waitUntil { await MainActor.run { store.messages.contains { $0.turnId == "cron-late" } } })
        #expect(store.visibleMessages.contains { $0.turnId == "cron-late" })
    }
}
