import Foundation

/// What the line under Marlow's avatar says: nothing in particular, or what
/// the turn in flight is doing right now.
///
/// The per-message playback (`ChatStore`'s `ActivityPlaybackState`) already
/// paces and coalesces the labels; this only picks WHICH message's label the
/// header shows. It used to sit inside that message's row as a spinner and a
/// line of text, which scrolled away with the row. The header does not.
enum ChatPresence: Equatable {
    case idle
    case active(String)

    /// How far back from the tail to look for a turn in flight. Placeholders
    /// are appended at send time, so a live one is always among the newest
    /// rows. Bounding the search keeps an idle transcript, where nothing
    /// matches, from costing a full walk on every publish.
    static let searchWindow = 20

    /// The OLDEST in-flight reply wins, not the newest. The server runs one
    /// turn at a time, so with two sends queued the older one is the one
    /// actually working, and the newer sits on its default "Thinking…" label.
    /// Showing the newest would say "Thinking…" while the real turn searches
    /// the web.
    ///
    /// `.stalled` is left out on purpose. Its row carries its own explanation
    /// (the fast poll window closed and checking continues in the background),
    /// and a header still claiming "Thinking…" half an hour later would be a
    /// claim nobody is checking.
    static func of(_ messages: some BidirectionalCollection<ChatMessage>) -> ChatPresence {
        guard let reply = messages.suffix(searchWindow).first(where: isInFlightReply) else {
            return .idle
        }

        if reply.status == .sending { return .active("Sending…") }
        let label = reply.activityLabel ?? TurnActivity.thinking
        // The reply is already typing itself into the transcript, so
        // "Thinking…" would contradict what is on screen.
        if label == TurnActivity.thinking, !(reply.streamedText ?? "").isEmpty {
            return .active(TurnActivity.writing)
        }
        return .active(label)
    }

    private static func isInFlightReply(_ message: ChatMessage) -> Bool {
        guard message.role == .assistant else { return false }
        switch message.status {
        case .sending, .running: return true
        case .stalled, .done, .error: return false
        }
    }
}
