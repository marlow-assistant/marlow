import Testing
import UserNotifications
@testable import Marlow

/// `APNsRegistrar` itself needs live `UIKit`/`UNUserNotificationCenter` to
/// exercise end to end (docs/tasks/ios-permission-round-trip-ui.md §7 covers
/// that gap with the manual on-device test instead) — but its decisions are
/// pure functions of their inputs, pulled out specifically so they don't have
/// to go untested along with the rest.
struct APNsRegistrarTests {
    @Test @MainActor func turnReadySoundMarkerIsScopedAndConsumedOnce() {
        let tracker = TurnReadySoundTracker()
        tracker.record(userInfo: ["type": "turn_ready", "ref": "turn-1"])

        #expect(tracker.consume("turn-1"))
        #expect(!tracker.consume("turn-1"))
    }

    /// A reminder push is a turn too, so iOS already played its sound.
    @Test @MainActor func notifyPushAlsoSuppressesTheDuplicateChatSound() {
        let tracker = TurnReadySoundTracker()
        tracker.record(userInfo: ["type": "notify", "ref": "turn-2"])

        #expect(tracker.consume("turn-2"))
        #expect(!tracker.consume("turn-2"))
    }

    @Test @MainActor func nonTurnReadyPushDoesNotSuppressAChatSound() {
        let tracker = TurnReadySoundTracker()
        tracker.record(userInfo: ["type": "reauth_needed", "ref": "turn-1"])

        #expect(!tracker.consume("turn-1"))
    }

    @Test func onlyATurnBearingPushMarksTheChatUnread() {
        #expect(APNsRegistrar.marksChatUnread(forUserInfoType: "turn_ready", appIsActive: false))
        // The server badges a `notify` like a ready turn; the in-app unread
        // state has to agree or the icon badge outlives a clean-looking chat.
        #expect(APNsRegistrar.marksChatUnread(forUserInfoType: "notify", appIsActive: false))
        #expect(!APNsRegistrar.marksChatUnread(forUserInfoType: "reauth_needed", appIsActive: false))
        #expect(!APNsRegistrar.marksChatUnread(forUserInfoType: "permission_request", appIsActive: false))
        #expect(!APNsRegistrar.marksChatUnread(forUserInfoType: nil, appIsActive: false))
    }

    @Test func nothingIsUnreadWhileTheChatIsOnScreen() {
        #expect(!APNsRegistrar.marksChatUnread(forUserInfoType: "turn_ready", appIsActive: true))
        #expect(!APNsRegistrar.marksChatUnread(forUserInfoType: "notify", appIsActive: true))
    }

    /// The lock screen never answers Allow (2026-10-02): only Deny maps to a
    /// decision, and a stale PERMISSION_ALLOW identifier from a notification
    /// delivered before this build must not approve anything.
    @Test func onlyTheDenyActionMapsToADecision() {
        #expect(PermissionAction.decision(forActionIdentifier: "PERMISSION_DENY") == "deny")
        #expect(PermissionAction.decision(forActionIdentifier: "PERMISSION_ALLOW") == nil)
        #expect(PermissionAction.decision(forActionIdentifier: "PERMISSION_REVIEW") == nil)
        #expect(PermissionAction.decision(forActionIdentifier: "com.apple.UNNotificationDefaultActionIdentifier") == nil)
        #expect(PermissionAction.decision(forActionIdentifier: "") == nil)
    }

    @Test func aPermissionPushFollowsTheForegroundRuleLikeAnyOther() {
        // Foregrounded, the approval card is already on screen, so the banner
        // is suppressed. Backgrounded it must present: the remote alert now
        // carries the request's text, and nothing composes a local copy.
        #expect(APNsRegistrar.presentationOptions(forUserInfoType: "permission_request", appIsActive: true) == [])
        #expect(
            APNsRegistrar.presentationOptions(forUserInfoType: "permission_request", appIsActive: false)
                == [.banner, .sound, .list]
        )
    }

    @Test func otherPushTypesFollowTheForegroundSuppressionRule() {
        #expect(APNsRegistrar.presentationOptions(forUserInfoType: "reauth_needed", appIsActive: true) == [])
        #expect(
            APNsRegistrar.presentationOptions(forUserInfoType: "reauth_needed", appIsActive: false)
                == [.banner, .sound, .list]
        )
        #expect(APNsRegistrar.presentationOptions(forUserInfoType: nil, appIsActive: false) == [.banner, .sound, .list])
    }
}
