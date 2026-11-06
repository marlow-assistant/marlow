import SwiftUI

/// What the checklist icon opens (docs/tasks/todos-and-reminders.md, part 2):
/// the to-do list in up to three sections (To do, Marlow suggested, Done this
/// week), each hidden when empty. All wording and order come from `TodoFormat`;
/// the store holds the state and does the taps.
///
/// Presented as one case of `ChatView`'s single `ActiveSheet`, so a permission
/// request still pre-empts it. Talk about it is handed back to `ChatView`
/// rather than sent from here: it closes the sheet and goes through the
/// composer's own send path, so the bubble and the polling are exactly a typed
/// message's.
///
/// A `List`, not a stack in a `ScrollView`, because `onMove` is the only
/// reorder with the system's lift, auto-scroll and make-way behaviour, and it
/// works by long press outside edit mode. VoiceOver can't reach that drag, so
/// the row's menu has Move too.
struct TodosPanel: View {
    @ObservedObject var store: TodosStore
    let onTalkAbout: (Todo) -> Void
    @Environment(\.dismiss) private var dismiss
    /// The item whose "Pick a time" sheet is up.
    @State private var picking: Todo?

    var body: some View {
        NavigationStack {
            content
                .navigationTitle("To-dos")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .confirmationAction) {
                        Button("Done") { dismiss() }
                    }
                }
        }
        // Fetched fresh on every open.
        .task { await store.panelOpened() }
        .sheet(item: $picking) { todo in
            SnoozePickerSheet(todo: todo, now: Date()) { until in
                Task { await store.snooze(todo, until: until) }
            }
            .presentationDetents([.medium, .large])
        }
    }

    @ViewBuilder
    private var content: some View {
        let groups = store.sections
        if groups.isEmpty {
            // No spinner and no error line: a failed fetch is quiet, so
            // "nothing yet" and "couldn't ask" look the same, and calm.
            VStack(spacing: 10) {
                if let notice = store.notice { NoticeLine(text: notice) }
                Text(TodoFormat.emptyLine)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 32)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            List {
                if let notice = store.notice { NoticeLine(text: notice).todoListRow() }
                ForEach(groups) { group in
                    Text(group.title)
                        .font(.footnote.weight(.semibold))
                        .textCase(.uppercase)
                        .foregroundStyle(.secondary)
                        .padding(.top, 12)
                        .padding(.leading, 4)
                        .accessibilityAddTraits(.isHeader)
                        .todoListRow()
                    ForEach(group.todos) { todo in
                        row(todo, in: group).todoListRow()
                            .moveDisabled(group.section != .todo || todo.state != .open || group.todos.count < 2)
                    }
                    .onMove(perform: group.section == .todo ? { offsets, destination in
                        dragged(group.todos, offsets: offsets, destination: destination)
                    } : nil)
                    if group.section == .suggested {
                        Text(TodoFormat.suggestedFootnote)
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                            .padding(.leading, 4)
                            .todoListRow()
                    }
                }
            }
            .listStyle(.plain)
        }
    }

    private func row(_ todo: Todo, in group: TodoFormat.Group) -> some View {
        let index = group.todos.firstIndex(of: todo) ?? 0
        return TodoRow(
            todo: todo,
            isOverdue: store.isOverdue(todo),
            dueLine: store.dueLine(todo),
            snoozeLine: store.snoozeLine(todo),
            presets: store.snoozePresets(),
            moves: group.section == .todo ? TodoFormat.moveChoices(index: index, count: group.todos.count) : [],
            isBusy: store.busyIds.contains(todo.id),
            onDone: { Task { await store.done(todo) } },
            onReopen: { Task { await store.reopen(todo) } },
            onAccept: { Task { await store.accept(todo) } },
            onDrop: { Task { await store.drop(todo) } },
            onSnooze: { until in Task { await store.snooze(todo, until: until) } },
            onPickTime: { picking = todo },
            onMove: { to in move(todo, to: to, in: group.todos) },
            onTalkAbout: { onTalkAbout(todo) }
        )
    }

    /// `onMove`'s offsets, as the index the row lands at.
    private func dragged(_ todos: [Todo], offsets: IndexSet, destination: Int) {
        guard let from = offsets.first else { return }
        var ids = todos.map(\.id)
        ids.move(fromOffsets: offsets, toOffset: destination)
        guard let to = ids.firstIndex(of: todos[from].id), to != from else { return }
        move(todos[from], to: to, in: todos)
    }

    private func move(_ todo: Todo, to index: Int, in todos: [Todo]) {
        store.move(todo, before: TodoFormat.anchor(todos.map(\.id), moving: todo.id, to: index))
        AccessibilityNotification.Announcement(TodoFormat.movedLine(todo.title, to: index, of: todos.count)).post()
    }
}

private extension View {
    /// A row of the panel's `List` that looks like a card on the sheet: no
    /// separator or row background, spaced as the rows always were.
    func todoListRow() -> some View {
        listRowSeparator(.hidden)
            .listRowBackground(Color.clear)
            .listRowInsets(EdgeInsets(top: 5, leading: 16, bottom: 5, trailing: 16))
    }
}

// MARK: - Notice

/// "That to-do changed; here's the latest." Calm, in the panel, never in chat.
private struct NoticeLine: View {
    let text: String

    var body: some View {
        Text(text)
            .font(.footnote)
            .foregroundStyle(.secondary)
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color(.secondarySystemBackground).opacity(0.6), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            .accessibilityElement(children: .combine)
    }
}

// MARK: - Row

private struct TodoRow: View {
    let todo: Todo
    let isOverdue: Bool
    let dueLine: String?
    let snoozeLine: String?
    let presets: [TodoFormat.SnoozePreset]
    /// Empty when the row can't move (or it's the only one).
    let moves: [TodoFormat.MoveChoice]
    let isBusy: Bool
    let onDone: () -> Void
    let onReopen: () -> Void
    let onAccept: () -> Void
    let onDrop: () -> Void
    let onSnooze: (Date) -> Void
    let onPickTime: () -> Void
    let onMove: (Int) -> Void
    let onTalkAbout: () -> Void

    /// Notes are one greyed line until the row is tapped.
    @State private var showsNotes = false

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            leading
            VStack(alignment: .leading, spacing: 4) {
                Text(todo.title)
                    .font(.body.weight(.medium))
                    .strikethrough(todo.state == .done)
                    .foregroundStyle(todo.state == .done ? .secondary : .primary)
                if todo.state != .done, let dueLine {
                    Text(dueLine)
                        .font(.caption.weight(.medium))
                        .foregroundStyle(isOverdue ? Color.red : Color.secondary)
                }
                if todo.state == .open, let snoozeLine {
                    Text(snoozeLine).font(.caption).foregroundStyle(.secondary)
                }
                if let notes = todo.notes, !notes.isEmpty {
                    Text(notes)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .lineLimit(showsNotes ? nil : 1)
                        .fixedSize(horizontal: false, vertical: showsNotes)
                }
                if todo.state == .proposed { proposedActions.padding(.top, 4) }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .contentShape(Rectangle())
            .onTapGesture {
                if todo.notes?.isEmpty == false { withAnimation(.snappy(duration: 0.2)) { showsNotes.toggle() } }
            }
            if todo.state == .open { menu }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .opacity(isBusy ? 0.6 : 1)
        .allowsHitTesting(!isBusy)
        .accessibilityElement(children: .contain)
    }

    // MARK: Pieces

    @ViewBuilder
    private var leading: some View {
        switch todo.state {
        case .open:
            Button(action: onDone) {
                Image(systemName: isBusy ? "checkmark.circle.fill" : "circle")
                    .font(.title2)
                    .foregroundStyle(isBusy ? Color.accentColor : Color.secondary)
                    .frame(width: 32, height: 32)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Mark done")
            .accessibilityHint(todo.title)
        case .done:
            // Untick: a done item can be taken back (Mike, 2026-11-04).
            Button(action: onReopen) {
                Image(systemName: isBusy ? "circle" : "checkmark.circle.fill")
                    .font(.title2)
                    .foregroundStyle(isBusy ? Color.secondary : Color.accentColor)
                    .frame(width: 32, height: 32)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Mark not done")
            .accessibilityHint(todo.title)
        case .proposed, .dropped:
            Image(systemName: "sparkles")
                .font(.title3)
                .foregroundStyle(Color.accentColor)
                .frame(width: 32, height: 32)
                .accessibilityHidden(true)
        }
    }

    /// Snooze, Move, Talk about it and Drop behind one control: the checkbox
    /// is the everyday tap, these are the occasional ones.
    private var menu: some View {
        Menu {
            Menu(TodoFormat.snooze, systemImage: "clock") {
                ForEach(presets) { preset in
                    Button(preset.title) { onSnooze(preset.until) }
                }
                Button(TodoFormat.pickATime, action: onPickTime)
            }
            if !moves.isEmpty {
                Menu(TodoFormat.move, systemImage: "arrow.up.arrow.down") {
                    ForEach(moves) { choice in
                        Button(choice.title) { onMove(choice.to) }
                    }
                }
            }
            Button(TodoFormat.talkAbout, systemImage: "bubble.left", action: onTalkAbout)
            Button(TodoFormat.drop, systemImage: "xmark.circle", role: .destructive, action: onDrop)
        } label: {
            Image(systemName: "ellipsis.circle")
                .font(.title3)
                .foregroundStyle(.secondary)
                .frame(width: 32, height: 32)
                .contentShape(Rectangle())
        }
        .accessibilityLabel("More actions")
        .accessibilityHint(todo.title)
    }

    private var proposedActions: some View {
        HStack(spacing: 10) {
            Button(TodoFormat.accept, action: onAccept)
                .buttonStyle(.borderedProminent)
            Button(TodoFormat.drop, action: onDrop)
                .buttonStyle(.bordered)
            Button(TodoFormat.talkAbout, action: onTalkAbout)
                .buttonStyle(.bordered)
        }
        .controlSize(.small)
    }
}

// MARK: - Pick a time

/// A date and time for a snooze. Resolved in the phone's own zone and sent as
/// an instant (`TodosStore.snooze`); the picker will not go into the past,
/// which the box would refuse anyway.
private struct SnoozePickerSheet: View {
    let todo: Todo
    let onPick: (Date) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var picked: Date
    private let earliest: Date

    init(todo: Todo, now: Date, onPick: @escaping (Date) -> Void) {
        self.todo = todo
        self.onPick = onPick
        // A bit ahead, so the default is already in the future by the time
        // it is sent. Whole minutes: the picker has no seconds.
        let soon = now.addingTimeInterval(3600)
        _picked = State(initialValue: soon)
        earliest = now
    }

    var body: some View {
        NavigationStack {
            Form {
                DatePicker(
                    "Snooze until", selection: $picked, in: earliest...,
                    displayedComponents: [.date, .hourAndMinute]
                )
                .datePickerStyle(.graphical)
            }
            .navigationTitle(todo.title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button(TodoFormat.snooze) {
                        onPick(picked)
                        dismiss()
                    }
                }
            }
        }
    }
}
