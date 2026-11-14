import Foundation

/// Messages the share extension could not deliver, left in the App Group
/// container for the app to send (docs/tasks/share-and-files.md, Phase 3).
///
/// A share is made from inside another app, often with no signal, and the
/// extension is closed the moment it returns. "Saved, it'll send when you open
/// Marlow" has to be true, so what it saved has to survive until the app next
/// runs and finds it.
///
/// **One directory per message, never one shared file**, for the reason
/// `TurnInbox` keeps one file per turn: iOS can run several extension
/// instances at once, and a read-modify-write of a shared index silently
/// drops whichever finished first. Here that is a message the user was told
/// was saved.
///
/// **Written whole and renamed into place**, because the app can drain while
/// an extension is still writing, and a directory with a manifest but half its
/// photos would be sent as a message missing them (CMP-4: never the caption
/// with some of its attachments). The extension can also be killed mid-write
/// for memory; that leaves a `.tmp-` directory, which is never an entry.
///
/// Inside, `message.json` says what the message was and the bytes sit beside
/// it as they are (`i<n>.jpg`, `f<n>.dat`): no base64 on disk, and a file's
/// name is manifest data, never a path.
///
/// Like `TurnInbox` this is not a cache. It is the only copy of what the user
/// shared, so nothing prunes it: an entry leaves on a 2xx, on a 4xx (which a
/// retry can't fix), or on sign-out.
struct ShareOutbox {
    /// What one queued message holds: what `POST /v1/message` needs to be
    /// sent again, under the id the first attempt used.
    struct Entry {
        let clientMsgId: String
        var text: String
        var createdAt: Date
        /// Downscaled JPEGs, in order: the wire's `images`.
        var photos: [Data]
        /// The wire's `files`.
        var files: [StagedFile]
    }

    enum Outcome: Equatable {
        case sent
        /// Offline, a timeout or a 5xx: keep it, try again later.
        case transient
        /// A 4xx: a retry can't fix it.
        case rejected(detail: String?)
    }

    struct DrainReport: Equatable {
        var sent: Int
        /// The server's detail for each entry it refused ("" when it gave none).
        var rejected: [String]
        /// Entries still in the outbox when the drain stopped.
        var kept: Int
    }

    private struct Manifest: Codable {
        struct FileRecord: Codable {
            var filename: String
            var name: String
            var mime: String
        }
        var clientMsgId: String
        var text: String
        var createdAt: Double
        var photos: [String]
        var files: [FileRecord]
    }

    struct StoreError: Error {}

    /// Nil where there is no App Group container (an unsigned build): the
    /// outbox is then empty and refuses to store.
    let directory: URL?

    init(directory: URL? = ShareOutbox.appGroupDirectory) {
        self.directory = directory
    }

    static var appGroupDirectory: URL? {
        FileManager.default
            .containerURL(forSecurityApplicationGroupIdentifier: NotificationShared.appGroup)?
            .appendingPathComponent("share-outbox", isDirectory: true)
    }

    static let shared = ShareOutbox()

    private static let manifestName = "message.json"

    // MARK: - Extension side

    /// Writes the whole entry or none of it.
    func store(_ entry: Entry) throws {
        guard let directory, Self.isValid(id: entry.clientMsgId) else { throw StoreError() }
        let fm = FileManager.default
        let staging = directory.appendingPathComponent(".tmp-\(entry.clientMsgId)", isDirectory: true)
        try? fm.removeItem(at: staging)
        // The extension runs on a locked phone: the default protection
        // (`.complete`) would make this unwritable exactly when it is needed.
        try fm.createDirectory(
            at: staging, withIntermediateDirectories: true,
            attributes: [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication]
        )
        do {
            let options: Data.WritingOptions = [.atomic, .completeFileProtectionUntilFirstUserAuthentication]
            var photoNames: [String] = []
            for (index, photo) in entry.photos.enumerated() {
                let name = "i\(index).jpg"
                try photo.write(to: staging.appendingPathComponent(name), options: options)
                photoNames.append(name)
            }
            var records: [Manifest.FileRecord] = []
            for (index, file) in entry.files.enumerated() {
                let name = "f\(index).dat"
                try file.data.write(to: staging.appendingPathComponent(name), options: options)
                records.append(.init(filename: name, name: file.name, mime: file.mime))
            }
            let manifest = Manifest(
                clientMsgId: entry.clientMsgId, text: entry.text,
                createdAt: entry.createdAt.timeIntervalSince1970, photos: photoNames, files: records
            )
            // Last: an entry with no manifest is not one.
            try JSONEncoder().encode(manifest)
                .write(to: staging.appendingPathComponent(Self.manifestName), options: options)
            let final = directory.appendingPathComponent(entry.clientMsgId, isDirectory: true)
            try? fm.removeItem(at: final)
            try fm.moveItem(at: staging, to: final)
        } catch {
            try? fm.removeItem(at: staging)
            throw error
        }
    }

    // MARK: - App side

    /// Entry ids, oldest first.
    func pendingIds() -> [String] {
        guard let directory,
              let names = try? FileManager.default.contentsOfDirectory(atPath: directory.path)
        else { return [] }
        removeStaleStaging(in: directory, names: names)
        return names
            .filter { Self.isValid(id: $0) }
            // The manifest alone: this runs on every foreground, and the photos
            // and files beside it can be 25MB.
            .compactMap { id in manifest(id: id).map { (id, $0.createdAt) } }
            .sorted { $0.1 < $1.1 }
            .map(\.0)
    }

    private func manifest(id: String) -> Manifest? {
        guard let directory, Self.isValid(id: id),
              let data = try? Data(contentsOf: directory
                .appendingPathComponent(id, isDirectory: true)
                .appendingPathComponent(Self.manifestName)),
              let manifest = try? JSONDecoder().decode(Manifest.self, from: data),
              manifest.clientMsgId == id
        else { return nil }
        return manifest
    }

    func load(id: String) -> Entry? {
        guard let directory, let manifest = manifest(id: id) else { return nil }
        let dir = directory.appendingPathComponent(id, isDirectory: true)
        var photos: [Data] = []
        for name in manifest.photos {
            guard Self.isPlainFilename(name),
                  let photo = try? Data(contentsOf: dir.appendingPathComponent(name)) else { return nil }
            photos.append(photo)
        }
        var files: [StagedFile] = []
        for record in manifest.files {
            guard Self.isPlainFilename(record.filename),
                  let bytes = try? Data(contentsOf: dir.appendingPathComponent(record.filename)) else { return nil }
            files.append(StagedFile(name: record.name, mime: record.mime, data: bytes))
        }
        return Entry(
            clientMsgId: id, text: manifest.text, createdAt: Date(timeIntervalSince1970: manifest.createdAt),
            photos: photos, files: files
        )
    }

    func remove(id: String) {
        guard let directory, Self.isValid(id: id) else { return }
        try? FileManager.default.removeItem(at: directory.appendingPathComponent(id, isDirectory: true))
    }

    /// Called on sign-out, alongside the credential these would be sent with:
    /// they are message content.
    func clear() {
        guard let directory else { return }
        try? FileManager.default.removeItem(at: directory)
    }

    /// Sends each entry, oldest first, with `send`. A sent or rejected entry is
    /// deleted; a transient failure keeps it and **stops**, since the network
    /// that failed this one will not take the next.
    func drain(send: (Entry) async -> Outcome) async -> DrainReport {
        var report = DrainReport(sent: 0, rejected: [], kept: 0)
        let ids = pendingIds()
        for (position, id) in ids.enumerated() {
            guard let entry = load(id: id) else { continue }
            switch await send(entry) {
            case .sent:
                remove(id: id)
                report.sent += 1
            case .rejected(let detail):
                remove(id: id)
                report.rejected.append(detail ?? "")
            case .transient:
                report.kept = ids.count - position
                return report
            }
        }
        return report
    }

    // MARK: - Names

    /// A client_msg_id is a UUID and becomes a directory name, so anything not
    /// UUID-shaped is refused rather than sanitised (as `TurnInbox` does).
    private static func isValid(id: String) -> Bool {
        UUID(uuidString: id) != nil
    }

    /// The manifest is a file this process wrote, but a name read back from
    /// disk is still not a path.
    private static func isPlainFilename(_ name: String) -> Bool {
        !name.isEmpty && !name.contains("/") && !name.hasPrefix(".")
    }

    /// An extension killed mid-write leaves its `.tmp-` directory behind, and
    /// it can hold 25MB. An hour is long past any live write.
    private func removeStaleStaging(in directory: URL, names: [String]) {
        for name in names where name.hasPrefix(".tmp-") {
            let url = directory.appendingPathComponent(name)
            let modified = (try? url.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate
            if let modified, Date().timeIntervalSince(modified) > 3_600 {
                try? FileManager.default.removeItem(at: url)
            }
        }
    }
}
