import Foundation
import UniformTypeIdentifiers

/// Turns one file on disk into what the composer stages, for every way a file
/// arrives (the Files picker, the share sheet).
///
/// **Anything that decodes as an image takes the photo path** (the downscaler,
/// wire `images`), whichever picker chose it; everything else is a file. What
/// decides is whether ImageIO can decode the bytes, never the name or the
/// type. The only refusal is size.
enum AttachmentIntake {
    enum Outcome: Equatable {
        case photo(Data)
        case file(StagedFile)
        /// Over the per-file cap, decided from the size on disk without
        /// reading the bytes.
        case tooLarge(name: String)
        case unreadable
    }

    /// `name` overrides the file's own, for a share sheet's temp copy.
    static func read(fileAt url: URL, name: String? = nil) -> Outcome {
        let name = name ?? url.lastPathComponent
        let values = try? url.resourceValues(forKeys: [.fileSizeKey, .contentTypeKey])
        guard let size = values?.fileSize else { return .unreadable }

        // First: a photo from Files is as much a photo as one from Photos. A
        // file that is not an image costs ImageIO a look at its header, not a
        // read of its bytes.
        if let jpeg = ImageDownscaler.downscaledJPEG(fromFileAt: url) { return .photo(jpeg) }

        if size > AttachmentLimits.maxFileBytes { return .tooLarge(name: name) }
        guard let data = try? Data(contentsOf: url) else { return .unreadable }
        // The platform's guess, or "": the server sniffs and decides nothing
        // from this. The name's extension, not the temp copy's, for a share.
        let type = UTType(filenameExtension: (name as NSString).pathExtension) ?? values?.contentType
        return .file(StagedFile(name: name, mime: type?.preferredMIMEType ?? "", data: data))
    }
}
