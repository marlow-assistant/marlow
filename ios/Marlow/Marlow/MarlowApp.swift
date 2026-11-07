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
        // Settings' Intelligence and Location pickers, the only segmented
        // controls. SwiftUI has no font for them, and the default 13pt reads
        // small beside the rows around it (Mike, 2026-11-06). Scaled to follow
        // the phone's text size, as the default does not; capped because past
        // 18pt "Approximate" no longer fits a third of the control.
        let segmentFont = UIFontMetrics(forTextStyle: .subheadline)
            .scaledFont(for: .systemFont(ofSize: 15), maximumPointSize: 18)
        UISegmentedControl.appearance().setTitleTextAttributes([.font: segmentFont], for: .normal)
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
