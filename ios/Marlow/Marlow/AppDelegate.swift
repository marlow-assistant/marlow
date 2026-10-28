import UIKit

final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        Task { @MainActor in
            APNsRegistrar.shared.requestAuthorizationAndRegister()
        }
        return true
    }

    func application(
        _ application: UIApplication,
        didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data
    ) {
        Task { @MainActor in
            APNsRegistrar.shared.didRegister(deviceToken: deviceToken)
        }
    }

    func application(
        _ application: UIApplication,
        didFailToRegisterForRemoteNotificationsWithError error: Error
    ) {
        Task { @MainActor in
            APNsRegistrar.shared.didFailToRegister(error: error)
        }
    }

    /// Fires for the wake-and-fetch `permission_request` push (§5) — and for
    /// every other remote notification too, since `content-available: 1` is
    /// set on all of them (server/push.py); `APNsRegistrar` no-ops on types
    /// it doesn't recognize.
    func application(
        _ application: UIApplication,
        didReceiveRemoteNotification userInfo: [AnyHashable: Any],
        fetchCompletionHandler completionHandler: @escaping (UIBackgroundFetchResult) -> Void
    ) {
        Task { @MainActor in
            let result = await APNsRegistrar.shared.handleRemoteNotification(userInfo: userInfo)
            completionHandler(result)
        }
    }
}
