import Foundation
import Testing
import UIKit
@testable import Marlow

/// What the composer (and the share sheet) lets onto the next message
/// (docs/tasks/share-and-files.md, Phase 2). The rules are the count and the
/// sizes, never the type: the server sniffs the bytes and decides how Marlow
/// reads them, so a client that filtered by type would need a release for every
/// new one.
struct StagedAttachmentsTests {

    private func file(_ name: String = "Notes.txt", bytes: Int = 10, mime: String = "") -> StagedFile {
        StagedFile(name: name, mime: mime, data: Data(count: bytes))
    }

    // MARK: - The count

    /// The server's six is photos and files together: three of each fill it.
    @Test func sixItemsFitWhateverTheMix() {
        var staged = StagedAttachments()
        for _ in 0..<3 { #expect(staged.addPhoto(Data([1])) == nil) }
        for index in 0..<3 { #expect(staged.addFile(file("f\(index).pdf")) == nil) }
        #expect(staged.count == 6)
    }

    @Test func aSeventhItemIsRefusedWhicheverKindItIs() {
        var staged = StagedAttachments()
        for _ in 0..<4 { staged.addPhoto(Data([1])) }
        for _ in 0..<2 { staged.addFile(file()) }

        #expect(staged.addPhoto(Data([2])) == .tooMany)
        #expect(staged.addFile(file()) == .tooMany)
        #expect(staged.count == 6, "a refused item is not staged")
        #expect(StagedAttachments.Refusal.tooMany.notice == "You can attach up to 6 items to a message.")
    }

    /// One constant for the composer's cap and the server's `_MAX_IMAGES`
    /// (server/app.py is the authority), whatever the old name for it.
    @Test func theCapIsTheSharedConstant() {
        #expect(AttachmentLimits.maxItems == 6)
        #expect(ChatMessage.maxImages == AttachmentLimits.maxItems)
        #expect(AttachmentLimits.maxFileBytes == 20 * 1024 * 1024)
        #expect(AttachmentLimits.maxMessageBytes == 25 * 1024 * 1024)
    }

    // MARK: - No type is refused

    /// The case the whole rewrite is about: a type nobody has heard of stages
    /// like any other, with no notice. The server labels it "a type you can't
    /// read" and Marlow says so.
    @Test func anUnfamiliarTypeIsStagedWithoutAFuss() {
        var staged = StagedAttachments()
        let refusal = staged.addFile(file("Budget.numbers", bytes: 2_000, mime: ""))
        #expect(refusal == nil)
        #expect(staged.files.map(\.name) == ["Budget.numbers"])

        #expect(staged.addFile(file("clip.unheard-of-ext", mime: "application/x-whatever")) == nil)
        #expect(staged.addFile(file("no-extension-at-all")) == nil)
        #expect(staged.count == 3)
    }

    // MARK: - Sizes

    @Test func aFileOverTwentyMegabytesIsRefusedAndNamed() {
        var staged = StagedAttachments()
        let big = file("Huge.pdf", bytes: AttachmentLimits.maxFileBytes + 1)
        #expect(staged.addFile(big) == .fileTooLarge(name: "Huge.pdf"))
        #expect(staged.isEmpty)
        #expect(StagedAttachments.Refusal.fileTooLarge(name: "Huge.pdf").notice == "“Huge.pdf” is over 20MB.")
    }

    @Test func aFileOfExactlyTwentyMegabytesFits() {
        var staged = StagedAttachments()
        #expect(staged.addFile(file("Edge.pdf", bytes: AttachmentLimits.maxFileBytes)) == nil)
    }

    /// Checked from the size on disk, before the bytes are read: a 2GB video
    /// must be refused without being loaded.
    @Test func theSizeCanBeCheckedBeforeTheBytesAreRead() {
        let staged = StagedAttachments()
        #expect(staged.refusal(forFileNamed: "Movie.mov", byteCount: 2_000_000_000) == .fileTooLarge(name: "Movie.mov"))
        #expect(staged.refusal(forFileNamed: "Small.txt", byteCount: 10) == nil)
    }

    /// 25MB is the message, photos and files together.
    @Test func theMessageTotalIsCappedAtTwentyFiveMegabytes() {
        var staged = StagedAttachments()
        #expect(staged.addFile(file("a.bin", bytes: 20 * 1024 * 1024)) == nil)
        #expect(staged.addFile(file("b.bin", bytes: 5 * 1024 * 1024)) == nil, "20MB + 5MB is exactly the cap")
        #expect(staged.addFile(file("c.bin", bytes: 1)) == .messageTooLarge)
        #expect(staged.addPhoto(Data([1])) == .messageTooLarge, "photos count toward the same total")
        #expect(staged.count == 2)
        #expect(StagedAttachments.Refusal.messageTooLarge.notice == "That would put the message over 25MB.")
    }

    /// Count is checked before size, so a full tray says "six" however big the
    /// next file is.
    @Test func aFullTrayReportsTheCountBeforeTheSize() {
        var staged = StagedAttachments()
        for _ in 0..<6 { staged.addPhoto(Data([1])) }
        #expect(staged.addFile(file("big.bin", bytes: AttachmentLimits.maxFileBytes + 1)) == .tooMany)
    }

    // MARK: - Order and removal

    @Test func photosAndFilesKeepTheirOwnOrderForTheSend() {
        var staged = StagedAttachments()
        staged.addPhoto(Data([1]))
        staged.addFile(file("one.pdf"))
        staged.addPhoto(Data([2]))
        staged.addFile(file("two.pdf"))

        #expect(staged.photos == [Data([1]), Data([2])])
        #expect(staged.files.map(\.name) == ["one.pdf", "two.pdf"])
        #expect(staged.items.count == 4, "the tray shows them in the order they were added")
    }

    @Test func removingByIdTakesTheOneTappedNotWhateverSlidIntoItsSlot() {
        var staged = StagedAttachments()
        staged.addPhoto(Data([1]))
        staged.addPhoto(Data([1]))   // identical bytes: removal by content would be ambiguous
        let second = staged.items[1].id
        staged.remove(id: second)
        staged.remove(id: second)    // a second tap before the re-render is a no-op

        #expect(staged.count == 1)
        #expect(staged.items[0].id != second)
    }
}

/// A file picked from anywhere that can be decoded as an image takes the photo
/// path (the downscaler, `images`); everything else is a file.
struct AttachmentIntakeTests {

    private func write(_ data: Data, named name: String) throws -> URL {
        let dir = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("IntakeTests-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let url = dir.appendingPathComponent(name)
        try data.write(to: url)
        return url
    }

    private func pngData() throws -> Data {
        let image = UIGraphicsImageRenderer(size: CGSize(width: 40, height: 30)).image { context in
            UIColor.red.setFill()
            context.fill(CGRect(x: 0, y: 0, width: 40, height: 30))
        }
        return try #require(image.pngData())
    }

    @Test func anImageFileTakesThePhotoPathWhicheverPickerChoseIt() throws {
        let url = try write(pngData(), named: "Screenshot.png")

        guard case .photo(let jpeg) = AttachmentIntake.read(fileAt: url) else {
            Issue.record("a PNG should go through the downscaler")
            return
        }
        #expect(Array(jpeg.prefix(2)) == [0xFF, 0xD8], "the downscaler's only output is JPEG")
    }

    @Test func aPDFIsAFileWithItsNameAndTheMimeThePlatformGives() throws {
        let bytes = Data("%PDF-1.7\n%âãÏÓ\n".utf8)
        let url = try write(bytes, named: "Booking confirmation.pdf")

        guard case .file(let staged) = AttachmentIntake.read(fileAt: url) else {
            Issue.record("a PDF is not an image")
            return
        }
        #expect(staged.name == "Booking confirmation.pdf")
        #expect(staged.mime == "application/pdf")
        #expect(staged.data == bytes)
    }

    /// A real, renderable PDF, not just its header (the test above): ImageIO
    /// draws a PDF's first page, so "does it decode" alone turned it into a
    /// nameless photo tile and sent a JPEG of page one (Mike, on device,
    /// 2026-11-14). The header-only PDF above never reached that path.
    @Test func aRealPDFThatImageIOCouldRenderIsStillAFile() throws {
        let renderer = UIGraphicsPDFRenderer(bounds: CGRect(x: 0, y: 0, width: 612, height: 792))
        let bytes = renderer.pdfData { context in
            context.beginPage()
            ("Booking" as NSString).draw(at: CGPoint(x: 72, y: 72), withAttributes: nil)
        }
        let url = try write(bytes, named: "Booking confirmation.pdf")

        guard case .file(let staged) = AttachmentIntake.read(fileAt: url) else {
            Issue.record("a PDF must stay a file, never a photo of its first page")
            return
        }
        #expect(staged.name == "Booking confirmation.pdf")
        #expect(staged.data == bytes)
    }

    /// What ImageIO says it opened decides the photo path, not whether it
    /// managed to draw something: ImageIO renders a PDF's first page.
    @Test func onlyAnImageTypeTakesThePhotoPath() {
        #expect(ImageDownscaler.isPhotoType("public.jpeg"))
        #expect(ImageDownscaler.isPhotoType("public.png"))
        #expect(ImageDownscaler.isPhotoType("public.heic"))
        #expect(ImageDownscaler.isPhotoType("com.compuserve.gif"))
        #expect(!ImageDownscaler.isPhotoType("com.adobe.pdf"), "a PDF is a document, whatever ImageIO can draw of it")
        #expect(!ImageDownscaler.isPhotoType(nil))
        #expect(!ImageDownscaler.isPhotoType("not.a.real.type"))
    }

    /// The platform has no mime for it: "" goes on the wire, and the server
    /// sniffs.
    @Test func anUnknownExtensionHasAnEmptyMime() throws {
        let url = try write(Data([1, 2, 3, 4]), named: "Budget.numbers-nope")
        guard case .file(let staged) = AttachmentIntake.read(fileAt: url) else {
            Issue.record("expected a file")
            return
        }
        #expect(staged.mime == "")
        #expect(staged.name == "Budget.numbers-nope")
    }

    /// The extension says image, the bytes say otherwise: what counts is
    /// whether it decodes, not what it is called.
    @Test func anImageByNameThatDoesNotDecodeIsAFile() throws {
        let url = try write(Data([0, 1, 2, 3]), named: "broken.png")
        guard case .file = AttachmentIntake.read(fileAt: url) else {
            Issue.record("undecodable bytes are a file, not a photo")
            return
        }
    }

    @Test func aFileOverTwentyMegabytesIsRefusedFromItsSizeOnDisk() throws {
        let url = try write(Data(), named: "Movie.mov")
        let handle = try FileHandle(forWritingTo: url)
        try handle.truncate(atOffset: UInt64(AttachmentLimits.maxFileBytes + 1))   // sparse: nothing is written
        try handle.close()

        #expect(AttachmentIntake.read(fileAt: url) == .tooLarge(name: "Movie.mov"))
    }

    @Test func aMissingFileIsUnreadableNotACrash() {
        let url = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("gone-\(UUID().uuidString).pdf")
        #expect(AttachmentIntake.read(fileAt: url) == .unreadable)
    }

    @Test func aNameOverridesTheFilesOwn() throws {
        // A share sheet hands over a temp copy whose name is not the user's.
        let url = try write(Data("%PDF-1.4".utf8), named: "tmp-1234.pdf")
        guard case .file(let staged) = AttachmentIntake.read(fileAt: url, name: "Trip notes.pdf") else {
            Issue.record("expected a file")
            return
        }
        #expect(staged.name == "Trip notes.pdf")
    }
}
