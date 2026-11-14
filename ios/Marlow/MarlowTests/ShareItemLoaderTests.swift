import Foundation
import Testing
import UIKit
import UniformTypeIdentifiers
@testable import Marlow

/// Items from a share sheet become staged tiles and a pre-filled caption,
/// loaded one at a time (an extension runs in about 120MB). Driven here with
/// real `NSItemProvider`s; what the real share sheet hands over for Photos,
/// Files, Mail and Safari is only checked on a device.
@MainActor
struct ShareItemLoaderTests {

    private func makeModel() -> ShareModel {
        ShareModel(
            token: "tok", baseURL: URL(string: "https://marlow.example.ts.net:8443")!,
            outbox: ShareOutbox(directory: nil), upload: { _, _ in (Data(), 202) }
        )
    }

    private func write(_ data: Data, named name: String) throws -> URL {
        let dir = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("ShareLoaderTests-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let url = dir.appendingPathComponent(name)
        try data.write(to: url)
        return url
    }

    private func pngData() throws -> Data {
        let image = UIGraphicsImageRenderer(size: CGSize(width: 40, height: 30)).image { context in
            UIColor.blue.setFill()
            context.fill(CGRect(x: 0, y: 0, width: 40, height: 30))
        }
        return try #require(image.pngData())
    }

    @Test func aSharedPDFIsStagedAsAFileWithItsName() async throws {
        let url = try write(Data("%PDF-1.7\n".utf8), named: "Booking confirmation.pdf")
        let model = makeModel()

        await ShareItemLoader.load([try #require(NSItemProvider(contentsOf: url))], into: model)

        #expect(model.staged.files.map(\.name) == ["Booking confirmation.pdf"])
        #expect(model.staged.photos.isEmpty)
        #expect(model.caption.isEmpty)
        #expect(model.notice == nil)
    }

    @Test func aSharedPhotoTakesThePhotoPath() async throws {
        let url = try write(pngData(), named: "IMG_0001.png")
        let model = makeModel()

        await ShareItemLoader.load([try #require(NSItemProvider(contentsOf: url))], into: model)

        #expect(model.staged.photos.count == 1)
        #expect(Array(model.staged.photos[0].prefix(2)) == [0xFF, 0xD8], "downscaled to JPEG as it came in")
        #expect(model.staged.files.isEmpty)
    }

    /// A type nobody here has heard of is staged like any other, with no
    /// notice.
    @Test func anUnfamiliarTypeIsStagedWithoutANotice() async throws {
        let url = try write(Data([1, 2, 3, 4]), named: "Budget.numbers")
        let model = makeModel()

        await ShareItemLoader.load([try #require(NSItemProvider(contentsOf: url))], into: model)

        #expect(model.staged.files.map(\.name) == ["Budget.numbers"])
        #expect(model.notice == nil)
    }

    /// A Safari page: the URL goes in the caption for Marlow to fetch. It is
    /// not a file.
    @Test func aSharedWebPageFillsTheCaptionWithItsURL() async throws {
        let model = makeModel()
        let provider = NSItemProvider(object: URL(string: "https://example.com/article?id=7")! as NSURL)

        await ShareItemLoader.load([provider], into: model)

        #expect(model.caption == "https://example.com/article?id=7")
        #expect(model.staged.isEmpty)
    }

    @Test func sharedTextFillsTheCaption() async {
        let model = makeModel()
        await ShareItemLoader.load([NSItemProvider(object: "Dinner at 7, bring the thing" as NSString)], into: model)

        #expect(model.caption == "Dinner at 7, bring the thing")
        #expect(model.staged.isEmpty)
    }

    @Test func textAndAFileInOneShareGiveBoth() async throws {
        let model = makeModel()
        let url = try write(Data("%PDF-1.7\n".utf8), named: "a.pdf")
        await ShareItemLoader.load([
            NSItemProvider(object: "see this" as NSString), try #require(NSItemProvider(contentsOf: url)),
        ], into: model)

        #expect(model.caption == "see this")
        #expect(model.staged.files.count == 1)
    }

    /// The share sheet is only offered for six, but a source can lie: past the
    /// cap the rest are dropped with the same notice the composer gives.
    @Test func itemsPastTheCapAreRefusedWithTheComposersNotice() async throws {
        let model = makeModel()
        var providers: [NSItemProvider] = []
        for index in 0..<(AttachmentLimits.maxItems + 2) {
            providers.append(try #require(NSItemProvider(contentsOf: try write(Data([UInt8(index)]), named: "f\(index).bin"))))
        }

        await ShareItemLoader.load(providers, into: model)

        #expect(model.staged.count == AttachmentLimits.maxItems)
        #expect(model.notice == StagedAttachments.Refusal.tooMany.notice)
    }

    @Test func aFileOverTwentyMegabytesIsRefusedAndTheRestStillLoad() async throws {
        let model = makeModel()
        let big = try write(Data(), named: "Movie.mov")
        let handle = try FileHandle(forWritingTo: big)
        try handle.truncate(atOffset: UInt64(AttachmentLimits.maxFileBytes + 1))
        try handle.close()
        let small = try write(Data("hello".utf8), named: "note.txt")

        await ShareItemLoader.load([
            try #require(NSItemProvider(contentsOf: big)), try #require(NSItemProvider(contentsOf: small)),
        ], into: model)

        #expect(model.staged.files.map(\.name) == ["note.txt"])
        #expect(model.notice == StagedAttachments.Refusal.fileTooLarge(name: "Movie.mov").notice)
    }
}
