import Foundation

/// How a file is drawn: its icon and its size. Display only. Nothing here
/// decides whether a file may be attached; the server sniffs the bytes, and a
/// name's extension is only a hint for which icon to draw.
enum FileDisplay {
    /// An SF Symbol for the name's extension. An unfamiliar or missing one is
    /// the plain document, never a refusal.
    static func symbol(forName name: String?) -> String {
        guard let name else { return "doc" }
        switch (name as NSString).pathExtension.lowercased() {
        case "pdf": return "doc.richtext"
        case "doc", "docx", "rtf", "txt", "md", "pages": return "doc.text"
        case "xls", "xlsx", "csv", "tsv": return "tablecells"
        case "ppt", "pptx": return "rectangle.on.rectangle.angled"
        case "zip", "gz", "tar", "7z": return "doc.zipper"
        case "m4a", "mp3", "wav", "aac", "flac", "aiff": return "waveform"
        case "mov", "mp4", "m4v", "avi", "mkv": return "film"
        case "py", "js", "ts", "swift", "json", "html", "xml", "yaml", "yml", "sh", "css", "c", "cpp", "java", "rb", "go", "rs":
            return "chevron.left.forwardslash.chevron.right"
        default: return "doc"
        }
    }

    /// The name cut before its extension, for a row that truncates the stem
    /// and keeps the extension. A leading dot is not an extension.
    static func stemAndExtension(_ name: String) -> (stem: String, ext: String) {
        guard let dot = name.lastIndex(of: "."), dot != name.startIndex,
              name.index(after: dot) != name.endIndex
        else { return (name, "") }
        return (String(name[..<dot]), String(name[dot...]))
    }

    /// "52 KB", "2.1 MB". Nil when the size is not known: an old row has none,
    /// and shows none rather than "0 bytes".
    static func sizeText(_ bytes: Int?) -> String? {
        guard let bytes else { return nil }
        return ByteCountFormatter.string(fromByteCount: Int64(bytes), countStyle: .file)
    }
}
