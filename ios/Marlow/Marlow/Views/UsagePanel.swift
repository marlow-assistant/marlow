import SwiftUI

/// The Usage panel of the Marlow sheet (docs/tasks/usage-visibility.md): the
/// plan's windows, how big this conversation has got, and what Marlow itself
/// has been doing. Read-only, quiet by design: nothing here is worded as a
/// warning. All wording comes from `UsageFormat`.
struct UsagePanel: View {
    @ObservedObject var store: UsageStore

    var body: some View {
        content
            // The sheet refreshes everything once on open; this covers a sheet
            // left open while the reading moved. `refresh` ignores a call made
            // while one is in flight, so the open-time pair does not double up.
            .task { await store.refresh() }
    }

    @ViewBuilder
    private var content: some View {
        if let report = store.report {
            List {
                planSection(report.plan, subscriptionType: report.subscriptionType)
                conversationSection(report.conversation)
                marlowSection(report.marlow)
            }
            .listSectionSpacing(.compact)
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

    // MARK: Plan

    @ViewBuilder
    private func planSection(_ plan: UsagePlan?, subscriptionType: String?) -> some View {
        let rows = plan.map { UsageFormat.planRows($0) } ?? []
        let planName = UsageFormat.planName(subscriptionType)
        Section {
            if !rows.isEmpty {
                ForEach(rows, id: \.id) { row in
                    UsageBarRow(title: row.title, fraction: row.fraction, detail: row.detail, tint: row.isHigh ? .yellow : .accentColor)
                }
                Text(UsageFormat.planFootnote(planName: planName)).font(.footnote).foregroundStyle(.secondary)
            } else {
                QuietLine(text: UsageFormat.noReading)
            }
        } header: {
            HStack {
                Text(planName)
                Spacer()
                if let asOf = plan.flatMap({ UsageFormat.asOfTime($0) }) {
                    Text(asOf).font(.footnote).foregroundStyle(.secondary)
                }
            }
        }
    }

    // MARK: Conversation

    @ViewBuilder
    private func conversationSection(_ conversation: UsageConversation?) -> some View {
        Section("This conversation") {
            if let conversation {
                let summary = UsageFormat.conversation(conversation)
                if let size = summary.size {
                    if let fraction = summary.fraction {
                        UsageBarRow(title: size, fraction: fraction, detail: summary.thresholdText ?? "", tint: .accentColor)
                    } else {
                        Text(size).font(.body.weight(.semibold))
                    }
                }
                if let notice = summary.notice {
                    Text(notice).font(.subheadline).foregroundStyle(.secondary)
                }
                if let history = summary.history {
                    Text(history).font(.subheadline).foregroundStyle(.secondary)
                }
            } else {
                QuietLine(text: UsageFormat.noReading)
            }
        }
    }

    // MARK: Marlow's turns

    @ViewBuilder
    private func marlowSection(_ marlow: [String: UsageBuckets]?) -> some View {
        let parts = marlow.map(UsageFormat.marlowParts) ?? []
        Section("Marlow's turns") {
            if parts.isEmpty {
                QuietLine(text: UsageFormat.noReading)
            } else {
                ForEach(parts, id: \.id) { part in
                    VStack(alignment: .leading, spacing: 3) {
                        Text(part.title).font(.body.weight(.semibold))
                        if let counts = part.counts {
                            Text(counts).font(.subheadline).monospacedDigit()
                        } else {
                            // A bare title would read as a missing row.
                            Text(UsageFormat.noTurns).font(.subheadline).foregroundStyle(.secondary)
                        }
                    }
                    .padding(.vertical, 2)
                }
            }
        }
    }
}

/// A title, a bar and a line under it.
private struct UsageBarRow: View {
    let title: String
    let fraction: Double
    let detail: String
    let tint: Color

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.body.weight(.semibold))
            ProgressView(value: fraction).tint(tint)
            if !detail.isEmpty {
                Text(detail).font(.subheadline).monospacedDigit().foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 2)
        .accessibilityElement(children: .combine)
    }
}

private struct QuietLine: View {
    let text: String

    var body: some View {
        Text(text).font(.subheadline).foregroundStyle(.secondary)
    }
}
