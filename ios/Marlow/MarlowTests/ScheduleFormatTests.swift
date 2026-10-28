import Foundation
import Testing
@testable import Marlow

/// Grouping and wording for the Schedule panel (docs/tasks/ux-lane.md §3a).
struct ScheduleFormatTests {
    private var calendar: Calendar {
        var c = Calendar(identifier: .gregorian)
        c.timeZone = TimeZone(identifier: "America/Toronto")!
        return c
    }

    private func at(_ y: Int, _ mo: Int, _ d: Int, _ h: Int, _ mi: Int) -> Date {
        calendar.date(from: DateComponents(year: y, month: mo, day: d, hour: h, minute: mi))!
    }

    private func entry(
        _ id: String, _ recurrence: Recurrence, notify: String? = nil,
        next: Date? = nil, cron: String? = nil, at raw: String? = nil
    ) -> ScheduleEntry {
        ScheduleEntry(
            id: id, cron: cron, at: raw, recurrence: recurrence,
            nextFireAt: next?.timeIntervalSince1970, notify: notify, prompt: nil,
            job: notify == nil ? id : nil, jobFile: nil
        )
    }


    private func when(_ e: ScheduleEntry) -> String {
        ScheduleFormat.whenText(e, calendar: calendar, now: at(2026, 10, 14, 9, 0))
    }

    // MARK: Sections

    @Test func sectionsFollowAFixedOrderAndOmitEmptyOnes() {
        let entries = [
            entry("o", .other, cron: "0 8 1 6 *"),
            entry("m", .monthly(days: [1], times: ["08:00"])),
            entry("w", .weekly(weekdays: [1], times: ["08:00"])),
            entry("d", .daily(times: ["08:00"])),
            entry("r", .once, notify: "Do it", next: at(2026, 10, 20, 8, 15)),
            entry("j", .once, next: at(2026, 10, 21, 8, 15)),
        ]
        let groups = ScheduleFormat.sections(entries)
        #expect(groups.map(\.section) == [.reminders, .daily, .weekly, .monthly, .oneOff, .other])
        #expect(groups.map { $0.entries.map(\.id) } == [["r"], ["d"], ["w"], ["m"], ["j"], ["o"]])
        #expect(ScheduleFormat.sections([entry("d", .daily(times: ["08:00"]))]).map(\.section) == [.daily])
        #expect(ScheduleFormat.sections([]).isEmpty)
    }

    @Test func aNotifyEntryIsAReminderWhateverItsRecurrence() {
        let daily = entry("a", .daily(times: ["08:00"]), notify: "x")
        let weekly = entry("b", .weekly(weekdays: [1], times: ["08:00"]), notify: "x")
        #expect(ScheduleFormat.sections([daily, weekly]).map(\.section) == [.reminders])
    }

    @Test func aWeeklyEntryOnAllSevenDaysIsDaily() {
        let all = entry("all", .weekly(weekdays: [0, 1, 2, 3, 4, 5, 6], times: ["09:00"]))
        let six = entry("six", .weekly(weekdays: [1, 2, 3, 4, 5, 6], times: ["09:00"]))
        let groups = ScheduleFormat.sections([all, six])
        #expect(groups.map(\.section) == [.daily, .weekly])
        #expect(when(all) == "9:00")
    }

    @Test func remindersSortBySoonestWithNoTimeLast() {
        let late = entry("late", .once, notify: "x", next: at(2026, 11, 1, 8, 0))
        let none = entry("none", .once, notify: "x")
        let soon = entry("soon", .once, notify: "x", next: at(2026, 10, 16, 8, 0))
        #expect(ScheduleFormat.sections([late, none, soon])[0].entries.map(\.id) == ["soon", "late", "none"])
    }

    @Test func dailyWeeklyMonthlySortWithinTheirSections() {
        let daily = [
            entry("evening", .daily(times: ["20:57"])),
            entry("night", .daily(times: ["01:00"])),
            entry("morning", .daily(times: ["08:00", "20:00"])),
        ]
        #expect(ScheduleFormat.sections(daily)[0].entries.map(\.id) == ["night", "morning", "evening"])

        // Monday-first: Sunday sorts last, then by time.
        let weekly = [
            entry("sun", .weekly(weekdays: [0], times: ["18:00"])),
            entry("mon-late", .weekly(weekdays: [1], times: ["09:00"])),
            entry("mon-early", .weekly(weekdays: [1], times: ["08:03"])),
            entry("fri", .weekly(weekdays: [5], times: ["07:00"])),
        ]
        #expect(ScheduleFormat.sections(weekly)[0].entries.map(\.id) == ["mon-early", "mon-late", "fri", "sun"])

        let monthly = [
            entry("fifteenth", .monthly(days: [15], times: ["08:00"])),
            entry("first-late", .monthly(days: [1, 15], times: ["09:00"])),
            entry("first", .monthly(days: [1], times: ["08:00"])),
        ]
        #expect(ScheduleFormat.sections(monthly)[0].entries.map(\.id) == ["first", "first-late", "fifteenth"])
    }

    @Test func oneOffJobsSortByTimeAndOtherKeepsStoreOrder() {
        let later = entry("later", .once, next: at(2026, 12, 1, 8, 0))
        let sooner = entry("sooner", .once, next: at(2026, 10, 20, 8, 0))
        #expect(ScheduleFormat.sections([later, sooner])[0].entries.map(\.id) == ["sooner", "later"])
        let b = entry("b", .other, cron: "* * * 6 *")
        let a = entry("a", .other, cron: "* * * 7 *")
        #expect(ScheduleFormat.sections([b, a])[0].entries.map(\.id) == ["b", "a"])
    }

    // MARK: Wording

    @Test func dailyShowsItsTimesAndCollapsesPastThree() {
        #expect(when(entry("a", .daily(times: ["08:00"]))) == "8:00")
        #expect(when(entry("a", .daily(times: ["08:00", "20:00"]))) == "8:00, 20:00")
        #expect(when(entry("a", .daily(times: ["01:00", "02:00", "03:00"]))) == "1:00, 2:00, 3:00")
        let many = (0..<18).map { String(format: "%02d:%02d", 8 + $0 / 2, ($0 % 2) * 30) }
        #expect(when(entry("a", .daily(times: many))) == "18\u{00D7} a day")
    }

    @Test func weeklyNamesDaysMondayFirst() {
        #expect(when(entry("a", .weekly(weekdays: [1], times: ["08:03"]))) == "Mon 8:03")
        #expect(when(entry("a", .weekly(weekdays: [0], times: ["18:00"]))) == "Sun 18:00")
        #expect(when(entry("a", .weekly(weekdays: [1, 2, 3, 4, 5], times: ["08:00"]))) == "Weekdays 8:00")
        #expect(when(entry("a", .weekly(weekdays: [0, 6], times: ["08:00"]))) == "Weekends 8:00")
        #expect(when(entry("a", .weekly(weekdays: [0, 5, 3, 1], times: ["08:00"]))) == "Mon, Wed, Fri, Sun 8:00")
        #expect(when(entry("a", .weekly(weekdays: [5, 3, 1], times: ["08:00"]))) == "Mon, Wed, Fri 8:00")
        #expect(when(entry("a", .weekly(weekdays: [1], times: ["01:00", "02:00", "03:00", "04:00"]))) == "Mon 4\u{00D7} a day")
    }

    @Test func monthlyUsesOrdinals() {
        #expect(when(entry("a", .monthly(days: [1], times: ["08:00"]))) == "1st 8:00")
        #expect(when(entry("a", .monthly(days: [1, 15], times: ["08:00"]))) == "1st, 15th 8:00")
        let days = [2, 3, 4, 11, 12, 13, 21, 22, 23, 31]
        #expect(when(entry("a", .monthly(days: days, times: ["08:00"])))
                == "2nd, 3rd, 4th, 11th, 12th, 13th, 21st, 22nd, 23rd, 31st 8:00")
    }

    @Test func onceReadsTheDateInTheStoreZoneWithTheYearOnlyIfNotCurrent() {
        let this = entry("a", .once, next: at(2026, 10, 20, 8, 15))
        #expect(when(this) == "Oct 20, 8:15")
        let next = entry("a", .once, next: at(2027, 1, 5, 8, 15))
        #expect(when(next) == "Jan 5 2027, 8:15")
        // Store zone, not the phone's: 8:15 Toronto stays 8:15 from Edmonton.
        var phone = Calendar(identifier: .gregorian)
        phone.timeZone = TimeZone(identifier: "America/Edmonton")!
        let store = IdentityFormat.calendar(storeZone: "America/Toronto", base: phone)
        #expect(ScheduleFormat.whenText(this, calendar: store, now: at(2026, 10, 14, 9, 0)) == "Oct 20, 8:15")
    }

    @Test func aOnceWithNoInstantShowsItsRawAt() {
        #expect(when(entry("a", .once, at: "2026-10-20T08:15-0400")) == "2026-10-20T08:15-0400")
    }

    @Test func otherShowsTheRawCron() {
        #expect(when(entry("a", .other, cron: "0 8 1 6 *")) == "0 8 1 6 *")
    }

    @Test func recurringRemindersCarryTheirCadence() {
        #expect(when(entry("a", .daily(times: ["08:00"]), notify: "x")) == "Daily 8:00")
        #expect(when(entry("a", .weekly(weekdays: [1], times: ["08:03"]), notify: "x")) == "Mon 8:03")
        #expect(when(entry("a", .monthly(days: [1], times: ["08:00"]), notify: "x")) == "Monthly 1st 8:00")
        #expect(when(entry("a", .once, notify: "x", next: at(2026, 10, 20, 8, 15))) == "Oct 20, 8:15")
    }

    // MARK: Overdue

    @Test func onlyAPastOnceIsOverdue() {
        let now = at(2026, 10, 14, 9, 0)
        #expect(ScheduleFormat.isOverdue(entry("a", .once, next: at(2026, 10, 14, 8, 0)), now: now))
        #expect(ScheduleFormat.isOverdue(entry("a", .once, next: now), now: now))
        #expect(!ScheduleFormat.isOverdue(entry("a", .once, next: at(2026, 10, 15, 8, 0)), now: now))
        #expect(!ScheduleFormat.isOverdue(entry("a", .once), now: now))
        #expect(!ScheduleFormat.isOverdue(entry("a", .daily(times: ["08:00"]), next: at(2026, 10, 14, 8, 0)), now: now))
    }
}
