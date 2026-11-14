import Foundation

/// A file as it goes on the wire: `files[i]` in `POST /v1/message`
/// (docs/tasks/share-and-files-wire.md). Already base64, encoded once before
/// the send's retry loop, like `imagesBase64`.
struct OutgoingFile: Equatable {
    let b64: String
    /// The platform's guess, or "". Decides nothing on the server.
    let mime: String
    /// Display only; the server cleans it.
    let name: String

    init(b64: String, mime: String, name: String) {
        self.b64 = b64
        self.mime = mime
        self.name = name
    }

    init(_ file: StagedFile) {
        self.init(b64: file.data.base64EncodedString(), mime: file.mime, name: file.name)
    }
}

/// The attachment half of the message body, and the share extension's way of
/// writing the whole body without holding it in memory. Compiled into the app
/// and the share extension, so both say the same thing.
enum MessageBody {
    /// `images` and `files`, each ABSENT when empty, never an empty list: the
    /// server answers `images: []` or `files: []` with a 400. Every image is
    /// JPEG (the downscaler's only output); a file carries what the platform
    /// said about it.
    static func attachmentFields(imagesBase64: [String], files: [OutgoingFile]) -> [String: Any] {
        var fields: [String: Any] = [:]
        if !imagesBase64.isEmpty {
            fields["images"] = imagesBase64.map { ["b64": $0, "mime": "image/jpeg"] }
        }
        if !files.isEmpty {
            fields["files"] = files.map { ["b64": $0.b64, "mime": $0.mime, "name": $0.name] }
        }
        return fields
    }

    /// Bytes encoded per step: a multiple of 3 so no chunk but the last ends
    /// mid-group and carries padding.
    private static let chunk = 3 * 16_384

    /// The same body `APIClient.sendMessage` builds, minus what only the app
    /// has (effort, location), written straight to `url` a chunk at a time.
    /// The share extension runs in about 120MB: a 25MB message built as a
    /// string and then serialised would hold it three times over, and this
    /// holds it once, as the staged bytes, and uploads from the file.
    static func write(
        to url: URL, text: String, clientMsgId: String, timezone: String, images: [Data], files: [StagedFile]
    ) throws {
        FileManager.default.createFile(atPath: url.path, contents: nil)
        let handle = try FileHandle(forWritingTo: url)
        defer { try? handle.close() }

        func put(_ string: String) throws { try handle.write(contentsOf: Data(string.utf8)) }
        func json(_ value: Any) throws -> String {
            let data = try JSONSerialization.data(withJSONObject: value, options: [.fragmentsAllowed])
            return String(decoding: data, as: UTF8.self)
        }
        func putBase64(_ data: Data) throws {
            var offset = data.startIndex
            while offset < data.endIndex {
                let end = data.index(offset, offsetBy: chunk, limitedBy: data.endIndex) ?? data.endIndex
                try handle.write(contentsOf: data[offset..<end].base64EncodedData())
                offset = end
            }
        }

        try put("{\"text\":\(try json(text)),\"client_msg_id\":\(try json(clientMsgId)),\"timezone\":\(try json(timezone))")
        if !images.isEmpty {
            try put(",\"images\":[")
            for (index, image) in images.enumerated() {
                try put((index == 0 ? "" : ",") + "{\"b64\":\"")
                try putBase64(image)
                try put("\",\"mime\":\"image/jpeg\"}")
            }
            try put("]")
        }
        if !files.isEmpty {
            try put(",\"files\":[")
            for (index, file) in files.enumerated() {
                try put((index == 0 ? "" : ",") + "{\"b64\":\"")
                try putBase64(file.data)
                try put("\",\"mime\":\(try json(file.mime)),\"name\":\(try json(file.name))}")
            }
            try put("]")
        }
        try put("}")
    }
}
