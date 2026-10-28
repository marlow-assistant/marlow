import Foundation

/// The server this phone paired with, compiled into BOTH the app and the
/// notification service extension.
///
/// Until 2026-10-17 the app only used the host typed on the pairing screen for
/// the pairing call itself; every other request, and the extension's fetch,
/// went to one hardcoded server. A second instance on another port
/// (docs/tasks/marlow-for-taylor.md) would pair fine and then send its APNs
/// token, chat and replies to the first instance, which rejects its device
/// token: no pushes at all, and every banner left generic.
///
/// In the App Group's defaults rather than the app's own, because the
/// extension is a separate process and this is the only store both can read.
/// Not in the Keychain: a host is not a secret, and the shared Keychain copy
/// is deliberately the token alone (`SharedCredential`).
enum PairedHost {
    /// Read only by a phone with no stored host. Until 2026-10-26 it was the
    /// original box's URL, so phones paired before hosts were stored kept
    /// working with no re-pair. The one such phone re-paired that day, so it
    /// is now a placeholder that only has to parse: an unpaired app has no
    /// token and never gets as far as using it.
    static let fallback = "https://marlow.tail1234.ts.net:8443"

    static let storageKey = "pairedHost"

    static var defaults: UserDefaults {
        UserDefaults(suiteName: NotificationShared.appGroup) ?? .standard
    }

    /// Always a usable URL string: anything stored that does not parse falls
    /// back rather than leaving callers to handle a missing server.
    static func current(in defaults: UserDefaults = PairedHost.defaults) -> String {
        guard let stored = defaults.string(forKey: storageKey), isUsable(stored) else {
            return fallback
        }
        return stored
    }

    static func url(in defaults: UserDefaults = PairedHost.defaults) -> URL {
        // `current` only returns strings `isUsable` accepted, and the fallback
        // is a literal that parses.
        URL(string: current(in: defaults))!
    }

    /// Called once pairing has succeeded against `host`, never before: a host
    /// that failed to pair must not replace the one that works.
    ///
    /// Kept on sign-out. It is not a credential, and re-pairing is almost
    /// always to the same server, so the pairing screen pre-fills it.
    static func save(_ host: String, in defaults: UserDefaults = PairedHost.defaults) {
        let normalized = normalize(host)
        guard isUsable(normalized) else { return }
        defaults.set(normalized, forKey: storageKey)
    }

    /// Trimmed, with no trailing slash: the extension builds its URL by string
    /// interpolation, and `host/` would give it `//v1/turn/…`.
    static func normalize(_ host: String) -> String {
        var trimmed = host.trimmingCharacters(in: .whitespacesAndNewlines)
        while trimmed.hasSuffix("/") { trimmed.removeLast() }
        return trimmed
    }

    private static func isUsable(_ host: String) -> Bool {
        guard let url = URL(string: host), let scheme = url.scheme?.lowercased() else {
            return false
        }
        return (scheme == "https" || scheme == "http") && url.host?.isEmpty == false
    }
}
