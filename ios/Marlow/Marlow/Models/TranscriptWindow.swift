import Foundation

/// Which part of the transcript `ChatView` builds rows for.
///
/// The rows are eager (see the `ChatView` type comment for why that cannot go
/// back to `LazyVStack`), so every row in the window is built and its markdown
/// parsed before the first frame. Rendering the whole transcript made a cold
/// launch blank for half a second to a second by 2026-09-25, and that grows
/// with every day of use. Mike's rule (2026-09-06, ios-transcript-scaling.md):
/// about 48 hours needs to be there immediately; anything older may take a
/// visible pause to appear.
///
/// A window over the intact array, not a pruned one: the local transcript is
/// the only durable copy of the conversation (ios-transcript-scaling.md), so
/// nothing here removes a message, it only decides whether a row is built.
///
/// The boundary is a *date*, not an index or a message id, because
/// `ChatStore.ingest` inserts by `createdAt` rather than appending — a late
/// cron turn can land mid-transcript, which would shift an index, and a date
/// needs no bookkeeping to stay right when that happens.
enum TranscriptWindow {
    static let span: TimeInterval = 48 * 60 * 60
    /// A floor on rows shown, so a quiet weekend doesn't open onto one message
    /// with no context above it. Small enough that it costs nothing at launch.
    static let minimumCount = 30

    /// The earliest `createdAt` to render, covering `span` back from
    /// `reference` and at least `minimumCount` rows before `end` — or `nil`
    /// when that reaches the start of the transcript, meaning show everything.
    ///
    /// `end` and `reference` are what make this one step both at launch
    /// (`messages.count`, now) and on "show earlier" (the first shown row's
    /// index, that row's own date), so each tap reveals the 48 hours before
    /// what is already on screen rather than jumping to the whole history.
    static func floor(for messages: [ChatMessage], before end: Int, reference: Date) -> Date? {
        let end = min(max(end, 0), messages.count)
        let cutoff = reference.addingTimeInterval(-span)
        var byTime = end
        // From the end backwards, stopping at the first row older than the
        // cutoff: the transcript is time-ordered (see `ingest`), and an
        // out-of-order row left by an older build only makes the window a
        // little smaller, never hides a row the count floor would show.
        while byTime > 0, messages[byTime - 1].createdAt >= cutoff {
            byTime -= 1
        }
        let start = min(byTime, max(end - minimumCount, 0))
        return start == 0 ? nil : messages[start].createdAt
    }

    /// Index of the first row to render under `floor`. A scan, not a stored
    /// index, for the same insert-by-time reason as the type comment.
    static func startIndex(of messages: [ChatMessage], floor: Date?) -> Int {
        guard let floor else { return 0 }
        return messages.firstIndex { $0.createdAt >= floor } ?? messages.count
    }
}
