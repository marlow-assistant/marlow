import Foundation

/// About > Open-source notices. The list is `OpenSourceNotices.json` and the
/// texts are files beside it, copied from FluidAudio at the pinned revision,
/// not typed into Swift (docs/tasks/settings-overhaul.md).
struct OpenSourceNotice: Codable, Identifiable, Hashable {
    let name: String
    let license: String
    let url: URL
    /// The bundled text's name, without `.txt`.
    let file: String
    /// The `Package.resolved` identity, for an entry that is a whole package;
    /// `OpenSourceNoticesTests` holds every pin to having one.
    let package: String?

    var id: String { name }

    var text: String {
        guard let url = Bundle.main.url(forResource: file, withExtension: "txt"),
              let text = try? String(contentsOf: url, encoding: .utf8) else { return "" }
        return text
    }

    static let all: [OpenSourceNotice] = {
        guard let url = Bundle.main.url(forResource: "OpenSourceNotices", withExtension: "json"),
              let data = try? Data(contentsOf: url),
              let notices = try? JSONDecoder().decode([OpenSourceNotice].self, from: data) else { return [] }
        return notices
    }()
}
