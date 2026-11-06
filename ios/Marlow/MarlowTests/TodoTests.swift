import Foundation
import Testing
@testable import Marlow

// MARK: - Fixtures

/// Wednesday 2026-11-04, 15:00 in UTC and en_US, so the strings are the same
/// on every machine. (Mon 2 Nov, Fri 6 Nov.)
private func calendar(_ zone: String = "UTC") -> Calendar {
    var c = Calendar(identifier: .gregorian)
    c.timeZone = TimeZone(identifier: zone)!
    c.locale = Locale(identifier: "en_US")
    return c
}

private let utc = calendar()
private let newYork = calendar("America/New_York")

private func at(_ month: Int, _ day: Int, _ hour: Int = 15, _ minute: Int = 0, year: Int = 2026, in cal: Calendar = utc) -> Date {
    cal.date(from: DateComponents(year: year, month: month, day: day, hour: hour, minute: minute))!
}

private let now = at(11, 4)

private func todo(
    _ id: String, state: Todo.State = .open, due: String? = nil, notes: String? = nil,
    created: Date = at(11, 1), closed: Date? = nil, snoozedUntil: Date? = nil
) -> Todo {
    Todo(
        id: id, title: "Title \(id)", notes: notes, due: due, state: state, source: "user",
        created: created, closed: closed, snoozedUntil: snoozedUntil
    )
}

private func decode(_ json: String) throws -> Todo {
    try JSONDecoder().decode(Todo.self, from: Data(json.utf8))
}

// MARK: - Decoding

struct TodoDecodingTests {
    @Test func aFullItemDecodesEveryField() throws {
        let item = try decode("""
        {"id": "t12", "title": "Book the dentist", "notes": "ask about\\nthe cleaning",
         "due": "2026-11-06T09:00:00-05:00", "state": "open", "source": "inferred",
         "created": "2026-11-01T10:30:00+00:00", "closed": "2026-11-03T08:00:00-05:00",
         "snoozed_until": "2026-11-05T09:00:00-05:00"}
        """)
        #expect(item.id == "t12")
        #expect(item.title == "Book the dentist")
        #expect(item.notes == "ask about\nthe cleaning")
        #expect(item.due == "2026-11-06T09:00:00-05:00")
        #expect(item.state == .open)
        #expect(item.source == "inferred")
        #expect(item.created == at(11, 1, 10, 30))
        #expect(item.closed == at(11, 3, 13))
        #expect(item.snoozedUntil == at(11, 5, 14))
    }

    @Test func nullsDecodeAsNone() throws {
        let item = try decode("""
        {"id": "t1", "title": "x", "notes": null, "due": null, "state": "proposed", "source": "user",
         "created": "2026-11-01T10:30:00+00:00", "closed": null, "snoozed_until": null}
        """)
        #expect(item.notes == nil && item.due == nil && item.closed == nil && item.snoozedUntil == nil)
        #expect(item.state == .proposed)
    }

    @Test func aDateDueAndAnInstantDueBothKeepTheirText() throws {
        let day = try decode(#"{"id":"t1","title":"x","due":"2026-11-06","state":"open","source":"user","created":"2026-11-01T10:30:00+00:00"}"#)
        let instant = try decode(#"{"id":"t2","title":"x","due":"2026-11-06T09:00:00-05:00","state":"open","source":"user","created":"2026-11-01T10:30:00+00:00"}"#)
        #expect(day.due == "2026-11-06")
        #expect(TodoFormat.due(day.due, calendar: utc) == .day(at(11, 6, 0)))
        #expect(TodoFormat.due(instant.due, calendar: utc) == .at(at(11, 6, 14)))
    }

    @Test func fractionalSecondsInAnInstantStillDecode() throws {
        let item = try decode(#"{"id":"t1","title":"x","state":"done","source":"user","created":"2026-11-01T10:30:00.123456+00:00","closed":"2026-11-02T10:30:00.5+00:00"}"#)
        #expect(abs(item.created.timeIntervalSince(at(11, 1, 10, 30))) < 1)
        #expect(item.closed != nil)
    }

    @Test func aMalformedItemDropsAloneFromTheList() throws {
        let response = try JSONDecoder().decode(TodosResponse.self, from: Data("""
        {"todos": [
          {"id": "t1", "title": "ok", "state": "open", "source": "user", "created": "2026-11-01T10:30:00+00:00"},
          {"id": "t2", "title": "a state from the future", "state": "parked", "source": "user", "created": "2026-11-01T10:30:00+00:00"},
          {"id": "t3", "title": "no created", "state": "open", "source": "user"}
        ]}
        """.utf8))
        #expect(response.todos.map(\.id) == ["t1"])
    }

    @Test func aBadClosedOrSnoozeReadsAsAbsentNotAsAFailure() throws {
        let item = try decode(#"{"id":"t1","title":"x","state":"open","source":"user","created":"2026-11-01T10:30:00+00:00","closed":"yesterday","snoozed_until":"2026-11-05 09:00"}"#)
        #expect(item.closed == nil && item.snoozedUntil == nil)
    }

    @Test func aTapAnswersWithTheItemUnderATodoKey() throws {
        let reply = try JSONDecoder().decode(TodoResponse.self, from: Data(
            #"{"todo": {"id":"t1","title":"x","state":"dropped","source":"user","created":"2026-11-01T10:30:00+00:00"}}"#.utf8
        ))
        #expect(reply.todo.state == .dropped, "the server answers a drop with the item as it now is")
    }
}

// MARK: - Sections, sort

struct TodoSectionTests {
    private func sections(_ todos: [Todo]) -> [TodoFormat.Group] {
        TodoFormat.sections(todos, now: now, calendar: utc)
    }

    @Test func threeSectionsInFixedOrderAndEmptyOnesAreOmitted() {
        let all = sections([todo("d", state: .done, closed: at(11, 3)), todo("p", state: .proposed), todo("o")])
        #expect(all.map(\.title) == ["To do", "Marlow suggested", "Done this week"])
        #expect(sections([todo("p", state: .proposed)]).map(\.title) == ["Marlow suggested"])
        #expect(sections([]).isEmpty)
    }

    @Test func openItemsKeepTheServersOrderWhateverTheirDues() {
        let list = sections([
            todo("undated"),
            todo("far", due: "2026-12-31"),
            todo("soon", due: "2026-11-05"),
            todo("timed", due: "2026-11-06T09:00:00+00:00"),
        ])
        #expect(list[0].todos.map(\.id) == ["undated", "far", "soon", "timed"])
    }

    @Test func aTickedItemHoldsItsPlaceInThatOrder() {
        let list = TodoFormat.sections(
            [todo("a"), todo("b", state: .done, closed: at(11, 4)), todo("c")],
            placedAs: ["b": todo("b")], now: now, calendar: utc
        )
        #expect(list[0].todos.map(\.id) == ["a", "b", "c"])
    }

    // MARK: Moving

    @Test func theAnchorIsWhatEndsUpJustBelowTheMovedItem() {
        let ids = ["a", "b", "c", "d"]
        #expect(TodoFormat.anchor(ids, moving: "d", to: 0) == "a")
        #expect(TodoFormat.anchor(ids, moving: "a", to: 1) == "c")
        #expect(TodoFormat.anchor(ids, moving: "b", to: 2) == "d")
        #expect(TodoFormat.anchor(ids, moving: "a", to: 3) == nil)
    }

    @Test func reorderedAppliesTheMoveTheServerWill() {
        let list = [todo("a"), todo("x", state: .done), todo("b"), todo("c")]
        #expect(TodoFormat.reordered(list, moving: "c", before: "a").map(\.id) == ["c", "a", "x", "b"])
        #expect(TodoFormat.reordered(list, moving: "c", before: "x").map(\.id) == ["a", "c", "x", "b"])
        #expect(TodoFormat.reordered(list, moving: "a", before: nil).map(\.id) == ["x", "b", "c", "a"])
    }

    @Test func aBottomMoveLandsAfterTheLastOpenItem() {
        let list = [todo("a"), todo("b"), todo("p", state: .proposed)]
        #expect(TodoFormat.reordered(list, moving: "a", before: nil).map(\.id) == ["b", "a", "p"])
    }

    @Test func theMenuOffersOnlyMovesThatDoSomethingDifferent() {
        func titles(_ index: Int, _ count: Int) -> [String] {
            TodoFormat.moveChoices(index: index, count: count).map { "\($0.title) \($0.to)" }
        }
        #expect(titles(0, 1).isEmpty)
        #expect(titles(0, 2) == ["Move down 1"])
        #expect(titles(1, 2) == ["Move up 0"])
        #expect(titles(0, 4) == ["Move down 1", "Move to bottom 3"])
        #expect(titles(2, 5) == ["Move to top 0", "Move up 1", "Move down 3", "Move to bottom 4"])
    }

    @Test func aMoveIsSpokenAsAPlaceInTheList() {
        #expect(TodoFormat.movedLine("Call the garage", to: 0, of: 3) == "Call the garage moved to 1 of 3.")
    }

    @Test func suggestedAreNewestFirstAndDoneAreNewestClosedFirst() {
        let list = sections([
            todo("p1", state: .proposed, created: at(11, 1)),
            todo("p2", state: .proposed, created: at(11, 3)),
            todo("d1", state: .done, closed: at(11, 2)),
            todo("d2", state: .done, closed: at(11, 4, 9)),
        ])
        #expect(list[0].todos.map(\.id) == ["p2", "p1"])
        #expect(list[1].todos.map(\.id) == ["d2", "d1"])
    }

    @Test func aDroppedItemIsNeverShown() {
        #expect(sections([todo("x", state: .dropped)]).isEmpty)
    }
}

// MARK: - Overdue and the badge

struct TodoOverdueTests {
    @Test func aTimedDueBeforeNowIsOverdue() {
        #expect(TodoFormat.isOverdue(todo("a", due: "2026-11-04T14:59:00+00:00"), now: now, calendar: utc))
        #expect(!TodoFormat.isOverdue(todo("a", due: "2026-11-04T15:01:00+00:00"), now: now, calendar: utc))
    }

    @Test func aDateDueBeforeTodayIsOverdueAndTodayIsNot() {
        #expect(TodoFormat.isOverdue(todo("a", due: "2026-11-03"), now: now, calendar: utc))
        #expect(!TodoFormat.isOverdue(todo("a", due: "2026-11-04"), now: now, calendar: utc), "today is still today at 15:00")
        #expect(!TodoFormat.isOverdue(todo("a", due: "2026-11-05"), now: now, calendar: utc))
    }

    @Test func todayIsThePhonesOwnDay() {
        // 2026-11-05 02:00 UTC is still the evening of 4 Nov in New York.
        let lateNY = at(11, 5, 2)
        #expect(!TodoFormat.isOverdue(todo("a", due: "2026-11-04"), now: lateNY, calendar: newYork))
        #expect(TodoFormat.isOverdue(todo("a", due: "2026-11-04"), now: lateNY, calendar: utc))
    }

    @Test func onlyOpenItemsAreOverdueAndNoDueIsNever() {
        #expect(!TodoFormat.isOverdue(todo("a", state: .proposed, due: "2026-11-01"), now: now, calendar: utc))
        #expect(!TodoFormat.isOverdue(todo("a", state: .done, due: "2026-11-01", closed: at(11, 2)), now: now, calendar: utc))
        #expect(!TodoFormat.isOverdue(todo("a"), now: now, calendar: utc))
        #expect(!TodoFormat.isOverdue(todo("a", due: "soon"), now: now, calendar: utc), "a malformed due is absent")
    }

    @Test func theBadgeCountsOverdueOpenItemsAndProposedOnes() {
        let list = [
            todo("overdue-timed", due: "2026-11-04T09:00:00+00:00"),
            todo("overdue-date", due: "2026-11-03"),
            todo("future", due: "2026-11-09"),
            todo("undated"),
            todo("suggested", state: .proposed),
            todo("suggested-overdue", state: .proposed, due: "2026-11-01"),
            todo("done", state: .done, due: "2026-11-01", closed: at(11, 2)),
        ]
        #expect(TodoFormat.badgeCount(list, now: now, calendar: utc) == 4)
        #expect(TodoFormat.badgeCount([], now: now, calendar: utc) == 0)
    }
}

// MARK: - Wording

struct TodoWordingTests {
    private func line(_ due: String?, in cal: Calendar = utc, state: Todo.State = .open) -> String? {
        TodoFormat.dueLine(todo("a", state: state, due: due), now: now, calendar: cal)
    }

    @Test func aDateDueIsNamedByDay() {
        #expect(line("2026-11-04") == "Today")
        #expect(line("2026-11-05") == "Tomorrow")
        #expect(line("2026-11-06") == "Fri 6 Nov")
    }

    @Test func aTimedDueAddsTheLocalTime() {
        #expect(line("2026-11-04T18:00:00+00:00") == "Today 18:00")
        #expect(line("2026-11-05T09:00:00+00:00") == "Tomorrow 09:00")
        #expect(line("2026-11-06T09:05:00+00:00") == "Fri 6 Nov 09:05")
    }

    @Test func theTimeIsReadInThePhonesZone() {
        #expect(line("2026-11-06T09:00:00-05:00") == "Fri 6 Nov 14:00")
        #expect(line("2026-11-06T09:00:00-05:00", in: newYork) == "Fri 6 Nov 09:00")
    }

    @Test func anOverdueDueSaysSoWithTheAbsoluteDay() {
        #expect(line("2026-11-02T14:00:00+00:00") == "Overdue \u{00B7} Mon 2 Nov 14:00")
        #expect(line("2026-11-03") == "Overdue \u{00B7} Tue 3 Nov")
        #expect(line("2026-11-04T09:00:00+00:00") == "Overdue \u{00B7} Wed 4 Nov 09:00")
    }

    @Test func aDueInAnotherYearSaysWhich() {
        #expect(line("2027-01-04") == "Mon 4 Jan 2027")
    }

    @Test func noDueAMalformedDueAndNoOverdueMarkOnAProposedItem() {
        #expect(line(nil) == nil)
        #expect(line("soon") == nil)
        #expect(line("2026-11-02", state: .proposed) == "Mon 2 Nov", "a suggestion is not overdue until accepted")
    }

    @Test func aSnoozeThatHasNotEndedIsNamedAndOneThatHasIsNot() {
        let snoozed = todo("a", snoozedUntil: at(11, 5, 9))
        #expect(TodoFormat.snoozeLine(snoozed, now: now, calendar: utc) == "Snoozed until Tomorrow 09:00")
        let later = todo("a", snoozedUntil: at(11, 8, 18))
        #expect(TodoFormat.snoozeLine(later, now: now, calendar: utc) == "Snoozed until Sun 8 Nov 18:00")
        let over = todo("a", snoozedUntil: at(11, 4, 14))
        #expect(TodoFormat.snoozeLine(over, now: now, calendar: utc) == nil)
        #expect(TodoFormat.snoozeLine(todo("a"), now: now, calendar: utc) == nil)
    }
}

// MARK: - Snooze presets

struct TodoSnoozeTests {
    private func presets(_ date: Date, in cal: Calendar = utc) -> [TodoFormat.SnoozePreset] {
        TodoFormat.snoozePresets(now: date, calendar: cal)
    }

    @Test func threePresetsBeforeFiveInTheAfternoon() {
        let list = presets(at(11, 4, 10))
        #expect(list.map(\.title) == ["In 2 hours", "This evening 18:00", "Tomorrow 09:00"])
        #expect(list.map(\.until) == [at(11, 4, 12), at(11, 4, 18), at(11, 5, 9)])
    }

    @Test func thisEveningIsOfferedOnlyBeforeFive() {
        #expect(presets(at(11, 4, 16, 59)).map(\.title).contains("This evening 18:00"))
        #expect(!presets(at(11, 4, 17, 0)).map(\.title).contains("This evening 18:00"))
        #expect(presets(at(11, 4, 20)).map(\.title) == ["In 2 hours", "Tomorrow 09:00"])
    }

    @Test func tomorrowMorningIsTomorrowEvenJustAfterMidnight() {
        #expect(presets(at(11, 4, 0, 30)).last?.until == at(11, 5, 9))
        #expect(presets(at(11, 4, 23, 30)).last?.until == at(11, 5, 9))
    }

    @Test func theWireInstantCarriesTheOffsetOfTheTargetMoment() {
        let tomorrow = presets(at(11, 4, 10, in: newYork), in: newYork).last!
        #expect(TodoFormat.wireInstant(tomorrow.until, timeZone: newYork.timeZone) == "2026-11-05T09:00:00-05:00")
        // 7 March is EST, 8 March EDT: the offset is the target's, not "now"'s.
        let acrossDST = presets(at(3, 7, 10, in: newYork), in: newYork).last!
        #expect(TodoFormat.wireInstant(acrossDST.until, timeZone: newYork.timeZone) == "2026-03-08T09:00:00-04:00")
    }

    @Test func utcIsWrittenAsAnExplicitZeroOffsetNotZ() {
        #expect(TodoFormat.wireInstant(at(11, 5, 9), timeZone: utc.timeZone) == "2026-11-05T09:00:00+00:00")
    }

    @Test func aPickedTimeDropsSubSeconds() {
        let picked = at(11, 6, 7, 45).addingTimeInterval(0.9)
        #expect(TodoFormat.wireInstant(picked, timeZone: newYork.timeZone) == "2026-11-06T02:45:00-05:00")
    }
}
