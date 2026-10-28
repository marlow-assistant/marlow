import Foundation

/// Folds server transcript rows into the phone's `messages`
/// (docs/tasks/ios-server-transcript.md). Pure: no store, no network, no
/// clock, so every rule can be pinned by a test and `ChatStore` only has to
/// carry out the `Effects` it returns.
///
/// **Local identity wins.** A message this phone created keeps its own `id` for
/// life, and the server row that confirms it is folded into it. Only a row with
/// no local counterpart becomes a new `ChatMessage`, with the row's own id.
/// Swapping the `id` of a message already on screen is a row teardown in
/// `ChatView`, and mid-stream it would break follow-scroll.
enum TranscriptMerge {
    /// What the store must do besides render `messages`. Kept out of the merge
    /// because each one touches tasks, queues or the network.
    struct Effects {
        /// Turn ids for `completedAssistantTurns` — the receive sound. Only for
        /// a reply that newly landed, and never on the bootstrap fetch.
        var completedTurnIds: [String] = []
        /// A placeholder inserted for a user row nothing has answered: poll its
        /// turn, so a reply sent from another device streams here.
        var startPolls: [(messageId: UUID, turnId: String)] = []
        /// One entry per message, carrying every image attachment of its row.
        var fetchAttachments: [(messageId: UUID, attachmentIds: [String])] = []
        /// Placeholders a row resolved or removed: their poll has nothing left
        /// to learn, and left running would report the turn a second time.
        var cancelPolls: [UUID] = []
    }

    static let genericError = "Something went wrong."

    /// `announce` is false for the bootstrap fetch (an empty cache) and for
    /// earlier pages, so neither plays a receive sound per row it backfills.
    static func apply(
        _ rows: [TranscriptRow], to messages: [ChatMessage], announce: Bool
    ) -> (messages: [ChatMessage], effects: Effects) {
        var messages = messages
        var effects = Effects()
        // A row already on a message is skipped, which is what makes a page
        // idempotent: a retried or overlapping fetch changes nothing.
        var seen = Set(messages.compactMap(\.seq))
        let ordered = rows.sorted { $0.seq < $1.seq }

        // Turns an assistant row in this batch answers, including rows later in
        // the batch than the user row asking. A user row from another device
        // arrives in the same page as its reply whenever the phone was away.
        var answeredInBatch = Set<String>()
        for row in ordered where row.role == .assistant {
            answeredInBatch.insert(row.turnId)
            answeredInBatch.formUnion(row.answers)
        }

        for row in ordered {
            guard row.role != .other, seen.insert(row.seq).inserted else { continue }
            switch row.role {
            case .user:
                mergeUser(row, into: &messages, effects: &effects, answeredInBatch: answeredInBatch)
            case .assistant:
                mergeAssistant(row, into: &messages, effects: &effects, announce: announce)
            case .other:
                break
            }
        }
        return (messages, effects)
    }

    // MARK: - User rows

    private static func mergeUser(
        _ row: TranscriptRow, into messages: inout [ChatMessage], effects: inout Effects,
        answeredInBatch: Set<String>
    ) {
        // This phone's own send: the server row confirms the bubble that is
        // already on screen. Nothing else about it changes — in particular the
        // local `imageFilenames`, so the photo is not downloaded back.
        if let clientMsgId = row.clientMsgId, let uuid = UUID(uuidString: clientMsgId),
           let index = messages.firstIndex(where: { $0.role == .user && $0.id == uuid }) {
            messages[index].seq = row.seq
            // The bubble already has the quote the tap built; the server's only
            // fills a gap (a send from before this build). A row without one
            // never removes it: the idea may have stopped being open between
            // the tap and the server's check, and the bubble should not change
            // under Mike's eyes.
            messages[index].quote = messages[index].quote ?? row.quote
            return
        }

        let id = UUID(uuidString: row.id) ?? UUID()
        if let index = messages.firstIndex(where: { $0.id == id }) {
            messages[index].seq = messages[index].seq ?? row.seq
            messages[index].quote = messages[index].quote ?? row.quote
            return
        }

        var user = ChatMessage(
            id: id, role: .user, text: row.text,
            createdAt: Date(timeIntervalSince1970: row.createdAt),
            turnId: row.turnId, status: .done, clientMsgId: nil
        )
        user.seq = row.seq
        user.quote = row.quote
        // Every image, in row order -- not just the first. A non-image
        // attachment is skipped, as before.
        let imageIds = row.attachments.filter { $0.mime.hasPrefix("image/") }.map(\.id)
        if !imageIds.isEmpty {
            user.attachmentIds = imageIds
            effects.fetchAttachments.append((messageId: id, attachmentIds: imageIds))
        }
        let index = insertionIndex(forSeq: row.seq, in: messages)
        messages.insert(user, at: index)

        // "In flight" is exactly "no assistant row answers this turn". Checked
        // against what is already merged as well as the batch: the reply may
        // have arrived in an earlier page, or already be a placeholder here.
        guard !answeredInBatch.contains(row.turnId), !isAnswered(row.turnId, in: messages) else { return }
        let placeholder = ChatMessage(
            id: UUID(), role: .assistant, text: "",
            createdAt: Date(timeIntervalSince1970: row.createdAt),
            turnId: row.turnId, status: .running, clientMsgId: nil
        )
        messages.insert(placeholder, at: index + 1)
        effects.startPolls.append((messageId: placeholder.id, turnId: row.turnId))
    }

    /// Whether any assistant message already stands for `turnId` — a server
    /// row that answers it, or a placeholder polling it.
    private static func isAnswered(_ turnId: String, in messages: [ChatMessage]) -> Bool {
        messages.contains {
            $0.role == .assistant && ($0.turnId == turnId || ($0.answers?.contains(turnId) ?? false))
        }
    }

    // MARK: - Assistant rows

    private static func mergeAssistant(
        _ row: TranscriptRow, into messages: inout [ChatMessage], effects: inout Effects,
        announce: Bool
    ) {
        let status: ChatMessage.Status = row.status == .error ? .error : .done
        let text = status == .error && row.text.isEmpty ? genericError : row.text

        // The placeholder this reply finishes: by its own turn first, then by a
        // turn it answers. The second is the retry — the row's turn is the
        // fresh one B and it answers the original A, but the placeholder may
        // still be carrying A (docs/tasks/server-transcript.md).
        let waiting = { (message: ChatMessage) in message.role == .assistant && message.seq == nil }
        let index = messages.firstIndex { waiting($0) && $0.turnId == row.turnId }
            ?? messages.firstIndex { message in
                waiting(message) && (message.turnId.map { row.answers.contains($0) } ?? false)
            }

        guard let index else {
            var message = ChatMessage(
                id: UUID(uuidString: row.id) ?? UUID(), role: .assistant, text: text,
                createdAt: Date(timeIntervalSince1970: row.createdAt),
                turnId: row.turnId, status: status, clientMsgId: nil
            )
            message.seq = row.seq
            message.answers = row.answers
            messages.insert(message, at: insertionIndex(forSeq: row.seq, in: messages))
            if status == .done && announce { effects.completedTurnIds.append(row.turnId) }
            return
        }

        // A placeholder its own poll already finished has sounded: the row
        // only confirms it. Announcing again would play the receive sound a
        // second time for one reply. One that is not `.done` — still running,
        // or an error the poll gave up with — has not.
        let alreadySounded = messages[index].status == .done
        messages[index].seq = row.seq
        messages[index].text = text
        messages[index].status = status
        messages[index].answers = row.answers
        let resolved = messages[index].id
        effects.cancelPolls.append(resolved)
        if status == .done && announce && !alreadySounded {
            effects.completedTurnIds.append(row.turnId)
        }

        // Every other placeholder for a turn this reply answers was merged into
        // it. The poll does the same on a `merged` status; the row may simply
        // get there first, and one reply must not sit under two bubbles.
        let merged = messages.filter {
            waiting($0) && $0.id != resolved && ($0.turnId.map { row.answers.contains($0) } ?? false)
        }.map(\.id)
        if !merged.isEmpty {
            messages.removeAll { merged.contains($0.id) }
            effects.cancelPolls.append(contentsOf: merged)
        }
    }

    // MARK: - Placement

    /// Right after the last message a server row backs with a lower `seq`, or
    /// at the start if there is none. A still-unconfirmed user message that
    /// already followed that point stays after the insert — which is what puts
    /// a cron briefing above this phone's unconfirmed send, in server order.
    ///
    /// The exception is a live reply placeholder (local-only, `.sending`,
    /// `.running` or `.stalled`) directly under a confirmed user message: the
    /// insert goes below it. Without that, a row landing mid-stream sat between
    /// a confirmed send and the reply streaming under it, and `ReplyFollow`
    /// follows that user bubble and reply as an adjacent pair
    /// (review, 2026-10-01).
    ///
    /// By `seq`, never by arrival and never by clock. On 2026-09-09 Mike
    /// unlocked to the morning briefing with the *nightly consolidate* — the
    /// older turn — rendered under it, and a 30-minute timestamp divider on the
    /// older message then read as a lie: the notification extension's prefetch
    /// and the later drain did not deliver turns in the order they were made.
    /// The server is the only writer, so its order is the display order, and
    /// rows are applied oldest-first so receive sounds queue the same way.
    private static func insertionIndex(forSeq seq: Int, in messages: [ChatMessage]) -> Int {
        guard var index = messages.lastIndex(where: { ($0.seq ?? Int.max) < seq }).map({ $0 + 1 })
        else { return 0 }
        while index < messages.count, isLivePlaceholder(messages[index]) { index += 1 }
        return index
    }

    private static func isLivePlaceholder(_ message: ChatMessage) -> Bool {
        guard message.role == .assistant, message.seq == nil else { return false }
        switch message.status {
        case .sending, .running, .stalled: return true
        case .done, .error: return false
        }
    }
}
