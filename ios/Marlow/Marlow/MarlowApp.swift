import SwiftUI

@main
struct MarlowApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var session = SessionStore()
    /// App-level, not per-`ChatStore`: the store is rebuilt with each
    /// `ChatView`, but the fix, the geocoded place and the permission state
    /// belong to the install.
    @StateObject private var location = LocationProvider()
    @Environment(\.scenePhase) private var scenePhase

    init() {
        // Retired dictation settings and folders (see `DictationSettings`).
        // No-ops once they are gone; the folders can be large, so off-main.
        DictationSettings.retireLegacyKeys()
        Task.detached(priority: .utility) { DictationSettings.removeRetiredFolders() }
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(session)
                .environmentObject(location)
                // `initial: true` because the launch-time `.active` never
                // fires a change. `.active` only: location is gathered in the
                // foreground, with no background mode.
                .onChange(of: scenePhase, initial: true) { _, phase in
                    location.setAppActive(phase == .active)
                    if phase == .active {
                        Task { await AppIconSync.activated() }
                    }
                }
                .onChange(of: session.isPaired) { _, isPaired in
                    if isPaired {
                        APNsRegistrar.shared.syncIfPossible()
                    }
                }
        }
    }
}
