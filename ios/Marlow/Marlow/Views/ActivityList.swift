import SwiftUI

/// What Marlow has done, one row per turn (docs/tasks/ux-lane.md §2c).
///
/// The Activity panel of `MarlowView` (§3a): the avatar opens that sheet, and
/// this list is one icon in its strip. It was a pane of its own until then;
/// the `NavigationStack`, Done button and initial fetch moved up to the sheet,
/// which also owns the store so switching panels does not refetch. Read-only:
/// nothing here writes state, so it needs no `UIFixture` guard beyond a fake
/// data source.
struct ActivityList: View {
    @ObservedObject var store: ActivityStore
    @State private var expanded: Set<String> = []
    /// Per device, not a synced preference: whether to look at the harness's
    /// own upkeep is a viewing choice, and two devices may reasonably differ
    /// (docs/tasks/default-infrastructure.md). On by default: Activity
    /// defaults to transparency, and hiding the upkeep is the opt-out (Mike,
    /// 2026-10-21).
    @AppStorage("activityShowBackground") private var showBackground = true

    var body: some View {
        content
    }

    @ViewBuilder
    private var content: some View {
        let visible = store.visibleEntries(showBackground: showBackground)
        if visible.isEmpty && !store.hasMore {
            if let error = store.errorMessage {
                ContentUnavailableView {
                    Label("Couldn't load", systemImage: "exclamationmark.triangle")
                } description: {
                    Text(error)
                } actions: {
                    Button("Try again") { Task { await store.refresh() } }
                }
            } else if store.hasLoaded {
                ContentUnavailableView {
                    Label("Nothing yet", systemImage: "clock")
                } description: {
                    Text("Scheduled jobs, and the files and services Marlow uses, show up here.")
                } actions: {
                    // With only background jobs in the log, the list (and its
                    // toggle) would otherwise never appear.
                    if !showBackground && store.entries.contains(where: ActivityFormat.isBackground) {
                        Button("Show background jobs") { showBackground = true }
                    }
                }
            } else {
                ProgressView()
            }
        } else {
            List {
                Section {
                    Toggle("Show background jobs", isOn: $showBackground)
                        .font(.subheadline)
                }
                ForEach(daySections(visible), id: \.title) { section in
                    Section(section.title) {
                        ForEach(section.entries) { entry in
                            ActivityRow(
                                entry: entry,
                                isExpanded: expanded.contains(entry.id),
                                output: store.outputs[entry.id]
                            )
                            .contentShape(Rectangle())
                            .onTapGesture { toggle(entry) }
                        }
                    }
                }
                if store.hasMore {
                    Button {
                        Task { await store.loadMore() }
                    } label: {
                        HStack {
                            Text("Show older")
                            if store.isLoading { Spacer(); ProgressView() }
                        }
                    }
                    .disabled(store.isLoading)
                }
                if let error = store.errorMessage {
                    Text(error)
                        .font(.footnote)
                        .foregroundStyle(.orange)
                }
                Section {
                } footer: {
                    Text("Chat messages that used no tools aren't listed — they're in the conversation.")
                }
            }
            .refreshable { await store.refresh() }
        }
    }

    private func toggle(_ entry: ActivityEntry) {
        if expanded.contains(entry.id) {
            expanded.remove(entry.id)
        } else {
            expanded.insert(entry.id)
            Task { await store.loadOutput(for: entry) }
        }
    }

    private struct DaySection {
        let title: String
        let entries: [ActivityEntry]
    }

    /// Entries arrive newest first, so days come out in order by walking them.
    private func daySections(_ entries: [ActivityEntry]) -> [DaySection] {
        var sections: [DaySection] = []
        var currentTitle: String?
        var current: [ActivityEntry] = []
        for entry in entries {
            let title = Self.dayTitle(Date(timeIntervalSince1970: entry.finishedAt))
            if title != currentTitle, let finished = currentTitle {
                sections.append(DaySection(title: finished, entries: current))
                current = []
            }
            currentTitle = title
            current.append(entry)
        }
        if let currentTitle { sections.append(DaySection(title: currentTitle, entries: current)) }
        return sections
    }

    static func dayTitle(_ date: Date, calendar: Calendar = .current) -> String {
        if calendar.isDateInToday(date) { return "Today" }
        if calendar.isDateInYesterday(date) { return "Yesterday" }
        return date.formatted(.dateTime.weekday(.abbreviated).day().month(.abbreviated))
    }
}

private struct ActivityRow: View {
    let entry: ActivityEntry
    let isExpanded: Bool
    let output: ActivityStore.Output?

    /// A background job is present but quieter than the jobs the user asked for.
    private var isBackground: Bool { ActivityFormat.isBackground(entry) }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline) {
                Text(ActivityFormat.title(entry))
                    .font(.body.weight(isBackground ? .medium : .semibold))
                    .foregroundStyle(isBackground ? .secondary : .primary)
                Spacer()
                Text(Date(timeIntervalSince1970: entry.finishedAt), format: .dateTime.hour().minute())
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }
            if let summary = ActivityFormat.summaryLine(entry) {
                Text(summary)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .lineLimit(isExpanded ? nil : 2)
            }
            let flags = ActivityFormat.flags(entry)
            if !flags.isEmpty {
                flagsText(flags)
                    .font(.footnote)
            }
            if isExpanded {
                detail
            }
        }
        .padding(.vertical, 2)
        .accessibilityElement(children: .combine)
        .accessibilityHint(isExpanded ? "Hides the tool list" : "Shows each tool used")
    }

    /// One line, coloured per part: warnings orange, approvals grey.
    private func flagsText(_ flags: [ActivityFormat.Flag]) -> Text {
        flags.enumerated().reduce(Text("")) { line, item in
            let part = Text(item.element.text)
                .foregroundStyle(item.element.isWarning ? Color.orange : Color.secondary)
            return item.offset == 0
                ? part
                : line + Text(" · ").foregroundStyle(Color.secondary) + part
        }
    }

    @ViewBuilder
    private var detail: some View {
        VStack(alignment: .leading, spacing: 3) {
            ForEach(Array(ActivityFormat.detailLines(entry).enumerated()), id: \.offset) { _, line in
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(line.label)
                        .foregroundStyle(.secondary)
                    if let target = line.target {
                        // Verbatim: a path or a query, never markdown.
                        Text(verbatim: target)
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }
                    if let count = line.countText {
                        Text(verbatim: count)
                            .foregroundStyle(.secondary)
                    }
                }
                .font(.footnote)
            }
            if entry.hasOutput, let output {
                outputView(output)
                    .font(.subheadline)
                    .padding(.top, 2)
            }
            if let run = ActivityFormat.runLine(entry) {
                // Verbatim: the model id is the server's, not copy.
                Text(verbatim: run)
                    .font(.footnote)
                    .foregroundStyle(.tertiary)
            }
        }
        .padding(.top, 4)
    }

    /// What the job said. Verbatim, never markdown: it is the model's reply,
    /// shown as written.
    @ViewBuilder
    private func outputView(_ output: ActivityStore.Output) -> some View {
        switch output {
        case .loading:
            ProgressView()
        case .failed:
            Text("Couldn't load what it said.")
                .foregroundStyle(.secondary)
        case .loaded(let text):
            Text(verbatim: ActivityFormat.outputText(text))
        }
    }
}
