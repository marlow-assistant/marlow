import SwiftUI

/// Settings > Appearance, and the face atop the Marlow sheet. Each portrait
/// is shown large, in its `large` crop: monocle against glasses is part of
/// the choice, and at the header's 52pt the difference is hard to see.
struct FacePicker: View {
    @EnvironmentObject private var session: SessionStore
    @AppStorage(FacePreference.storageKey) private var storedFaceID = MarlowFace.default.id

    private let columns = [GridItem(.flexible(), spacing: 20), GridItem(.flexible(), spacing: 20)]

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVGrid(columns: columns, spacing: 24) {
                    ForEach(MarlowFace.all) { face in
                        tile(face).id(face.id)
                    }
                }
                .padding(20)
            }
            // Opens on the current face: a later one sat below the fold, so
            // the picker opened showing no selection (Mike, 2026-10-16).
            .onAppear {
                proxy.scrollTo(MarlowFace.with(id: storedFaceID).id, anchor: .center)
            }
        }
        .navigationTitle("Appearance")
        .navigationBarTitleDisplayMode(.inline)
        .onAppear { AppIconSync.choosing = true }
        .onDisappear { AppIconSync.choosing = false }
    }

    private func tile(_ face: MarlowFace) -> some View {
        let selected = MarlowFace.with(id: storedFaceID) == face
        return Button {
            choose(face)
        } label: {
            MarlowAvatar(size: 140, face: face, crop: .large)
                .overlay(
                    Circle()
                        .stroke(Color.accentColor, lineWidth: 3)
                        .padding(-6)
                        .opacity(selected ? 1 : 0)
                )
                .overlay(alignment: .bottomTrailing) {
                    if selected {
                        Image(systemName: "checkmark.circle.fill")
                            .font(.title2)
                            .symbolRenderingMode(.palette)
                            .foregroundStyle(.white, Color.accentColor)
                    }
                }
                .frame(maxWidth: .infinity)
        }
        .buttonStyle(.plain)
        .accessibilityLabel(face.accessibilityName)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    /// The avatar changes here and the box is told, so every client shows
    /// it. The icon waits for Done on the sheet that pushed this (Settings or
    /// Marlow), which runs `AppIconSync.reconcile()`.
    private func choose(_ face: MarlowFace) {
        FacePreference.set(face)
        PreferenceSync.picked(.face)
        guard let deviceToken = session.deviceToken else { return }
        Task { await PreferenceSync.send(api: APIClient(), deviceToken: deviceToken) }
    }
}

#Preview {
    NavigationStack { FacePicker() }
        .environmentObject(SessionStore())
}
