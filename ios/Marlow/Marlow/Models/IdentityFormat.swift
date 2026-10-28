import Foundation

/// How the Marlow sheet's files and schedules read on screen
/// (docs/tasks/ux-lane.md §3a). Pure, and separate from the views, so the
/// wording and the frontmatter rule are testable without rendering.
enum IdentityFormat {
    // MARK: Names

    /// "memory/marlow-features.md" -> "Marlow features". Filenames are Marlow's
    /// own, so the humaniser is `ActivityFormat`'s — one rule for every id.
    static func memoryName(_ id: String) -> String {
        ActivityFormat.humanise(baseName(id))
    }

    /// The screen title for a file. The three fixed files get the names the
    /// icon strip uses ("Soul", "You", "Instructions"), not their filenames.
    static func title(id: String, kind: IdentityKind) -> String {
        switch kind {
        case .soul: return "Soul"
        case .user: return "You"
        case .instructions: return "Instructions"
        case .job: return ActivityFormat.jobTitle(baseName(id))
        case .memory, .unknown: return memoryName(id)
        }
    }

    private static func baseName(_ id: String) -> String {
        let name = (id as NSString).lastPathComponent
        return name.hasSuffix(".md") ? String(name.dropLast(3)) : name
    }

    // MARK: About this file

    /// One fixed sentence per kind, written in the app and never part of the
    /// file. Says what the file is and how Marlow uses it (§3 "About this file").
    static func aboutNote(_ kind: IdentityKind) -> String {
        switch kind {
        case .soul:
            return "Marlow's personality and values. Marlow reads this at the start of every conversation."
        case .user:
            return "What Marlow knows about you. Marlow reads this at the start of every conversation."
        case .instructions:
            return "Marlow's operating instructions — how it works, what tools it uses, and its rules."
        case .memory:
            return "One of Marlow's memory files. Marlow sees its one-line description every turn and reads the full file when it's relevant. It can be out of date."
        case .job:
            return "The instructions Marlow follows when this scheduled job runs."
        case .unknown:
            return "A file Marlow keeps."
        }
    }

    // MARK: Frontmatter

    /// Drops a leading YAML frontmatter block: a first line of `---` through
    /// the next line of `---`. A file that opens with `---` as a horizontal
    /// rule and never closes it is left whole — losing the body to a fence
    /// that was never a fence would be worse than showing a stray rule.
    static func stripFrontmatter(_ text: String) -> String {
        // `isNewline`, not a "\n" split: Swift reads "\r\n" as one Character.
        let lines = text.split(omittingEmptySubsequences: false, whereSeparator: \.isNewline).map(String.init)
        func isFence(_ line: String) -> Bool {
            line.trimmingCharacters(in: .whitespaces) == "---"
        }
        guard let first = lines.first, isFence(first) else { return text }
        guard let close = lines.indices.dropFirst().first(where: { isFence(lines[$0]) }) else { return text }
        let body = lines[(close + 1)...].drop(while: {
            $0.trimmingCharacters(in: .whitespaces).isEmpty
        })
        return body.joined(separator: "\n")
    }

    // MARK: Dates

    /// The calendar schedule times are read in: the schedule store's zone,
    /// which is the zone the server's wall-clock times are in. The phone's own zone
    /// when the store names none or one this OS doesn't know.
    static func calendar(storeZone: String?, base: Calendar = .current) -> Calendar {
        var calendar = base
        if let storeZone, let zone = TimeZone(identifier: storeZone) {
            calendar.timeZone = zone
        }
        return calendar
    }

    /// "Changed 3 days ago" for a memory row; "just now" inside a minute,
    /// because the relative formatter says "in 0 seconds" for those.
    static func changed(
        _ timestamp: Double, now: Date = Date(), locale: Locale = .current
    ) -> String {
        let date = Date(timeIntervalSince1970: timestamp)
        if abs(now.timeIntervalSince(date)) < 60 { return "Changed just now" }
        let formatter = RelativeDateTimeFormatter()
        formatter.locale = locale
        formatter.unitsStyle = .full
        return "Changed " + formatter.localizedString(for: date, relativeTo: now)
    }
}
