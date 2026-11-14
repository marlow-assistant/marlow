import Foundation
import Testing
@testable import Marlow

/// The app's half of the share extension (docs/tasks/share-and-files.md,
/// Phase 3): on launch and on becoming active it sends what the extension
/// left in the outbox, under the saved `client_msg_id`, and the server's dedup
/// makes a repeat safe.
@MainActor
struct ShareOutboxDrainTests {

    private func makeOutbox() -> ShareOutbox {
        ShareOutbox(directory: URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("ShareDrainTests-\(UUID().uuidString)", isDirectory: true))
    }

    private func makeStore(_ fake: FakeChatAPI, outbox: ShareOutbox) -> ChatStore {
        ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(),
            attachmentsDirectoryURL: makeTempAttachmentsDirectoryURL(),
            previewsDirectoryURL: makeTempAttachmentsDirectoryURL(), shareOutbox: outbox,
            policy: .test(sendMaxAttempts: 1)
        )
    }

    private func queue(
        _ outbox: ShareOutbox, text: String = "from the share sheet", at: Double = 1_000
    ) throws -> ShareOutbox.Entry {
        let entry = ShareOutbox.Entry(
            clientMsgId: UUID().uuidString, text: text, createdAt: Date(timeIntervalSince1970: at),
            photos: [Data([0xFF, 0xD8, 0xFF, 0xD9])],
            files: [StagedFile(name: "Booking.pdf", mime: "application/pdf", data: Data("%PDF-1.7".utf8))]
        )
        try outbox.store(entry)
        return entry
    }

    // MARK: - Send and delete

    @Test func aQueuedShareIsSentWithItsSavedIdAndThenDeleted() async throws {
        let fake = FakeChatAPI()
        let outbox = makeOutbox()
        let entry = try queue(outbox)
        let store = makeStore(fake, outbox: outbox)
        defer { store.cancelActiveWork() }

        await store.drainShareOutbox()

        let sent = try requireCount(await fake.sentMessages, 1)
        #expect(sent[0].clientMsgId == entry.clientMsgId, "the id the extension saved, so the server can dedup")
        #expect(sent[0].text == "from the share sheet")
        #expect(sent[0].imagesBase64 == [Data([0xFF, 0xD8, 0xFF, 0xD9]).base64EncodedString()])
        #expect(sent[0].files == [OutgoingFile(
            b64: Data("%PDF-1.7".utf8).base64EncodedString(), mime: "application/pdf", name: "Booking.pdf"
        )])
        #expect(sent[0].deviceToken == "tok")
        #expect(outbox.pendingIds().isEmpty)
        #expect(store.permissionNotice == nil)
    }

    @Test func anEmptyOutboxSendsNothing() async throws {
        let fake = FakeChatAPI()
        let store = makeStore(fake, outbox: makeOutbox())
        defer { store.cancelActiveWork() }

        await store.drainShareOutbox()
        #expect(await fake.sentMessages.isEmpty)
    }

    // MARK: - Failures

    /// Offline, or the box is down: keep it, and try the same id next time.
    @Test func aTransientFailureKeepsTheEntryAndTheNextDrainSendsTheSameId() async throws {
        let fake = FakeChatAPI()
        await fake.stubSend(.failure(URLError(.notConnectedToInternet)))
        let outbox = makeOutbox()
        let entry = try queue(outbox)
        let store = makeStore(fake, outbox: outbox)
        defer { store.cancelActiveWork() }

        await store.drainShareOutbox()
        #expect(outbox.pendingIds() == [entry.clientMsgId])
        #expect(store.permissionNotice == nil, "an offline phone is not news")

        await fake.stubSend(.success(MessageResponse(turnId: "turn-s")))
        await store.drainShareOutbox()

        let sent = try requireCount(await fake.sentMessages, 2)
        #expect(sent.map(\.clientMsgId) == [entry.clientMsgId, entry.clientMsgId])
        #expect(outbox.pendingIds().isEmpty)
    }

    @Test func aServerErrorIsKeptToo() async throws {
        let fake = FakeChatAPI()
        await fake.stubSend(.failure(APIError.server(status: 503, detail: nil)))
        let outbox = makeOutbox()
        _ = try queue(outbox)
        let store = makeStore(fake, outbox: outbox)
        defer { store.cancelActiveWork() }

        await store.drainShareOutbox()
        #expect(outbox.pendingIds().count == 1)
    }

    /// A 4xx: it will never go through, so it is deleted and the user is told.
    @Test func aRejectedShareIsDeletedWithANotice() async throws {
        let fake = FakeChatAPI()
        await fake.stubSend(.failure(APIError.server(status: 413, detail: "files[0] is over 20MB")))
        let outbox = makeOutbox()
        _ = try queue(outbox)
        let store = makeStore(fake, outbox: outbox)
        defer { store.cancelActiveWork() }

        await store.drainShareOutbox()

        #expect(outbox.pendingIds().isEmpty)
        let notice = try #require(store.permissionNotice)
        #expect(notice.contains("files[0] is over 20MB"))
        #expect(notice.contains("shared"))
    }

    /// The oldest first, and a failure leaves the later ones for next time.
    @Test func entriesGoOldestFirst() async throws {
        let fake = FakeChatAPI()
        let outbox = makeOutbox()
        let older = try queue(outbox, text: "first", at: 1)
        let newer = try queue(outbox, text: "second", at: 2)
        let store = makeStore(fake, outbox: outbox)
        defer { store.cancelActiveWork() }

        await store.drainShareOutbox()

        #expect(try requireCount(await fake.sentMessages, 2).map(\.clientMsgId) == [older.clientMsgId, newer.clientMsgId])
    }

    /// Becoming active while the launch drain is still going must not send
    /// everything twice.
    @Test func twoOverlappingDrainsSendEachEntryOnce() async throws {
        let fake = FakeChatAPI()
        let outbox = makeOutbox()
        _ = try queue(outbox)
        let store = makeStore(fake, outbox: outbox)
        defer { store.cancelActiveWork() }

        async let first: Void = store.drainShareOutbox()
        async let second: Void = store.drainShareOutbox()
        _ = await (first, second)

        #expect(await fake.sentMessages.count == 1)
    }

    // MARK: - Showing up in the transcript

    /// A message sent from the extension has no local bubble. It reaches the
    /// transcript through the normal sync, as a row from "another device": the
    /// drain asks for the sync itself so it appears without waiting a tick.
    @Test func afterASendTheTranscriptIsSyncedSoTheRowAppears() async throws {
        let fake = FakeChatAPI()
        let outbox = makeOutbox()
        let entry = try queue(outbox)
        let photoId = UUID().uuidString
        let pdfId = UUID().uuidString
        await fake.stubAttachment(id: photoId, .success(Data([0xFF, 0xD8, 0xFF, 0xD9])))
        let row = TranscriptRow(
            seq: 1, id: UUID().uuidString, role: .user, turnId: "t-share", clientMsgId: entry.clientMsgId,
            text: "from the share sheet", createdAt: Date().timeIntervalSince1970 - 60,
            attachments: [
                .init(id: photoId, mime: "image/jpeg", name: nil, size: 4),
                .init(id: pdfId, mime: "application/pdf", name: "Booking.pdf", size: 8),
            ]
        )
        // The first sync (the store's own, at launch) finds nothing; the one
        // the drain asks for finds the row.
        await fake.stubTranscript([
            .success(TranscriptPage(rows: [], latestSeq: 0, hasMore: false)),
            .success(TranscriptPage(rows: [row], latestSeq: 1, hasMore: false)),
        ])
        await fake.stubTurns([.success(.fixture(turnId: "t-share", status: "done", resultText: "Got it."))])
        let store = makeStore(fake, outbox: outbox)
        defer { store.cancelActiveWork() }
        #expect(await waitUntil { await fake.transcriptCallCount >= 1 })

        await store.drainShareOutbox()
        #expect(await waitUntil { await MainActor.run { store.messages.contains { $0.role == .user } } })

        let user = try #require(store.messages.first { $0.role == .user })
        #expect(user.text == "from the share sheet")
        #expect(user.files?.map(\.name) == ["Booking.pdf"], "the file is a chip, not fetched")
        #expect(user.attachmentIds == [photoId], "the photo is downloaded for the grid")
        #expect(await waitUntil { await MainActor.run { store.messages.first { $0.role == .user }?.imageFilenames != nil } })
        #expect(await fake.attachmentCalls == [photoId])
        let reply = try #require(store.messages.last)
        #expect(reply.role == .assistant, "a placeholder is inserted and polled for the reply")
        #expect(await waitUntil { await MainActor.run { store.messages.last?.text == "Got it." } })
    }
}
