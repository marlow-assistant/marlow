import Foundation
import Testing
@testable import Marlow

/// The quote above a bubble carries an idea or a to-do (docs/tasks/todos-and-reminders.md,
/// "talk about it"); `IdeaQuoteTests` covers the idea path end to end.
struct TodoQuoteTests {
    private func rows(_ quote: String) throws -> [TranscriptRow] {
        try JSONDecoder().decode(TranscriptPage.self, from: Data("""
        {"rows": [{"seq": 1, "id": "11111111-1111-1111-1111-111111111111", "role": "user",
          "source": "chat", "turn_id": "t1", "client_msg_id": null, "text": "About this to-do", "status": null,
          "created_at": 5.0, "answers": [], "attachments": [], "quote": \(quote)}],
         "latest_seq": 1, "has_more": false}
        """.utf8)).rows
    }

    @Test func aRowDecodesATodoQuote() throws {
        let decoded = try rows(#"{"todo_id": "t3", "emoji": "☑️", "title": "Book the dentist"}"#)
        let quote = try #require(decoded[0].quote)
        #expect(quote.todoId == "t3")
        #expect(quote.ideaId == nil)
        #expect(quote.emoji == "\u{2611}\u{FE0F}")
        #expect(quote.title == "Book the dentist")
    }

    @Test func anIdeaQuoteStillDecodesWithNoTodoId() throws {
        let decoded = try rows(#"{"idea_id": "a", "emoji": "x", "title": "Draft the invites"}"#)
        let quote = try #require(decoded[0].quote)
        #expect(quote.ideaId == "a")
        #expect(quote.todoId == nil)
        #expect(quote == IdeaQuote(ideaId: "a", emoji: "x", title: "Draft the invites"))
    }

    @Test func aQuoteWithNeitherIdOrWithoutItsWordsReadsAsAbsentAndTheRowSurvives() throws {
        for bad in [#"{"emoji": "x", "title": "t"}"#, #"{"todo_id": "t3"}"#, #"{"todo_id": 3, "emoji": "x", "title": "t"}"#] {
            let decoded = try rows(bad)
            #expect(decoded.count == 1, "\(bad)")
            #expect(decoded[0].quote == nil, "\(bad)")
        }
    }

    @Test func aTodoQuoteIsBuiltFromTheTodoTapped() {
        let item = Todo(
            id: "t3", title: "Book the dentist", notes: nil, due: nil, state: .open, source: "user",
            created: Date(timeIntervalSince1970: 0), closed: nil, snoozedUntil: nil
        )
        #expect(IdeaQuote(item) == IdeaQuote(todoId: "t3", emoji: TodoFormat.quoteEmoji, title: "Book the dentist"))
        #expect(TodoFormat.quoteEmoji == "\u{2611}\u{FE0F}", "the server writes the same emoji on its row")
    }

    @Test func aMessageKeepsATodoQuoteAndTheTodoIdInTheCacheAndAnOldCacheStillDecodes() throws {
        let sent = ChatMessage(
            id: UUID(), role: .user, text: TodoFormat.talkAboutMessage, createdAt: Date(), turnId: nil, status: .done,
            clientMsgId: nil, todoId: "t3", quote: IdeaQuote(todoId: "t3", emoji: "x", title: "Book the dentist")
        )
        let data = try JSONEncoder().encode(sent)
        let back = try JSONDecoder().decode(ChatMessage.self, from: data)
        #expect(back.quote == sent.quote)
        #expect(back.todoId == "t3")

        // A cache written before to-dos: an idea quote, no `todoId` key.
        var object = try #require(try JSONSerialization.jsonObject(with: data) as? [String: Any])
        object.removeValue(forKey: "todoId")
        object["quote"] = ["idea_id": "a", "emoji": "x", "title": "Draft the invites"]
        let old = try JSONDecoder().decode(ChatMessage.self, from: try JSONSerialization.data(withJSONObject: object))
        #expect(old.todoId == nil)
        #expect(old.quote == IdeaQuote(ideaId: "a", emoji: "x", title: "Draft the invites"))
    }

    @Test func anIdeaQuoteEncodesAsBeforeWithNoTodoKey() throws {
        let data = try JSONEncoder().encode(IdeaQuote(ideaId: "a", emoji: "x", title: "t"))
        let object = try #require(try JSONSerialization.jsonObject(with: data) as? [String: Any])
        #expect(Set(object.keys) == ["idea_id", "emoji", "title"])
    }

    @Test func aTodoQuoteMergesIntoTheOptimisticBubbleAndSurvivesARowWithoutOne() throws {
        let quoted = IdeaQuote(todoId: "t3", emoji: "x", title: "Book the dentist")
        let user = ChatMessage(
            id: UUID(), role: .user, text: "About this to-do", createdAt: Date(), turnId: nil, status: .done,
            clientMsgId: nil, todoId: "t3", quote: quoted
        )
        let row = TranscriptRow(
            seq: 1, id: UUID().uuidString, role: .user, turnId: "t1", clientMsgId: user.id.uuidString,
            text: "About this to-do", createdAt: 5, quote: nil
        )

        let result = TranscriptMerge.apply([row], to: [user], announce: true)

        #expect(result.messages.first?.quote == quoted)
    }
}
