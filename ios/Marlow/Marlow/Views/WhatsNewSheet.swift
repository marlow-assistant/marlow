import SwiftUI

/// "Marlow was updated", shown once per update on opening the app
/// (docs/tasks/whats-new.md). One of ChatView's single sheet, so it can never
/// sit over an approval card. OK, with a hint at Settings' switch.
struct WhatsNewSheet: View {
    @ObservedObject var store: WhatsNewStore
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        summary
        .presentationDetents([.medium, .large])
        .presentationDragIndicator(.visible)
    }

    private var summary: some View {
        VStack(spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    HStack(spacing: 12) {
                        MarlowAvatar(size: 44)
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Marlow was updated")
                                .font(.title3.weight(.semibold))
                            Text("Here's what changed.")
                                .font(.subheadline)
                                .foregroundStyle(Color(.secondaryLabel))
                        }
                    }
                    ForEach(store.pending) { entry in
                        WhatsNewRow(entry: entry)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 20)
                .padding(.top, 24)
                // More than the 18 between entries: at 12 the last entry sat
                // tight on OK (Mike, on device, 2026-10-23).
                .padding(.bottom, 24)
            }
            VStack(spacing: 10) {
                Button {
                    dismiss()
                } label: {
                    Text("OK").frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                // Not a button: "Don't show this again" read as hiding this
                // one change. The switch is in Settings.
                Text("You can turn off What's new in Settings.")
                    .font(.footnote)
                    .foregroundStyle(Color(.secondaryLabel))
                    .multilineTextAlignment(.center)
            }
            .controlSize(.large)
            .padding(.horizontal, 20)
            .padding(.bottom, 16)
        }
    }
}

/// One entry, in the card and in Settings' list.
struct WhatsNewRow: View {
    let entry: ChangeEntry

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Text(WhatsNew.label(entry.category))
                    .foregroundStyle(WhatsNew.isAlwaysTell(entry.category) ? Color.accentColor : Color(.secondaryLabel))
                Text("·").foregroundStyle(Color(.secondaryLabel))
                Text(WhatsNew.dateText(entry.date)).foregroundStyle(Color(.secondaryLabel))
            }
            .font(.caption.weight(.semibold))
            Text(entry.text)
                .font(.body)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

/// Settings > What's new: every change this device knows of, newest first.
struct WhatsNewList: View {
    @ObservedObject var store: WhatsNewStore

    var body: some View {
        List {
            if store.entries.isEmpty {
                Text("Nothing yet.")
                    .foregroundStyle(Color(.secondaryLabel))
            }
            ForEach(store.entries) { entry in
                WhatsNewRow(entry: entry)
                    .padding(.vertical, 4)
            }
        }
        .navigationTitle("What's new")
        .navigationBarTitleDisplayMode(.inline)
    }
}
