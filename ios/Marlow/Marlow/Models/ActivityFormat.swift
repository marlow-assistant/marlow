import Foundation

/// How an activity-log record reads on screen (docs/tasks/ux-lane.md §2c).
///
/// Pure, and separate from the view, because the row IS this text and the
/// wording is expected to change after Mike has lived with it for a few days.
///
/// **Tool names select wording; they are never shown.** Same rule as
/// `TurnActivity.label`: MCP connectors choose those names, so an unknown one
/// is semi-trusted text, not copy. A known service gets its name ("Gmail"),
/// anything else counts as "another tool". Targets (file paths, search
/// queries) ARE shown — they are Marlow's own tool input, filtered by the
/// server's allowlist, and rendered as plain text, never markdown.
enum ActivityFormat {
    // MARK: Title

    /// Cron job ids with a better name than their id. Anything else is the
    /// id humanised, which covers schedules entries Marlow names itself.
    private static let jobTitles: [String: String] = [
        "morning": "Morning briefing",
        "consolidate": "Nightly tidy",
        "ideas": "Overnight ideas",
        // The inbox check. "Email flash" is our name for it, not the user's
        // (Taylor's first run, docs/tasks/onboarding-v2.md).
        "email-flash": "Check-in",
        "weekly-digest": "Weekly digest",
    ]

    static func title(_ entry: ActivityEntry) -> String {
        if TurnSource.isChat(entry.source) { return "Chat" }
        guard let label = entry.label, !label.isEmpty else { return "Scheduled job" }
        return jobTitle(label)
    }

    /// A schedule id as a title. Shared with the Schedule panel and job
    /// files (docs/tasks/ux-lane.md §3a) so a job reads the same everywhere.
    static func jobTitle(_ id: String) -> String {
        jobTitles[id] ?? humanise(id)
    }

    /// "physio-nudge" -> "Physio nudge": separators to spaces, first letter up.
    static func humanise(_ id: String) -> String {
        let words = id.replacingOccurrences(of: "-", with: " ")
            .replacingOccurrences(of: "_", with: " ")
        return words.prefix(1).uppercased() + words.dropFirst()
    }

    // MARK: Services

    /// A connector's service name, from its tool name. Substring matches for
    /// the same reason `TurnActivity.label` uses them: the prefix is the
    /// connector's, the service word is what stays stable.
    static func service(forTool name: String) -> String? {
        let n = name.lowercased()
        guard n.hasPrefix("mcp__") else { return nil }
        if n.contains("gmail") { return "Gmail" }
        if n.contains("calendar") { return "Calendar" }
        if n.contains("trello") { return "Trello" }
        if n.contains("drive") { return "Drive" }
        return nil
    }

    private static let readTools: Set<String> = ["Read"]
    private static let writeTools: Set<String> = ["Edit", "Write", "NotebookEdit"]
    private static let fileSearchTools: Set<String> = ["Grep", "Glob"]

    // MARK: Summary

    /// "Checked Gmail, Calendar · read travel.md · changed travel.md". Empty
    /// for a turn that used no tools and changed nothing.
    static func summary(_ entry: ActivityEntry) -> String {
        var services: [String] = []
        var reads: [String?] = []
        var searches: [String?] = []
        var fetches: [String] = []
        var changed: [String] = []
        var fileSearches = 0
        var commands = 0
        var other = entry.toolsDropped

        for call in entry.tools {
            if let service = service(forTool: call.name) {
                appendUnique(service, to: &services)
            } else if readTools.contains(call.name) {
                reads.append(call.target.map(fileName))
            } else if writeTools.contains(call.name) {
                if let target = call.target { appendUnique(fileName(target), to: &changed) }
            } else if call.name == "WebSearch" {
                searches.append(call.target)
            } else if call.name == "WebFetch" {
                if let host = call.target { appendUnique(host, to: &fetches) } else { other += 1 }
            } else if fileSearchTools.contains(call.name) {
                fileSearches += 1
            } else if call.name == "Bash" {
                commands += 1
            } else {
                other += 1
            }
        }
        // The commit is the authority on what changed in the identity dir;
        // Edit targets add files outside it, which never commit.
        for file in entry.files { appendUnique(fileName(file), to: &changed) }

        var phrases: [String] = []
        if !services.isEmpty { phrases.append("checked " + services.joined(separator: ", ")) }
        if !reads.isEmpty { phrases.append(readPhrase(reads)) }
        if !searches.isEmpty { phrases.append(searchPhrase(searches)) }
        if !fetches.isEmpty { phrases.append("opened " + list(fetches)) }
        if fileSearches > 0 { phrases.append("searched files") }
        if commands > 0 { phrases.append(commands == 1 ? "ran a command" : "ran \(commands) commands") }
        if !changed.isEmpty { phrases.append("changed " + list(changed)) }
        if other > 0 { phrases.append(other == 1 ? "used another tool" : "used \(other) other tools") }

        guard let first = phrases.first else { return "" }
        phrases[0] = first.prefix(1).uppercased() + first.dropFirst()
        return phrases.joined(separator: " · ")
    }

    /// The row's second line. `summary`, or for a turn that used no tools,
    /// "Replied" — unless it failed, when it did not reply at all and the
    /// flags line's "Didn't finish" says so alone (nil: no line). Showing
    /// "Replied" over a turn the API killed was the first device pass's
    /// finding, 2026-09-28.
    static func summaryLine(_ entry: ActivityEntry) -> String? {
        let summary = summary(entry)
        if !summary.isEmpty { return summary }
        return entry.status == "error" ? nil : "Replied"
    }

    /// One part of the flags line. `isWarning` parts render orange; Mike's
    /// own approvals are a record, not a problem, and render grey — the whole
    /// line going orange made "you approved 4" read as a warning (device
    /// pass, 2026-09-28).
    struct Flag: Equatable {
        let text: String
        let isWarning: Bool
    }

    /// What went wrong or needed Mike, in one line; empty when nothing did.
    static func flags(_ entry: ActivityEntry) -> [Flag] {
        var parts: [Flag] = []
        if entry.status == "error" { parts.append(Flag(text: "Didn't finish", isWarning: true)) }
        let approved = entry.approvals.filter { $0.decision == "allow" }.count
        let declined = entry.approvals.filter { $0.decision == "deny" }.count
        let missed = entry.approvals.filter { $0.decision == "timeout" }.count
        if approved > 0 { parts.append(Flag(text: "you approved \(approved)", isWarning: false)) }
        if declined > 0 { parts.append(Flag(text: "you declined \(declined)", isWarning: true)) }
        if missed > 0 {
            parts.append(Flag(text: missed == 1 ? "1 request missed" : "\(missed) requests missed", isWarning: true))
        }
        // Denials include the ones Mike declined above; count the rest.
        let blocked = max(0, entry.denials.count - declined - missed)
        if blocked > 0 { parts.append(Flag(text: "\(blocked) blocked", isWarning: true)) }
        if let first = parts.first {
            parts[0] = Flag(text: first.text.prefix(1).uppercased() + first.text.dropFirst(), isWarning: first.isWarning)
        }
        return parts
    }

    /// Whether a record earns a row. A chat turn that used no tools is just
    /// the conversation, already on screen in the transcript; every scheduled
    /// turn shows, even an idle one, because "did the morning job run" is
    /// half of what this log is for.
    static func isNoteworthy(_ entry: ActivityEntry) -> Bool {
        !TurnSource.isChat(entry.source)
            || !entry.tools.isEmpty
            || entry.toolsDropped > 0
            || !entry.files.isEmpty
            || !entry.denials.isEmpty
            || !entry.approvals.isEmpty
            || entry.status == "error"
    }

    // MARK: Background jobs

    /// The harness's own upkeep (docs/tasks/default-infrastructure.md, tier
    /// 1): the nightly tidy and overnight ideas. Shown unless the user turns
    /// off "Show background jobs".
    static func isBackground(_ entry: ActivityEntry) -> Bool {
        entry.tier == "plumbing"
    }

    /// Whether a record gets a row, given this device's background toggle. A
    /// background job that didn't finish shows regardless: a failed tidy means
    /// yesterday's conversation carried into today, and the toggle must never
    /// hide that.
    static func isVisible(_ entry: ActivityEntry, showBackground: Bool) -> Bool {
        guard isNoteworthy(entry) else { return false }
        return showBackground || !isBackground(entry) || entry.status != "done"
    }

    /// What a job said, for the expanded row. A job with nothing to say
    /// replies with the server's silent token, which is a mechanism, not
    /// something to show.
    static func outputText(_ text: String) -> String {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let bare = trimmed.trimmingCharacters(in: CharacterSet(charactersIn: "`"))
            .trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return bare.isEmpty || bare == "[silent]" ? "Nothing to report." : trimmed
    }

    // MARK: Detail

    struct DetailLine: Equatable {
        let label: String
        let target: String?
        /// How many calls this line stands for; 1 for most.
        var count: Int

        /// "×3" after a merged line; nil for a single call.
        var countText: String? { count > 1 ? "×\(count)" : nil }
    }

    /// One line per distinct tool call for the expanded row: a plain label,
    /// plus the target when there is one. Repeats of the same label and target
    /// merge into one line with a count, in the order they first appeared, so
    /// reading travel.md three times is "Read travel.md ×3", not three
    /// identical lines (Mike, 2026-10-21). Merged here, not on the server: the
    /// record is a true log of every call, and a year of stored records
    /// already has the repeats.
    static func detailLines(_ entry: ActivityEntry) -> [DetailLine] {
        var lines: [DetailLine] = []
        for call in entry.tools {
            let label = toolLabel(call.name)
            if let i = lines.firstIndex(where: { $0.label == label && $0.target == call.target }) {
                lines[i].count += 1
            } else {
                lines.append(DetailLine(label: label, target: call.target, count: 1))
            }
        }
        if entry.toolsDropped > 0 {
            lines.append(DetailLine(label: "\(entry.toolsDropped) more not recorded", target: nil, count: 1))
        }
        return lines
    }

    static func toolLabel(_ name: String) -> String {
        if let service = service(forTool: name) { return service }
        switch name {
        case "Read": return "Read"
        case "Edit", "Write", "NotebookEdit": return "Changed"
        case "WebSearch": return "Web search"
        case "WebFetch": return "Opened"
        case "Grep", "Glob": return "Searched files"
        case "Bash": return "Command"
        default: return "Other tool"
        }
    }

    /// "14s", "2m 5s"; nil without a start time.
    static func duration(_ entry: ActivityEntry) -> String? {
        guard let started = entry.startedAt else { return nil }
        let seconds = max(0, Int((entry.finishedAt - started).rounded()))
        if seconds < 60 { return "\(seconds)s" }
        let minutes = seconds / 60
        let rest = seconds % 60
        return rest == 0 ? "\(minutes)m" : "\(minutes)m \(rest)s"
    }

    /// The expanded row's last line: "Took 14s · Sonnet 5.5 · High effort".
    /// Whatever is known, in that order; nil when nothing is (an old record
    /// with no start time).
    static func runLine(_ entry: ActivityEntry) -> String? {
        var parts: [String] = []
        if let duration = duration(entry) { parts.append("Took \(duration)") }
        if let model = entry.model, !model.isEmpty { parts.append(modelName(model)) }
        if let effort = entry.effort, !effort.isEmpty {
            let word = ThinkingEffort(rawValue: effort)?.displayName ?? effort
            parts.append("\(word) effort")
        }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    /// "claude-sonnet-5-5" -> "Sonnet 5.5", "claude-opus-5" -> "Opus 5". The
    /// version is the point here, unlike Settings' tier-only name: this says
    /// what actually ran, and an alias quietly moving to a new model is one of
    /// the things it should show. Version parts are short numbers, so a date
    /// suffix stops the version rather than joining it. Anything else falls
    /// back to `capitalizedModelName`, which leaves an unfamiliar id as is.
    static func modelName(_ id: String) -> String {
        let parts = id.split(separator: "-").map(String.init)
        guard parts.count >= 2, parts[0].lowercased() == "claude" else {
            return id.capitalizedModelName
        }
        let version = parts.dropFirst(2)
            .prefix { $0.count <= 2 && $0.allSatisfy(\.isNumber) }
        let tier = parts[1].capitalized
        return version.isEmpty ? tier : "\(tier) \(version.joined(separator: "."))"
    }

    // MARK: Helpers

    private static func readPhrase(_ reads: [String?]) -> String {
        let named = reads.compactMap { $0 }.reduce(into: [String]()) { appendUnique($1, to: &$0) }
        if named.isEmpty {
            return reads.count == 1 ? "read a file" : "read \(reads.count) files"
        }
        return "read " + list(named)
    }

    private static func searchPhrase(_ searches: [String?]) -> String {
        guard let first = searches.compactMap({ $0 }).first else { return "searched the web" }
        let more = searches.count - 1
        return "searched “\(first)”" + (more > 0 ? " +\(more) more" : "")
    }

    /// Two names, then a count: a row is one glance, not an inventory.
    private static func list(_ items: [String]) -> String {
        if items.count <= 2 { return items.joined(separator: ", ") }
        return items.prefix(2).joined(separator: ", ") + " +\(items.count - 2) more"
    }

    private static func fileName(_ path: String) -> String {
        (path as NSString).lastPathComponent
    }

    private static func appendUnique(_ item: String, to items: inout [String]) {
        if !items.contains(item) { items.append(item) }
    }
}
