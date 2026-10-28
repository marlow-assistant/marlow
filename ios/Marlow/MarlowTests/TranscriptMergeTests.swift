import Foundation
import Testing
@testable import Marlow

/// `TranscriptMerge` is pure, so every rule in docs/tasks/ios-server-transcript.md
/// is pinned here against plain values, with no store, task or clock in the way.
struct TranscriptMergeTests {
    // MARK: - Builders

    private func localUser(
        _ text: String = "hello", id: UUID = UUID(), imageFilename: String? = nil
    ) -> ChatMessage {
        ChatMessage(
            id: id, role: .user, text: text, createdAt: Date(), turnId: nil, status: .done,
            clientMsgId: nil, imageFilename: imageFilename
        )
    }

    private func placeholder(
        turn: String?, status: ChatMessage.Status = .running, text: String = "",
        pairedWith user: ChatMessage? = nil
    ) -> ChatMessage {
        ChatMessage(
            id: UUID(), role: .assistant, text: text, createdAt: Date(), turnId: turn,
            status: status, clientMsgId: user?.id.uuidString
        )
    }

    private func serverMessage(
        seq: Int, role: ChatMessage.Role = .assistant, turn: String? = nil
    ) -> ChatMessage {
        var message = ChatMessage(
            id: UUID(), role: role, text: "row \(seq)", createdAt: Date(timeIntervalSince1970: Double(seq)),
            turnId: turn, status: .done, clientMsgId: nil
        )
        message.seq = seq
        return message
    }

    private func userRow(
        _ seq: Int, turn: String, clientMsgId: String? = nil, text: String = "from elsewhere",
        attachments: [TranscriptRow.Attachment] = []
    ) -> TranscriptRow {
        TranscriptRow(
            seq: seq, id: UUID().uuidString, role: .user, turnId: turn, clientMsgId: clientMsgId,
            text: text, createdAt: 1_000 + Double(seq), attachments: attachments
        )
    }

    /// `answers` defaults to the row's own turn, which is what the server writes
    /// for a phone-sent turn; a cron or system row passes `[]`.
    private func assistantRow(
        _ seq: Int, turn: String, text: String = "reply", status: TranscriptRow.Status = .done,
        answers: [String]? = nil, source: String = "chat"
    ) -> TranscriptRow {
        TranscriptRow(
            seq: seq, id: UUID().uuidString, role: .assistant, source: source, turnId: turn,
            text: text, status: status, createdAt: 1_000 + Double(seq), answers: answers ?? [turn]
        )
    }

    // MARK: - User rows

    @Test func anOwnSendsRowIsFoldedIntoTheBubbleAlreadyOnScreen() throws {
        let user = localUser("with a photo", imageFilename: "local.jpg")
        let pending = placeholder(turn: "t1", pairedWith: user)
        let row = userRow(
            1, turn: "t1", clientMsgId: user.id.uuidString, text: "with a photo",
            attachments: [.init(id: UUID().uuidString, mime: "image/jpeg")]
        )

        let result = TranscriptMerge.apply([row], to: [user, pending], announce: true)

        let messages = try requireCount(result.messages, 2)
        #expect(messages[0].id == user.id, "local identity wins: the id never changes")
        #expect(messages[0].seq == 1)
        #expect(messages[0].imageFilename == "local.jpg", "the photo this phone sent is not downloaded back")
        #expect(messages[0].attachmentIds == nil)
        #expect(messages[1] == pending, "the placeholder is untouched by its user row")
        #expect(result.effects.fetchAttachments.isEmpty)
        #expect(result.effects.startPolls.isEmpty, "this phone already polls its own turn")
    }

    @Test func anOwnSendsRowMatchesWhateverTheCaseOfItsClientMsgId() throws {
        let user = localUser()

        let result = TranscriptMerge.apply(
            [userRow(1, turn: "t1", clientMsgId: user.id.uuidString.lowercased())],
            to: [user], announce: true
        )

        #expect(try requireCount(result.messages, 1)[0].seq == 1)
    }

    @Test func aForeignUserRowGetsAPlaceholderAndAPoll() throws {
        let row = userRow(5, turn: "tX", text: "asked from the desktop")

        let result = TranscriptMerge.apply([row], to: [], announce: true)

        let messages = try requireCount(result.messages, 2)
        #expect(messages[0].role == .user)
        #expect(messages[0].id == UUID(uuidString: row.id), "a row with no local counterpart takes the row's id")
        #expect(messages[0].text == "asked from the desktop")
        #expect(messages[0].status == .done)
        #expect(messages[0].turnId == "tX")
        #expect(messages[0].seq == 5)
        #expect(messages[0].createdAt == Date(timeIntervalSince1970: row.createdAt))
        #expect(messages[1].role == .assistant, "the placeholder sits right after the user message")
        #expect(messages[1].status == .running)
        #expect(messages[1].turnId == "tX")
        #expect(messages[1].seq == nil)
        #expect(messages[1].clientMsgId == nil)
        let polls = try requireCount(result.effects.startPolls.map { [$0.messageId.uuidString, $0.turnId] }, 1)
        #expect(polls[0] == [messages[1].id.uuidString, "tX"])
    }

    @Test func aForeignUserRowAnsweredInTheSameBatchGetsNoPlaceholder() throws {
        let result = TranscriptMerge.apply(
            [userRow(5, turn: "tX"), assistantRow(6, turn: "tX")], to: [], announce: true
        )

        #expect(result.messages.map(\.role) == [.user, .assistant])
        #expect(result.messages[1].seq == 6)
        #expect(result.effects.startPolls.isEmpty)
    }

    /// The match is on `answers`, not the reply's own turn: a retry's reply
    /// finishes a fresh turn B but resolves the original row's turn A.
    @Test func aForeignUserRowAnsweredThroughAnswersGetsNoPlaceholder() {
        let result = TranscriptMerge.apply(
            [userRow(5, turn: "A"), assistantRow(6, turn: "B", answers: ["A"])],
            to: [], announce: true
        )

        #expect(result.messages.map(\.role) == [.user, .assistant])
        #expect(result.effects.startPolls.isEmpty)
    }

    @Test func aForeignUserRowWhoseReplyIsAlreadyInTheCacheGetsNoPlaceholder() {
        // The reply arrived in an earlier fetch, the question in a later page.
        var reply = serverMessage(seq: 6, turn: "B")
        reply.answers = ["A"]

        let result = TranscriptMerge.apply([userRow(5, turn: "A")], to: [reply], announce: false)

        #expect(result.messages.map(\.role) == [.user, .assistant])
        #expect(result.effects.startPolls.isEmpty)
    }

    @Test func aForeignUserRowWithAPlaceholderAlreadyPollingItGetsNoSecond() {
        let polling = placeholder(turn: "tX")

        let result = TranscriptMerge.apply([userRow(5, turn: "tX")], to: [polling], announce: true)

        #expect(result.messages.filter { $0.role == .assistant }.count == 1)
        #expect(result.effects.startPolls.isEmpty)
    }

    @Test func anImageAttachmentOnAForeignUserRowEmitsAFetch() throws {
        let image = UUID().uuidString
        let row = userRow(
            5, turn: "tX",
            attachments: [
                .init(id: UUID().uuidString, mime: "application/pdf"),
                .init(id: image, mime: "image/png"),
            ]
        )

        let result = TranscriptMerge.apply([row], to: [], announce: true)

        #expect(result.messages[0].attachmentIds == [image], "the image, not the PDF")
        #expect(result.messages[0].imageFilenames == nil, "set by the store once it is downloaded")
        let fetches = try requireCount(result.effects.fetchAttachments.map { [$0.messageId.uuidString] + $0.attachmentIds }, 1)
        #expect(fetches[0] == [result.messages[0].id.uuidString, image])
    }

    @Test func everyImageAttachmentOnAForeignUserRowIsFetchedInRowOrder() throws {
        let ids = (0..<3).map { _ in UUID().uuidString }
        let row = userRow(
            5, turn: "tX",
            attachments: [.init(id: ids[0], mime: "image/jpeg"), .init(id: UUID().uuidString, mime: "application/pdf")]
                + ids.dropFirst().map { .init(id: $0, mime: "image/jpeg") }
        )

        let result = TranscriptMerge.apply([row], to: [], announce: true)

        #expect(result.messages[0].attachmentIds == ids)
        let fetch = try #require(result.effects.fetchAttachments.first)
        #expect(result.effects.fetchAttachments.count == 1, "one fetch per message, not per image")
        #expect(fetch.attachmentIds == ids)
    }

    @Test func aRowWithNoImageAttachmentEmitsNoFetch() {
        let row = userRow(5, turn: "tX", attachments: [.init(id: UUID().uuidString, mime: "application/pdf")])

        let result = TranscriptMerge.apply([row], to: [], announce: true)

        #expect(result.messages[0].attachmentIds == nil)
        #expect(result.effects.fetchAttachments.isEmpty)
    }

    // MARK: - Assistant rows

    @Test func anAssistantRowResolvesThePlaceholderForItsTurn() throws {
        let user = localUser()
        var pending = placeholder(turn: "t1", pairedWith: user)
        pending.streamedText = "par"
        pending.activityLabel = "Searching…"

        let result = TranscriptMerge.apply(
            [assistantRow(2, turn: "t1", text: "The answer.")], to: [user, pending], announce: true
        )

        let messages = try requireCount(result.messages, 2)
        #expect(messages[1].id == pending.id, "the placeholder keeps its id: a teardown would break follow-scroll")
        #expect(messages[1].seq == 2)
        #expect(messages[1].text == "The answer.")
        #expect(messages[1].status == .done)
        #expect(messages[1].answers == ["t1"])
        #expect(result.effects.cancelPolls == [pending.id])
        #expect(result.effects.completedTurnIds == ["t1"])
        // Clearing the streamed text is the store's job, through the cancelled
        // ids (`clearTurnProgress`); the merge does not reach into it.
        #expect(messages[1].streamedText == "par")
    }

    @Test func anAssistantRowResolvesThePlaceholderForATurnItAnswers() throws {
        // The placeholder still carries the original turn A; the reply finishes
        // a fresh turn B and says it answers A.
        let pending = placeholder(turn: "A")

        let result = TranscriptMerge.apply(
            [assistantRow(2, turn: "B", text: "Second time lucky.", answers: ["A"])],
            to: [pending], announce: true
        )

        let messages = try requireCount(result.messages, 1)
        #expect(messages[0].id == pending.id)
        #expect(messages[0].text == "Second time lucky.")
        #expect(result.effects.cancelPolls == [pending.id])
    }

    /// After a failed turn the user taps Retry: the same placeholder is sent
    /// again and takes the new turn id B, while the server's reply row names B
    /// as its own turn and the original A as the one it answers.
    @Test func aRetryAfterAnErrorResolvesThePlaceholderCarryingTheNewTurn() throws {
        var pending = placeholder(turn: "B")
        pending.status = .running

        let result = TranscriptMerge.apply(
            [assistantRow(4, turn: "B", text: "Fixed.", answers: ["A"])], to: [pending], announce: true
        )

        let messages = try requireCount(result.messages, 1)
        #expect(messages[0].id == pending.id)
        #expect(messages[0].status == .done)
        #expect(messages[0].text == "Fixed.")
        #expect(result.effects.completedTurnIds == ["B"])
    }

    @Test func aMergedSiblingPlaceholderIsRemoved() throws {
        // Two sends coalesced: turn t1 merged into t2, and one reply answers both.
        let u1 = localUser("first")
        let p1 = placeholder(turn: "t1", pairedWith: u1)
        let u2 = localUser("second")
        let p2 = placeholder(turn: "t2", pairedWith: u2)

        let result = TranscriptMerge.apply(
            [assistantRow(3, turn: "t2", answers: ["t1", "t2"])], to: [u1, p1, u2, p2], announce: true
        )

        let messages = try requireCount(result.messages, 3)
        #expect(messages.map(\.id) == [u1.id, u2.id, p2.id], "one reply must not sit under two bubbles")
        #expect(messages[2].seq == 3)
        #expect(Set(result.effects.cancelPolls) == [p1.id, p2.id])
        #expect(result.effects.completedTurnIds == ["t2"], "one sound for one reply")
    }

    @Test func aReplyRowNeverRemovesAPlaceholderItDoesNotAnswer() throws {
        let other = placeholder(turn: "unrelated")
        let mine = placeholder(turn: "t2")

        let result = TranscriptMerge.apply(
            [assistantRow(3, turn: "t2")], to: [other, mine], announce: true
        )

        #expect(try requireCount(result.messages, 2)[0] == other)
    }

    @Test func aRowWithNoPlaceholderIsInsertedWithTheRowsOwnId() throws {
        let row = assistantRow(7, turn: "cron-1", text: "**Morning.**", answers: [], source: "cron")

        let result = TranscriptMerge.apply([row], to: [], announce: true)

        let messages = try requireCount(result.messages, 1)
        #expect(messages[0].id == UUID(uuidString: row.id))
        #expect(messages[0].role == .assistant)
        #expect(messages[0].text == "**Morning.**")
        #expect(messages[0].status == .done)
        #expect(messages[0].turnId == "cron-1")
        #expect(messages[0].seq == 7)
        #expect(result.effects.completedTurnIds == ["cron-1"])
        #expect(result.effects.cancelPolls.isEmpty)
    }

    @Test func anErrorRowWithNoTextSaysSomethingWentWrong() throws {
        let result = TranscriptMerge.apply(
            [assistantRow(2, turn: "t1", text: "", status: .error)], to: [placeholder(turn: "t1")], announce: true
        )

        let messages = try requireCount(result.messages, 1)
        #expect(messages[0].status == .error)
        #expect(messages[0].text == "Something went wrong.")
        #expect(result.effects.completedTurnIds.isEmpty, "the receive sound is for a reply that landed")
    }

    @Test func anErrorRowKeepsTheTextTheServerGaveIt() throws {
        let result = TranscriptMerge.apply(
            [assistantRow(2, turn: "t1", text: "claude exited 1", status: .error)], to: [], announce: true
        )

        let messages = try requireCount(result.messages, 1)
        #expect(messages[0].status == .error)
        #expect(messages[0].text == "claude exited 1")
    }

    // MARK: - Announcing

    @Test func announceFalseEmitsNoCompletions() throws {
        let pending = placeholder(turn: "t1")

        let result = TranscriptMerge.apply(
            [assistantRow(1, turn: "old"), assistantRow(2, turn: "t1")], to: [pending], announce: false
        )

        #expect(result.messages.count == 2)
        #expect(result.effects.completedTurnIds.isEmpty, "a fresh install must not play 100 receive sounds")
        #expect(result.effects.cancelPolls == [pending.id], "silence is about sounds, not about stopping polls")
    }

    /// The placeholder's own poll got to `.done` first and already sounded. The
    /// row that arrives afterwards must only confirm it.
    @Test func aPlaceholderAlreadyDoneDoesNotCompleteAgain() throws {
        let done = placeholder(turn: "t1", status: .done, text: "Hi.")

        let result = TranscriptMerge.apply([assistantRow(2, turn: "t1", text: "Hi.")], to: [done], announce: true)

        let messages = try requireCount(result.messages, 1)
        #expect(messages[0].id == done.id)
        #expect(messages[0].seq == 2)
        #expect(result.effects.completedTurnIds.isEmpty, "the receive sound would play twice for one reply")
    }

    @Test func aPlaceholderThatGaveUpWithAnErrorStillAnnouncesTheRealReply() {
        let gaveUp = placeholder(turn: "t1", status: .error, text: "Marlow lost track of this message.")

        let result = TranscriptMerge.apply([assistantRow(2, turn: "t1", text: "Here it is.")], to: [gaveUp], announce: true)

        #expect(result.messages[0].status == .done)
        #expect(result.messages[0].text == "Here it is.")
        #expect(result.effects.completedTurnIds == ["t1"], "the user never heard this one")
    }

    // MARK: - Placement and idempotence

    @Test func aCronRowLandsAboveThisPhonesStillPendingSend() throws {
        let earlier = serverMessage(seq: 3)
        let user = localUser("what's the weather")
        let pending = placeholder(turn: nil, status: .sending, pairedWith: user)

        let result = TranscriptMerge.apply(
            [assistantRow(4, turn: "cron-1", answers: [], source: "cron")],
            to: [earlier, user, pending], announce: true
        )

        let messages = try requireCount(result.messages, 4)
        #expect(messages[0].id == earlier.id)
        #expect(messages[1].turnId == "cron-1", "in server order, above the unconfirmed send")
        #expect(messages[2].id == user.id)
        #expect(messages[3].id == pending.id)
    }

    /// `ReplyFollow` follows a user bubble and the reply streaming under it as
    /// an adjacent pair. A row that lands mid-stream goes below the live
    /// placeholder, not between the two.
    @Test(arguments: [ChatMessage.Status.sending, .running, .stalled])
    func aLiveRowDoesNotLandBetweenAConfirmedSendAndItsStreamingReply(
        status: ChatMessage.Status
    ) throws {
        var user = localUser("question")
        user.seq = 10
        let streaming = placeholder(turn: "t1", status: status, pairedWith: user)

        let result = TranscriptMerge.apply(
            [assistantRow(11, turn: "cron-1", answers: [], source: "cron")],
            to: [user, streaming], announce: true
        )

        let messages = try requireCount(result.messages, 3)
        #expect(messages[0].id == user.id)
        #expect(messages[1].id == streaming.id, "the reply stays directly under its confirmed send")
        #expect(messages[2].turnId == "cron-1")
    }

    @Test func aRowStillLandsAboveAnUnconfirmedSendWhoseReplyIsStreaming() throws {
        // The unconfirmed user message (no `seq`) is not a confirmed send: the
        // row goes above it, as in server order.
        let earlier = serverMessage(seq: 3)
        let user = localUser("question")
        let streaming = placeholder(turn: "t1", pairedWith: user)

        let result = TranscriptMerge.apply(
            [assistantRow(4, turn: "cron-1", answers: [], source: "cron")],
            to: [earlier, user, streaming], announce: true
        )

        #expect(result.messages.map(\.id) == [earlier.id, result.messages[1].id, user.id, streaming.id])
        #expect(result.messages[1].turnId == "cron-1")
    }

    @Test func aFinishedUnconfirmedReplyDoesNotHoldARowBack() throws {
        // Only a reply still in flight keeps its place; one that already
        // finished (awaiting only its row) is not streaming under anything.
        var user = localUser("question")
        user.seq = 10
        let finished = placeholder(turn: "t1", status: .done, text: "Hi.", pairedWith: user)

        let result = TranscriptMerge.apply(
            [assistantRow(11, turn: "cron-1", answers: [], source: "cron")],
            to: [user, finished], announce: true
        )

        #expect(result.messages.map(\.turnId) == [nil, "cron-1", "t1"])
    }

    @Test func aCronRowIsAboveALocalSendWhenNothingElseIsConfirmed() throws {
        let user = localUser()
        let pending = placeholder(turn: nil, status: .sending, pairedWith: user)

        let result = TranscriptMerge.apply(
            [assistantRow(1, turn: "cron-1", answers: [], source: "cron")], to: [user, pending], announce: true
        )

        #expect(try requireCount(result.messages, 3)[0].turnId == "cron-1")
    }

    @Test func rowsLandInServerOrderWhicheverOrderTheyArriveIn() throws {
        let newer = serverMessage(seq: 12)
        let older = serverMessage(seq: 10)

        let result = TranscriptMerge.apply(
            [assistantRow(11, turn: "mid", answers: []), assistantRow(5, turn: "ancient", answers: [])],
            to: [older, newer], announce: false
        )

        #expect(result.messages.map(\.seq) == [5, 10, 11, 12])
    }

    @Test func rowsAreAppliedInAscendingSeqEvenIfHandedOverDescending() {
        // The bootstrap page arrives newest-first; the store reverses it, but
        // the merge must not depend on that for its placeholders to pair up.
        let result = TranscriptMerge.apply(
            [assistantRow(6, turn: "tX"), userRow(5, turn: "tX")], to: [], announce: false
        )

        #expect(result.messages.map(\.role) == [.user, .assistant])
        #expect(result.effects.startPolls.isEmpty)
    }

    @Test func reapplyingAPageChangesNothing() {
        let rows = [userRow(1, turn: "tX"), assistantRow(2, turn: "tX"), assistantRow(3, turn: "c", answers: [])]
        let first = TranscriptMerge.apply(rows, to: [], announce: true)

        let second = TranscriptMerge.apply(rows, to: first.messages, announce: true)

        #expect(second.messages == first.messages)
        #expect(second.effects.completedTurnIds.isEmpty)
        #expect(second.effects.startPolls.isEmpty)
        #expect(second.effects.cancelPolls.isEmpty)
        #expect(second.effects.fetchAttachments.isEmpty)
    }

    // MARK: - Decoding

    // The rows below keep `"source": "ios"` on purpose: they are stored rows
    // from before the server's `ios` -> `chat` rename (30-day retention), so the
    // decoder must keep accepting the old value.
    @Test func anUnknownRoleStatusOrKeyDoesNotFailThePage() throws {
        let json = """
        {"rows": [
          {"seq": 1, "id": "\(UUID().uuidString)", "role": "tool", "source": "ios", "turn_id": "t0",
           "client_msg_id": null, "text": "?", "status": null, "created_at": 1.0,
           "answers": [], "attachments": [], "a_future_key": {"nested": true}},
          {"seq": 2, "id": "\(UUID().uuidString)", "role": "assistant", "source": "ios", "turn_id": "t1",
           "client_msg_id": null, "text": "ok", "status": "half-done", "created_at": 2.0,
           "answers": ["t1"], "attachments": [{"id": "a1", "mime": "image/png", "extra": 1}]},
          {"seq": 3, "id": "\(UUID().uuidString)", "role": "user", "source": "ios", "turn_id": "t1",
           "client_msg_id": "c", "text": "hi", "status": null, "created_at": 3.0,
           "answers": [], "attachments": []}
        ], "latest_seq": 3, "has_more": true, "another_future_key": 1}
        """

        let page = try JSONDecoder().decode(TranscriptPage.self, from: Data(json.utf8))

        #expect(page.rows.map(\.role) == [.other, .assistant, .user])
        #expect(page.rows[1].status == .error, "an unknown status is an error, as ChatMessage.Status treats it")
        #expect(page.rows[1].attachments == [.init(id: "a1", mime: "image/png")])
        #expect(page.latestSeq == 3)
        #expect(page.hasMore)

        let merged = TranscriptMerge.apply(page.rows, to: [], announce: false)
        #expect(!merged.messages.contains { $0.seq == 1 }, "an unknown role is skipped, not rendered as a bubble")
        #expect(merged.messages.contains { $0.seq == 2 })
    }

    @Test func aRowWithOnlyTheRequiredKeysStillDecodes() throws {
        let json = #"{"rows": [{"seq": 4, "id": "x", "role": "user"}], "latest_seq": 4}"#

        let page = try JSONDecoder().decode(TranscriptPage.self, from: Data(json.utf8))

        #expect(page.rows[0].answers.isEmpty)
        #expect(page.rows[0].attachments.isEmpty)
        #expect(page.rows[0].status == nil)
        #expect(page.hasMore == false)
    }

    @Test func aMessageCachedBeforeTheNewFieldsDecodesWithNilForThem() throws {
        let json = """
        {"id": "\(UUID().uuidString)", "role": "user", "text": "old", "createdAt": 0,
         "turnId": null, "status": "done", "clientMsgId": null}
        """

        let message = try JSONDecoder().decode(ChatMessage.self, from: Data(json.utf8))

        #expect(message.seq == nil)
        #expect(message.attachmentId == nil)
        #expect(message.attachmentIds == nil)
        #expect(message.imageFilenames == nil)
        #expect(message.answers == nil)
    }
}
