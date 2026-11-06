import SwiftUI
import UIKit

/// A value someone may need to paste into a bug report: one line, however
/// long, cut in the middle, because a host's or a build's ends are the parts
/// that differ. Before this they wrapped onto a second line (Mike,
/// 2026-10-21). A tap copies the whole value.
struct CopyableRow: View {
    let label: String
    let value: String
    @State private var copied = false

    var body: some View {
        Button {
            UIPasteboard.general.string = value
            copied = true
            Task {
                try? await Task.sleep(for: .seconds(1.5))
                copied = false
            }
        } label: {
            LabeledContent(label) {
                Text(copied ? "Copied" : value)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
        }
        .buttonStyle(.plain)
        .accessibilityHint("Copies the value")
    }
}

/// Settings > Voice: what used to be the Voice mode section, one level down
/// so the main pane stays short.
struct VoiceSettingsView: View {
    var body: some View {
        Form {
            VoiceSettingsSection()
        }
        .navigationTitle("Voice")
        .navigationBarTitleDisplayMode(.inline)
    }
}

/// Settings > Server connection: which box this is, and the way to leave it.
/// Below the main pane because nobody changes these in normal use, which also
/// keeps Unpair away from the everyday controls.
struct ServerConnectionView: View {
    let host: String
    let connection: String
    /// The last six of the token. Nil once it's gone, mid-unpair.
    let device: String?
    let isUnpairing: Bool
    /// Under a UI fixture Unpair would sign a paired phone out for real, so
    /// the row shows and does nothing (`UIFixture`).
    let isDisabled: Bool
    let unpair: () -> Void
    @State private var showingConfirmation = false

    var body: some View {
        Form {
            Section {
                CopyableRow(label: "Server", value: host)
                LabeledContent("Connection", value: connection)
                if let device {
                    LabeledContent("Device", value: device)
                }
            }
            Section {
                Button("Unpair", role: .destructive) {
                    showingConfirmation = true
                }
                .disabled(isUnpairing || isDisabled)
            }
        }
        .navigationTitle("Server connection")
        .navigationBarTitleDisplayMode(.inline)
        .confirmationDialog(
            "Unpair from Marlow?",
            isPresented: $showingConfirmation,
            titleVisibility: .visible
        ) {
            Button("Unpair", role: .destructive, action: unpair)
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This stops active work and revokes this device on Marlow. Reconnecting needs a new pairing code.")
        }
    }
}

/// Settings > About: the builds and the licences.
struct AboutView: View {
    let appBuild: String
    let serverBuild: String

    var body: some View {
        Form {
            // Both sides, deliberately adjacent and in the same format:
            // the question this answers is "do these two agree?", which is
            // exactly what a feature-branch deploy makes worth asking.
            Section("Build") {
                CopyableRow(label: "App", value: appBuild)
                CopyableRow(label: "Server", value: serverBuild)
            }
            Section {
                NavigationLink("Open-source notices") {
                    OpenSourceNoticesView()
                }
            }
        }
        .navigationTitle("About")
        .navigationBarTitleDisplayMode(.inline)
    }
}

struct OpenSourceNoticesView: View {
    var body: some View {
        List(OpenSourceNotice.all) { notice in
            NavigationLink {
                OpenSourceNoticeView(notice: notice)
            } label: {
                LabeledContent(notice.name, value: notice.license)
            }
        }
        .navigationTitle("Open-source notices")
        .navigationBarTitleDisplayMode(.inline)
    }
}

private struct OpenSourceNoticeView: View {
    let notice: OpenSourceNotice

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                Link(notice.url.absoluteString, destination: notice.url)
                    .font(.footnote)
                Text(notice.text)
                    .font(.footnote)
                    .textSelection(.enabled)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding()
        }
        .navigationTitle(notice.name)
        .navigationBarTitleDisplayMode(.inline)
    }
}
