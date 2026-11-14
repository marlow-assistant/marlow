import SwiftUI

/// The share sheet's contents: the staged items as the composer's tiles, an
/// optional note, Cancel and Send.
struct ShareView: View {
    @ObservedObject var model: ShareModel
    let close: () -> Void
    let cancel: () -> Void

    var body: some View {
        NavigationStack {
            content
                .navigationTitle("Marlow")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    // With no token the sheet's own Close button is the one way
                    // out; a second in the bar would be the same thing twice.
                    if model.phase != .needsPairing {
                        ToolbarItem(placement: .cancellationAction) {
                            Button("Cancel", action: cancel)
                                .disabled(model.phase == .sending)
                        }
                        ToolbarItem(placement: .confirmationAction) {
                            Button("Send") { Task { await model.send() } }
                                .disabled(!model.canSend)
                        }
                    }
                }
        }
        // `.sent` closes at once; "Saved" is shown long enough to read first.
        .onChange(of: model.phase) { _, phase in
            switch phase {
            case .finished(.sent):
                close()
            case .finished(.savedForLater):
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.6) { close() }
            default:
                break
            }
        }
    }

    @ViewBuilder
    private var content: some View {
        switch model.phase {
        case .needsPairing:
            VStack(spacing: 16) {
                Image(systemName: "link.badge.plus")
                    .font(.system(size: 36))
                    .foregroundStyle(.secondary)
                Text(ShareModel.pairFirstMessage)
                    .font(.headline)
                Button("Close", action: cancel)
                    .buttonStyle(.borderedProminent)
            }
            .padding()
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        case .finished(.savedForLater):
            Text(ShareModel.savedMessage)
                .font(.headline)
                .multilineTextAlignment(.center)
                .padding()
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        case .composing, .sending, .finished(.sent):
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    TextField("Add a note (optional)", text: $model.caption, axis: .vertical)
                        .lineLimit(1...8)
                        .textFieldStyle(.roundedBorder)
                        .disabled(model.phase == .sending)
                    StagedAttachmentTray(items: model.staged.items) { model.staged.remove(id: $0) }
                    if let notice = model.notice {
                        Text(notice)
                            .font(.footnote)
                            .foregroundStyle(.red)
                    }
                    if model.phase == .sending {
                        ProgressView("Sending\u{2026}")
                            .frame(maxWidth: .infinity)
                    }
                }
                .padding()
            }
        }
    }
}
