import Foundation
import Testing
@testable import Marlow

/// The idea quoted above Mike's "Do it" bubble (docs/tasks/ideas-panel.md): the
/// server's `quote` on a transcript row, the optimistic bubble's own copy, and
/// the merge between the two.
struct IdeaQuoteTests {
    private func rows(_ json: String) throws -> [TranscriptRow] {
        try JSONDecoder().decode(TranscriptPage.self, from: Data(json.utf8)).rows
    }

    /// One user row, with `quote` spliced in as raw JSON, or the key left out.
    private func page(quote: String?) -> String {
        let extra = quote.map { #", "quote": \#($0)"# } ?? ""
        return """
        {"rows": [{"seq": 1, "id": "11111111-1111-1111-1111-111111111111", "role": "user",
          "source": "chat", "turn_id": "t1", "client_msg_id": null, "text": "Do it", "status": null,
          "created_at": 5.0, "answers": [], "attachments": []\(extra)}],
         "latest_seq": 1, "has_more": false}
        """
    }

    private func quoted(_ id: String = "a") -> IdeaQuote {
        IdeaQuote(ideaId: id, emoji: "x", title: "Title \(id)")
    }

    private func localDoIt(quote: IdeaQuote?) -> ChatMessage {
        ChatMessage(
            id: UUID(), role: .user, text: "Do it", createdAt: Date(), turnId: nil, status: .done,
            clientMsgId: nil, ideaId: "a", quote: quote
        )
    }

    private func serverRow(clientMsgId: String?, quote: IdeaQuote?, seq: Int = 1) -> TranscriptRow {
        TranscriptRow(
            seq: seq, id: UUID().uuidString, role: .user, turnId: "t1", clientMsgId: clientMsgId,
            text: "Do it", createdAt: 5, quote: quote
        )
    }

    // MARK: Decoding

    @Test func aRowDecodesItsQuote() throws {
        let decoded = try rows(page(quote: #"{"idea_id": "a", "emoji": "x", "title": "Draft the invites"}"#))
        #expect(decoded[0].quote == IdeaQuote(ideaId: "a", emoji: "x", title: "Draft the invites"))
    }

    @Test func aNullQuoteAndAnAbsentOneBothDecodeAsNone() throws {
        #expect(try rows(page(quote: "null"))[0].quote == nil)
        #expect(try rows(page(quote: nil))[0].quote == nil, "an older server never sends the key")
    }

    @Test func aMalformedQuoteDropsAloneAndTheRowSurvives() throws {
        let decoded = try rows(page(quote: #"{"idea_id": "a"}"#))
        #expect(decoded.count == 1)
        #expect(decoded[0].quote == nil)
        #expect(decoded[0].text == "Do it")
    }

    // MARK: Merge

    @Test func theServersRowMergesIntoTheOptimisticBubbleAndKeepsItsQuote() throws {
        let user = localDoIt(quote: quoted())
        let pending = ChatMessage(
            id: UUID(), role: .assistant, text: "", createdAt: Date(), turnId: "t1", status: .running,
            clientMsgId: user.id.uuidString
        )

        let result = TranscriptMerge.apply(
            [serverRow(clientMsgId: user.id.uuidString, quote: quoted())], to: [user, pending], announce: true
        )

        let messages = try requireCount(result.messages, 2)
        #expect(messages[0].id == user.id, "folded into the bubble on screen, not added beside it")
        #expect(messages[0].seq == 1)
        #expect(messages[0].quote == quoted())
    }

    @Test func aServerRowWithoutAQuoteDoesNotTakeTheLocalOneAway() throws {
        let user = localDoIt(quote: quoted())

        let result = TranscriptMerge.apply(
            [serverRow(clientMsgId: user.id.uuidString, quote: nil)], to: [user], announce: true
        )

        #expect(try requireCount(result.messages, 1)[0].quote == quoted(), "the bubble must not change under Mike's eyes")
    }

    @Test func aRowFillsAQuoteTheLocalBubbleLacked() throws {
        let user = localDoIt(quote: nil)

        let result = TranscriptMerge.apply(
            [serverRow(clientMsgId: user.id.uuidString, quote: quoted())], to: [user], announce: true
        )

        #expect(try requireCount(result.messages, 1)[0].quote == quoted())
    }

    @Test func aDoItSentFromAnotherDeviceArrivesQuoted() {
        let result = TranscriptMerge.apply(
            [serverRow(clientMsgId: nil, quote: quoted("b"), seq: 4)], to: [], announce: true
        )

        #expect(result.messages.first(where: { $0.role == .user })?.quote == quoted("b"))
    }

    // MARK: Persistence

    @Test func aMessageKeepsItsQuoteInTheCacheAndOldCachesStillDecode() throws {
        let data = try JSONEncoder().encode(localDoIt(quote: quoted()))
        #expect(try JSONDecoder().decode(ChatMessage.self, from: data).quote == quoted())

        var object = try #require(try JSONSerialization.jsonObject(with: data) as? [String: Any])
        object.removeValue(forKey: "quote")
        let old = try JSONSerialization.data(withJSONObject: object)
        #expect(try JSONDecoder().decode(ChatMessage.self, from: old).quote == nil)
    }
}
