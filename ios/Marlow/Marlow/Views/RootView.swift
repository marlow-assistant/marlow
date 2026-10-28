import SwiftUI

/// Not yet paired is the app's launch state. Once paired, the chat screen
/// (step 4 of docs/tasks/ios-minimal-client.md) takes over.
struct RootView: View {
    @EnvironmentObject private var session: SessionStore
    @EnvironmentObject private var location: LocationProvider

    var body: some View {
        NavigationStack {
            #if DEBUG
            if let fixture = UIFixture.state {
                ChatView(store: UIFixture.makeStore(state: fixture))
            } else if let deviceToken = session.deviceToken {
                ChatView(deviceToken: deviceToken, location: location)
            } else {
                PairingView()
            }
            #else
            if let deviceToken = session.deviceToken {
                ChatView(deviceToken: deviceToken, location: location)
            } else {
                PairingView()
            }
            #endif
        }
    }
}

#Preview {
    RootView().environmentObject(SessionStore()).environmentObject(LocationProvider())
}
