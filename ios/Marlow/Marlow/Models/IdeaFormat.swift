import Foundation

/// Grouping, expiry and wording for the Ideas panel
/// (docs/tasks/ideas-panel.md, "v1"). The server sends facts (`section`,
/// `until`, `added`, `spotlight`); what is hidden, what is marked as nearing
/// and every word on screen is decided here, so a wording tweak never needs a
/// box deploy, and an idea that expires today is gone from the phone today
/// rather than after the nightly run retires it.
///
/// Days are the phone's own: `until` and `added` are bare `YYYY-MM-DD` dates
/// with no zone, and "today" is read in the caller's calendar. Everything takes
/// the calendar and the date as parameters so a test pins both.
enum IdeaFormat {
    enum Section: String, CaseIterable, Equatable {
        case now
        case standing

        var title: String {
            switch self {
            case .now: return "For you now"
            case .standing: return "Things I can do"
            }
        }
    }

    struct Group: Equatable, Identifiable {
        let section: Section
        let ideas: [Idea]
        var id: Section { section }
        var title: String { section.title }
    }

    // MARK: Wording

    static let emptyLine = "No ideas right now."
    static let doIt = "Do it"
    static let notNow = "Not now"
    static let dismissedRow = "Dismissed \u{2014} add a reason"
    static let reasonPlaceholder = "Why not?"

    /// What "Do it" sends. Just the words: the idea is quoted above them by
    /// `IdeaQuote`, and the plan travels server-side by `idea_id`.
    static let doItMessage = doIt

    // MARK: Days

    /// "2026-10-13" as the start of that day in `calendar`. Anything that is
    /// not exactly `YYYY-MM-DD` is nil, so a malformed date is treated as
    /// absent (never hides an idea, never marks one) rather than guessed at.
    static func day(_ text: String, calendar: Calendar) -> Date? {
        let parts = text.split(separator: "-", omittingEmptySubsequences: false)
        guard parts.count == 3, parts[0].count == 4, parts[1].count == 2, parts[2].count == 2,
              let year = Int(parts[0]), let month = Int(parts[1]), let day = Int(parts[2])
        else { return nil }
        var components = DateComponents()
        components.year = year
        components.month = month
        components.day = day
        guard let date = calendar.date(from: components),
              // `Calendar` rolls "2026-02-31" over into March; a date that does
              // not round-trip was not a date.
              calendar.dateComponents([.year, .month, .day], from: date) == components
        else { return nil }
        return date
    }

    /// Whole days from `today` to the idea's `until`: 0 on the last day,
    /// negative once it has passed, nil with no (readable) `until`.
    static func daysLeft(_ idea: Idea, today: Date, calendar: Calendar) -> Int? {
        guard let until = idea.until, let end = day(until, calendar: calendar) else { return nil }
        return calendar.dateComponents([.day], from: calendar.startOfDay(for: today), to: end).day
    }

    /// `until` is the last day the idea is any use, so it is still shown on
    /// that day and hidden from the next.
    static func isHidden(_ idea: Idea, today: Date, calendar: Calendar) -> Bool {
        (daysLeft(idea, today: today, calendar: calendar) ?? 0) < 0
    }

    /// Today, tomorrow or the day after.
    static func isNearing(_ idea: Idea, today: Date, calendar: Calendar) -> Bool {
        guard let left = daysLeft(idea, today: today, calendar: calendar) else { return false }
        return (0...2).contains(left)
    }

    /// "until Oct 13", only while the idea is nearing its end. A date weeks
    /// out is not worth a line on a card.
    static func untilText(_ idea: Idea, today: Date, calendar: Calendar) -> String? {
        guard isNearing(idea, today: today, calendar: calendar),
              let until = idea.until, let end = day(until, calendar: calendar)
        else { return nil }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.calendar = calendar
        formatter.timeZone = calendar.timeZone
        formatter.dateFormat = "MMM d"
        return "until \(formatter.string(from: end))"
    }

    // MARK: Selection

    /// What the phone may show: not past its `until`, and in a section this
    /// build knows. An unknown section (a newer server) is dropped, not
    /// guessed into one of the two.
    static func visible(_ ideas: [Idea], today: Date, calendar: Calendar) -> [Idea] {
        ideas.filter {
            Section(rawValue: $0.section) != nil && !isHidden($0, today: today, calendar: calendar)
        }
    }

    /// Fixed section order, store order inside each, empty sections omitted.
    static func sections(_ ideas: [Idea], today: Date, calendar: Calendar) -> [Group] {
        let shown = visible(ideas, today: today, calendar: calendar)
        return Section.allCases.compactMap { section in
            let members = shown.filter { $0.section == section.rawValue }
            return members.isEmpty ? nil : Group(section: section, ideas: members)
        }
    }

    /// The idea to offer above the composer: the first marked `spotlight`
    /// that is still shown. The server allows at most one, but an expired one
    /// is hidden here before the nightly run replaces it, and then there is
    /// none.
    static func spotlight(_ ideas: [Idea], today: Date, calendar: Calendar) -> Idea? {
        visible(ideas, today: today, calendar: calendar).first { $0.spotlight }
    }

    /// The lightbulb's dot: has Mike read the list since something was added?
    /// An idea added on a later day than the panel was last opened; never
    /// opened counts as before everything. A spotlight does not raise it: its
    /// card above the composer is already the louder signal, and a dot only
    /// a decision could clear was noise (Mike, 2026-10-12).
    /// Compared by day because `added` has no time: an idea added the same
    /// day the panel was opened was already there to see (the nightly run
    /// writes at 00:30, before Mike is up).
    static func showsDot(_ ideas: [Idea], lastOpened: Date?, today: Date, calendar: Calendar) -> Bool {
        let shown = visible(ideas, today: today, calendar: calendar)
        guard let lastOpened else { return !shown.isEmpty }
        let openedDay = calendar.startOfDay(for: lastOpened)
        return shown.contains { idea in
            guard let added = day(idea.added, calendar: calendar) else { return false }
            return added > openedDay
        }
    }
}
