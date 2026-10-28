import Foundation

/// The calls `PreferenceSync` makes, as a seam for tests.
protocol PreferencesAPI {
    func preferences(deviceToken: String) async throws -> Preferences
    func setPreferences(_ update: PreferencesUpdate, deviceToken: String) async throws
}

extension APIClient: PreferencesAPI {}

/// What a client sends: only what it has pending. Nil is "not sent".
struct PreferencesUpdate: Equatable {
    var face: Int?
    var effort: String?
}

/// The face and the Thinking level, the same on every client: the box holds
/// them (server/preferences.py). Until 2026-10-16 both were per device, and
/// web and iOS showed different Marlows that thought differently (Mike).
///
/// `UserDefaults` stays the copy everything reads (`MarlowAvatar`, the send
/// path's `EffortPreference`), so a pick shows at once, nothing waits on the
/// network, and an offline launch still has both.
///
/// A pick is marked pending until the box has it, and a pull never runs over
/// a pending pick: otherwise a pick made offline would be put back to the
/// box's older value on the next foreground. A box with nothing yet takes this
/// device's value, so devices that chose before sync existed agree from then on.
@MainActor
enum PreferenceSync {
    enum Item: CaseIterable {
        case face, effort

        var storageKey: String {
            switch self {
            case .face: FacePreference.storageKey
            case .effort: EffortPreference.storageKey
            }
        }

        var pendingKey: String { storageKey + "Pending" }
    }

    /// After a picker has stored its value: hold it for `send`.
    static func picked(_ item: Item, in defaults: UserDefaults = .standard) {
        defaults.set(true, forKey: item.pendingKey)
    }

    /// Sends whatever is pending, in one call. An item stays pending if the
    /// box can't be reached, or if it was picked again while this was in
    /// flight (that value sends next).
    static func send(api: PreferencesAPI, deviceToken: String, defaults: UserDefaults = .standard) async {
        let pending = Item.allCases.filter { defaults.bool(forKey: $0.pendingKey) }
        guard !pending.isEmpty else { return }
        var update = PreferencesUpdate()
        for item in pending {
            switch item {
            case .face: update.face = defaults.integer(forKey: item.storageKey)
            case .effort: update.effort = defaults.string(forKey: item.storageKey)
            }
        }
        guard (try? await api.setPreferences(update, deviceToken: deviceToken)) != nil else { return }
        if pending.contains(.face), defaults.integer(forKey: Item.face.storageKey) == update.face {
            defaults.removeObject(forKey: Item.face.pendingKey)
        }
        if pending.contains(.effort), defaults.string(forKey: Item.effort.storageKey) == update.effort {
            defaults.removeObject(forKey: Item.effort.pendingKey)
        }
    }

    /// Launch and each foreground. Returns what changed, so the caller can
    /// bring the icon along for a new face.
    @discardableResult
    static func pull(api: PreferencesAPI, deviceToken: String, defaults: UserDefaults = .standard) async -> Set<Item> {
        await send(api: api, deviceToken: deviceToken, defaults: defaults)
        guard let remote = try? await api.preferences(deviceToken: deviceToken) else { return [] }
        var changed = Set<Item>()
        var seeded = false
        for item in Item.allCases {
            // A pick made during the fetch, or not yet sent, wins.
            guard !defaults.bool(forKey: item.pendingKey) else { continue }
            let value: Any?
            switch item {
            case .face: value = remote.face
            case .effort: value = remote.effort
            }
            guard let value else {
                if defaults.object(forKey: item.storageKey) != nil {
                    picked(item, in: defaults)
                    seeded = true
                }
                continue
            }
            // The raw value, not a parsed one: a face or level this build
            // doesn't know shows the default here but stays the choice.
            if !isStored(value, for: item, in: defaults) {
                defaults.set(value, forKey: item.storageKey)
                changed.insert(item)
            }
        }
        if seeded { await send(api: api, deviceToken: deviceToken, defaults: defaults) }
        return changed
    }

    private static func isStored(_ value: Any, for item: Item, in defaults: UserDefaults) -> Bool {
        switch item {
        case .face: defaults.object(forKey: item.storageKey) != nil && defaults.integer(forKey: item.storageKey) == value as? Int
        case .effort: defaults.string(forKey: item.storageKey) == value as? String
        }
    }
}
