import Foundation
import UserNotifications
import UIKit

/// Identifiers for the permission-request notification's category and
/// actions (docs/tasks/ios-permission-round-trip-ui.md §5).
///
/// One category, Review and Deny — never Allow. The owner decided on
/// 2026-10-02 that the lock screen never offers Allow: a notification, even
/// one carrying the server's plain-language title, is not the real request,
/// and approving a tool from a glance at it is a blind approval. Allow lives
/// only on the in-app card, beside the preview and the raw details. Deny stays
/// a single frictionless tap, since denying is always safe. The server sends
/// this category on every permission push (`aps.category`), so it is the only
/// one registered; the earlier `full` category, which offered Allow, is gone.
private enum PermissionCategory: String {
    case truncated = "PERMISSION_REQUEST_TRUNCATED"
}

enum PermissionAction: String {
    case deny = "PERMISSION_DENY"
    case review = "PERMISSION_REVIEW"

    /// Pure mapping from a notification action tap to the decision string the
    /// server's `POST /v1/permission/{id}` expects. Split out from
    /// `userNotificationCenter(_:didReceive:)` so it's testable without a real
    /// `UNNotificationResponse`, which needs live `UserNotifications` machinery
    /// to construct. Review is not a decision: it only opens the app.
    static func decision(forActionIdentifier identifier: String) -> String? {
        switch identifier {
        case deny.rawValue: return "deny"
        default: return nil
        }
    }
}

/// Correlates a turn fetched after foregrounding with the notification sound
/// iOS already played for that same turn while the app was backgrounded.
/// Turn ids are unique and entries are consumed when ChatView renders them.
@MainActor
final class TurnReadySoundTracker {
    static let shared = TurnReadySoundTracker()

    private var notifiedTurnIds: Set<String> = []

    /// `notify` belongs here alongside `turn_ready`: a reminder is also a real
    /// turn in the transcript, so without this the app plays a second receive
    /// sound for the one iOS already sounded on the lock screen — exactly the
    /// duplicate this tracker exists to suppress (review 2026-08-30).
    static let soundedTypes: Set<String> = ["turn_ready", "notify"]

    func record(userInfo: [AnyHashable: Any]) {
        guard
            let type = userInfo["type"] as? String,
            TurnReadySoundTracker.soundedTypes.contains(type),
            let turnId = userInfo["ref"] as? String
        else { return }
        notifiedTurnIds.insert(turnId)
    }

    /// True exactly once for a turn whose APNs notification already supplied
    /// its receive sound.
    func consume(_ turnId: String) -> Bool {
        notifiedTurnIds.remove(turnId) != nil
    }
}

/// Owns the "APNs token -> POST /v1/device/apns-token" pipeline and the
/// notification delegate, including the Review/Deny actions on a
/// `permission_request` push (§5). Called on every launch,
/// not only right after pairing, because tokens rotate on reinstall, restore,
/// and OS update, and a stale token means silent failure of the one feature
/// this app exists for.
@MainActor
final class APNsRegistrar: NSObject, UNUserNotificationCenterDelegate {
    static let shared = APNsRegistrar()

    private var latestAPNsToken: String?
    private var client = APIClient()

    func requestAuthorizationAndRegister() {
        UNUserNotificationCenter.current().delegate = self
        registerPermissionCategories()
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { granted, _ in
            guard granted else { return }
            Task { @MainActor in
                UIApplication.shared.registerForRemoteNotifications()
            }
        }
    }

    func didRegister(deviceToken: Data) {
        let hex = deviceToken.map { String(format: "%02x", $0) }.joined()
        latestAPNsToken = hex
        syncIfPossible()
    }

    func didFailToRegister(error: Error) {
        print("Marlow: APNs registration failed: \(error.localizedDescription)")
    }

    /// Call again once pairing completes, in case the APNs token arrived first.
    func syncIfPossible() {
        guard let apnsToken = latestAPNsToken, let deviceToken = KeychainStore.load() else { return }
        Task {
            do {
                try await client.registerAPNsToken(apnsToken, deviceToken: deviceToken)
            } catch {
                print("Marlow: failed to register APNs token with server: \(error.localizedDescription)")
            }
        }
    }

    // MARK: - Permission-request notification (§5)

    private func registerPermissionCategories() {
        let deny = UNNotificationAction(identifier: PermissionAction.deny.rawValue, title: "Deny", options: [.destructive])
        // .foreground: tapping Review has to launch the app, since the real
        // request, with its preview and raw details, is only in the approval
        // card.
        let review = UNNotificationAction(identifier: PermissionAction.review.rawValue, title: "Review", options: [.foreground])

        let category = UNNotificationCategory(
            identifier: PermissionCategory.truncated.rawValue, actions: [review, deny], intentIdentifiers: []
        )
        UNUserNotificationCenter.current().setNotificationCategories([category])
    }

    /// Entry point from `AppDelegate.didReceiveRemoteNotification`. Bookkeeping
    /// only: the badge, and the turn-sound marker. A `permission_request` push
    /// used to be content-free and this fetched `/v1/permissions` to compose a
    /// local notification from it; the remote alert now carries the server's
    /// title and body itself, so there is nothing left to fetch or compose and
    /// that path is gone (2026-10-02).
    func handleRemoteNotification(userInfo: [AnyHashable: Any]) async -> UIBackgroundFetchResult {
        let appIsActive = UIApplication.shared.applicationState == .active
        if !appIsActive {
            TurnReadySoundTracker.shared.record(userInfo: userInfo)
        }
        // The app sets its own badge rather than relying only on the `badge`
        // APNs applies from the payload. It is the only party that knows the
        // chat hasn't been opened — the server has no per-device read state —
        // and this path runs whenever iOS wakes us for the push, which is the
        // same moment the user saw the banner. The payload's badge still
        // matters as the fallback for a wake that never happens (background
        // execution denied, or iOS declining to launch us at all).
        if APNsRegistrar.marksChatUnread(forUserInfoType: userInfo["type"] as? String, appIsActive: appIsActive) {
            try? await UNUserNotificationCenter.current().setBadgeCount(1)
        }
        return .noData
    }

    /// Fires for Deny/Review taps and the default tap, without bringing the
    /// app to the foreground for Deny. `request_id` is a top-level key of the
    /// remote push itself. Review and the default tap do nothing here beyond
    /// their `.foreground` option launching the app — the approval card is
    /// already driven by `ChatStore`'s poll loop, so there's nothing left to
    /// wire up.
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse
    ) async {
        let userInfo = response.notification.request.content.userInfo
        // This also covers a visible notification that arrived while the app
        // was terminated, where no background-fetch callback had a process in
        // which to remember the turn.
        TurnReadySoundTracker.shared.record(userInfo: userInfo)
        guard let requestId = userInfo["request_id"] as? String else { return }
        guard let decision = PermissionAction.decision(forActionIdentifier: response.actionIdentifier) else { return }
        guard let deviceToken = KeychainStore.load() else { return }
        do {
            _ = try await client.answerPermission(requestId: requestId, decision: decision, deviceToken: deviceToken)
        } catch {
            print("Marlow: failed to answer permission request from notification: \(error.localizedDescription)")
        }
    }

    // While the app is foregrounded, ChatStore's poll loop and health banner
    // already surface everything a push could tell you (a live-updating
    // transcript, the approval card, reauth/paused banners), so presenting it
    // too is just a redundant, noisier copy of UI already on screen — and for
    // a `permission_request` the card is literally on screen. Backgrounded or
    // locked, present normally: the remote alert carries its own text. (There
    // used to be a special case suppressing the remote permission push
    // unconditionally, because a richer local notification was composed in its
    // place; that local notification no longer exists.)
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        APNsRegistrar.presentationOptions(
            forUserInfoType: notification.request.content.userInfo["type"] as? String,
            appIsActive: UIApplication.shared.applicationState == .active
        )
    }

    /// Whether a push means "there is something in the chat you haven't
    /// read". A ready turn does, and so does a `notify` reminder — it is a
    /// pre-written turn sitting in the transcript, and the server already sets
    /// the APNs badge for it, so leaving it out gave a badged app icon over a
    /// chat with no unread state (review 2026-08-30). Reauthorization and
    /// permission pushes still do not: they are their own surfaces and marking
    /// the transcript unread for them would make the badge mean nothing in
    /// particular. Pure, so the rule is testable without a live notification
    /// centre.
    nonisolated static func marksChatUnread(forUserInfoType type: String?, appIsActive: Bool) -> Bool {
        guard !appIsActive else { return false }
        return type == "turn_ready" || type == "notify"
    }

    /// Pure decision behind `willPresent`, split out so the foreground rule is
    /// testable without constructing a real `UNNotification`. The type is
    /// still a parameter so a future push that should present in the
    /// foreground has somewhere to say so.
    nonisolated static func presentationOptions(
        forUserInfoType type: String?, appIsActive: Bool
    ) -> UNNotificationPresentationOptions {
        appIsActive ? [] : [.banner, .sound, .list]
    }
}
