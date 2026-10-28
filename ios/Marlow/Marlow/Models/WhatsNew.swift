import Foundation

/// One entry of What's new (docs/tasks/whats-new.md). Both halves come from
/// the repo-root `changes.json`: `app` entries are bundled into this build,
/// `server` entries come from `GET /v1/changes`, so each half announces only
/// what is actually running.
struct ChangeEntry: Decodable, Identifiable, Equatable {
    let id: Int
    let date: String
    let part: String
    let category: String
    let text: String
}

struct ChangesResponse: Decodable {
    let changes: [ChangeEntry]
    /// False on a fresh install until first-run onboarding ends: the card
    /// waits. A box older than the flag sends none, which means there is no
    /// onboarding to wait for.
    let onboardingComplete: Bool

    init(changes: [ChangeEntry], onboardingComplete: Bool = true) {
        self.changes = changes
        self.onboardingComplete = onboardingComplete
    }

    private enum CodingKeys: String, CodingKey {
        case changes
        case onboardingComplete = "onboarding_complete"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        changes = try c.decode([ChangeEntry].self, forKey: .changes)
        onboardingComplete = try c.decodeIfPresent(Bool.self, forKey: .onboardingComplete) ?? true
    }
}

/// Pure rules, so they are testable without a store or a bundle.
enum WhatsNew {
    /// A device that has never shown the card (a fresh install, or the first
    /// build with this feature) gets the newest few, not the whole history.
    /// Five, not three: the first build carries four entries, and three would
    /// drop an always-tell one.
    static let firstRunCount = 5

    private struct File: Decodable {
        let entries: [ChangeEntry]
    }

    /// This build's own half. Nil-safe: a missing or broken resource is no
    /// card, never a crash (the server's tests check the file itself).
    static func bundled(in bundle: Bundle = .main) -> [ChangeEntry] {
        guard let url = bundle.url(forResource: "changes", withExtension: "json"),
              let data = try? Data(contentsOf: url),
              let file = try? JSONDecoder().decode(File.self, from: data)
        else { return [] }
        return file.entries.filter { $0.part == "app" }
    }

    /// Each half contributes only its own part, whatever it was handed: an app
    /// built from a newer `main` than its box carries server entries the box
    /// isn't running yet. Newest first.
    static func merge(app: [ChangeEntry], server: [ChangeEntry]) -> [ChangeEntry] {
        var byId: [Int: ChangeEntry] = [:]
        for entry in app where entry.part == "app" { byId[entry.id] = entry }
        for entry in server where entry.part == "server" { byId[entry.id] = entry }
        return byId.values.sorted { $0.id > $1.id }
    }

    /// What the card should show. `seen` is a set rather than a high-water
    /// mark because the halves update at different times: app entry 12 can be
    /// shown before the box deploys server entry 11, and a "highest seen" of
    /// 12 would then hide 11 for good. Nil means this device has never shown
    /// the card.
    static func unseen(_ entries: [ChangeEntry], seen: Set<Int>?) -> [ChangeEntry] {
        guard let seen else { return Array(entries.prefix(firstRunCount)) }
        return entries.filter { !seen.contains($0.id) }
    }

    /// The policy's must-tell categories. Their label is drawn in the accent
    /// colour so a change to what Marlow can reach or does on its own never
    /// reads as just another feature.
    static func isAlwaysTell(_ category: String) -> Bool {
        ["access", "unattended", "data", "personality", "removed"].contains(category)
    }

    static func label(_ category: String) -> String {
        switch category {
        case "access": return "What Marlow can reach"
        case "unattended": return "Runs on its own"
        case "data": return "Your data"
        case "personality": return "How Marlow behaves"
        case "removed": return "Removed"
        case "fix": return "Fixed"
        default: return "New"
        }
    }

    /// "Oct 23" from the file's "2026-10-23"; the raw string if it won't parse.
    static func dateText(_ date: String) -> String {
        guard let parsed = dayParser.date(from: date) else { return date }
        return parsed.formatted(dayStyle)
    }

    /// Built once: rows call `dateText` on every body evaluation.
    private static let dayParser: DateFormatter = {
        let parser = DateFormatter()
        parser.locale = Locale(identifier: "en_US_POSIX")
        parser.timeZone = TimeZone(identifier: "UTC")
        parser.dateFormat = "yyyy-MM-dd"
        return parser
    }()

    private static let dayStyle: Date.FormatStyle = {
        var style = Date.FormatStyle.dateTime.month(.abbreviated).day()
        style.timeZone = TimeZone(identifier: "UTC")!
        return style
    }()
}
