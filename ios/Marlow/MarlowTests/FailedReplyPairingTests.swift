import Foundation
import Testing
@testable import Marlow

/// The failed-send layout moves Retry onto Mike's bubble. The failure worth
/// guarding is Retry vanishing: a failed reply that pairs with nothing on
/// screen must keep its own button, which it does by being absent here.
struct FailedReplyPairingTests {

    private static func user() -> ChatMessage {
        ChatMessage(id: UUID(), role: .user, text: "hi", createdAt: Date(), turnId: nil, status: .done)
    }

    private static func reply(to user: ChatMessage?, _ status: ChatMessage.Status) -> ChatMessage {
        ChatMessage(
            id: UUID(), role: .assistant, text: "Something went wrong.", createdAt: Date(),
            turnId: nil, status: status, clientMsgId: user?.id.uuidString
        )
    }

    @Test func aFailedReplyPairsWithItsMessage() {
        let mine = Self.user()
        let failed = Self.reply(to: mine, .error)
        #expect(ChatView.failedReplies(in: [mine, failed]) == [mine.id: failed.id])
    }

    @Test func aReplyThatDidNotFailPairsWithNothing() {
        let mine = Self.user()
        #expect(ChatView.failedReplies(in: [mine, Self.reply(to: mine, .done)]).isEmpty)
    }

    @Test func aFailedReplyWhoseMessageScrolledOutKeepsItsOwnRetry() {
        let offScreen = Self.user()
        #expect(ChatView.failedReplies(in: [Self.reply(to: offScreen, .error)]).isEmpty)
    }

    @Test func aFailedCronTurnPairsWithNothing() {
        #expect(ChatView.failedReplies(in: [Self.user(), Self.reply(to: nil, .error)]).isEmpty)
    }
}
