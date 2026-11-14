import Foundation
import Testing
@testable import Marlow

/// Files on a message (docs/tasks/share-and-files.md Phase 2): the all-or-
/// nothing save for retry (CMP-6), the request body, the chips of a row from
/// another device, and the local copy a tap opens.
@MainActor
struct ChatStoreFileTests {

    static let docx = AttachmentFileTests.docx

    private func makeStore(
        _ fake: FakeChatAPI, attachments: URL = makeTempAttachmentsDirectoryURL(),
        previews: URL = makeTempAttachmentsDirectoryURL(), persistURL: URL = makeTempTranscriptURL(),
        policy: PollingPolicy = .test()
    ) -> ChatStore {
        ChatStore(
            deviceToken: "tok", client: fake, persistURL: persistURL,
            attachmentsDirectoryURL: attachments, previewsDirectoryURL: previews, policy: policy
        )
    }

    private func settledFake() async -> FakeChatAPI {
        let fake = FakeChatAPI()
        await fake.stubSend(.success(MessageResponse(turnId: "turn-f")))
        await fake.stubTurns([.success(.fixture(turnId: "turn-f", status: "done", resultText: "read it"))])
        return fake
    }

    // MARK: - Send and the retry save

    @Test func aFileSendGetsTheLongerUploadDeadlineAndATextSendKeepsTwentySeconds() async throws {
        let fake = FakeChatAPI()
        await fake.stubTurns([
            .success(.fixture(status: "done", resultText: "ok")),
            .success(.fixture(status: "done", resultText: "ok")),
        ])
        var policy = PollingPolicy.test()
        policy.sendAttemptTimeout = PollingPolicy.default.sendAttemptTimeout
        policy.fileSendAttemptTimeout = PollingPolicy.default.fileSendAttemptTimeout
        let store = makeStore(fake, policy: policy)
        defer { store.cancelActiveWork() }

        store.send(text: "read this", files: [StagedFile(name: "a.pdf", mime: "application/pdf", data: Data([1]))])
        await awaitSettled(store)
        store.send(text: "and this")
        await awaitSettled(store)

        let sent = try requireCount(await fake.sentMessages, 2)
        #expect(sent[0].timeout == 120, "up to 25MB of files plus a conversion: the web client's 120s")
        #expect(sent[1].timeout == 20, "text keeps Mike's settled 20s bound")
    }


    @Test func aSendCarriesTheFilesBesideTheImagesAndSavesEachForRetry() async throws {
        let fake = await settledFake()
        let store = makeStore(fake)
        defer { store.cancelActiveWork() }

        let notes = StagedFile(name: "Trip notes.docx", mime: Self.docx, data: Data([1, 2, 3]))
        let unknown = StagedFile(name: "Budget.numbers", mime: "", data: Data([9, 9]))
        store.send(text: "have a look", images: [Data([7])], files: [notes, unknown])
        await awaitSettled(store)

        let user = try #require(store.messages.first)
        let id = user.id.uuidString
        #expect(user.imageFilenames == ["\(id)-0.jpg"])
        #expect(user.files?.map(\.localFilename) == ["\(id)-f0.docx", "\(id)-f1.numbers"],
                "<clientMsgId>-f<i>.<ext>, the extension from the name")
        #expect(store.loadAttachment(filename: "\(id)-f0.docx") == notes.data)
        #expect(user.files?.map(\.name) == ["Trip notes.docx", "Budget.numbers"])
        #expect(user.files?.map(\.mime) == [Self.docx, ""])
        #expect(user.files?.map(\.size) == [3, 2])

        let sent = try requireCount(await fake.sentMessages, 1)
        #expect(sent[0].imagesBase64 == [Data([7]).base64EncodedString()])
        #expect(sent[0].files == [
            OutgoingFile(b64: Data([1, 2, 3]).base64EncodedString(), mime: Self.docx, name: "Trip notes.docx"),
            OutgoingFile(b64: Data([9, 9]).base64EncodedString(), mime: "", name: "Budget.numbers"),
        ])
    }

    @Test func aFileAloneWithNoCaptionStillSends() async throws {
        let fake = await settledFake()
        let store = makeStore(fake)
        defer { store.cancelActiveWork() }

        let sent = store.send(text: "  ", files: [StagedFile(name: "a.pdf", mime: "application/pdf", data: Data([1]))])
        #expect(sent)
        await awaitSettled(store)
        #expect(try requireCount(await fake.sentMessages, 1)[0].files.count == 1)
    }

    @Test func aSendWithoutFilesCarriesNone() async throws {
        let fake = await settledFake()
        let store = makeStore(fake)
        defer { store.cancelActiveWork() }

        store.send(text: "plain")
        await awaitSettled(store)

        #expect(try requireCount(await fake.sentMessages, 1)[0].files.isEmpty)
        #expect(store.messages[0].files == nil)
    }

    @Test func retryResendsEverySavedFileUnderTheSameName() async throws {
        let fake = FakeChatAPI()
        await fake.stubSend(.failure(URLError(.notConnectedToInternet)))
        let store = makeStore(fake, policy: .test(sendMaxAttempts: 1))
        defer { store.cancelActiveWork() }

        let pdf = StagedFile(name: "Booking.pdf", mime: "application/pdf", data: Data([4, 5, 6]))
        store.send(text: "offline", files: [pdf])
        await awaitSettled(store)
        let failed = try requireCount(store.messages, 2)
        await fake.stubSend(.success(MessageResponse(turnId: "turn-r")))
        await fake.stubTurns([.success(.fixture(turnId: "turn-r", status: "done", resultText: "ok"))])

        store.retry(failed[1].id)
        await awaitSettled(store)

        let sent = try requireCount(await fake.sentMessages, 2)
        #expect(sent[1].files == sent[0].files, "the saved copy rebuilds the body it first sent")
        #expect(sent[1].files == [OutgoingFile(b64: pdf.data.base64EncodedString(), mime: "application/pdf", name: "Booking.pdf")])
        #expect(sent[1].clientMsgId == sent[0].clientMsgId)
    }

    /// CMP-4/CMP-6: never the caption with some of its attachments. A missing
    /// file drops the images with it, and says so.
    @Test func retryWithOneFileMissingSendsTextOnlyAndSaysSo() async throws {
        let fake = FakeChatAPI()
        await fake.stubSend(.failure(URLError(.notConnectedToInternet)))
        let attachments = makeTempAttachmentsDirectoryURL()
        let store = makeStore(fake, attachments: attachments, policy: .test(sendMaxAttempts: 1))
        defer { store.cancelActiveWork() }

        store.send(text: "offline", images: [Data([1])], files: [
            StagedFile(name: "a.pdf", mime: "application/pdf", data: Data([2])),
            StagedFile(name: "b.txt", mime: "text/plain", data: Data([3])),
        ])
        await awaitSettled(store)
        let failed = try requireCount(store.messages, 2)
        let second = try #require(store.messages[0].files?[1].localFilename)
        try FileManager.default.removeItem(at: attachments.appendingPathComponent(second))
        await fake.stubSend(.success(MessageResponse(turnId: "turn-m")))
        await fake.stubTurns([.success(.fixture(turnId: "turn-m", status: "done", resultText: "ok"))])

        store.retry(failed[1].id)
        await awaitSettled(store)

        let sent = try requireCount(await fake.sentMessages, 2)
        #expect(sent[1].files.isEmpty)
        #expect(sent[1].imagesBase64.isEmpty, "not the photo without the file it was sent with")
        #expect(store.permissionNotice != nil)
    }

    /// The relaunch path: a `.sending` message found on disk with files saved.
    @Test func aSendingMessageWithFilesOnDiskIsResentWithThem() async throws {
        let fake = await settledFake()
        let persistURL = makeTempTranscriptURL()
        let attachments = makeTempAttachmentsDirectoryURL()
        let user = ChatMessage(
            id: UUID(), role: .user, text: "interrupted", createdAt: Date(), turnId: nil, status: .done,
            clientMsgId: nil
        )
        var withFile = user
        let filename = "\(user.id.uuidString)-f0.pdf"
        try Data([8, 8]).write(to: attachments.appendingPathComponent(filename))
        withFile.files = [MessageFile(id: filename, name: "Booking.pdf", mime: "application/pdf", size: 2, localFilename: filename)]
        let placeholder = ChatMessage(
            id: UUID(), role: .assistant, text: "", createdAt: Date(), turnId: nil, status: .sending,
            clientMsgId: user.id.uuidString
        )
        try writeTranscript([withFile, placeholder], to: persistURL)

        let store = makeStore(fake, attachments: attachments, persistURL: persistURL)
        defer { store.cancelActiveWork() }
        await awaitSettled(store)

        let sent = try requireCount(await fake.sentMessages, 1)
        #expect(sent[0].files == [OutgoingFile(b64: Data([8, 8]).base64EncodedString(), mime: "application/pdf", name: "Booking.pdf")])
    }

    // MARK: - Old transcripts

    /// A transcript cache written before files existed has no `files` key, and
    /// one unreadable row discards the whole conversation.
    @Test func aMessageWrittenBeforeFilesExistedStillDecodes() throws {
        let json = """
        {"id": "\(UUID().uuidString)", "role": "user", "text": "hi", "createdAt": 0,
         "status": "done", "imageFilename": "x.jpg"}
        """
        let message = try JSONDecoder().decode(ChatMessage.self, from: Data(json.utf8))
        #expect(message.files == nil)
        #expect(message.allImageFilenames == ["x.jpg"])
    }

    @Test func filesRoundTripThroughThePersistedTranscript() async throws {
        let fake = await settledFake()
        let persistURL = makeTempTranscriptURL()
        let store = makeStore(fake, persistURL: persistURL)
        defer { store.cancelActiveWork() }

        store.send(text: "x", files: [StagedFile(name: "n.txt", mime: "text/plain", data: Data([1]))])
        await awaitSettled(store)

        let restored = try #require(readTranscript(at: persistURL).first)
        #expect(restored.files == store.messages[0].files)
    }

    /// `pruneUnreferencedAttachments` runs at launch and removes whatever no
    /// message names: a file the transcript still lists must survive it.
    @Test func aSavedFileASurvivingMessageNamesIsNotPruned() async throws {
        let persistURL = makeTempTranscriptURL()
        let attachments = makeTempAttachmentsDirectoryURL()
        let user = ChatMessage(
            id: UUID(), role: .user, text: "keep", createdAt: Date(), turnId: nil, status: .done, clientMsgId: nil
        )
        var kept = user
        let name = "\(user.id.uuidString)-f0.pdf"
        let orphan = "orphan-f0.pdf"
        try Data([1]).write(to: attachments.appendingPathComponent(name))
        try Data([2]).write(to: attachments.appendingPathComponent(orphan))
        // Older than this launch, as the prune requires.
        let past = Date().addingTimeInterval(-3_600)
        for file in [name, orphan] {
            try FileManager.default.setAttributes(
                [.modificationDate: past], ofItemAtPath: attachments.appendingPathComponent(file).path
            )
        }
        kept.files = [MessageFile(id: name, name: "k.pdf", mime: "application/pdf", size: 1, localFilename: name)]
        try writeTranscript([kept], to: persistURL)

        let store = makeStore(FakeChatAPI(), attachments: attachments, persistURL: persistURL)
        defer { store.cancelActiveWork() }
        #expect(await waitUntil { !FileManager.default.fileExists(atPath: attachments.appendingPathComponent(orphan).path) })
        #expect(FileManager.default.fileExists(atPath: attachments.appendingPathComponent(name).path))
    }

    // MARK: - Opening a chip

    /// A file this phone sent opens from the copy saved for retry, with no
    /// request, named for the file and not for the client_msg_id.
    @Test func aFileSentFromThisPhoneOpensFromTheSavedCopy() async throws {
        let fake = await settledFake()
        let previews = makeTempAttachmentsDirectoryURL()
        let store = makeStore(fake, previews: previews)
        defer { store.cancelActiveWork() }
        store.send(text: "x", files: [StagedFile(name: "Trip notes.docx", mime: Self.docx, data: Data([1, 2, 3]))])
        await awaitSettled(store)

        let file = try #require(store.messages[0].files?.first)
        let url = try #require(await store.previewURL(for: file))

        #expect(url.lastPathComponent == "Trip notes.docx")
        #expect(url.path.hasPrefix(previews.path))
        #expect(try Data(contentsOf: url) == Data([1, 2, 3]))
        #expect(await fake.attachmentCalls.isEmpty)
    }

    /// From another device: downloaded on tap, not before, with the stored
    /// mime choosing the extension.
    @Test func aFileFromAnotherDeviceIsDownloadedOnTapNotBefore() async throws {
        let attachmentId = UUID().uuidString
        let fake = FakeChatAPI()
        await fake.stubAttachment(id: attachmentId, .success(Data([0x25, 0x50, 0x44, 0x46])))
        let store = makeStore(fake)
        defer { store.cancelActiveWork() }
        let file = MessageFile(id: attachmentId, name: "Report", mime: "application/pdf", size: 4, attachmentId: attachmentId)

        #expect(await fake.attachmentCalls.isEmpty, "nothing is fetched to draw the chip")
        let url = try #require(await store.previewURL(for: file))
        #expect(url.lastPathComponent == "Report.pdf")
        #expect(try Data(contentsOf: url) == Data([0x25, 0x50, 0x44, 0x46]))
        #expect(await fake.attachmentCalls == [attachmentId])

        _ = await store.previewURL(for: file)
        #expect(await fake.attachmentCalls == [attachmentId], "a second tap reuses the copy")
    }

    @Test func aFailedDownloadOpensNothingAndSaysSo() async throws {
        let fake = FakeChatAPI()   // an id it has no entry for answers 404
        let store = makeStore(fake)
        defer { store.cancelActiveWork() }
        let id = UUID().uuidString
        let file = MessageFile(id: id, name: nil, mime: "application/pdf", size: nil, attachmentId: id)

        #expect(await store.previewURL(for: file) == nil)
        #expect(store.permissionNotice != nil)
    }

    /// The id is the server's, and becomes a directory name.
    @Test func anAttachmentIdThatIsNotAUUIDIsNeverUsedAsAPath() async throws {
        let fake = FakeChatAPI()
        let store = makeStore(fake)
        defer { store.cancelActiveWork() }
        let file = MessageFile(id: "../../etc", name: "x", mime: "application/pdf", size: 1, attachmentId: "../../etc")

        #expect(await store.previewURL(for: file) == nil)
        #expect(await fake.attachmentCalls.isEmpty)
    }
}
