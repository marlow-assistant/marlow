import Foundation

/// Grouping and wording for the Schedule panel (docs/tasks/ux-lane.md §3a).
/// The server sends structure (`Recurrence`); every word on screen is made
/// here so a wording tweak never needs a box deploy.
enum ScheduleFormat {
    enum Section: String, CaseIterable, Equatable {
        case reminders = "Reminders"
        case daily = "Daily"
        case weekly = "Weekly"
        case monthly = "Monthly"
        case oneOff = "One-off jobs"
        case other = "Other"
    }

    struct Group: Equatable {
        let section: Section
        let entries: [ScheduleEntry]
    }

    // MARK: Grouping

    static func section(_ entry: ScheduleEntry) -> Section {
        if entry.notify != nil { return .reminders }
        switch entry.recurrence {
        case .daily: return .daily
        case .weekly(let weekdays, _): return Set(weekdays).count == 7 ? .daily : .weekly
        case .monthly: return .monthly
        case .once: return .oneOff
        case .other: return .other
        }
    }

    /// Fixed section order, empty sections omitted. Sorting is stable, so
    /// ties (and the whole of "Other") keep store order.
    static func sections(_ entries: [ScheduleEntry]) -> [Group] {
        Section.allCases.compactMap { section in
            let members = entries.filter { Self.section($0) == section }
            guard !members.isEmpty else { return nil }
            return Group(section: section, entries: sorted(members, in: section))
        }
    }

    private static func sorted(_ entries: [ScheduleEntry], in section: Section) -> [ScheduleEntry] {
        let indexed = Array(entries.enumerated())
        func key(_ entry: ScheduleEntry) -> [Double] {
            switch section {
            case .reminders, .oneOff:
                return [entry.nextFireAt ?? .infinity]
            case .daily:
                return [firstMinute(entry.recurrence)]
            case .weekly:
                return [firstWeekdayRank(entry.recurrence), firstMinute(entry.recurrence)]
            case .monthly:
                return [firstDay(entry.recurrence), firstMinute(entry.recurrence)]
            case .other:
                return []
            }
        }
        return indexed.sorted { a, b in
            let ka = key(a.element), kb = key(b.element)
            for (x, y) in zip(ka, kb) where x != y { return x < y }
            return a.offset < b.offset
        }.map(\.element)
    }

    private static func times(_ recurrence: Recurrence) -> [String] {
        switch recurrence {
        case .daily(let t), .weekly(_, let t), .monthly(_, let t): return t
        case .once, .other: return []
        }
    }

    private static func firstMinute(_ recurrence: Recurrence) -> Double {
        guard let first = times(recurrence).first, let (h, m) = parse(first) else { return .infinity }
        return Double(h * 60 + m)
    }

    /// Monday-first: Mon 0 ... Sat 5, Sun 6.
    private static let mondayFirst = [1, 2, 3, 4, 5, 6, 0]

    private static func firstWeekdayRank(_ recurrence: Recurrence) -> Double {
        guard case .weekly(let days, _) = recurrence else { return .infinity }
        let ranks = days.compactMap { mondayFirst.firstIndex(of: $0) }
        return Double(ranks.min() ?? 99)
    }

    private static func firstDay(_ recurrence: Recurrence) -> Double {
        guard case .monthly(let days, _) = recurrence, let first = days.min() else { return .infinity }
        return Double(first)
    }

    // MARK: Wording

    static func isOverdue(_ entry: ScheduleEntry, now: Date = Date()) -> Bool {
        guard case .once = entry.recurrence, let next = entry.nextFireAt else { return false }
        return next <= now.timeIntervalSince1970
    }

    /// The short right-hand text of a row.
    static func whenText(
        _ entry: ScheduleEntry, calendar: Calendar, now: Date = Date()
    ) -> String {
        let reminder = entry.notify != nil
        switch entry.recurrence {
        case .daily(let t):
            let text = timesText(t)
            return reminder ? join("Daily", text) : text
        case .weekly(let days, let t):
            if Set(days).count == 7 {
                let text = timesText(t)
                return reminder ? join("Daily", text) : text
            }
            return join(weekdaysText(days), timesText(t))
        case .monthly(let days, let t):
            let text = join(days.map(ordinal).joined(separator: ", "), timesText(t))
            return reminder ? join("Monthly", text) : text
        case .once:
            guard let next = entry.nextFireAt else { return entry.at ?? "" }
            return dateText(Date(timeIntervalSince1970: next), calendar: calendar, now: now)
        case .other:
            return entry.cron ?? entry.at ?? ""
        }
    }

    private static func join(_ a: String, _ b: String) -> String {
        [a, b].filter { !$0.isEmpty }.joined(separator: " ")
    }

    private static func parse(_ time: String) -> (Int, Int)? {
        let parts = time.split(separator: ":")
        guard parts.count == 2, let h = Int(parts[0]), let m = Int(parts[1]) else { return nil }
        return (h, m)
    }

    /// "08:03" -> "8:03"; anything unparseable is shown as sent.
    private static func clock(_ time: String) -> String {
        guard let (h, m) = parse(time) else { return time }
        return String(format: "%d:%02d", h, m)
    }

    private static func timesText(_ times: [String]) -> String {
        if times.count > 3 { return "\(times.count)\u{00D7} a day" }
        return times.map(clock).joined(separator: ", ")
    }

    private static let dayNames = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

    private static func weekdaysText(_ days: [Int]) -> String {
        let set = Set(days)
        if set == [1, 2, 3, 4, 5] { return "Weekdays" }
        if set == [0, 6] { return "Weekends" }
        return mondayFirst.filter(set.contains).map { dayNames[$0] }.joined(separator: ", ")
    }

    private static func ordinal(_ n: Int) -> String {
        let suffix: String
        if (11...13).contains(n % 100) {
            suffix = "th"
        } else {
            switch n % 10 {
            case 1: suffix = "st"
            case 2: suffix = "nd"
            case 3: suffix = "rd"
            default: suffix = "th"
            }
        }
        return "\(n)\(suffix)"
    }

    /// "Oct 20, 8:15"; "Jan 5 2027, 8:15" when the year isn't the current one.
    private static func dateText(_ date: Date, calendar: Calendar, now: Date) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.calendar = calendar
        formatter.timeZone = calendar.timeZone
        let sameYear = calendar.component(.year, from: date) == calendar.component(.year, from: now)
        formatter.dateFormat = sameYear ? "MMM d, H:mm" : "MMM d yyyy, H:mm"
        return formatter.string(from: date)
    }
}
