import Foundation

/// A single line in the chat transcript: a cache of the server's rows
/// (`seq != nil`) plus whatever this phone has made that the server has not yet
/// confirmed (`seq == nil`: a sent bubble, its assistant placeholder). The
/// server owns the conversation (docs/tasks/ios-server-transcript.md); a
/// message's `id` is the one thing that never changes, whichever side made it,
/// because `ChatView`'s scroll and follow behaviour is keyed on it.
struct ChatMessage: Identifiable, Codable, Equatable {
    enum Role: String, Codable {
        case user
        case assistant
    }

    enum Status: String, Codable {
        case sending
        case running
        /// The fast poll window closed without an answer. Deliberately **not**
        /// terminal: the message text tells the user checking continues in the
        /// background, and this case is what keeps that true — the slow poll
        /// phase covers it while the app runs, and
        /// `ChatStore.reconcilePendingMessages` resumes it across a relaunch.
        /// Persisting it as `.error` instead would strand a recoverable turn
        /// behind text promising work that had silently stopped.
        case stalled
        case done
        case error

        /// `ChatStore.load()` decodes the whole array at once and falls back to
        /// empty, so one unreadable row discards the entire history. A status
        /// this build doesn't know means the transcript was written by a newer
        /// one — treat it as `.error` rather than throwing away the chat.
        init(from decoder: any Decoder) throws {
            let raw = try decoder.singleValueContainer().decode(String.self)
            self = Status(rawValue: raw) ?? .error
        }
    }

    let id: UUID
    var role: Role
    var text: String
    let createdAt: Date
    var turnId: String?
    var status: Status
    /// Set only on the assistant placeholder; identifies the paired `.user`
    /// message (by its `id.uuidString`) so a `.sending`/`.running` message
    /// found on disk at launch can be resumed rather than left stuck.
    var clientMsgId: String?
    /// Set only on a `.user` message that carried an image
    /// (docs/tasks/ios-image-input.md). The transcript's own JSON can't hold
    /// the image inline, so this is a filename inside `ChatStore`'s attachment
    /// directory in Application Support, not the bytes themselves. `Optional`
    /// so a transcript written before this field existed decodes fine with
    /// nil — no image is exactly what an old row means.
    ///
    /// LEGACY, read-only in practice: a build before multi-image messages wrote
    /// the one image here. New code writes `imageFilenames`; read both through
    /// `allImageFilenames`. Kept as a stored property (not a decode shim) so
    /// the synthesized decoder still reads an old cache untouched.
    var imageFilename: String? = nil
    /// Current reassurance copy for an in-flight turn. Optional keeps
    /// transcripts written by older app versions backwards-compatible.
    var activityLabel: String? = nil
    /// The reply so far, as it is being typed out
    /// (docs/tasks/streaming-reply-text.md). Only ever set while `.running`;
    /// `text` takes over from the server's authoritative `result_text` the
    /// moment the turn is done. Optional for the same backwards-compatibility
    /// reason as `activityLabel` — a synthesized decoder does not apply a
    /// default to a missing key unless the property is Optional, and one
    /// unreadable row discards the entire transcript (`ChatStore.load()`).
    var streamedText: String? = nil
    /// Offsets into `streamedText` where a tool call interrupted the reply, so
    /// the bubble can break between two thoughts instead of running them
    /// together. Only the marks already reached by the typing cursor.
    var streamedTextMarks: [Int]? = nil
    /// The server row backing this message, once there is one. `nil` while the
    /// message is local-only — the marker `TranscriptMerge` uses to tell an
    /// outbox bubble or a placeholder still waiting on its reply from a row
    /// the server has confirmed. Optional so a cache written before this field
    /// existed decodes with nil.
    var seq: Int? = nil
    /// The server's attachment for a row whose image this phone does not have
    /// yet; `imageFilename` is set once it has been downloaded. Optional for
    /// the same decode-compatibility reason as the other additions.
    ///
    /// LEGACY like `imageFilename`: new code writes `attachmentIds`; read both
    /// through `allAttachmentIds`.
    var attachmentId: String? = nil
    /// An assistant message backed by a server row remembers the user-row
    /// `turn_id`s that row resolved (`TranscriptRow.answers`), so a later
    /// merge can tell a user row that is already answered from one still in
    /// flight, across a relaunch. Stored on the message rather than kept as a
    /// set beside it because the cache already persists `messages`, and a
    /// side table would have to be persisted and pruned in step.
    var answers: [String]? = nil
    /// Set only on a `.user` message sent by "Do it" on an idea
    /// (docs/tasks/ideas-panel.md), so a retry or a relaunch resend carries the
    /// same `idea_id` the first attempt did. Optional for the same
    /// decode-compatibility reason as the other additions.
    var ideaId: String? = nil
    /// The idea a "Do it" answers, drawn as a quote block above the text. Set
    /// locally on the optimistic bubble from the idea tapped, and by the
    /// server's row for a "Do it" sent from elsewhere. Optional for the same
    /// decode-compatibility reason as the other additions.
    var quote: IdeaQuote? = nil
    /// Every image of a `.user` message, in attach order, as filenames inside
    /// `ChatStore`'s attachment directory (multi-image messages, up to
    /// `ChatMessage.maxImages`). Optional for the same decode-compatibility
    /// reason as the other additions; an old cache's single image is in
    /// `imageFilename`.
    var imageFilenames: [String]? = nil
    /// The server's attachments for a row whose images this phone does not
    /// have (all of them, in row order); each lands as `<id>.jpg` and is added
    /// to `imageFilenames` once downloaded.
    var attachmentIds: [String]? = nil

    /// Images per message. One constant for the composer's cap, the picker's
    /// `maxSelectionCount` and the tests; the server's `_MAX_IMAGES` is the same
    /// number and the authority (chat-ux-contract.md CMP-3).
    static let maxImages = 6

    /// Columns of the bubble's image grid for `count` images: one image is shown
    /// whole (a single column), 2 and 4 pair up, 3 and 5-6 run three across
    /// (so 3 is one row, 5 is 3+2, 6 is 3+3).
    static func gridColumns(forImageCount count: Int) -> Int {
        switch count {
        case ...1: return 1
        case 2, 4: return 2
        default: return 3
        }
    }

    /// Image filenames, old single-image field included.
    var allImageFilenames: [String] {
        if let imageFilenames { return imageFilenames }
        return imageFilename.map { [$0] } ?? []
    }

    /// Attachment ids, old single-id field included.
    var allAttachmentIds: [String] {
        if let attachmentIds { return attachmentIds }
        return attachmentId.map { [$0] } ?? []
    }

    /// Attachment ids whose file this message does not list yet: the ones a
    /// fetch still has to bring down.
    var undownloadedAttachmentIds: [String] {
        let have = Set(allImageFilenames)
        return allAttachmentIds.filter { !have.contains("\($0).jpg") }
    }
}
