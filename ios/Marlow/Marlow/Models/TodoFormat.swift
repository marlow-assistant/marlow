import Foundation

/// Sections, sort order, overdue, the badge and every word on the To-dos panel
/// (docs/tasks/todos-and-reminders.md, part 2). The server sends facts (a
/// state, a due that is a day or an instant, instants); what counts as overdue,
/// where an item sorts and how a due reads is decided here, in the phone's own
/// calendar and zone, so a wording tweak never needs a box deploy.
///
/// Everything takes `now` and the calendar as parameters so a test pins both.
/// A `due` is either a bare `YYYY-MM-DD` day (no zone; the day in the caller's
/// calendar) or an ISO instant with an offset (one moment, shown in the
/// calendar's zone). One this can't read is treated as absent rather than
/// guessed at, like `IdeaFormat`'s dates.
enum TodoFormat {
    enum Section: CaseIterable, Equatable {
        case todo
        case suggested
        case done

        var title: String {
            switch self {
            case .todo: return "To do"
            case .suggested: return "Marlow suggested"
            case .done: return "Done this week"
            }
        }

        fileprivate var state: Todo.State {
            switch self {
            case .todo: return .open
            case .suggested: return .proposed
            case .done: return .done
            }
        }
    }

    struct Group: Equatable, Identifiable {
        let section: Section
        let todos: [Todo]
        var id: String { section.title }
        var title: String { section.title }
    }

    /// A due, resolved: a day (the start of it in the calendar) or a moment.
    enum Due: Equatable {
        case day(Date)
        case at(Date)

        /// What the item sorts and goes overdue by: a day counts from its start.
        var start: Date {
            switch self {
            case .day(let date), .at(let date): return date
            }
        }
    }

    // MARK: Wording

    static let emptyLine = "Nothing on your list. Ask Marlow to add something."
    static let suggestedFootnote = "Marlow suggested these. They won't remind you until you accept."
    static let changedNotice = "That to-do changed; here's the latest."
    static let unreachableNotice = "Couldn't reach Marlow. Try again."
    static let accept = "Accept"
    static let drop = "Drop"
    static let snooze = "Snooze"
    static let talkAbout = "Talk about it"
    static let pickATime = "Pick a time\u{2026}"

    /// What "Talk about it" sends when the chip goes out with nothing typed.
    /// The item is quoted above the words by `IdeaQuote`, and its details
    /// travel server-side by `todo_id`.
    static let talkAboutMessage = "About this to-do"

    /// The words a message about a staged to-do carries: what they typed, or
    /// `talkAboutMessage` when they sent the chip alone. Talk about it stages
    /// the item above the composer instead of sending at once (Mike,
    /// 2026-11-04: a tap that speaks for him isn't talking about it).
    static func talkAboutText(_ typed: String) -> String {
        typed.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? talkAboutMessage : typed
    }

    /// The staged chip's remove control.
    static let unstageLabel = "Don't talk about this to-do"

    /// The emoji on a to-do's quote. The server writes the same one on its row.
    static let quoteEmoji = "\u{2611}\u{FE0F}"

    // MARK: Instants

    private static let isoFormatter: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()

    /// An ISO-8601 instant with an offset, `Z` or `+00:00`, with or without
    /// fractional seconds (the server writes whole seconds, but a hand-edited
    /// store may not). Anything else, a bare local time included, is nil.
    static func instant(_ text: String) -> Date? {
        var base = text
        var fraction = 0.0
        if let dot = text.firstIndex(of: ".") {
            let afterDot = text.index(after: dot)
            let end = text[afterDot...].firstIndex(where: { !$0.isNumber }) ?? text.endIndex
            let digits = text[afterDot..<end]
            guard !digits.isEmpty, let parsed = Double("0." + digits) else { return nil }
            fraction = parsed
            base = String(text[..<dot]) + String(text[end...])
        }
        return isoFormatter.date(from: base)?.addingTimeInterval(fraction)
    }

    /// The instant as the server wants it for a snooze: local wall time with an
    /// explicit offset (`+00:00`, never `Z`), whole seconds.
    static func wireInstant(_ date: Date, timeZone: TimeZone) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.timeZone = timeZone
        formatter.dateFormat = "yyyy-MM-dd'T'HH:mm:ssxxx"
        return formatter.string(from: date)
    }

    static func due(_ text: String?, calendar: Calendar) -> Due? {
        guard let text else { return nil }
        if let day = IdeaFormat.day(text, calendar: calendar) { return .day(day) }
        return instant(text).map(Due.at)
    }

    // MARK: Selection

    /// Fixed section order, empty sections omitted. Dropped items are never
    /// shown (the list does not carry them; a drop's answer does).
    static func sections(_ todos: [Todo], now: Date, calendar: Calendar) -> [Group] {
        Section.allCases.compactMap { section in
            let members = todos.filter { $0.state == section.state }
            guard !members.isEmpty else { return nil }
            return Group(section: section, todos: sorted(members, section: section, calendar: calendar))
        }
    }

    private static func sorted(_ todos: [Todo], section: Section, calendar: Calendar) -> [Todo] {
        let indexed = Array(todos.enumerated())
        let ordered: [(offset: Int, element: Todo)]
        switch section {
        case .todo:
            // Dated and timed by due ascending (a day from its start), then the
            // undated, oldest first. Ties fall back to age, then list order.
            ordered = indexed.sorted { a, b in
                let ad = due(a.element.due, calendar: calendar)?.start
                let bd = due(b.element.due, calendar: calendar)?.start
                switch (ad, bd) {
                case let (x?, y?) where x != y: return x < y
                case (.some, nil): return true
                case (nil, .some): return false
                default:
                    if a.element.created != b.element.created { return a.element.created < b.element.created }
                    return a.offset < b.offset
                }
            }
        case .suggested:
            ordered = indexed.sorted { a, b in
                a.element.created != b.element.created ? a.element.created > b.element.created : a.offset < b.offset
            }
        case .done:
            ordered = indexed.sorted { a, b in
                let ac = a.element.closed ?? .distantPast
                let bc = b.element.closed ?? .distantPast
                return ac != bc ? ac > bc : a.offset < b.offset
            }
        }
        return ordered.map(\.element)
    }

    /// An open item whose due has passed: a timed due before now, a day due
    /// before today. A suggestion is not overdue until it is accepted.
    static func isOverdue(_ todo: Todo, now: Date, calendar: Calendar) -> Bool {
        guard todo.state == .open, let due = due(todo.due, calendar: calendar) else { return false }
        switch due {
        case .day(let day): return day < calendar.startOfDay(for: now)
        case .at(let moment): return moment < now
        }
    }

    /// The icon's badge: overdue open items plus suggestions waiting on an
    /// answer. Zero shows nothing.
    static func badgeCount(_ todos: [Todo], now: Date, calendar: Calendar) -> Int {
        todos.filter { $0.state == .proposed || isOverdue($0, now: now, calendar: calendar) }.count
    }

    // MARK: Due lines

    private static func formatter(_ format: String, calendar: Calendar) -> DateFormatter {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.calendar = calendar
        formatter.timeZone = calendar.timeZone
        formatter.dateFormat = format
        return formatter
    }

    /// "Fri 6 Nov", with the year when it is not this one; "Fri 6 Nov 09:00"
    /// for a moment.
    private static func absolute(_ due: Due, now: Date, calendar: Calendar) -> String {
        let sameYear = calendar.component(.year, from: due.start) == calendar.component(.year, from: now)
        var format = sameYear ? "EEE d MMM" : "EEE d MMM yyyy"
        if case .at = due { format += " HH:mm" }
        return formatter(format, calendar: calendar).string(from: due.start)
    }

    /// "Today", "Tomorrow 09:00", "Fri 6 Nov", "Fri 6 Nov 09:00".
    private static func friendly(_ due: Due, now: Date, calendar: Calendar) -> String {
        let days = calendar.dateComponents(
            [.day], from: calendar.startOfDay(for: now), to: calendar.startOfDay(for: due.start)
        ).day
        let name: String
        switch days {
        case 0: name = "Today"
        case 1: name = "Tomorrow"
        default: return absolute(due, now: now, calendar: calendar)
        }
        guard case .at(let moment) = due else { return name }
        return "\(name) \(formatter("HH:mm", calendar: calendar).string(from: moment))"
    }

    /// The row's due line, nil with no (readable) due. An overdue one says so
    /// and always names the day outright: "Overdue \u{00B7} Mon 2 Nov 14:00".
    static func dueLine(_ todo: Todo, now: Date, calendar: Calendar) -> String? {
        guard let due = due(todo.due, calendar: calendar) else { return nil }
        if isOverdue(todo, now: now, calendar: calendar) {
            return "Overdue \u{00B7} \(absolute(due, now: now, calendar: calendar))"
        }
        return friendly(due, now: now, calendar: calendar)
    }

    /// "Snoozed until Tomorrow 09:00" while the snooze runs; once it has ended
    /// the item is just open again.
    static func snoozeLine(_ todo: Todo, now: Date, calendar: Calendar) -> String? {
        guard let until = todo.snoozedUntil, until > now else { return nil }
        return "Snoozed until \(friendly(.at(until), now: now, calendar: calendar))"
    }

    // MARK: Snooze

    struct SnoozePreset: Equatable, Identifiable {
        let title: String
        let until: Date
        var id: String { title }
    }

    /// In two hours; this evening at 18:00, only while it is before 17:00 (a
    /// snooze to "this evening" at 17:30 would be nearly the same as now);
    /// tomorrow at 09:00. Each is an instant, resolved in the calendar's zone
    /// and sent with that zone's offset at that moment (`wireInstant`).
    static func snoozePresets(now: Date, calendar: Calendar) -> [SnoozePreset] {
        var presets = [SnoozePreset(title: "In 2 hours", until: now.addingTimeInterval(2 * 3600))]
        if calendar.component(.hour, from: now) < 17,
           let evening = calendar.date(bySettingHour: 18, minute: 0, second: 0, of: now) {
            presets.append(SnoozePreset(title: "This evening 18:00", until: evening))
        }
        if let tomorrow = calendar.date(byAdding: .day, value: 1, to: calendar.startOfDay(for: now)),
           let morning = calendar.date(bySettingHour: 9, minute: 0, second: 0, of: tomorrow) {
            presets.append(SnoozePreset(title: "Tomorrow 09:00", until: morning))
        }
        return presets
    }
}
