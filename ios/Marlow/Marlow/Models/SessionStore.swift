import Foundation
import Combine

/// App-wide pairing state. The device_token is the source of truth for
/// "paired or not" — launch state is unpaired until Keychain proves otherwise.
@MainActor
final class SessionStore: ObservableObject {
    @Published private(set) var deviceToken: String?
    /// One line for the pairing screen after a sign-out, when something the
    /// user should know happened (the box couldn't be told to revoke this
    /// device). Cleared by the next pairing.
    @Published private(set) var notice: String?

    init() {
        deviceToken = KeychainStore.load()
        syncSharedCredential()
        APIClient.onUnauthorized = { [weak self] token in
            Task { @MainActor in self?.handleUnauthorized(token) }
        }
    }

    /// The box refused this phone's token. Only if it is still the current one:
    /// a request that was in flight when the phone re-paired must not undo the
    /// new pairing. Back to the pairing screen with a line saying why, instead
    /// of server errors in the chat.
    func handleUnauthorized(_ token: String) {
        guard token == deviceToken else { return }
        signOut(notice: "This phone is no longer paired with Marlow. Pair it again with a new code.")
    }

    var isPaired: Bool { deviceToken != nil }

    /// `host` is the server that just accepted the pairing. Saved before the
    /// token, so nothing that reacts to becoming paired can reach for the
    /// previous server (`PairedHost`).
    func completePairing(deviceToken: String, host: String) {
        PairedHost.save(host)
        KeychainStore.save(deviceToken)
        self.deviceToken = deviceToken
        notice = nil
        syncSharedCredential()
    }

    func signOut(notice: String? = nil) {
        KeychainStore.clear()
        deviceToken = nil
        self.notice = notice
        syncSharedCredential()
    }

    /// The notification service extension is a separate executable and cannot
    /// read this target's private Keychain item, so it reads a copy in a
    /// shared access group (`SharedCredential`).
    ///
    /// Here rather than at a call site because this type is the only choke
    /// point all three transitions pass through, and the one that matters is
    /// `signOut`: a copy outliving the credential it mirrors leaves a live
    /// bearer token on a phone the user believes they signed out of. The
    /// prefetched replies go with it for the same reason — they were fetched
    /// with that token and are message content.
    private func syncSharedCredential() {
        if let deviceToken {
            SharedCredential.mirror(deviceToken)
        } else {
            SharedCredential.clear()
            TurnInbox.clear()
        }
    }
}
