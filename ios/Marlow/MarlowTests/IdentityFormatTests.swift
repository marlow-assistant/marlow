import Foundation
import Testing
@testable import Marlow

/// The wording and rules behind the Marlow sheet (docs/tasks/ux-lane.md §3a).
struct IdentityFormatTests {
    // MARK: Names

    @Test func memoryNamesDropTheExtensionAndFolderAndHumanise() {
        #expect(IdentityFormat.memoryName("memory/marlow-features.md") == "Marlow features")
        #expect(IdentityFormat.memoryName("travel.md") == "Travel")
        #expect(IdentityFormat.memoryName("memory/some_notes.md") == "Some notes")
    }

    @Test func titlesUseTheStripNamesForTheFixedFiles() {
        #expect(IdentityFormat.title(id: "SOUL.md", kind: .soul) == "Soul")
        #expect(IdentityFormat.title(id: "USER.md", kind: .user) == "You")
        #expect(IdentityFormat.title(id: "CLAUDE.md", kind: .instructions) == "Instructions")
        // A job reads the same here as it does in the activity list.
        #expect(IdentityFormat.title(id: "cron/morning.md", kind: .job) == "Morning briefing")
        #expect(IdentityFormat.title(id: "cron/physio-nudge.md", kind: .job) == "Physio nudge")
    }

    @Test func everyKindHasAnAboutNote() {
        for kind in [IdentityKind.soul, .user, .instructions, .memory, .job, .unknown] {
            #expect(!IdentityFormat.aboutNote(kind).isEmpty)
        }
        #expect(IdentityFormat.aboutNote(.memory).contains("can be out of date"))
    }

    // MARK: Frontmatter

    @Test func frontmatterIsStripped() {
        let text = "---\nname: travel\ndescription: Trips\n---\n\n# Travel\n\nBody"
        #expect(IdentityFormat.stripFrontmatter(text) == "# Travel\n\nBody")
    }

    @Test func textWithoutFrontmatterIsUntouched() {
        let text = "# Title\n\nBody\n---\nmore"
        #expect(IdentityFormat.stripFrontmatter(text) == text)
    }

    /// A rule at the top with no closing fence is not frontmatter; stripping
    /// it would swallow the whole file.
    @Test func anUnclosedLeadingRuleKeepsItsBody() {
        let text = "---\n\nSome body text\n\nMore text"
        #expect(IdentityFormat.stripFrontmatter(text) == text)
    }

    @Test func crlfFencesStillCount() {
        #expect(IdentityFormat.stripFrontmatter("---\r\na: b\r\n---\r\nBody") == "Body")
    }

    @Test func aFenceOnlyInTheMiddleIsNotFrontmatter() {
        let text = "Intro\n---\na: b\n---\nBody"
        #expect(IdentityFormat.stripFrontmatter(text) == text)
    }

    // MARK: Dates

    private var calendar: Calendar {
        var c = Calendar(identifier: .gregorian)
        c.timeZone = TimeZone(identifier: "America/Toronto")!
        return c
    }

    private func at(_ y: Int, _ mo: Int, _ d: Int, _ h: Int, _ mi: Int) -> Date {
        calendar.date(from: DateComponents(year: y, month: mo, day: d, hour: h, minute: mi))!
    }

    @Test func storeCalendarUsesTheStoreZoneNotThePhones() {
        var phone = Calendar(identifier: .gregorian)
        phone.timeZone = TimeZone(identifier: "America/Edmonton")!
        let store = IdentityFormat.calendar(storeZone: "America/Toronto", base: phone)
        #expect(store.timeZone.identifier == "America/Toronto")
        #expect(IdentityFormat.calendar(storeZone: nil, base: phone).timeZone == phone.timeZone)
        #expect(IdentityFormat.calendar(storeZone: "Not/AZone", base: phone).timeZone == phone.timeZone)
    }

    @Test func changedIsRelativeAndSaysJustNowInsideAMinute() {
        let now = at(2026, 10, 14, 9, 0)
        let locale = Locale(identifier: "en_US")
        let threeDaysAgo = now.addingTimeInterval(-3 * 86400).timeIntervalSince1970
        #expect(IdentityFormat.changed(threeDaysAgo, now: now, locale: locale) == "Changed 3 days ago")
        #expect(IdentityFormat.changed(now.timeIntervalSince1970 - 10, now: now, locale: locale) == "Changed just now")
    }
}
