import Foundation
import Testing
@testable import Marlow

// MARK: - Talk about it through the chat path

@MainActor
struct TodoSendTests {
    private func quote() -> IdeaQuote {
        IdeaQuote(todoId: "t3", emoji: TodoFormat.quoteEmoji, title: "Book the dentist")
    }

    @Test func talkAboutSendsTheTodoIdAndTheQuoteAndAnOrdinarySendDoesNot() async throws {
        let fake = FakeChatAPI()
        await fake.stubSend(.success(MessageResponse(turnId: "t1")))
        await fake.stubTurns([.success(.fixture(turnId: "t1", status: "done", resultText: "Sure."))])
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        store.send(text: TodoFormat.talkAboutMessage, todoId: "t3", quote: quote())
        await awaitSettled(store)
        store.send(text: "thanks")
        await awaitSettled(store)

        let sent = try requireCount(await fake.sentMessages, 2)
        #expect(sent[0].text == "About this to-do")
        #expect(sent[0].todoId == "t3")
        #expect(sent[0].ideaId == nil)
        #expect(sent[1].todoId == nil)
        #expect(store.messages.first?.quote == quote(), "the bubble quotes the to-do at once, before any sync")
    }

    @Test func aRetryResendsUnderTheSameTodoId() async throws {
        let fake = FakeChatAPI()
        await fake.stubSend(.failure(URLError(.notConnectedToInternet)))
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test(sendMaxAttempts: 1)
        )
        defer { store.cancelActiveWork() }

        store.send(text: TodoFormat.talkAboutMessage, todoId: "t3", quote: quote())
        await awaitSettled(store)
        let failed = try requireCount(store.messages, 2)
        #expect(failed[1].status == .error)

        await fake.stubSend(.success(MessageResponse(turnId: "t2")))
        await fake.stubTurns([.success(.fixture(turnId: "t2", status: "done", resultText: "Sure."))])
        store.retry(failed[1].id)
        await awaitSettled(store)

        let sent = try requireCount(await fake.sentMessages, 2)
        #expect(sent[1].todoId == "t3", "the item must not be lost to a flaky first attempt")
    }
}
