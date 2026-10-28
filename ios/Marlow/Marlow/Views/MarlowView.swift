import SwiftUI

/// What the avatar opens: Marlow's standing state in one sheet
/// (docs/tasks/ux-lane.md §3a). A strip of six labelled icons; the selected
/// one takes the accent and the panel below switches. Read-only throughout.
///
/// The sheet owns its stores (created once, so switching panels does not
/// refetch) and fetches them fresh on every open. It is one case of
/// `ChatView`'s single `ActiveSheet`, so a permission request still pre-empts
/// it.
struct MarlowView: View {
    @StateObject private var marlow: MarlowStore
    @StateObject private var activity: ActivityStore
    @StateObject private var usage: UsageStore
    @Environment(\.dismiss) private var dismiss
    @State private var selection: Panel = .activity

    init(
        marlow: @autoclosure @escaping () -> MarlowStore,
        activity: @autoclosure @escaping () -> ActivityStore,
        usage: @autoclosure @escaping () -> UsageStore
    ) {
        _marlow = StateObject(wrappedValue: marlow())
        _activity = StateObject(wrappedValue: activity())
        _usage = StateObject(wrappedValue: usage())
    }

    enum Panel: CaseIterable {
        case activity, usage, schedule, memory, you, soul

        var symbol: String {
            switch self {
            case .activity: return "clock.arrow.circlepath"
            case .usage: return "gauge.with.dots.needle.33percent"
            case .schedule: return "alarm"
            case .memory: return "brain"
            case .you: return "person"
            case .soul: return "sparkles"
            }
        }

        var label: String {
            switch self {
            case .activity: return "Activity"
            case .usage: return "Usage"
            case .schedule: return "Schedule"
            case .memory: return "Memory"
            case .you: return "You"
            case .soul: return "Soul"
            }
        }
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                FaceHeader()
                PanelStrip(selection: $selection)
                Divider()
                // Fill the rest of the sheet whatever the panel holds. A bare
                // ProgressView (Activity's first load) is only its own size, so
                // without this the stack shrank and the strip sat mid-screen
                // until the list arrived (Mike, 2026-10-05).
                panel
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            // No bar title: FaceHeader shows the name under the face. An
            // empty principal item did not hide it on device; the title still
            // showed above the face (Mike, 2026-10-16).
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    // As in Settings: the icon changes on Done, not per tap.
                    Button("Done") {
                        Task { await AppIconSync.reconcile() }
                        dismiss()
                    }
                }
            }
        }
        .task {
            async let a: Void = activity.refresh()
            async let m: Void = marlow.refresh()
            async let u: Void = usage.refresh()
            _ = await (a, m, u)
        }
    }

    @ViewBuilder
    private var panel: some View {
        switch selection {
        case .activity:
            ActivityList(store: activity)
        case .usage:
            UsagePanel(store: usage)
        case .schedule:
            IdentityPanel(store: marlow) { index in SchedulePanel(schedules: index.schedules, store: marlow) }
        case .memory:
            IdentityPanel(store: marlow) { _ in MemoryPanel(store: marlow) }
        case .you:
            IdentityPanel(store: marlow) { _ in
                FilePanel(store: marlow, kind: .user, emptyText: "USER.md isn't there yet.")
            }
        case .soul:
            IdentityPanel(store: marlow) { _ in
                FilePanel(store: marlow, kind: .soul, emptyText: "SOUL.md isn't there yet.", showsInstructions: true)
            }
        }
    }
}

// MARK: - Face

/// Marlow's face over the strip, with the name under it, as Messages heads a
/// conversation with the contact. It replaces the bar's title rather than
/// repeating it. A tap opens the face picker, the same one Settings reaches
/// (Mike, 2026-10-16).
private struct FaceHeader: View {
    var body: some View {
        NavigationLink {
            FacePicker()
        } label: {
            VStack(spacing: 6) {
                MarlowAvatar(size: 64)
                Text("Marlow")
                    .font(.headline)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Marlow")
        .accessibilityHint("Changes Marlow's appearance")
        .padding(.top, 4)
        .padding(.bottom, 4)
    }
}

// MARK: - Strip

private struct PanelStrip: View {
    @Binding var selection: MarlowView.Panel

    var body: some View {
        HStack(spacing: 0) {
            ForEach(MarlowView.Panel.allCases, id: \.self) { panel in
                let isSelected = panel == selection
                Button {
                    selection = panel
                } label: {
                    VStack(spacing: 4) {
                        Image(systemName: panel.symbol)
                            .font(.title3)
                            .frame(height: 26)
                        Text(panel.label)
                            .font(.caption2)
                    }
                    .frame(maxWidth: .infinity)
                    .foregroundStyle(isSelected ? Color.accentColor : Color.secondary)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityAddTraits(isSelected ? .isSelected : [])
            }
        }
        .padding(.vertical, 8)
        .padding(.horizontal, 8)
    }
}

// MARK: - Shared loading shell

/// Loading and error states around the identity-backed panels; the content
/// closure only runs once an index is in hand.
private struct IdentityPanel<Content: View>: View {
    @ObservedObject var store: MarlowStore
    @ViewBuilder let content: (IdentityIndex) -> Content

    var body: some View {
        if let index = store.index {
            content(index)
        } else if let error = store.errorMessage {
            ContentUnavailableView {
                Label("Couldn't load", systemImage: "exclamationmark.triangle")
            } description: {
                Text(error)
            } actions: {
                Button("Try again") { Task { await store.refresh() } }
            }
        } else {
            ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}

// MARK: - Schedule

private struct SchedulePanel: View {
    let schedules: ScheduleIndex
    @ObservedObject var store: MarlowStore

    /// Times read in the store's zone: the zone its wall-clock times are in.
    /// In the phone's zone "8:00" turns into "6:00" whenever Mike is away
    /// from it — true, and read as a bug.
    private var storeCalendar: Calendar {
        IdentityFormat.calendar(storeZone: schedules.timezone)
    }

    var body: some View {
        if schedules.entries.isEmpty && schedules.rejected == nil {
            ContentUnavailableView(
                "No schedules",
                systemImage: "alarm",
                description: Text("Reminders and recurring jobs Marlow sets up show up here.")
            )
        } else {
            List {
                if let reason = schedules.rejected {
                    Section {
                        VStack(alignment: .leading, spacing: 6) {
                            Label(
                                "Marlow's schedule file has an error. The last good version is still running.",
                                systemImage: "exclamationmark.triangle"
                            )
                            .font(.subheadline)
                            .foregroundStyle(.orange)
                            Text(verbatim: reason)
                                .font(.footnote.monospaced())
                                .foregroundStyle(.secondary)
                        }
                        .padding(.vertical, 2)
                    }
                }
                ForEach(ScheduleFormat.sections(schedules.entries), id: \.section) { group in
                    Section(group.section.rawValue) {
                        ForEach(group.entries) { entry in
                            if let jobFile = entry.jobFile {
                                NavigationLink {
                                    IdentityFileView(
                                        title: ActivityFormat.jobTitle(entry.id),
                                        loader: store.loader(for: jobFile), kind: .job
                                    )
                                } label: {
                                    ScheduleRow(entry: entry, calendar: storeCalendar)
                                }
                            } else {
                                ScheduleRow(entry: entry, calendar: storeCalendar)
                            }
                        }
                    }
                }
            }
            .listSectionSpacing(.compact)
        }
    }
}

private struct ScheduleRow: View {
    let entry: ScheduleEntry
    let calendar: Calendar

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(alignment: .firstTextBaseline) {
                Text(ActivityFormat.jobTitle(entry.id))
                    .font(.body.weight(.semibold))
                Spacer(minLength: 8)
                Text(verbatim: ScheduleFormat.whenText(entry, calendar: calendar))
                    .font(.subheadline)
                    .monospacedDigit()
                    .foregroundStyle(ScheduleFormat.isOverdue(entry) ? Color.orange : Color.secondary)
            }
            if let text = entry.notify ?? entry.prompt, !text.isEmpty {
                Text(verbatim: text)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .lineLimit(3)
            }
        }
        .padding(.vertical, 2)
    }
}

// MARK: - Memory

private struct MemoryPanel: View {
    @ObservedObject var store: MarlowStore

    var body: some View {
        let files = store.memoryFiles
        if files.isEmpty {
            ContentUnavailableView(
                "No memory files",
                systemImage: "brain",
                description: Text("What Marlow remembers between conversations shows up here.")
            )
        } else {
            List(files) { file in
                NavigationLink {
                    IdentityFileView(
                        title: IdentityFormat.memoryName(file.id),
                        loader: store.loader(for: file.id), kind: .memory
                    )
                } label: {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(IdentityFormat.memoryName(file.id))
                            .font(.body.weight(.semibold))
                        if let description = file.description, !description.isEmpty {
                            Text(verbatim: description)
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                                .lineLimit(2)
                        }
                        Text(IdentityFormat.changed(file.modifiedAt))
                            .font(.caption)
                            .foregroundStyle(.tertiary)
                    }
                    .padding(.vertical, 2)
                }
            }
        }
    }
}

// MARK: - You / Soul

/// A file rendered in place; Soul adds the row that pushes CLAUDE.md.
private struct FilePanel: View {
    @ObservedObject var store: MarlowStore
    let kind: IdentityKind
    let emptyText: String
    var showsInstructions = false

    var body: some View {
        if let file = store.file(of: kind) {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    IdentityFileContent(loader: store.loader(for: file.id), kind: kind)
                    if showsInstructions, let instructions = store.file(of: .instructions) {
                        NavigationLink {
                            IdentityFileView(
                                title: "Instructions",
                                loader: store.loader(for: instructions.id), kind: .instructions
                            )
                        } label: {
                            HStack {
                                Text("Instructions")
                                Spacer()
                                Image(systemName: "chevron.right")
                                    .font(.footnote.weight(.semibold))
                                    .foregroundStyle(.tertiary)
                            }
                            .padding(12)
                            .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 12))
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal)
                .padding(.vertical, 12)
            }
        } else {
            ContentUnavailableView(emptyText, systemImage: "doc")
        }
    }
}
