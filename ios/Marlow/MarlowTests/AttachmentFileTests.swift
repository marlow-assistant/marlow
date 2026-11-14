import Foundation
import Testing
@testable import Marlow

/// How a file chip is drawn and how its preview copy is named
/// (docs/tasks/share-and-files.md Phase 2, docs/tasks/share-and-files-wire.md
/// "Opening a file chip").
struct AttachmentFileTests {

    // MARK: - The mime -> extension map

    static let docx = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    static let xlsx = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    static let pptx = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

    /// Every stored mime the wire doc lists, to the extension QuickLook needs
    /// to pick a previewer. The stored mime is the server's reading of the
    /// bytes, so it wins over whatever the file was called.
    @Test(arguments: [
        ("application/pdf", "pdf"), ("text/plain", "txt"),
        (docx, "docx"), (xlsx, "xlsx"), (pptx, "pptx"),
        ("image/jpeg", "jpg"), ("image/png", "png"), ("image/gif", "gif"), ("image/webp", "webp"),
    ])
    func theStoredMimeDecidesTheExtension(mime: String, expected: String) {
        // The name says one thing, the server's sniff says another.
        #expect(AttachmentFileNaming.fileExtension(forMime: mime, name: "whatever.zzz") == expected)
    }

    /// Other: the server stores `application/octet-stream` and knows nothing
    /// more, so the only hint left is the name's own extension.
    @Test func anUnreadableTypeKeepsTheNamesOwnExtension() {
        #expect(AttachmentFileNaming.fileExtension(forMime: "application/octet-stream", name: "Budget.numbers") == "numbers")
        #expect(AttachmentFileNaming.fileExtension(forMime: "application/octet-stream", name: "Photo.HEIC") == "heic", "lowercased")
        #expect(AttachmentFileNaming.fileExtension(forMime: "application/octet-stream", name: "noextension") == "bin")
        #expect(AttachmentFileNaming.fileExtension(forMime: "application/octet-stream", name: nil) == "bin")
        #expect(AttachmentFileNaming.fileExtension(forMime: "something/new-from-a-newer-server", name: "x.keynote") == "keynote")
    }

    /// The extension becomes part of a path, so only a plain alphanumeric one
    /// survives.
    @Test func aHostileExtensionFallsBackToBin() {
        #expect(AttachmentFileNaming.fileExtension(forMime: "application/octet-stream", name: "a.b/../c") == "bin")
        #expect(AttachmentFileNaming.fileExtension(forMime: "application/octet-stream", name: "x.<script>") == "bin")
        #expect(AttachmentFileNaming.fileExtension(forMime: "application/octet-stream", name: "x.waytoolongtobeanextension") == "bin")
    }

    // MARK: - The preview copy's name

    @Test func thePreviewIsNamedForTheFileWithAnExtensionThatMatchesItsType() {
        #expect(AttachmentFileNaming.previewFilename(name: "Booking confirmation.pdf", mime: "application/pdf") == "Booking confirmation.pdf")
        // A text file keeps its own name's base; the sniffed kind picks `.txt`.
        #expect(AttachmentFileNaming.previewFilename(name: "invite.ics", mime: "text/plain") == "invite.txt")
        #expect(AttachmentFileNaming.previewFilename(name: "Trip notes.DOCX", mime: Self.docx) == "Trip notes.docx")
        #expect(AttachmentFileNaming.previewFilename(name: "Budget.numbers", mime: "application/octet-stream") == "Budget.numbers")
    }

    @Test func aNameWithNoUsableBaseFallsBack() {
        #expect(AttachmentFileNaming.previewFilename(name: nil, mime: "application/pdf") == "file.pdf")
        #expect(AttachmentFileNaming.previewFilename(name: "", mime: "text/plain") == "file.txt")
        #expect(AttachmentFileNaming.previewFilename(name: "...", mime: "text/plain") == "file.txt")
    }

    /// The name is the sender's, and it is about to become a path component in
    /// a directory we write to.
    @Test func aHostileNameCannotEscapeTheDirectory() {
        let name = AttachmentFileNaming.previewFilename(name: "../../Library/evil\n.pdf", mime: "application/pdf")
        #expect(!name.contains("/"))
        #expect(!name.contains("\n"))
        #expect(!name.hasPrefix("."))
        #expect(name.hasSuffix(".pdf"))
        #expect(AttachmentFileNaming.previewFilename(name: "a/b\\c:d.pdf", mime: "application/pdf") == "a-b-c-d.pdf")
    }

    @Test func aLongNameIsCapped() {
        let name = AttachmentFileNaming.previewFilename(name: String(repeating: "x", count: 500) + ".pdf", mime: "application/pdf")
        #expect(name.count <= 64 + 4)
        #expect(name.hasSuffix(".pdf"))
    }

    // MARK: - The saved copy's name (CMP-6)

    @Test func theSavedCopyKeepsTheNamesExtensionOrBin() {
        #expect(AttachmentFileNaming.savedExtension(forName: "Trip notes.docx") == "docx")
        #expect(AttachmentFileNaming.savedExtension(forName: "Budget.NUMBERS") == "numbers")
        #expect(AttachmentFileNaming.savedExtension(forName: "README") == "bin")
        #expect(AttachmentFileNaming.savedExtension(forName: "x.<b>") == "bin")
    }

    // MARK: - Icon and size

    @Test func theIconComesFromTheNamesExtension() {
        #expect(FileDisplay.symbol(forName: "Booking.pdf") == "doc.richtext")
        #expect(FileDisplay.symbol(forName: "Trip notes.DOCX") == "doc.text")
        #expect(FileDisplay.symbol(forName: "notes.txt") == "doc.text")
        #expect(FileDisplay.symbol(forName: "Budget.xlsx") == "tablecells")
        #expect(FileDisplay.symbol(forName: "data.csv") == "tablecells")
        #expect(FileDisplay.symbol(forName: "Deck.pptx") == "rectangle.on.rectangle.angled")
        #expect(FileDisplay.symbol(forName: "bundle.zip") == "doc.zipper")
        #expect(FileDisplay.symbol(forName: "voice.m4a") == "waveform")
        #expect(FileDisplay.symbol(forName: "clip.mov") == "film")
        #expect(FileDisplay.symbol(forName: "script.py") == "chevron.left.forwardslash.chevron.right")
    }

    /// Display only: an unfamiliar or missing extension is a plain document,
    /// never a refusal.
    @Test func anUnfamiliarTypeGetsThePlainDocumentIcon() {
        #expect(FileDisplay.symbol(forName: "Budget.numbers") == "doc")
        #expect(FileDisplay.symbol(forName: "noextension") == "doc")
        #expect(FileDisplay.symbol(forName: nil) == "doc")
    }

    @Test func theSizeIsReadable() {
        #expect(FileDisplay.sizeText(2_048)?.contains("KB") == true)
        #expect(FileDisplay.sizeText(3 * 1024 * 1024)?.contains("MB") == true)
        #expect(FileDisplay.sizeText(nil) == nil, "an old row has no size, and shows none")
    }
}
