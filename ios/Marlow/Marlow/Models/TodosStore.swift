import Foundation

/// The calls `TodosStore` makes, as a seam for tests and `UIFixture` (same
/// shape as `IdeasAPI`).
protocol TodosAPI {
    func todos(deviceToken: String) async throws -> [Todo]
    func doneTodo(id: String, deviceToken: String) async throws -> Todo
    /// Undoes a done (the untick).
    func reopenTodo(id: String, deviceToken: String) async throws -> Todo
    func dropTodo(id: String, deviceToken: String) async throws -> Todo
    func acceptTodo(id: String, deviceToken: String) async throws -> Todo
    /// `until` is an ISO instant with an offset (`TodoFormat.wireInstant`).
    func snoozeTodo(id: String, until: String, deviceToken: String) async throws -> Todo
    /// Puts an open item just above `before`, or at the bottom for nil.
    /// Answers with the whole list.
    func moveTodo(id: String, before: String?, deviceToken: String) async throws -> [Todo]
}

extension APIClient: TodosAPI {}

/// What the To-dos panel and the icon's badge show
/// (docs/tasks/todos-and-reminders.md, part 2). Like `IdeasStore` it lives as
/// long as the chat screen, because the badge is visible without the panel
/// open, so something has to have fetched.
///
/// **Quiet on a failed fetch**, by design (Mike: nothing off-putting in chat):
/// the last good list stays and nothing is said.
///
/// **Taps are not optimistic.** The server decides whether an item can take a
/// tap (a done item can't be snoozed; another device may have got there
/// first), so a tap waits for the answer and the row is replaced with the item
/// the server returns. The row is `busy` meanwhile, so a double tap is one
/// call. A 404 or 409 means the list on screen was out of date: refetch it and
/// say so in one calm line. A tap that never reached the box leaves the list
/// as it was and says so. The line lives in the panel, never in the chat.
///
/// **A move is the exception**: a dragged row can't wait in mid-air, so it
/// shows at once, then the server's list replaces it, and a failure puts the
/// old order back (`move`).
@MainActor
final class TodosStore: ObservableObject {
    /// The server's list in list order, with taps applied. Grouping and order
    /// are `TodoFormat`'s.
    @Published private(set) var todos: [Todo] = []
    /// A line for the panel about the last tap, nil when it worked.
    @Published private(set) var notice: String?
    /// Ids with a tap on the wire.
    @Published private(set) var busyIds: Set<String> = []
    /// Items ticked or unticked since the panel opened, as they were before
    /// the first such tap. They keep that place, struck through or not, so a
    /// tick can be seen and taken back (Mike, 2026-11-04: a ticked item jumped
    /// to the bottom with no undo). The next open lets them sort.
    private var held: [String: Todo] = [:]

    private let api: TodosAPI
    private let deviceToken: String
    private let now: () -> Date
    private let calendar: Calendar
    private var isFetching = false
    /// Bumped by every tap that changed the list. A fetch that started before
    /// one carries the state from before it, so its result is not applied.
    private var generation = 0
    /// One move at a time: each answer is the whole list, and a later move
    /// restoring an earlier snapshot could lose the first. A second drag in
    /// that moment snaps back, which beats an order nobody chose.
    private var isMoving = false

    init(
        api: TodosAPI = APIClient(), deviceToken: String,
        now: @escaping () -> Date = { Date() }, calendar: Calendar = .current
    ) {
        self.api = api
        self.deviceToken = deviceToken
        self.now = now
        self.calendar = calendar
    }

    // MARK: Derived

    var sections: [TodoFormat.Group] {
        TodoFormat.sections(todos, placedAs: held, now: now(), calendar: calendar)
    }

    /// The icon's badge. Read against the clock each time it is drawn, so an
    /// item going overdue is counted at the next redraw, not only at a fetch.
    var badgeCount: Int {
        TodoFormat.badgeCount(todos, now: now(), calendar: calendar)
    }

    func isOverdue(_ todo: Todo) -> Bool {
        TodoFormat.isOverdue(todo, now: now(), calendar: calendar)
    }

    func dueLine(_ todo: Todo) -> String? {
        TodoFormat.dueLine(todo, now: now(), calendar: calendar)
    }

    func snoozeLine(_ todo: Todo) -> String? {
        TodoFormat.snoozeLine(todo, now: now(), calendar: calendar)
    }

    func snoozePresets() -> [TodoFormat.SnoozePreset] {
        TodoFormat.snoozePresets(now: now(), calendar: calendar)
    }

    // MARK: Fetch

    /// On panel open and on app foreground. A failure keeps what was shown and
    /// shows nothing (type comment).
    func refresh() async {
        guard !isFetching else { return }
        isFetching = true
        defer { isFetching = false }
        // A tap that lands while the list is on the wire makes that list stale;
        // ask again rather than draw what a tap just undid. Bounded: taps are
        // human-paced.
        for _ in 0..<2 {
            let started = generation
            guard let fetched = try? await api.todos(deviceToken: deviceToken) else { return }
            if generation == started {
                todos = fetched.filter { $0.state != .dropped }
                return
            }
        }
    }

    /// Opening the panel fetches fresh and clears the last tap's line.
    func panelOpened() async {
        notice = nil
        held = [:]
        await refresh()
    }

    // MARK: Taps

    func done(_ todo: Todo) async {
        await tap(todo, holdsPlace: true) { [api, deviceToken] in
            try await api.doneTodo(id: todo.id, deviceToken: deviceToken)
        }
    }

    func reopen(_ todo: Todo) async {
        await tap(todo, holdsPlace: true) { [api, deviceToken] in
            try await api.reopenTodo(id: todo.id, deviceToken: deviceToken)
        }
    }

    func drop(_ todo: Todo) async {
        await tap(todo) { [api, deviceToken] in try await api.dropTodo(id: todo.id, deviceToken: deviceToken) }
    }

    func accept(_ todo: Todo) async {
        await tap(todo) { [api, deviceToken] in try await api.acceptTodo(id: todo.id, deviceToken: deviceToken) }
    }

    /// The instant is sent with the offset of the phone's own zone.
    func snooze(_ todo: Todo, until: Date) async {
        let wire = TodoFormat.wireInstant(until, timeZone: calendar.timeZone)
        await tap(todo) { [api, deviceToken] in
            try await api.snoozeTodo(id: todo.id, until: wire, deviceToken: deviceToken)
        }
    }

    /// False while another move is on the wire or the row is busy, when `move`
    /// would ignore it.
    func canMove(_ todo: Todo) -> Bool {
        !isMoving && !busyIds.contains(todo.id)
    }

    /// The list changes before this returns, not in the task: `List.onMove`
    /// needs its data moved when the closure ends, or the row jumps back for a
    /// frame. The task is the wire, for tests to wait on.
    @discardableResult
    func move(_ todo: Todo, before: String?) -> Task<Void, Never> {
        guard canMove(todo) else { return Task {} }
        isMoving = true
        let shown = todos
        todos = TodoFormat.reordered(todos, moving: todo.id, before: before)
        generation += 1 // a fetch in the air carries the old order
        return Task { [api, deviceToken] in
            defer { isMoving = false }
            do {
                let fresh = try await api.moveTodo(id: todo.id, before: before, deviceToken: deviceToken)
                notice = nil
                generation += 1
                todos = fresh.filter { $0.state != .dropped }
            } catch APIError.server(let status, _) where status == 404 || status == 409 {
                todos = TodoFormat.inOrder(todos, of: shown)
                notice = TodoFormat.changedNotice
                generation += 1
                await refresh()
            } catch {
                todos = TodoFormat.inOrder(todos, of: shown)
                notice = TodoFormat.unreachableNotice
            }
        }
    }

    private func tap(_ todo: Todo, holdsPlace: Bool = false, _ send: () async throws -> Todo) async {
        guard !busyIds.contains(todo.id) else { return }
        busyIds.insert(todo.id)
        defer { busyIds.remove(todo.id) }
        do {
            let answered = try await send()
            notice = nil
            generation += 1
            if holdsPlace, held[todo.id] == nil { held[todo.id] = todo }
            // A drop answers with the item as dropped; it leaves the list.
            if answered.state == .dropped {
                todos.removeAll { $0.id == answered.id }
            } else if let index = todos.firstIndex(where: { $0.id == answered.id }) {
                todos[index] = answered
            }
        } catch APIError.server(let status, _) where status == 404 || status == 409 {
            notice = TodoFormat.changedNotice
            generation += 1 // whatever fetch is in the air predates this answer
            await refresh()
        } catch {
            notice = TodoFormat.unreachableNotice
        }
    }
}
