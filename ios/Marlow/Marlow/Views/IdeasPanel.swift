import SwiftUI

/// What the lightbulb opens (docs/tasks/ideas-panel.md, "v1"): offers Marlow
/// would act on, in two sections, each with a plan and two buttons. All wording
/// comes from `IdeaFormat`; the store holds the state.
///
/// Presented as one case of `ChatView`'s single `ActiveSheet`, so a permission
/// request still pre-empts it. Do it is handed back to `ChatView` rather than
/// sent from here: it closes the sheet and goes through the composer's own send
/// path, so the bubble and the polling are exactly a typed message's.
struct IdeasPanel: View {
    @ObservedObject var store: IdeasStore
    let onDoIt: (Idea) -> Void
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            content
                .navigationTitle("Ideas")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .confirmationAction) {
                        Button("Done") { dismiss() }
                    }
                }
        }
        // Fetched fresh on every open, which also marks the panel opened.
        .task { await store.panelOpened() }
    }

    @ViewBuilder
    private var content: some View {
        let groups = store.sections
        if groups.isEmpty {
            // No spinner and no error line: a failed fetch is quiet, so
            // "nothing yet" and "couldn't ask" look the same, and calm.
            Text(IdeaFormat.emptyLine)
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 10, pinnedViews: []) {
                    ForEach(groups) { group in
                        Text(group.title)
                            .font(.footnote.weight(.semibold))
                            .textCase(.uppercase)
                            .foregroundStyle(.secondary)
                            .padding(.top, 12)
                            .padding(.leading, 4)
                            .accessibilityAddTraits(.isHeader)
                        ForEach(group.ideas) { idea in
                            if store.collapsedIds.contains(idea.id) {
                                DismissedIdeaRow(idea: idea, sentReason: store.reasons[idea.id]) { reason in
                                    await store.dismiss(idea, reason: reason)
                                }
                            } else {
                                IdeaCard(
                                    idea: idea, untilText: store.untilText(idea),
                                    onDoIt: { onDoIt(idea) },
                                    onNotNow: { Task { await store.dismiss(idea) } }
                                )
                            }
                        }
                    }
                }
                .padding(.horizontal, 16)
                .padding(.bottom, 16)
            }
        }
    }
}

// MARK: - Card

private struct IdeaCard: View {
    let idea: Idea
    let untilText: String?
    let onDoIt: () -> Void
    let onNotNow: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(idea.category)
                    .font(.caption2.weight(.medium))
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 2)
                    .background(Color.primary.opacity(0.06), in: Capsule())
                Spacer(minLength: 8)
                if let untilText {
                    Text(untilText)
                        .font(.caption.weight(.medium))
                        .foregroundStyle(.orange)
                }
            }
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(idea.emoji).font(.title3)
                Text(idea.title).font(.headline)
            }
            Text(idea.plan).font(.body)
            if let source = idea.source {
                Text(source).font(.caption).foregroundStyle(.secondary)
            }
            HStack(spacing: 10) {
                Button(IdeaFormat.doIt, action: onDoIt)
                    .buttonStyle(.borderedProminent)
                Button(IdeaFormat.notNow, action: onNotNow)
                    .buttonStyle(.bordered)
            }
            .padding(.top, 2)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .accessibilityElement(children: .contain)
    }
}

// MARK: - Dismissed row

/// What a card becomes after Not now: one line, with a way to say why. The
/// dismissal has already been posted by the time this shows, so the reason is
/// an extra, never a gate.
private struct DismissedIdeaRow: View {
    let idea: Idea
    let sentReason: String?
    /// Posts the reason; true once the server has it.
    let onReason: (String) async -> Bool

    @State private var reason = ""
    @State private var isSending = false

    var body: some View {
        HStack(spacing: 8) {
            if let sentReason {
                Text("Dismissed \u{00B7} \(sentReason)")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                Spacer(minLength: 0)
            } else {
                Text(IdeaFormat.dismissedRow)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .layoutPriority(1)
                TextField(IdeaFormat.reasonPlaceholder, text: $reason)
                    .textFieldStyle(.roundedBorder)
                    .font(.subheadline)
                    .submitLabel(.send)
                    .onSubmit(send)
                Button(action: send) {
                    Image(systemName: "arrow.up.circle.fill").font(.title2)
                }
                .disabled(isSending || reason.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .accessibilityLabel("Send reason")
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(.secondarySystemBackground).opacity(0.6), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(idea.title), dismissed")
    }

    private func send() {
        let text = reason.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !isSending else { return }
        isSending = true
        Task {
            // On failure the text stays for another try; nothing is shown.
            _ = await onReason(text)
            isSending = false
        }
    }
}

// MARK: - Spotlight

/// The one idea Marlow thinks is worth interrupting for, above the composer.
/// Not a chat message and never in the transcript: it stays out of the context
/// and is gone the moment Mike answers it or the next run replaces it. Tapping
/// the body opens the panel; the buttons do what the card's would.
struct SpotlightCard: View {
    let idea: Idea
    let onOpen: () -> Void
    let onDoIt: () -> Void
    let onNotNow: () -> Void

    var body: some View {
        HStack(spacing: 10) {
            Button(action: onOpen) {
                HStack(spacing: 8) {
                    Text(idea.emoji).font(.title3)
                    Text(idea.title)
                        .font(.subheadline.weight(.semibold))
                        .multilineTextAlignment(.leading)
                        .lineLimit(2)
                    Spacer(minLength: 0)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityHint("Opens ideas")
            // Do it first, as on the panel's cards and on web: one place for
            // the primary action on every surface and both clients.
            Button(IdeaFormat.doIt, action: onDoIt)
                .buttonStyle(.borderedProminent)
            Button(IdeaFormat.notNow, action: onNotNow)
                .buttonStyle(.bordered)
        }
        .controlSize(.small)
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).stroke(Color.accentColor.opacity(0.35), lineWidth: 1))
        .padding(.horizontal, 12)
        .padding(.top, 6)
    }
}
