import Foundation

/// The calls "Your day" makes, as a seam for tests.
protocol YourDayAPI {
    func preferences(deviceToken: String) async throws -> Preferences
    func setDay(start: String?, end: String?, checkinEveryHours: Int?, deviceToken: String) async throws -> Preferences
}

extension APIClient: YourDayAPI {}

/// "Your day": when the user's day starts and ends (server/preferences.py).
/// The box sends facts ("HH:MM", a refusal code); the wording is here.
enum YourDay {
    static let saveFailed = "Couldn't save that time."
    static let settingFailed = "Couldn't save that setting."
    static let offline = "Couldn't save. Check your connection."

    /// The 422 `detail` code as one line. Anything unrecognised is the generic
    /// failure, so a code a newer box adds never shows as a raw string.
    static func message(forCode code: String?) -> String {
        switch code {
        case "day_start_range": "Your day has to start between 4 and noon."
        case "day_end_range": "Your day has to end between 8 pm and 3 am."
        case "night_too_short": "Leave at least 4 hours between the end of your day and the next start."
        default: saveFailed
        }
    }

    /// A refusal carries its code; a call that never reached the box is the
    /// connection's fault.
    /// The check-in's refusal is FastAPI's own validation, whose `detail` is not
    /// a code, so for `checkin` any refusal reads the same.
    static func message(for error: Error, checkin: Bool = false) -> String {
        guard let error = error as? APIError else { return offline }
        switch error {
        case .server(_, let detail): return checkin ? settingFailed : message(forCode: detail)
        case .notPaired, .decoding: return checkin ? settingFailed : saveFailed
        }
    }

    /// 0 (off) through 6, the range the box accepts.
    static let checkinOptions = Array(0...6)

    static func checkinLabel(_ hours: Int) -> String {
        switch hours {
        case 0: "Off"
        case 1: "Every hour"
        default: "Every \(hours) hours"
        }
    }

    /// Minutes after midnight for a zero-padded 24h "HH:MM", else nil.
    static func minutes(from hm: String) -> Int? {
        let parts = hm.split(separator: ":", omittingEmptySubsequences: false)
        guard parts.count == 2, parts.allSatisfy({ $0.count == 2 }),
              let h = Int(parts[0]), let m = Int(parts[1]),
              (0..<24).contains(h), (0..<60).contains(m)
        else { return nil }
        return h * 60 + m
    }

    /// A mid-January day: a time of day is all a picker means by its `Date`,
    /// and a day with no clock change in the usual zones keeps 02:30 from
    /// landing in a spring-forward gap.
    private static let referenceDay = DateComponents(year: 2001, month: 1, day: 15)

    /// The `Date` a time picker shows for "HH:MM".
    static func date(from hm: String, calendar: Calendar = .current) -> Date? {
        guard let total = minutes(from: hm) else { return nil }
        var parts = referenceDay
        parts.hour = total / 60
        parts.minute = total % 60
        return calendar.date(from: parts)
    }

    /// "HH:MM" for whatever time of day a picker holds.
    static func string(from date: Date, calendar: Calendar = .current) -> String {
        let parts = calendar.dateComponents([.hour, .minute], from: date)
        return String(format: "%02d:%02d", parts.hour ?? 0, parts.minute ?? 0)
    }
}

/// What Settings shows. Server state only: no device copy and no offline
/// queue, so a failed save puts back what the box last said (unlike face and
/// effort, `PreferenceSync`). `times` stays nil until the box has answered, so
/// the section never shows a time the box didn't send.
@MainActor
final class YourDayModel: ObservableObject {
    struct Times: Equatable {
        var start: String
        var end: String
        /// Hours between check-ins, 0 = off. Nil from a box that predates it.
        var checkinEveryHours: Int?
    }

    enum Field { case start, end }

    @Published private(set) var times: Times?
    /// One line about the last save, nil when it worked.
    @Published private(set) var message: String?
    private var confirmed: Times?

    func load(api: YourDayAPI, deviceToken: String) async {
        guard let prefs = try? await api.preferences(deviceToken: deviceToken) else { return }
        apply(prefs)
    }

    func set(_ field: Field, to value: String, api: YourDayAPI, deviceToken: String) async {
        guard var shown = times else { return }
        let current = field == .start ? shown.start : shown.end
        guard current != value else { return }
        if field == .start { shown.start = value } else { shown.end = value }
        await save(shown, refusal: { YourDay.message(for: $0) }) {
            try await api.setDay(
                start: field == .start ? value : nil,
                end: field == .end ? value : nil,
                checkinEveryHours: nil,
                deviceToken: deviceToken
            )
        }
    }

    func setCheckin(_ hours: Int, api: YourDayAPI, deviceToken: String) async {
        guard var shown = times, shown.checkinEveryHours != nil, shown.checkinEveryHours != hours else { return }
        shown.checkinEveryHours = hours
        await save(shown, refusal: { YourDay.message(for: $0, checkin: true) }) {
            try await api.setDay(start: nil, end: nil, checkinEveryHours: hours, deviceToken: deviceToken)
        }
    }

    /// Shows `next` at once, sends it, and shows what the box answers with (the
    /// whole object); on any failure puts the last confirmed values back.
    private func save(
        _ next: Times, refusal: (Error) -> String, send: () async throws -> Preferences
    ) async {
        message = nil
        times = next
        do {
            guard apply(try await send()) else { throw APIError.server(status: 0, detail: nil) }
        } catch {
            times = confirmed
            message = refusal(error)
        }
    }

    @discardableResult
    private func apply(_ prefs: Preferences) -> Bool {
        guard let start = prefs.dayStart, let end = prefs.dayEnd,
              YourDay.minutes(from: start) != nil, YourDay.minutes(from: end) != nil
        else { return false }
        confirmed = Times(start: start, end: end, checkinEveryHours: prefs.checkinEveryHours)
        times = confirmed
        return true
    }
}
