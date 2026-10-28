import Foundation

/// The reply the notification extension already fetched, left on disk for the
/// app to render without asking the server again.
///
/// The problem it solves is not bandwidth, it is latency the user can feel.
/// Tapping a `turn_ready` banner relaunches or resumes the app, which finds a
/// `.running` placeholder on disk and restarts polling — and `pollLoop` sleeps
/// its interval *before* the first request, so the bubble sits empty for
/// 500ms plus a round trip while the phone re-downloads text the banner had
/// already shown. Mike reported that as the irritant this exists to remove.
///
/// **This is a cache, never the source of truth.** `GET /v1/turn/{id}` has
/// no side effects, and the transcript sync still brings every row, so
/// nothing here changes what the app ends up showing. An
/// inbox entry that is stale, corrupt, or missing costs a poll, which is
/// exactly what happened before this existed. Nothing downstream may assume
/// an entry is present.
///
/// One file per turn rather than one array, for the same reason
/// `NotificationDiagnostics` keeps one file per run: iOS can run several
/// service extension instances concurrently when pushes land together, and a
/// read-modify-write of a shared array silently drops whichever finished
/// first. Here that would lose the reply and reintroduce the wait.
enum TurnInbox {
    /// Small on purpose. This holds the last few replies for the seconds
    /// between a push and the tap that follows it — it is not an offline
    /// transcript, and the app has its own persisted one. Prunes oldest-first
    /// by write time.
    static let maxEntries = 20

    private static var directoryURL: URL? {
        FileManager.default
            .containerURL(forSecurityApplicationGroupIdentifier: NotificationShared.appGroup)?
            .appendingPathComponent("turn-inbox", isDirectory: true)
    }

    /// Extension-side. `response` is the raw `GET /v1/turn/{id}` body, stored
    /// verbatim so the app can decode it with its own `Turn` — the extension
    /// deliberately owns no copy of that model, which is what stops the two
    /// from drifting into disagreeing about the same bytes.
    static func store(turnId: String, response: Data) {
        guard let directoryURL, let name = filename(for: turnId) else { return }
        try? FileManager.default.createDirectory(
            at: directoryURL,
            withIntermediateDirectories: true,
            attributes: [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication]
        )
        // The extension runs on a locked phone, so the default
        // (`.complete`) would make this unwritable exactly when it is needed.
        try? response.write(
            to: directoryURL.appendingPathComponent(name),
            options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication]
        )
        prune()
    }

    /// App-side. Every stored response body, keyed by turn id.
    static func load() -> [String: Data] {
        guard let directoryURL,
              let names = try? FileManager.default.contentsOfDirectory(atPath: directoryURL.path)
        else { return [:] }
        var entries: [String: Data] = [:]
        for name in names where name.hasSuffix(".json") {
            guard let data = try? Data(contentsOf: directoryURL.appendingPathComponent(name)) else {
                continue
            }
            entries[String(name.dropLast(5))] = data
        }
        return entries
    }

    /// Called once the app has the turn in its own persisted transcript.
    /// Deleting earlier would race the write; deleting later is harmless, and
    /// pruning bounds it anyway.
    static func remove(turnId: String) {
        guard let directoryURL, let name = filename(for: turnId) else { return }
        try? FileManager.default.removeItem(at: directoryURL.appendingPathComponent(name))
    }

    /// Called on unpair, alongside the credential it was fetched with.
    static func clear() {
        guard let directoryURL else { return }
        try? FileManager.default.removeItem(at: directoryURL)
    }

    /// A turn id is a UUID, and this one arrived over the network. It becomes
    /// a filename, so anything that is not UUID-shaped is refused rather than
    /// sanitised — the same rule, and the same reason, as the extension's
    /// check before pasting it into a URL path.
    private static func filename(for turnId: String) -> String? {
        guard !turnId.isEmpty,
              turnId.allSatisfy({ $0.isHexDigit || $0 == "-" })
        else { return nil }
        return "\(turnId).json"
    }

    /// Best-effort and unsynchronised: the worst two concurrent prunes can do
    /// is both delete an already-old entry, and a lost entry costs a poll.
    private static func prune() {
        guard let directoryURL,
              let names = try? FileManager.default.contentsOfDirectory(atPath: directoryURL.path),
              names.count > maxEntries
        else { return }
        let dated = names.map { name -> (String, Date) in
            let url = directoryURL.appendingPathComponent(name)
            let modified = (try? url.resourceValues(forKeys: [.contentModificationDateKey]))?
                .contentModificationDate
            return (name, modified ?? .distantPast)
        }
        for (name, _) in dated.sorted(by: { $0.1 < $1.1 }).prefix(names.count - maxEntries) {
            try? FileManager.default.removeItem(at: directoryURL.appendingPathComponent(name))
        }
    }
}
