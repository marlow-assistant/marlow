import Foundation

/// Names for the local copy of a file attachment: the one QuickLook opens, and
/// the one saved for retry.
///
/// QuickLook picks its previewer from the *extension*, and shows the *file
/// name* as the title. So the preview copy is `<sanitized name base>.<ext>`,
/// where `ext` comes from the stored mime (the server's reading of the bytes)
/// and, for `application/octet-stream` (a type with no reader), from the
/// name's own extension, the only hint left.
///
/// The name is the sender's and the extension is a path component in a
/// directory this app writes to, so both are cleaned here and nothing else is
/// trusted with a path.
enum AttachmentFileNaming {
    /// The stored mimes in docs/tasks/share-and-files-wire.md, "Kinds".
    private static let extensionForMime: [String: String] = [
        "application/pdf": "pdf",
        "text/plain": "txt",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/gif": "gif",
        "image/webp": "webp",
    ]

    /// Longest base name kept; the rest is display the chip already has.
    private static let maxBaseLength = 64

    static func fileExtension(forMime mime: String, name: String?) -> String {
        let bare = mime.split(separator: ";").first.map { $0.trimmingCharacters(in: .whitespaces).lowercased() } ?? ""
        if let known = extensionForMime[bare] { return known }
        return plainExtension(of: name) ?? "bin"
    }

    /// The extension of the copy saved for retry (`<clientMsgId>-f<i>.<ext>`):
    /// the name's own, when it is a plain one.
    static func savedExtension(forName name: String) -> String {
        plainExtension(of: name) ?? "bin"
    }

    static func previewFilename(name: String?, mime: String) -> String {
        "\(base(of: name)).\(fileExtension(forMime: mime, name: name))"
    }

    /// 1-8 letters and digits, lowercased, or nil. Anything else (a slash, an
    /// angle bracket, a sentence after the last dot) is not an extension.
    private static func plainExtension(of name: String?) -> String? {
        guard let name, let dot = name.lastIndex(of: ".") else { return nil }
        let candidate = name[name.index(after: dot)...]
        guard (1...8).contains(candidate.count),
              candidate.allSatisfy({ $0.isASCII && ($0.isLetter || $0.isNumber) })
        else { return nil }
        return candidate.lowercased()
    }

    /// The name with its plain extension off, path separators and control
    /// characters neutralised, leading dots dropped, and a length cap; "file"
    /// when nothing is left.
    private static func base(of name: String?) -> String {
        var text = (name ?? "")
            .map { "/\\:".contains($0) ? "-" : String($0) }
            .joined()
        text = String(text.unicodeScalars.filter { !CharacterSet.controlCharacters.contains($0) })
        if plainExtension(of: text) != nil, let dot = text.lastIndex(of: ".") {
            text = String(text[..<dot])
        }
        text = String(text.drop { $0 == "." })
            .trimmingCharacters(in: .whitespaces)
        if text.count > maxBaseLength { text = String(text.prefix(maxBaseLength)) }
        return text.isEmpty ? "file" : text
    }
}
