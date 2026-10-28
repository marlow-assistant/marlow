import SwiftUI
import UIKit

/// Scan the QR from `deploy/pair-device.sh` (or paste its link) -> redeem the
/// single-use invite -> store device_token. The link names the server, so
/// there is no host to type. The invite is issued by whoever has the box
/// (POST /internal/admin/invite over its owner-only socket); there is no
/// in-app way to get one, by design.
struct PairingView: View {
    @EnvironmentObject private var session: SessionStore

    @State private var deviceName = UIDevice.current.name
    @State private var link: PairingLink?
    @State private var pasted = ""
    @State private var showingScanner = false
    @State private var isPairing = false
    @State private var errorMessage: String?

    var body: some View {
        Form {
            // The first screen a new user sees, so it shows who they are
            // pairing with: the face, large (Mike, 2026-10-16). An unpaired
            // install shows face 1 unless one was picked before an unpair.
            Section {
                MarlowAvatar(size: 120, crop: .large)
                    .frame(maxWidth: .infinity)
                    .listRowBackground(Color.clear)
                    .accessibilityHidden(true)
            }

            if let notice = session.notice {
                Section {
                    Text(notice).foregroundStyle(.secondary)
                }
            }

            Section {
                TextField("Device name", text: $deviceName)
            }

            Section {
                if let link {
                    LabeledContent("Pairing with", value: link.host)
                    Button("Use a different link") { reset() }
                } else {
                    Button {
                        showingScanner = true
                    } label: {
                        Label("Scan pairing code", systemImage: "qrcode.viewfinder")
                    }
                    TextField("or paste the pairing link", text: $pasted)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                        .onChange(of: pasted) { _, newValue in
                            if let parsed = PairingLink.parse(newValue) { accept(parsed) }
                        }
                }
            } header: {
                Text("Pairing link")
            } footer: {
                if link == nil {
                    Text("Run deploy/pair-device.sh on the box. Its code and link last ten minutes and work once.")
                }
            }

            if let errorMessage {
                Section {
                    Text(errorMessage).foregroundStyle(.red)
                }
            }

            Section {
                Button {
                    Task { await pair() }
                } label: {
                    if isPairing {
                        ProgressView()
                    } else {
                        Text("Pair")
                    }
                }
                .disabled(isPairing || link == nil || deviceName.isEmpty)
            }
        }
        .navigationTitle("Pair with Marlow")
        .sheet(isPresented: $showingScanner) {
            PairingScannerView(
                onScan: { payload in
                    showingScanner = false
                    if let parsed = PairingLink.parse(payload) { accept(parsed) }
                },
                onCancel: { showingScanner = false }
            )
        }
    }

    private func accept(_ parsed: PairingLink) {
        link = parsed
        // Out of the text field at once: a single-use secret shouldn't sit in
        // a visible, copyable field.
        pasted = ""
        errorMessage = nil
    }

    private func reset() {
        link = nil
        errorMessage = nil
    }

    private func pair() async {
        guard let link else { return }
        isPairing = true
        errorMessage = nil
        defer { isPairing = false }

        guard let url = URL(string: link.host) else {
            errorMessage = "That doesn't look like a valid pairing link."
            return
        }
        let client = APIClient(baseURL: url)
        do {
            let paired = try await client.pair(deviceName: deviceName, invite: link.invite)
            session.completePairing(deviceToken: paired.deviceToken, host: link.host)
        } catch APIError.server(let status, _) where status == 400 {
            // One answer for every refusal, so there is no finer thing to say.
            // The invite is spent or gone either way; a new one is needed.
            // Only the 400: a 5xx or an overloaded box says nothing about it.
            errorMessage = "That pairing code has expired or was already used. Get a new one."
            self.link = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

#Preview {
    NavigationStack {
        PairingView().environmentObject(SessionStore())
    }
}
