import Foundation

/// Every word, threshold and grouping behind the usage panel and the
/// >= 90% status row (docs/tasks/usage-visibility.md). The server sends
/// fractions, epochs and counts; this is where they become sentences, so a
/// wording tweak never needs a box deploy.
///
/// Two things are deliberately absent. There is no percentage of the PLAN for
/// Marlow's own part: the plan's accounting is not public, so the server
/// cannot measure it and the phone does not pretend (design doc, section 2).
/// And nothing here is phrased as a warning except the one row Mike asked
/// for: he wants fewer excuses not to use Marlow, so the rest stays quiet.
///
/// Times are read in the calendar's zone and locale, which default to the
/// phone's. `now` and `calendar` are parameters, like `ScheduleFormat`'s, so
/// tests pin them.
enum UsageFormat {
    /// At or above this utilization a window earns the yellow status row.
    /// Lives here, not on the server: the 90 is a display decision.
    static let highUsageThreshold = 0.90

    /// What the plan is called when the server doesn't say, or says something
    /// this build has no name for.
    static let fallbackPlanName = "Your Claude plan"

    static let noTurns = "No turns in this window."
    static let noReading = "No reading yet — appears after Marlow's next turn."

    // MARK: Windows

    /// The windows the panel has wording for, in display order. A window the
    /// server passes through under any other name is ignored: its length is
    /// unknown, so it can't be titled honestly.
    static let windowNames = ["five_hour", "seven_day"]

    static func windowTitle(_ name: String) -> String? {
        switch name {
        case "five_hour": return "This 5 hours"
        case "seven_day": return "This week"
        default: return nil
        }
    }

    /// "14" for 0.14. Whole percent, floored at 0.
    static func percent(_ utilization: Double) -> Int {
        Int((max(0, utilization) * 100).rounded())
    }

    // MARK: Plan

    /// "Claude Pro" for "pro", "Claude Max" for "max" (any case, trimmed);
    /// anything else, including nil, is the fallback.
    static func planName(_ subscriptionType: String?) -> String {
        switch subscriptionType?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
        case "pro": return "Claude Pro"
        case "max": return "Claude Max"
        default: return fallbackPlanName
        }
    }

    /// The caveat under the bars. Plan usage is shared with everything else
    /// the user does on Claude, which is why the bars can move without Marlow.
    static func planFootnote(planName: String) -> String {
        if planName == fallbackPlanName {
            return "Marlow shares your Claude plan's limits with everything else you use Claude for."
        }
        return "Marlow shares your \(planName) limits with everything else you use Claude for."
    }

    struct PlanRow: Equatable {
        let id: String
        let title: String
        /// 0...1, for the bar.
        let fraction: Double
        /// "14% used · resets 8:59 PM"
        let detail: String
        /// Tints the bar yellow: at the threshold and not yet reset.
        let isHigh: Bool
    }

    static func planRows(_ plan: UsagePlan, now: Date = Date(), calendar: Calendar = .current) -> [PlanRow] {
        windowNames.compactMap { name in
            guard let window = plan.windows[name], let title = windowTitle(name) else { return nil }
            let percentText = "\(percent(window.utilization))%"
            let resetsAt = window.resetsAt.map { Date(timeIntervalSince1970: $0) }
            let detail: String
            let live = resetsAt.map { $0 > now } ?? true
            if let resetsAt, live {
                detail = "\(percentText) used · resets \(dayTime(resetsAt, now: now, calendar: calendar))"
            } else if resetsAt != nil {
                // The reading is of a window that has since ended; the live
                // one is unknown until Marlow next runs.
                detail = "\(percentText) at last reading · has since reset"
            } else {
                detail = "\(percentText) used"
            }
            return PlanRow(
                id: name, title: title, fraction: min(1, max(0, window.utilization)), detail: detail,
                isHigh: live && window.utilization >= highUsageThreshold
            )
        }
    }

    /// "7:44 PM", with the weekday when it was not today: when the reading was
    /// taken, for the right of the section header. Nil without a timestamp. No
    /// staleness styling: the footnote carries the caveat.
    static func asOfTime(_ plan: UsagePlan, now: Date = Date(), calendar: Calendar = .current) -> String? {
        guard let observed = plan.observedAt else { return nil }
        return dayTime(Date(timeIntervalSince1970: observed), now: now, calendar: calendar)
    }

    // MARK: Conversation

    struct ConversationSummary: Equatable {
        /// "37K tokens"; nil when unmeasured or retired.
        let size: String?
        /// Size over the threshold, 0...1, for the bar; nil without both.
        let fraction: Double?
        /// "Marlow tidies up around 110K"
        let thresholdText: String?
        /// The sentence that stands in for the size, when there is none.
        let notice: String?
        /// "Started 6:00 AM" / "Started yesterday 9:12 PM"
        let started: String?
        /// "Tidied up once" / "Tidied up 3 times"
        let tidiedUp: String?

        /// Start and tidy-ups on one line: "Started 6:00 AM · tidied up once".
        /// Two short facts were two rows (Mike, 2026-10-05: wasted space).
        var history: String? {
            switch (started, tidiedUp) {
            case let (started?, tidiedUp?): return started + " · " + tidiedUp.lowercased()
            case let (started?, nil): return started
            case let (nil, tidiedUp?): return tidiedUp
            case (nil, nil): return nil
            }
        }
    }

    static func conversation(
        _ c: UsageConversation, now: Date = Date(), calendar: Calendar = .current
    ) -> ConversationSummary {
        // A retired session's numbers describe yesterday's conversation: say
        // only that a fresh one is coming, and show nothing else of it.
        if c.retired {
            return ConversationSummary(
                size: nil, fraction: nil, thresholdText: nil,
                notice: "A fresh conversation starts with your next message.",
                started: nil, tidiedUp: nil
            )
        }
        let threshold = c.compactionThresholdTokens.flatMap { $0 > 0 ? $0 : nil }
        var fraction: Double?
        var size: String?
        var notice: String?
        if let tokens = c.contextTokens {
            size = "\(tokenCount(tokens)) tokens"
            fraction = threshold.map { min(1, max(0, Double(tokens) / Double($0))) }
        } else {
            // After a tidy-up the size is unknown until the next turn measures
            // the new, smaller context; say so, or it reads as a broken reading.
            notice = c.compactions > 0
                ? "Tidied up · new size after your next message"
                : "Size shows after your next message"
        }
        return ConversationSummary(
            size: size, fraction: fraction,
            thresholdText: threshold.map { "Marlow tidies up around \(tokenCount($0))" },
            notice: notice,
            started: c.startedAt.map { startedText(Date(timeIntervalSince1970: $0), now: now, calendar: calendar) },
            // The notice already says it was tidied up; the count line would
            // repeat it right underneath.
            tidiedUp: c.contextTokens == nil ? nil : tidiedUpText(c.compactions)
        )
    }

    /// 36,938 -> "37K"; under 1,000 the number itself.
    static func tokenCount(_ tokens: Int) -> String {
        tokens < 1000 ? "\(tokens)" : "\((tokens + 500) / 1000)K"
    }

    private static func tidiedUpText(_ count: Int) -> String? {
        switch count {
        case ..<1: return nil
        case 1: return "Tidied up once"
        default: return "Tidied up \(count) times"
        }
    }

    private static func startedText(_ date: Date, now: Date, calendar: Calendar) -> String {
        let yesterday = calendar.date(byAdding: .day, value: -1, to: now)
        if let yesterday, calendar.isDate(date, inSameDayAs: yesterday) {
            return "Started yesterday " + clock(date, calendar: calendar)
        }
        return "Started " + dayTime(date, now: now, calendar: calendar)
    }

    // MARK: Marlow's turns

    struct MarlowPart: Equatable {
        let id: String
        let title: String
        /// "12 chat · 6 scheduled"; nil when there were no turns.
        let counts: String?
    }

    static func marlowParts(_ marlow: [String: UsageBuckets]) -> [MarlowPart] {
        windowNames.compactMap { name in
            guard let buckets = marlow[name], let title = windowTitle(name) else { return nil }
            return MarlowPart(id: name, title: title, counts: counts(buckets))
        }
    }

    static func counts(_ b: UsageBuckets) -> String? {
        var parts: [String] = []
        if b.chat.turns > 0 { parts.append("\(b.chat.turns) chat") }
        if b.scheduled.turns > 0 { parts.append("\(b.scheduled.turns) scheduled") }
        if b.compaction.turns > 0 {
            parts.append(b.compaction.turns == 1 ? "1 tidy-up" : "\(b.compaction.turns) tidy-ups")
        }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    // MARK: High usage

    struct HighUsage: Equatable {
        let windowTitle: String
        let percent: Int
        /// "8:59 PM" or "Fri 4:59 AM" — without the word "resets".
        let resetText: String
    }

    /// The most-used window at or above the threshold whose reset is still
    /// ahead, else nil. A window already past its reset is a stale reading of
    /// a window that no longer exists: showing it would keep the row up after
    /// the problem has gone.
    static func highUsage(_ report: UsageReport?, now: Date = Date(), calendar: Calendar = .current) -> HighUsage? {
        guard let plan = report?.plan else { return nil }
        var best: (name: String, window: UsageWindow)?
        for name in windowNames {
            guard let window = plan.windows[name], window.utilization >= highUsageThreshold,
                  let resetsAt = window.resetsAt, Date(timeIntervalSince1970: resetsAt) > now
            else { continue }
            if best == nil || window.utilization > best!.window.utilization { best = (name, window) }
        }
        guard let best, let title = windowTitle(best.name), let resetsAt = best.window.resetsAt else { return nil }
        return HighUsage(
            windowTitle: title, percent: percent(best.window.utilization),
            resetText: dayTime(Date(timeIntervalSince1970: resetsAt), now: now, calendar: calendar)
        )
    }

    /// "93% of this 5 hours used — resets 8:59 PM."
    static func highUsageText(_ high: HighUsage) -> String {
        "\(high.percent)% of \(high.windowTitle.lowercased()) used — resets \(high.resetText)."
    }

    // MARK: Dates

    /// "8:59 PM" today, "Fri 4:59 AM" on any other day.
    static func dayTime(_ date: Date, now: Date, calendar: Calendar) -> String {
        if calendar.isDate(date, inSameDayAs: now) { return clock(date, calendar: calendar) }
        return weekday(date, calendar: calendar) + " " + clock(date, calendar: calendar)
    }

    private static func clock(_ date: Date, calendar: Calendar) -> String {
        formatted(date, template: "jmm", calendar: calendar)
            // Recent ICU puts a narrow no-break space before "PM"; a plain
            // space reads the same and keeps the strings stable across OS
            // versions.
            .replacingOccurrences(of: "\u{202F}", with: " ")
    }

    private static func weekday(_ date: Date, calendar: Calendar) -> String {
        formatted(date, template: "EEE", calendar: calendar)
    }

    private static func formatted(_ date: Date, template: String, calendar: Calendar) -> String {
        let formatter = DateFormatter()
        formatter.calendar = calendar
        formatter.timeZone = calendar.timeZone
        formatter.locale = calendar.locale ?? .current
        formatter.setLocalizedDateFormatFromTemplate(template)
        return formatter.string(from: date)
    }
}
