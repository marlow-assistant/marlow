import Foundation
import Testing
@testable import Marlow

/// The header is the one place a working turn is described once it has
/// scrolled off screen, so the failure worth guarding is it describing the
/// WRONG turn, or claiming work nobody is doing.
struct ChatPresenceTests {

    private static func message(
        _ role: ChatMessage.Role, _ status: ChatMessage.Status,
        label: String? = nil, streamed: String? = nil
    ) -> ChatMessage {
        var m = ChatMessage(
            id: UUID(), role: role, text: "", createdAt: Date(), turnId: nil, status: status
        )
        m.activityLabel = label
        m.streamedText = streamed
        return m
    }

    @Test func anEmptyTranscriptIsIdle() {
        #expect(ChatPresence.of([ChatMessage]()) == .idle)
    }

    @Test func aFinishedTranscriptIsIdle() {
        let messages = [
            Self.message(.user, .done), Self.message(.assistant, .done), Self.message(.assistant, .error),
        ]
        #expect(ChatPresence.of(messages) == .idle)
    }

    @Test func sendingSaysSending() {
        let messages = [Self.message(.user, .done), Self.message(.assistant, .sending)]
        #expect(ChatPresence.of(messages) == .active("Sending…"))
    }

    @Test func runningWithNoLabelSaysThinking() {
        let messages = [Self.message(.user, .done), Self.message(.assistant, .running)]
        #expect(ChatPresence.of(messages) == .active(TurnActivity.thinking))
    }

    @Test func runningShowsTheToolLabel() {
        let messages = [Self.message(.assistant, .running, label: "Searching the web…")]
        #expect(ChatPresence.of(messages) == .active("Searching the web…"))
    }

    @Test func streamingTextNeverSaysThinking() {
        let messages = [Self.message(.assistant, .running, label: TurnActivity.thinking, streamed: "Sure —")]
        #expect(ChatPresence.of(messages) == .active(TurnActivity.writing))
    }

    /// Text, then back to a tool mid-answer: the label is the only thing
    /// explaining why the text stopped.
    @Test func aToolLabelSurvivesStreamedText() {
        let messages = [
            Self.message(.assistant, .running, label: "Checking your calendar…", streamed: "Let me look."),
        ]
        #expect(ChatPresence.of(messages) == .active("Checking your calendar…"))
    }

    @Test func stalledIsNotClaimedAsWork() {
        let messages = [Self.message(.assistant, .stalled, label: "Searching the web…")]
        #expect(ChatPresence.of(messages) == .idle)
    }

    /// The server runs one turn at a time: with two queued, the older is the
    /// one actually working.
    @Test func theOldestInFlightReplyWins() {
        let messages = [
            Self.message(.user, .done),
            Self.message(.assistant, .running, label: "Searching the web…"),
            Self.message(.user, .done),
            Self.message(.assistant, .running),
        ]
        #expect(ChatPresence.of(messages) == .active("Searching the web…"))
    }

    @Test func onlyTheTailIsSearched() {
        let stranded = Self.message(.assistant, .running, label: "Reading a file…")
        let done = (0..<ChatPresence.searchWindow).map { _ in Self.message(.assistant, .done) }
        #expect(ChatPresence.of([stranded] + done) == .idle)
    }
}
