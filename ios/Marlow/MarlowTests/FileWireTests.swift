import Foundation
import Testing
@testable import Marlow

/// The wire contract (docs/tasks/share-and-files-wire.md) as the phone reads
/// and writes it: transcript rows with `{id, mime, name, size}` attachments,
/// and `files` in the request body.
struct FileWireTests {

    // MARK: - Transcript rows

    private func decode(_ attachments: String) throws -> TranscriptRow {
        let json = """
        {"seq": 1, "id": "\(UUID().uuidString)", "role": "user", "source": "chat", "turn_id": "t1",
         "text": "see attached", "created_at": 1, "attachments": \(attachments)}
        """
        return try JSONDecoder().decode(TranscriptRow.self, from: Data(json.utf8))
    }

    @Test func aNewRowsAttachmentsCarryTheirNameAndSize() throws {
        let row = try decode(#"[{"id": "a1", "mime": "application/pdf", "name": "Booking.pdf", "size": 52113}]"#)
        let attachment = try #require(row.attachments.first)
        #expect(attachment.name == "Booking.pdf")
        #expect(attachment.size == 52_113)
        #expect(attachment.mime == "application/pdf")
    }

    /// Old rows, and photos sent through `images`, have `name` null and no
    /// `size` until recently: all three spellings decode, as optionals.
    @Test func anOldRowsAttachmentHasNoNameOrSize() throws {
        let missing = try decode(#"[{"id": "a1", "mime": "image/jpeg"}]"#)
        #expect(missing.attachments[0].name == nil)
        #expect(missing.attachments[0].size == nil)

        let nulls = try decode(#"[{"id": "a1", "mime": "image/jpeg", "name": null, "size": null}]"#)
        #expect(nulls.attachments[0].name == nil)
        #expect(nulls.attachments[0].size == nil)

        let photo = try decode(#"[{"id": "a1", "mime": "image/jpeg", "name": null, "size": 31337}]"#)
        #expect(photo.attachments[0].size == 31_337)
    }

    // MARK: - Another device's row

    private func row(_ attachments: [TranscriptRow.Attachment], clientMsgId: String? = nil) -> TranscriptRow {
        TranscriptRow(
            seq: 5, id: UUID().uuidString, role: .user, turnId: "t5", clientMsgId: clientMsgId,
            text: "have a look", createdAt: 100, attachments: attachments
        )
    }

    /// Photos come first and are downloaded for the grid, as before. A file
    /// becomes a chip and is NOT fetched: it is not needed to draw the bubble,
    /// and it can be large.
    @Test func aRowFromAnotherDeviceGetsItsPhotosFetchedAndItsFilesAsChips() throws {
        let photo = TranscriptRow.Attachment(id: UUID().uuidString, mime: "image/jpeg", name: nil, size: 900)
        let pdf = TranscriptRow.Attachment(id: UUID().uuidString, mime: "application/pdf", name: "Booking.pdf", size: 52_113)
        let other = TranscriptRow.Attachment(
            id: UUID().uuidString, mime: "application/octet-stream", name: "Budget.numbers", size: 2_100_000
        )

        let (messages, effects) = TranscriptMerge.apply([row([photo, pdf, other])], to: [], announce: false)

        let user = try #require(messages.first)
        #expect(user.attachmentIds == [photo.id], "only the photo is an image attachment")
        #expect(effects.fetchAttachments.map(\.attachmentIds) == [[photo.id]], "the files are not downloaded eagerly")
        #expect(user.files == [
            MessageFile(id: pdf.id, name: "Booking.pdf", mime: "application/pdf", size: 52_113, attachmentId: pdf.id),
            MessageFile(id: other.id, name: "Budget.numbers", mime: "application/octet-stream", size: 2_100_000, attachmentId: other.id),
        ])
    }

    @Test func aRowWithOnlyFilesFetchesNothing() throws {
        let pdf = TranscriptRow.Attachment(id: UUID().uuidString, mime: "application/pdf", name: "a.pdf", size: 1)
        let (messages, effects) = TranscriptMerge.apply([row([pdf])], to: [], announce: false)

        #expect(effects.fetchAttachments.isEmpty)
        #expect(messages.first?.attachmentIds == nil)
        #expect(messages.first?.files?.count == 1)
    }

    @Test func aRowWithoutAttachmentsHasNoFilesField() {
        let (messages, _) = TranscriptMerge.apply([row([])], to: [], announce: false)
        #expect(messages.first?.files == nil)
    }

    /// A stored mime this build has never seen is still a file, not a photo.
    @Test func anUnknownMimeIsAFileNotAnImage() {
        let odd = TranscriptRow.Attachment(id: UUID().uuidString, mime: "application/x-from-a-newer-server", name: nil, size: nil)
        let (messages, effects) = TranscriptMerge.apply([row([odd])], to: [], announce: false)
        #expect(effects.fetchAttachments.isEmpty)
        #expect(messages.first?.files?.first?.name == nil, "an old row has no name; the chip supplies a generic one")
    }

    /// This phone's own send keeps its bubble, its saved copy and its chips
    /// when the server's row confirms it.
    @Test func theRowThatConfirmsThisPhonesSendLeavesItsFilesAlone() throws {
        let id = UUID()
        var mine = ChatMessage(id: id, role: .user, text: "mine", createdAt: Date(), turnId: nil, status: .done, clientMsgId: nil)
        let local = MessageFile(id: "\(id)-f0.pdf", name: "a.pdf", mime: "application/pdf", size: 3, localFilename: "\(id)-f0.pdf")
        mine.files = [local]
        let pdf = TranscriptRow.Attachment(id: UUID().uuidString, mime: "application/pdf", name: "a.pdf", size: 3)

        let (messages, effects) = TranscriptMerge.apply(
            [row([pdf], clientMsgId: id.uuidString)], to: [mine], announce: false
        )

        #expect(messages.first?.files == [local])
        #expect(effects.fetchAttachments.isEmpty)
    }
}

@Suite(.serialized) // StubURLProtocol.handler is process-global
struct FileRequestBodyTests {

    private func makeClient() -> APIClient {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [StubURLProtocol.self]
        return APIClient(
            baseURL: URL(string: "https://marlow.example.ts.net:8443")!,
            session: URLSession(configuration: config)
        )
    }

    @Test func filesGoAlongsideImagesWithTheirMimeAndName() async throws {
        StubURLProtocol.respond(json: #"{"turn_id": "t1"}"#, status: 202)
        let jpeg = Data([0xFF, 0xD8, 0xFF, 0xD9])
        let pdf = OutgoingFile(b64: Data("%PDF-1.7".utf8).base64EncodedString(), mime: "application/pdf", name: "Booking.pdf")
        let unknown = OutgoingFile(b64: Data([1, 2, 3]).base64EncodedString(), mime: "", name: "Budget.numbers")

        _ = try await makeClient().sendMessage(
            text: "look", imagesBase64: [jpeg.base64EncodedString()], files: [pdf, unknown], deviceToken: "tok"
        )

        let body = try #require(StubURLProtocol.lastBodyJSON)
        #expect((body["images"] as? [[String: String]]) == [["b64": jpeg.base64EncodedString(), "mime": "image/jpeg"]])
        #expect((body["files"] as? [[String: String]]) == [
            ["b64": pdf.b64, "mime": "application/pdf", "name": "Booking.pdf"],
            ["b64": unknown.b64, "mime": "", "name": "Budget.numbers"],
        ], "mime is the platform's guess or an empty string, never omitted")
    }

    /// The server answers an empty `files` list with a 400.
    @Test func filesAreAbsentWhenThereAreNone() async throws {
        StubURLProtocol.respond(json: #"{"turn_id": "t2"}"#, status: 202)

        _ = try await makeClient().sendMessage(text: "hi", deviceToken: "tok")

        let body = try #require(StubURLProtocol.lastBodyJSON)
        #expect(body["files"] == nil)
    }
}

/// The share extension writes its body straight to a file and uploads from it
/// (extensions run in about 120MB), instead of building the JSON in memory.
/// This pins that the two ways of writing the body say the same thing.
struct MessageBodyFileTests {

    private func tempURL() -> URL {
        URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("body-\(UUID().uuidString).json")
    }

    @Test func theStreamedBodyHasTheSameShapeAsTheInMemoryOne() throws {
        let jpeg = Data([0xFF, 0xD8, 0xFF, 0xD9])
        let file = StagedFile(name: "Trip \"notes\".docx", mime: "application/x", data: Data([1, 2, 3, 4, 5]))
        let url = tempURL()
        defer { try? FileManager.default.removeItem(at: url) }

        try MessageBody.write(
            to: url, text: "line one\nline “two” \u{1F600}", clientMsgId: "cmid-1", timezone: "America/Toronto",
            images: [jpeg], files: [file]
        )

        let streamed = try #require(try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? NSDictionary)
        var expected: [String: Any] = [
            "text": "line one\nline “two” \u{1F600}", "client_msg_id": "cmid-1", "timezone": "America/Toronto",
        ]
        expected.merge(MessageBody.attachmentFields(
            imagesBase64: [jpeg.base64EncodedString()],
            files: [OutgoingFile(file)]
        )) { _, new in new }
        #expect(streamed == NSDictionary(dictionary: expected))
    }

    @Test func aBodyWithNothingAttachedOmitsBothLists() throws {
        let url = tempURL()
        defer { try? FileManager.default.removeItem(at: url) }
        try MessageBody.write(to: url, text: "hi", clientMsgId: "c", timezone: "UTC", images: [], files: [])

        let object = try #require(try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
        #expect(Set(object.keys) == ["text", "client_msg_id", "timezone"])
    }

    /// The chunking must not corrupt a file whose length is not a multiple of
    /// the chunk (base64 pads the last group, and a chunk that ends mid-group
    /// would put padding in the middle).
    @Test(arguments: [0 + 1, 2, 3, 49_151, 49_152, 49_153, 200_000])
    func aLargeFileSurvivesTheChunking(length: Int) throws {
        let data = Data((0..<length).map { UInt8(truncatingIfNeeded: $0 &* 31 &+ 7) })
        let url = tempURL()
        defer { try? FileManager.default.removeItem(at: url) }
        try MessageBody.write(
            to: url, text: "", clientMsgId: "c", timezone: "UTC", images: [],
            files: [StagedFile(name: "big.bin", mime: "", data: data)]
        )

        let object = try #require(try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
        let files = try #require(object["files"] as? [[String: String]])
        #expect(Data(base64Encoded: try #require(files.first?["b64"])) == data)
    }
}
