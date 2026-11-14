import Foundation

/// The caps on one message, photos and files together. **The server is the
/// authority**: `server/app.py` enforces each of these and answers a breach
/// with a 400 or 413 after the upload has already been paid for. The client
/// mirrors them only to refuse early, with a notice, and it mirrors the
/// *counts and sizes* and never the type. There is no type list to mirror:
/// the server sniffs the bytes (docs/tasks/share-and-files.md, "The type
/// rule"), so a new file type never needs a client release.
///
/// Compiled into the app and the share extension.
enum AttachmentLimits {
    /// Items per message, photos and files combined. `_MAX_IMAGES` in
    /// `server/app.py`, made one shared cap.
    static let maxItems = 6
    /// Decoded bytes of one file. `server/app.py` answers a larger one with a
    /// 413. Photos keep the server's own 6MB, which the downscaler never nears.
    static let maxFileBytes = 20 * 1024 * 1024
    /// Decoded bytes of the whole message, photos and files together; also
    /// `server/app.py`.
    static let maxMessageBytes = 25 * 1024 * 1024
}

/// A file waiting to go out. `mime` is the platform's own answer
/// (`UTType.preferredMIMEType`) or "": the server decides nothing from it.
struct StagedFile: Equatable, Identifiable {
    let id: UUID
    var name: String
    var mime: String
    var data: Data

    init(id: UUID = UUID(), name: String, mime: String, data: Data) {
        self.id = id
        self.name = name
        self.mime = mime
        self.data = data
    }

    var byteCount: Int { data.count }
}

/// What the composer, or the share sheet, has staged for the next send: photos
/// (already downscaled JPEG) and files, in the order they were added, under
/// the caps above. Pure, so the rules are tested without a view.
struct StagedAttachments: Equatable {
    enum Item: Equatable, Identifiable {
        case photo(id: UUID, data: Data)
        case file(StagedFile)

        var id: UUID {
            switch self {
            case .photo(let id, _): return id
            case .file(let file): return file.id
            }
        }

        var byteCount: Int {
            switch self {
            case .photo(_, let data): return data.count
            case .file(let file): return file.byteCount
            }
        }
    }

    /// Why an item was not staged. The notice is the whole reply to the user:
    /// there is no "unsupported type" case, on purpose.
    enum Refusal: Equatable {
        case tooMany
        case fileTooLarge(name: String)
        case messageTooLarge

        var notice: String {
            switch self {
            case .tooMany:
                return "You can attach up to \(AttachmentLimits.maxItems) items to a message."
            case .fileTooLarge(let name):
                return "\u{201C}\(name)\u{201D} is over \(AttachmentLimits.maxFileBytes / (1024 * 1024))MB."
            case .messageTooLarge:
                return "That would put the message over \(AttachmentLimits.maxMessageBytes / (1024 * 1024))MB."
            }
        }
    }

    private(set) var items: [Item] = []

    var count: Int { items.count }
    var isEmpty: Bool { items.isEmpty }
    var byteCount: Int { items.reduce(0) { $0 + $1.byteCount } }

    /// The downscaled photos, in order: the wire's `images`.
    var photos: [Data] {
        items.compactMap { if case .photo(_, let data) = $0 { return data } else { return nil } }
    }

    /// The files, in order: the wire's `files`.
    var files: [StagedFile] {
        items.compactMap { if case .file(let file) = $0 { return file } else { return nil } }
    }

    /// Whether a file of this size could be staged, from its size alone, so a
    /// caller can refuse before reading its bytes.
    func refusal(forFileNamed name: String, byteCount: Int) -> Refusal? {
        if count >= AttachmentLimits.maxItems { return .tooMany }
        if byteCount > AttachmentLimits.maxFileBytes { return .fileTooLarge(name: name) }
        if self.byteCount + byteCount > AttachmentLimits.maxMessageBytes { return .messageTooLarge }
        return nil
    }

    /// Returns why it was not staged, or nil. Count first, then size, so a
    /// full tray says so whatever the next item weighs.
    @discardableResult
    mutating func addPhoto(_ jpeg: Data) -> Refusal? {
        if count >= AttachmentLimits.maxItems { return .tooMany }
        if byteCount + jpeg.count > AttachmentLimits.maxMessageBytes { return .messageTooLarge }
        items.append(.photo(id: UUID(), data: jpeg))
        return nil
    }

    @discardableResult
    mutating func addFile(_ file: StagedFile) -> Refusal? {
        if let refusal = refusal(forFileNamed: file.name, byteCount: file.byteCount) { return refusal }
        items.append(.file(file))
        return nil
    }

    /// By id, never by position or content: a second tap before the re-render
    /// must not remove whatever slid into the slot, and two photos can be
    /// byte-identical.
    mutating func remove(id: UUID) {
        items.removeAll { $0.id == id }
    }
}
