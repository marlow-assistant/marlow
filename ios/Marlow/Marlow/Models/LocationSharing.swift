import Foundation

/// How much of where Mike is rides on each message (docs/tasks/message-location.md).
///
/// One control rather than a per-message button: the setting applies to every
/// message the app sends, and the server treats a message with no `location`
/// as "no location from this device right now". Off is the default, so nothing
/// leaves the phone until Mike asks for it.
///
/// Approximate is enforced on the phone, not trusted to the server: Core
/// Location is asked for reduced accuracy, so the precise value never exists
/// in the app, let alone on the wire.
enum LocationSharing: String, CaseIterable, Identifiable, Sendable {
    case off
    case approximate
    case precise

    var id: String { rawValue }

    var displayName: String {
        switch self {
        case .off: return "Off"
        case .approximate: return "Approximate"
        case .precise: return "Precise"
        }
    }

    static let `default`: LocationSharing = .off
}

/// Where the chosen mode lives. Same shape and reasons as `EffortPreference`:
/// `UserDefaults`, one key shared by the picker and the provider that reads it,
/// and a type rather than bare `@AppStorage` because `LocationProvider` is not
/// a view.
enum LocationPreference {
    static let storageKey = "locationSharing"

    static func current(in defaults: UserDefaults = .standard) -> LocationSharing {
        guard let raw = defaults.string(forKey: storageKey),
              let sharing = LocationSharing(rawValue: raw)
        else {
            // First launch, or a mode written by a build that knows one this
            // build doesn't. Off, not the nearest guess: an unrecognised value
            // must never turn into sharing the user didn't pick.
            return .default
        }
        return sharing
    }

    static func set(_ sharing: LocationSharing, in defaults: UserDefaults = .standard) {
        defaults.set(sharing.rawValue, forKey: storageKey)
    }
}

/// What goes on the wire as `location` (docs/tasks/message-location.md). Built
/// only by `LocationProvider.outgoing`, so a value of this type is already a
/// fresh fix the user agreed to share.
struct OutgoingLocation: Equatable, Sendable {
    let latitude: Double
    let longitude: Double
    /// Nil when Core Location reports no usable accuracy (a negative
    /// `horizontalAccuracy` means the coordinate is invalid; the server also
    /// rejects zero), so the key is left off rather than sent as nonsense.
    let accuracyM: Double?
    /// Reverse-geocoded on the phone; the server never sees a street.
    let place: String?
}
