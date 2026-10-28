import Foundation
import Testing
@testable import Marlow

/// The prefetch path: replies the notification extension already fetched must
/// render without a second round trip
/// (docs/tasks/ios-notification-open-and-content-swap.md Half B).
///
/// The behaviour under test is as much about what does NOT happen — no poll,
/// no 500ms `pollLoop` sleep — as about the text landing, so most of these
/// assert on `getTurnCallCount` alongside the transcript.
/// Lets a hook fill the inbox after the store exists, the way a push does:
/// the app is already running when the extension writes its file.
@MainActor
final class Prefetched {
    var entries: [String: Data] = [:]
}

@MainActor
struct TurnPrefetchTests {

    /// Encodes a turn the way the server would, because that is what the
    /// extension stores: the raw `GET /v1/turn/{id}` body, byte for byte. A
    /// hand-built `Turn` would not prove the app can read what the extension
    /// actually wrote.
    private func wireTurn(
        turnId: String, status: String, resultText: String? = nil,
        error: String? = nil, source: String = "chat"
    ) -> Data {
        var object: [String: Any] = [
            "turn_id": turnId,
            "status": status,
            "source": source,
            "had_permission_denial": false,
            "created_at": 0,
            "activities": [],
            "partial_text": "",
            "partial_text_marks": [],
        ]
        if let resultText { object["result_text"] = resultText }
        if let error { object["error"] = error }
        return try! JSONSerialization.data(withJSONObject: object)
    }

    private func runningPlaceholder(turnId: String) -> [ChatMessage] {
        let user = ChatMessage(
            id: UUID(), role: .user, text: "what's on today", createdAt: Date(), turnId: nil,
            status: .done, clientMsgId: nil
        )
        return [
            user,
            ChatMessage(
                id: UUID(), role: .assistant, text: "", createdAt: Date(), turnId: turnId,
                status: .running, clientMsgId: user.id.uuidString
            ),
        ]
    }

    // MARK: - The irritant this exists to remove

    /// Tapping the banner used to restart a poll that slept 500ms before even
    /// asking, to re-download text the notification had already displayed.
    @Test func aPrefetchedReplyRendersAtLaunchWithoutPolling() async throws {
        let persistURL = makeTempTranscriptURL()
        try writeTranscript(runningPlaceholder(turnId: "turn-9"), to: persistURL)

        let fake = FakeChatAPI()
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: persistURL, policy: .test(),
            inbox: { ["turn-9": self.wireTurn(turnId: "turn-9", status: "done", resultText: "Two emails need you.")] }
        )
        defer { store.cancelActiveWork() }

        // Read before awaiting: this is the state the first frame renders.
        // Anything that has to be awaited is, by definition, a wait the user
        // would see.
        let messages = try requireCount(store.messages, 2)
        #expect(messages[1].status == .done)
        #expect(messages[1].text == "Two emails need you.")

        await awaitSettled(store)
        #expect(
            await fake.getTurnCallCount == 0,
            "the reply was already on disk; asking the server again is the wait being removed"
        )
    }

    /// A turn prefetched by a notification extension that was written before
    /// the server's `ios` -> `chat` rename still says `ios`. Deliberately the
    /// old value: it must resolve its placeholder exactly like a `chat` one.
    @Test func aPrefetchedLegacyIosTurnResolvesThePlaceholderLikeChat() async throws {
        let persistURL = makeTempTranscriptURL()
        try writeTranscript(runningPlaceholder(turnId: "turn-9"), to: persistURL)

        let fake = FakeChatAPI()
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: persistURL, policy: .test(),
            inbox: {
                ["turn-9": self.wireTurn(
                    turnId: "turn-9", status: "done", resultText: "Old reply.", source: "ios"
                )]
            }
        )
        defer { store.cancelActiveWork() }

        let messages = try requireCount(store.messages, 2)
        #expect(messages[1].status == .done)
        #expect(messages[1].text == "Old reply.")
        await awaitSettled(store)
    }

    @Test func aPrefetchedFailureResolvesThePlaceholderToo() async throws {
        let persistURL = makeTempTranscriptURL()
        try writeTranscript(runningPlaceholder(turnId: "turn-9"), to: persistURL)

        let fake = FakeChatAPI()
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: persistURL, policy: .test(),
            inbox: { ["turn-9": self.wireTurn(turnId: "turn-9", status: "error", error: "claude exited 1")] }
        )
        defer { store.cancelActiveWork() }

        let messages = try requireCount(store.messages, 2)
        #expect(messages[1].status == .error)
        #expect(messages[1].text == "claude exited 1")
        await awaitSettled(store)
        #expect(await fake.getTurnCallCount == 0)
    }

    /// The resolved state has to reach disk in `init`, not on some later
    /// natural save: the process can be suspended again the moment the user
    /// looks away, and the inbox entry is deleted once it is consumed.
    @Test func theResolvedPlaceholderIsPersisted() async throws {
        let persistURL = makeTempTranscriptURL()
        try writeTranscript(runningPlaceholder(turnId: "turn-9"), to: persistURL)

        let fake = FakeChatAPI()
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: persistURL, policy: .test(),
            inbox: { ["turn-9": self.wireTurn(turnId: "turn-9", status: "done", resultText: "Done.")] }
        )
        defer { store.cancelActiveWork() }

        let persisted = try requireCount(readTranscript(at: persistURL), 2)
        #expect(persisted[1].status == .done)
        #expect(persisted[1].text == "Done.")
    }

    /// The named guard from `resolvePlaceholder`'s comment. Rendering is the
    /// visible half of what a terminal poll result does; retiring the poll is
    /// the invisible half, and it was missing in the first version of this
    /// code. A poll left running re-appends the turn id and plays a second
    /// receive sound for a turn APNs already sounded.
    ///
    /// Deliberately driven through `handleDidBecomeActive()` rather than
    /// `init`. Resolving in `init` happens before `reconcilePendingMessages`
    /// has started anything, so a poll never exists to be cancelled and the
    /// test would pass without exercising the cancel at all — the warm resume
    /// is both the path with a live poll and the common one, since the app is
    /// usually suspended rather than killed when the push lands.
    @Test func aPrefetchedTurnDoesEverythingATerminalPollWouldDo() async throws {
        let persistURL = makeTempTranscriptURL()
        try writeTranscript(runningPlaceholder(turnId: "turn-9"), to: persistURL)

        let prefetched = Prefetched()
        let box = StoreBox()
        let fake = FakeChatAPI()
        // The poll `reconcilePendingMessages` starts finds the turn still
        // running, then — on its next attempt, if it is still alive — done.
        // That second answer is what double-appends if the cancel is missing.
        await fake.stubTurns([
            .success(.fixture(turnId: "turn-9", status: "running")),
            .success(.fixture(turnId: "turn-9", status: "done", resultText: "Two emails need you.")),
        ])
        // The push landed while the app was suspended: fill the inbox and
        // foreground the app from inside the first poll, which is the only
        // window where a live poll and a prefetched answer coexist.
        await fake.setOnGetTurn { calls in
            guard calls == 1 else { return }
            await MainActor.run {
                prefetched.entries = [
                    "turn-9": self.wireTurn(
                        turnId: "turn-9", status: "done",
                        resultText: "Two emails need you."
                    ),
                ]
                box.store?.handleDidBecomeActive()
            }
        }

        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: persistURL,
            policy: .test(), inbox: { prefetched.entries }
        )
        box.store = store
        defer { store.cancelActiveWork() }
        await awaitSettled(store)

        let messages = try requireCount(store.messages, 2)
        #expect(messages[1].status == .done)
        #expect(messages[1].text == "Two emails need you.")
        #expect(
            store.takeCompletedAssistantTurns() == ["turn-9"],
            "one completion, or ChatView plays a sound APNs already played"
        )
    }

    // MARK: - The cache is never authoritative

    /// A non-terminal entry must not resolve anything. The extension only
    /// stores terminal turns, so this is defence against a stale file
    /// outliving a protocol change, and the cost of being wrong is a bubble
    /// frozen on a state the turn has already left.
    @Test func aNonTerminalPrefetchedTurnStillPolls() async throws {
        let persistURL = makeTempTranscriptURL()
        try writeTranscript(runningPlaceholder(turnId: "turn-9"), to: persistURL)

        let fake = FakeChatAPI()
        await fake.stubTurns([.success(.fixture(turnId: "turn-9", status: "done", resultText: "Fresh."))])
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: persistURL, policy: .test(),
            inbox: { ["turn-9": self.wireTurn(turnId: "turn-9", status: "running")] }
        )
        defer { store.cancelActiveWork() }
        await awaitSettled(store)

        let messages = try requireCount(store.messages, 2)
        #expect(messages[1].text == "Fresh.")
        #expect(await fake.getTurnCallCount > 0, "a running turn is not an answer")
    }

    /// Corrupt bytes must cost a poll, not the reply. This is the whole
    /// safety argument for the inbox being a cache: every way it can fail
    /// degrades to the behaviour that existed before it.
    @Test func anUnreadableEntryFallsBackToPolling() async throws {
        let persistURL = makeTempTranscriptURL()
        try writeTranscript(runningPlaceholder(turnId: "turn-9"), to: persistURL)

        let fake = FakeChatAPI()
        await fake.stubTurns([.success(.fixture(turnId: "turn-9", status: "done", resultText: "Fresh."))])
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: persistURL, policy: .test(),
            inbox: { ["turn-9": Data("not json".utf8)] }
        )
        defer { store.cancelActiveWork() }
        await awaitSettled(store)

        let messages = try requireCount(store.messages, 2)
        #expect(messages[1].status == .done)
        #expect(messages[1].text == "Fresh.")
    }

    /// An entry filed under one id whose body claims another is either a bug
    /// or something that should not be trusted to resolve a placeholder.
    @Test func anEntryWhoseBodyDisagreesWithItsFilenameIsDropped() async throws {
        let persistURL = makeTempTranscriptURL()
        try writeTranscript(runningPlaceholder(turnId: "turn-9"), to: persistURL)

        let fake = FakeChatAPI()
        await fake.stubTurns([.success(.fixture(turnId: "turn-9", status: "done", resultText: "Fresh."))])
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: persistURL, policy: .test(),
            inbox: { ["turn-9": self.wireTurn(turnId: "turn-OTHER", status: "done", resultText: "Wrong turn.")] }
        )
        defer { store.cancelActiveWork() }
        await awaitSettled(store)

        let messages = try requireCount(store.messages, 2)
        #expect(messages[1].text == "Fresh.")
    }

    /// A prefetched turn for a message this transcript has never heard of
    /// must not invent a bubble — `send()`'s own poll owns a chat turn from
    /// POST to terminal status.
    @Test func aPrefetchedIosTurnWithNoPlaceholderAppendsNothing() async throws {
        let fake = FakeChatAPI()
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test(),
            inbox: { ["turn-9": self.wireTurn(turnId: "turn-9", status: "done", resultText: "Orphan.")] }
        )
        defer { store.cancelActiveWork() }

        #expect(store.messages.isEmpty)
    }

    // MARK: - Cron turns

    /// A cron turn has no placeholder by construction, and the transcript sync
    /// is what brings it: in server order, once, and to every paired device.
    /// Rendering the prefetched copy as well would put it on screen twice, so
    /// the app leaves the inbox entry alone as a source of bubbles.
    @Test func aPrefetchedCronTurnIsNotRenderedFromTheInboxTheSyncBringsIt() async throws {
        let fake = FakeChatAPI()
        await fake.stubTranscript([.success(TranscriptPage(
            rows: [TranscriptRow(
                seq: 1, id: UUID().uuidString, role: .assistant, source: "cron", turnId: "turn-cron",
                text: "Three things this morning.", status: .done,
                createdAt: Date().timeIntervalSince1970 - 60, answers: []
            )],
            latestSeq: 1, hasMore: false
        ))])
        // Held until the test says so, so the store is observed BEFORE its
        // first sync lands: the inbox alone must not have produced a bubble.
        let gate = Gate()
        await fake.setOnTranscript { _ in await gate.wait() }
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test(),
            inbox: {
                ["turn-cron": self.wireTurn(
                    turnId: "turn-cron", status: "done",
                    resultText: "Three things this morning.", source: "cron"
                )]
            }
        )
        defer { store.cancelActiveWork() }

        #expect(store.messages.isEmpty, "the inbox no longer renders a cron turn")

        await gate.open()
        #expect(await waitUntil { await MainActor.run { store.messages.count == 1 } })
        try await Task.sleep(for: .milliseconds(30))
        #expect(store.messages.count == 1, "once, from the sync")
        #expect(store.messages[0].seq == 1)
    }
}
