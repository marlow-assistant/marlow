import Foundation
import UniformTypeIdentifiers

/// Turns what a share sheet hands over into staged tiles and a caption.
///
/// **One item at a time, and each is consumed as it arrives.** A share
/// extension runs in about 120MB (checked on no device yet; it is Apple's
/// documented order of magnitude), so a photo is downscaled the moment its
/// file is available and the original dropped before the next one is asked
/// for. Holding six raw 12MP photos would be the whole budget.
///
/// What each provider is:
///
/// - **an image**: the photo path, whatever it is registered as;
/// - **a web URL** (a Safari page): the URL goes in the caption, for Marlow to
///   fetch. It is not a file;
/// - **plain text** (a selection): the caption. A text *file* is also
///   `public.plain-text`, but it arrives with a name, which a selection does
///   not have, so a named one is a file;
/// - **anything else**: a file, whatever its type. The server reads it, or
///   labels it "a type you can't read".
enum ShareItemLoader {
    @MainActor
    static func load(_ providers: [NSItemProvider], into model: ShareModel) async {
        for provider in providers {
            // Past the cap nothing more is read at all: the cheapest way to
            // refuse is not to load it.
            guard model.staged.count < AttachmentLimits.maxItems else {
                model.notice = StagedAttachments.Refusal.tooMany.notice
                return
            }
            if let text = await caption(from: provider) {
                model.addToCaption(text)
            } else if let outcome = await attachment(from: provider) {
                model.stage(outcome)
            }
        }
    }

    // MARK: - Caption: a URL or some text

    private static func caption(from provider: NSItemProvider) async -> String? {
        let isFile = provider.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier)
            || provider.hasItemConformingToTypeIdentifier(UTType.image.identifier)
        guard !isFile else { return nil }

        if provider.hasItemConformingToTypeIdentifier(UTType.url.identifier) {
            return await withCheckedContinuation { continuation in
                _ = provider.loadObject(ofClass: URL.self) { url, _ in
                    continuation.resume(returning: url?.absoluteString)
                }
            }
        }
        if provider.hasItemConformingToTypeIdentifier(UTType.plainText.identifier), provider.suggestedName == nil {
            return await withCheckedContinuation { continuation in
                _ = provider.loadObject(ofClass: NSString.self) { text, _ in
                    continuation.resume(returning: text as? String)
                }
            }
        }
        return nil
    }

    // MARK: - Attachment: a photo or a file

    private static func attachment(from provider: NSItemProvider) async -> AttachmentIntake.Outcome? {
        guard let typeIdentifier = representation(of: provider) else { return nil }
        let name = await originalName(of: provider)
        return await withCheckedContinuation { continuation in
            _ = provider.loadFileRepresentation(forTypeIdentifier: typeIdentifier) { url, _ in
                // Read inside the handler: the system deletes the file when it
                // returns. `read` downscales a photo straight from the file,
                // so the original is never held whole.
                guard let url else {
                    continuation.resume(returning: .unreadable)
                    return
                }
                var name = name
                // A suggested name usually has no extension; the copy has one.
                if var suggested = name, (suggested as NSString).pathExtension.isEmpty, !url.pathExtension.isEmpty {
                    suggested += "." + url.pathExtension
                    name = suggested
                }
                continuation.resume(returning: AttachmentIntake.read(fileAt: url, name: name))
            }
        }
    }

    /// The user's name for the file. `loadFileRepresentation` hands over a temp
    /// copy named for the TYPE ("PDF document.pdf"), not for the file, so the
    /// name comes from the file's own URL when the provider has one, and from
    /// its suggested name otherwise. Nil falls back to the copy's name.
    private static func originalName(of provider: NSItemProvider) async -> String? {
        if provider.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier) {
            let url: URL? = await withCheckedContinuation { continuation in
                _ = provider.loadObject(ofClass: URL.self) { url, _ in continuation.resume(returning: url) }
            }
            if let name = url?.lastPathComponent, !name.isEmpty { return name }
        }
        guard let suggested = provider.suggestedName, !suggested.isEmpty else { return nil }
        return suggested
    }

    /// The registered type to ask for: an image's own, otherwise the most
    /// specific data type, otherwise any item. Never a type list: whatever is
    /// registered is taken, in the provider's own order.
    private static func representation(of provider: NSItemProvider) -> String? {
        // A file's `public.file-url` representation is the URL as text, not
        // the file: it is the last resort, behind the file's own type.
        let all = provider.registeredTypeIdentifiers
        let registered = all.filter { UTType($0)?.conforms(to: .url) != true }
        func first(conformingTo parent: UTType) -> String? {
            registered.first { UTType($0)?.conforms(to: parent) == true }
        }
        return first(conformingTo: .image) ?? first(conformingTo: .data) ?? first(conformingTo: .item)
            ?? registered.first ?? all.first
    }
}
