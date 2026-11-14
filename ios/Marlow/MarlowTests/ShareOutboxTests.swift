import Foundation
import Testing
@testable import Marlow

/// The share outbox (docs/tasks/share-and-files.md, Phase 3): what the share
/// extension leaves in the App Group container when the box could not be
/// reached, for the app to send. One directory per message, written whole and
/// renamed into place, for the reason `TurnInbox` keeps one file per turn:
/// several extension instances can run at once, and the app can drain while
/// one is still writing.
struct ShareOutboxTests {

    private func makeOutbox() -> ShareOutbox {
        let dir = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("ShareOutboxTests-\(UUID().uuidString)", isDirectory: true)
        return ShareOutbox(directory: dir)
    }

    private func entry(
        id: String = UUID().uuidString, text: String = "have a look", at: Date = Date(),
        photos: [Data] = [Data([0xFF, 0xD8, 0xFF, 0xD9])],
        files: [StagedFile] = [StagedFile(name: "Booking.pdf", mime: "application/pdf", data: Data("%PDF-1.7".utf8))]
    ) -> ShareOutbox.Entry {
        ShareOutbox.Entry(clientMsgId: id, text: text, createdAt: at, photos: photos, files: files)
    }

    // MARK: - Write, read, delete

    @Test func anEntryRoundTripsWithItsTextPhotosAndFiles() throws {
        let outbox = makeOutbox()
        let saved = entry(text: "see \u{201C}this\u{201D}\nand that")
        try outbox.store(saved)

        #expect(outbox.pendingIds() == [saved.clientMsgId])
        let loaded = try #require(outbox.load(id: saved.clientMsgId))
        #expect(loaded.clientMsgId == saved.clientMsgId, "the saved id is what the app sends under, so the server can dedupe")
        #expect(loaded.text == saved.text)
        #expect(loaded.photos == saved.photos)
        #expect(loaded.files.map(\.name) == ["Booking.pdf"])
        #expect(loaded.files.map(\.mime) == ["application/pdf"])
        #expect(loaded.files.map(\.data) == saved.files.map(\.data))
    }

    @Test func aFileKeepsItsNameWhateverItIsCalled() throws {
        let outbox = makeOutbox()
        let hostile = StagedFile(name: "../../escape \"quoted\".pdf", mime: "", data: Data([1]))
        let saved = entry(photos: [], files: [hostile])
        try outbox.store(saved)

        // The name is data in the manifest, never a path.
        #expect(try #require(outbox.load(id: saved.clientMsgId)).files.first?.name == "../../escape \"quoted\".pdf")
    }

    @Test func removeDeletesTheWholeEntry() throws {
        let outbox = makeOutbox()
        let saved = entry()
        try outbox.store(saved)
        outbox.remove(id: saved.clientMsgId)

        #expect(outbox.pendingIds().isEmpty)
        #expect(outbox.load(id: saved.clientMsgId) == nil)
    }

    /// Oldest first: a person's two shares arrive in the order they made them.
    @Test func pendingIsOldestFirst() throws {
        let outbox = makeOutbox()
        let older = entry(at: Date(timeIntervalSince1970: 1_000))
        let newer = entry(at: Date(timeIntervalSince1970: 2_000))
        try outbox.store(newer)
        try outbox.store(older)

        #expect(outbox.pendingIds() == [older.clientMsgId, newer.clientMsgId])
    }

    /// The id becomes a directory name.
    @Test func anIdThatIsNotUUIDShapedIsRefused() {
        let outbox = makeOutbox()
        #expect(throws: (any Error).self) { try outbox.store(entry(id: "../../etc")) }
        #expect(outbox.load(id: "../../etc") == nil)
        outbox.remove(id: "../../etc")   // must not delete anything, or crash
    }

    /// A write cut short (the extension killed for memory) leaves a partial
    /// directory. It is never an entry.
    @Test func aHalfWrittenEntryIsNotPending() throws {
        let outbox = makeOutbox()
        let dir = try #require(outbox.directory)
        let partial = dir.appendingPathComponent(".tmp-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: partial, withIntermediateDirectories: true)
        try Data([1]).write(to: partial.appendingPathComponent("i0.jpg"))

        #expect(outbox.pendingIds().isEmpty)
    }

    @Test func aDirectoryWithNoManifestIsNotAnEntryToSend() throws {
        let outbox = makeOutbox()
        let dir = try #require(outbox.directory)
        let id = UUID().uuidString
        try FileManager.default.createDirectory(at: dir.appendingPathComponent(id), withIntermediateDirectories: true)

        #expect(outbox.load(id: id) == nil)
    }

    @Test func clearRemovesEverything() throws {
        let outbox = makeOutbox()
        try outbox.store(entry())
        try outbox.store(entry())
        outbox.clear()
        #expect(outbox.pendingIds().isEmpty)
    }

    /// No App Group container (an unsigned build): nothing to read, and a write
    /// says so rather than pretending.
    @Test func withoutAContainerItIsEmptyAndRefusesToStore() {
        let outbox = ShareOutbox(directory: nil)
        #expect(outbox.pendingIds().isEmpty)
        #expect(throws: (any Error).self) { try outbox.store(entry()) }
    }

    // MARK: - Drain

    @Test func aSentEntryIsDeleted() async throws {
        let outbox = makeOutbox()
        let saved = entry()
        try outbox.store(saved)

        let report = await outbox.drain { _ in .sent }

        #expect(report == ShareOutbox.DrainReport(sent: 1, rejected: [], kept: 0))
        #expect(outbox.pendingIds().isEmpty)
    }

    /// A 4xx: a retry cannot fix it, so the entry goes, and the detail is
    /// handed back for a notice.
    @Test func aRejectedEntryIsDeletedAndReported() async throws {
        let outbox = makeOutbox()
        try outbox.store(entry())

        let report = await outbox.drain { _ in .rejected(detail: "files[0] is too large") }

        #expect(report.sent == 0)
        #expect(report.rejected == ["files[0] is too large"])
        #expect(outbox.pendingIds().isEmpty)
    }

    @Test func aTransientFailureKeepsTheEntryForTheNextDrain() async throws {
        let outbox = makeOutbox()
        let saved = entry()
        try outbox.store(saved)

        let report = await outbox.drain { _ in .transient }

        #expect(report == ShareOutbox.DrainReport(sent: 0, rejected: [], kept: 1))
        #expect(outbox.pendingIds() == [saved.clientMsgId])
    }

    /// The network that failed the first is not going to take the second:
    /// stop, keep the rest, in order.
    @Test func aTransientFailureStopsTheDrainAndKeepsTheRest() async throws {
        let outbox = makeOutbox()
        let first = entry(at: Date(timeIntervalSince1970: 1))
        let second = entry(at: Date(timeIntervalSince1970: 2))
        try outbox.store(first)
        try outbox.store(second)
        let attempted = Attempts()

        let report = await outbox.drain { entry in
            attempted.append(entry.clientMsgId)
            return .transient
        }

        #expect(attempted.values == [first.clientMsgId])
        #expect(report.kept == 2)
        #expect(outbox.pendingIds() == [first.clientMsgId, second.clientMsgId])
    }

    /// The saved `client_msg_id` is what makes a repeated send safe: the
    /// server dedups on it. A drain that failed and a later one that worked
    /// must say the same id.
    @Test func everyAttemptSendsTheSameClientMsgId() async throws {
        let outbox = makeOutbox()
        let saved = entry()
        try outbox.store(saved)
        let seen = Attempts()

        _ = await outbox.drain { entry in seen.append(entry.clientMsgId); return .transient }
        _ = await outbox.drain { entry in seen.append(entry.clientMsgId); return .transient }
        let report = await outbox.drain { entry in seen.append(entry.clientMsgId); return .sent }

        #expect(seen.values == Array(repeating: saved.clientMsgId, count: 3))
        #expect(report.sent == 1)
        #expect(outbox.pendingIds().isEmpty)
    }

    /// An entry that was already drained and deleted is not sent again by a
    /// second drain that started while the first was working.
    @Test func aDrainOfAnEmptyOutboxSendsNothing() async {
        let outbox = makeOutbox()
        let attempted = Attempts()
        let report = await outbox.drain { entry in attempted.append(entry.clientMsgId); return .sent }
        #expect(attempted.values.isEmpty)
        #expect(report == ShareOutbox.DrainReport(sent: 0, rejected: [], kept: 0))
    }
}

final class Attempts: @unchecked Sendable {
    private let lock = NSLock()
    private var stored: [String] = []
    func append(_ value: String) { lock.withLock { stored.append(value) } }
    var values: [String] { lock.withLock { stored } }
}
