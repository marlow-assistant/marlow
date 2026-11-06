import Foundation
import Testing
@testable import Marlow

// MARK: - Fixtures

/// Wednesday 2026-11-04, 15:00 in UTC (the same instant as `TodoTests`).
private let utc: Calendar = {
    var c = Calendar(identifier: .gregorian)
    c.timeZone = TimeZone(identifier: "UTC")!
    c.locale = Locale(identifier: "en_US")
    return c
}()

private func at(_ month: Int, _ day: Int, _ hour: Int = 15) -> Date {
    utc.date(from: DateComponents(year: 2026, month: month, day: day, hour: hour))!
}

private let now = at(11, 4)

private func todo(
    _ id: String, state: Todo.State = .open, due: String? = nil, closed: Date? = nil, snoozedUntil: Date? = nil
) -> Todo {
    Todo(
        id: id, title: "Title \(id)", notes: nil, due: due, state: state, source: "user",
        created: at(11, 1), closed: closed, snoozedUntil: snoozedUntil
    )
}

@MainActor
final class FakeTodosAPI: TodosAPI {
    var list: Result<[Todo], Error> = .success([])
    var tapResult: Result<Todo, Error> = .failure(URLError(.badServerResponse))
    private(set) var fetchCount = 0
    private(set) var taps: [(action: String, id: String, until: String?)] = []
    /// Runs inside a fetch, after the list was read and before it is returned.
    var duringFetch: (() async -> Void)?
    /// Runs inside a tap, before it answers.
    var duringTap: (() async -> Void)?
    var moveResult: Result<[Todo], Error> = .failure(URLError(.badServerResponse))
    private(set) var moves: [(id: String, before: String?)] = []
    /// Runs inside a move, before it answers.
    var duringMove: (() async -> Void)?

    func todos(deviceToken: String) async throws -> [Todo] {
        fetchCount += 1
        let snapshot = try list.get()
        await duringFetch?()
        return snapshot
    }

    func doneTodo(id: String, deviceToken: String) async throws -> Todo { try await tap("done", id) }
    func dropTodo(id: String, deviceToken: String) async throws -> Todo { try await tap("drop", id) }
    func acceptTodo(id: String, deviceToken: String) async throws -> Todo { try await tap("accept", id) }
    func reopenTodo(id: String, deviceToken: String) async throws -> Todo { try await tap("reopen", id) }
    func snoozeTodo(id: String, until: String, deviceToken: String) async throws -> Todo { try await tap("snooze", id, until) }

    func moveTodo(id: String, before: String?, deviceToken: String) async throws -> [Todo] {
        moves.append((id, before))
        await duringMove?()
        return try moveResult.get()
    }

    private func tap(_ action: String, _ id: String, _ until: String? = nil) async throws -> Todo {
        taps.append((action, id, until))
        await duringTap?()
        return try tapResult.get()
    }
}

@MainActor
struct TodosStoreTests {
    private func makeStore(_ api: FakeTodosAPI) -> TodosStore {
        TodosStore(api: api, deviceToken: "tok", now: { now }, calendar: utc)
    }

    @Test func refreshLoadsTheListAndTheSectionsAndBadgeFollow() async {
        let api = FakeTodosAPI()
        api.list = .success([todo("a", due: "2026-11-01"), todo("b", state: .proposed), todo("c")])
        let store = makeStore(api)
        #expect(store.badgeCount == 0, "nothing before the first fetch")

        await store.refresh()

        #expect(store.todos.map(\.id) == ["a", "b", "c"])
        #expect(store.sections.map(\.title) == ["To do", "Marlow suggested"])
        #expect(store.badgeCount == 2)
    }

    @Test func aFailedFetchKeepsTheLastGoodListAndSaysNothing() async {
        let api = FakeTodosAPI()
        api.list = .success([todo("a")])
        let store = makeStore(api)
        await store.refresh()

        api.list = .failure(URLError(.notConnectedToInternet))
        await store.refresh()

        #expect(store.todos.map(\.id) == ["a"])
        #expect(store.notice == nil)
    }

    @Test func doneReplacesTheItemWithTheReturnedOne() async {
        let api = FakeTodosAPI()
        api.list = .success([todo("a"), todo("b")])
        let store = makeStore(api)
        await store.refresh()
        api.tapResult = .success(todo("a", state: .done, closed: now))

        await store.done(store.todos[0])

        #expect(api.taps.map(\.action) == ["done"])
        #expect(api.taps.map(\.id) == ["a"])
        #expect(store.todos.map(\.state) == [.done, .open], "replaced in place")
        // It stays where it was, struck through, while the panel is open
        // (Mike, 2026-11-04: an item that jumped away on a tick, with no undo).
        #expect(store.sections.map(\.title) == ["To do"])
        #expect(store.sections[0].todos.map(\.id) == ["a", "b"])
        #expect(store.sections[0].todos[0].state == .done)
        #expect(store.notice == nil)
    }

    @Test func reopenUndoesATickInPlaceAndTheNextOpenSortsDoneToTheBottom() async {
        let api = FakeTodosAPI()
        api.list = .success([todo("a"), todo("b")])
        let store = makeStore(api)
        await store.panelOpened()
        api.tapResult = .success(todo("a", state: .done, closed: now))
        await store.done(store.todos[0])

        api.tapResult = .success(todo("a"))
        await store.reopen(store.todos[0])
        #expect(api.taps.map(\.action) == ["done", "reopen"])
        #expect(store.sections[0].todos.map(\.id) == ["a", "b"])
        #expect(store.sections[0].todos[0].state == .open)

        api.tapResult = .success(todo("a", state: .done, closed: now))
        await store.done(store.todos[0])
        api.list = .success([todo("a", state: .done, closed: now), todo("b")])
        await store.panelOpened()
        #expect(store.sections.map(\.title) == ["To do", "Done this week"])
        #expect(store.sections[0].todos.map(\.id) == ["b"])
    }

    @Test func reopeningAnItemFromDoneThisWeekKeepsItThereUntilTheNextOpen() async {
        let api = FakeTodosAPI()
        api.list = .success([todo("a"), todo("b", state: .done, closed: at(11, 3))])
        let store = makeStore(api)
        await store.panelOpened()
        api.tapResult = .success(todo("b"))

        await store.reopen(store.todos[1])

        #expect(store.sections.map(\.title) == ["To do", "Done this week"])
        #expect(store.sections[1].todos.map(\.id) == ["b"])
        #expect(store.sections[1].todos[0].state == .open)
    }

    @Test func acceptMovesASuggestionToTheList() async {
        let api = FakeTodosAPI()
        api.list = .success([todo("a", state: .proposed)])
        let store = makeStore(api)
        await store.refresh()
        api.tapResult = .success(todo("a"))

        await store.accept(store.todos[0])

        #expect(api.taps.map(\.action) == ["accept"])
        #expect(store.sections.map(\.title) == ["To do"])
    }

    @Test func dropTakesTheItemOffTheListWhateverStateTheServerAnswersWith() async {
        let api = FakeTodosAPI()
        api.list = .success([todo("a"), todo("b")])
        let store = makeStore(api)
        await store.refresh()
        api.tapResult = .success(todo("a", state: .dropped))

        await store.drop(store.todos[0])

        #expect(api.taps.map(\.action) == ["drop"])
        #expect(store.todos.map(\.id) == ["b"])
    }

    @Test func snoozeSendsTheInstantWithTheOffsetOfThePhonesZone() async {
        var newYork = Calendar(identifier: .gregorian)
        newYork.timeZone = TimeZone(identifier: "America/New_York")!
        let api = FakeTodosAPI()
        api.list = .success([todo("a")])
        let store = TodosStore(api: api, deviceToken: "tok", now: { now }, calendar: newYork)
        await store.refresh()
        let until = at(11, 5, 14) // 09:00 in New York
        api.tapResult = .success(todo("a", snoozedUntil: until))

        await store.snooze(store.todos[0], until: until)

        #expect(api.taps.first?.action == "snooze")
        #expect(api.taps.first?.until == "2026-11-05T09:00:00-05:00")
        #expect(store.todos[0].snoozedUntil == until)
    }

    @Test(arguments: [404, 409])
    func aRefusedTapRefetchesAndSaysTheItemChanged(status: Int) async {
        let api = FakeTodosAPI()
        api.list = .success([todo("a"), todo("b")])
        let store = makeStore(api)
        await store.refresh()
        api.tapResult = .failure(APIError.server(status: status, detail: "not open"))
        api.list = .success([todo("b")]) // "a" was dropped on another device

        await store.done(store.todos[0])

        #expect(api.fetchCount == 2)
        #expect(store.todos.map(\.id) == ["b"])
        #expect(store.notice == "That to-do changed; here's the latest.")
    }

    @Test func aTapThatNeverReachedTheBoxLeavesTheListAndSaysSoBriefly() async {
        let api = FakeTodosAPI()
        api.list = .success([todo("a")])
        let store = makeStore(api)
        await store.refresh()
        api.tapResult = .failure(URLError(.notConnectedToInternet))

        await store.done(store.todos[0])

        #expect(api.fetchCount == 1, "no refetch for a lost connection")
        #expect(store.todos.map(\.state) == [.open])
        #expect(store.notice == "Couldn't reach Marlow. Try again.")
    }

    @Test func theNoticeGoesWithTheNextSuccessfulTapAndWhenThePanelOpens() async {
        let api = FakeTodosAPI()
        api.list = .success([todo("a"), todo("b")])
        let store = makeStore(api)
        await store.refresh()
        api.tapResult = .failure(URLError(.notConnectedToInternet))
        await store.done(store.todos[0])
        #expect(store.notice != nil)

        api.tapResult = .success(todo("a", state: .done, closed: now))
        await store.done(store.todos[0])
        #expect(store.notice == nil)

        api.tapResult = .failure(URLError(.notConnectedToInternet))
        await store.done(store.todos[1])
        #expect(store.notice != nil)
        await store.panelOpened()
        #expect(store.notice == nil)
    }

    @Test func openingThePanelFetchesAgain() async {
        let api = FakeTodosAPI()
        let store = makeStore(api)
        await store.panelOpened()
        #expect(api.fetchCount == 1)
    }

    @Test func aFetchThatWasInFlightWhenATapLandedCannotBringTheOldStateBack() async {
        let api = FakeTodosAPI()
        api.list = .success([todo("a")])
        let store = makeStore(api)
        await store.refresh()

        // The server's list is read while "a" is still open; Done lands before
        // that (now stale) list is returned.
        api.duringFetch = { [unowned api] in
            api.duringFetch = nil
            api.tapResult = .success(todo("a", state: .done, closed: now))
            await store.done(store.todos[0])
            api.list = .success([todo("a", state: .done, closed: now)])
        }
        await store.refresh()

        #expect(store.todos.map(\.state) == [.done])
    }

    // MARK: Moving

    private func ids(_ store: TodosStore) -> [String] { store.todos.map(\.id) }

    @Test func aMoveShowsAtOnceAndEndsOnTheServersAnswer() async {
        let api = FakeTodosAPI()
        api.list = .success([todo("a"), todo("b")])
        let store = makeStore(api)
        await store.refresh()
        // The answer has more in it: another device added an item.
        api.moveResult = .success([todo("b"), todo("c"), todo("a")])
        var shownWhileWaiting: [String] = []
        api.duringMove = { shownWhileWaiting = store.todos.map(\.id) }

        let task = store.move(store.todos[1], before: "a")
        // Before any await: a drag's row can't wait in mid-air for the answer.
        #expect(ids(store) == ["b", "a"])
        await task.value

        #expect(shownWhileWaiting == ["b", "a"])
        #expect(api.moves.map(\.id) == ["b"])
        #expect(api.moves.first?.before == "a")
        #expect(ids(store) == ["b", "c", "a"])
        #expect(store.notice == nil)
    }

    @Test func aBottomMoveSendsNoAnchor() async {
        let api = FakeTodosAPI()
        api.list = .success([todo("a"), todo("b")])
        let store = makeStore(api)
        await store.refresh()
        api.moveResult = .success([todo("b"), todo("a")])

        await store.move(store.todos[0], before: nil).value

        #expect(api.moves.count == 1)
        #expect(api.moves.first?.before == nil)
        #expect(ids(store) == ["b", "a"])
    }

    @Test(arguments: [404, 409])
    func aRefusedMovePutsTheRowBackRefetchesAndSaysSo(status: Int) async {
        let api = FakeTodosAPI()
        api.list = .success([todo("a"), todo("b")])
        let store = makeStore(api)
        await store.refresh()
        api.moveResult = .failure(APIError.server(status: status, detail: "gone"))
        api.list = .success([todo("a")]) // what really happened to b

        await store.move(store.todos[1], before: "a").value

        #expect(api.fetchCount == 2)
        #expect(ids(store) == ["a"])
        #expect(store.notice == TodoFormat.changedNotice)
    }

    @Test func aMoveThatNeverReachedTheBoxGoesBackAndSaysSo() async {
        let api = FakeTodosAPI()
        api.list = .success([todo("a"), todo("b")])
        let store = makeStore(api)
        await store.refresh()
        api.moveResult = .failure(URLError(.notConnectedToInternet))

        await store.move(store.todos[1], before: "a").value

        #expect(ids(store) == ["a", "b"])
        #expect(store.notice == TodoFormat.unreachableNotice)
    }

    @Test func aFailedMovePutsTheOrderBackAndKeepsWhatElseHappened() async {
        let api = FakeTodosAPI()
        api.list = .success([todo("a"), todo("b"), todo("c")])
        let store = makeStore(api)
        await store.refresh()
        api.moveResult = .failure(URLError(.notConnectedToInternet))
        api.tapResult = .success(todo("b", state: .done, closed: now))
        api.duringMove = { await store.done(store.todos.first { $0.id == "b" }!) }

        await store.move(store.todos[2], before: "a").value

        #expect(ids(store) == ["a", "b", "c"])
        #expect(store.todos.first { $0.id == "b" }?.state == .done)
    }

    @Test func aRowCanMoveOnlyWhenNothingIsInTheWay() async {
        let api = FakeTodosAPI()
        api.list = .success([todo("a"), todo("b")])
        let store = makeStore(api)
        await store.refresh()
        api.moveResult = .success([todo("b"), todo("a")])
        let row = store.todos[1]
        #expect(store.canMove(row))
        var whileMoving = true
        api.duringMove = { whileMoving = store.canMove(row) }

        await store.move(row, before: "a").value

        #expect(!whileMoving)
        #expect(store.canMove(row))
    }

    @Test func aSecondMoveWhileOneIsOnTheWireIsNotSent() async {
        let api = FakeTodosAPI()
        api.list = .success([todo("a"), todo("b"), todo("c")])
        let store = makeStore(api)
        await store.refresh()
        api.moveResult = .success([todo("c"), todo("a"), todo("b")])

        api.duringMove = { [unowned api] in
            api.duringMove = nil
            await store.move(store.todos[2], before: "a").value
        }
        await store.move(store.todos[2], before: "a").value

        #expect(api.moves.count == 1)
    }

    @Test func aFetchThatStartedBeforeAMoveCantPutTheOldOrderBack() async {
        let api = FakeTodosAPI()
        api.list = .success([todo("a"), todo("b")])
        let store = makeStore(api)
        await store.refresh()
        api.moveResult = .success([todo("b"), todo("a")])

        api.duringFetch = { [unowned api] in
            api.duringFetch = nil
            await store.move(store.todos[1], before: "a").value
            api.list = .success([todo("b"), todo("a")])
        }
        await store.refresh()

        #expect(ids(store) == ["b", "a"])
    }

    @Test func aRowBusyWithATapIsNotTappedTwice() async {
        let api = FakeTodosAPI()
        api.list = .success([todo("a")])
        let store = makeStore(api)
        await store.refresh()
        api.tapResult = .success(todo("a", state: .done, closed: now))

        var second = 0
        api.duringTap = { [unowned api] in
            api.duringTap = nil
            second += 1
            await store.done(store.todos[0]) // a second tap while the first is on the wire
        }
        await store.done(store.todos[0])

        #expect(second == 1)
        #expect(api.taps.count == 1)
        #expect(store.busyIds.isEmpty, "released once the tap settles")
    }
}
