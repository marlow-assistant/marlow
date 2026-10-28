import Foundation

/// The thinking-effort level the phone asks the server to run a turn at
/// (docs/tasks/model-and-effort-config.md piece 2).
///
/// Three levels, not the CLI's five: `xhigh` and `max` only add latency on this
/// workload and there is no reason to expose them. This set must stay in step
/// with the server's allowlist — the server validates independently and falls
/// back to its default on anything it doesn't recognise, so a mismatch here
/// degrades quietly rather than breaking a turn, but it also means a level
/// added here alone would silently do nothing.
///
/// This is a "settle on a level" control, NOT a "think harder about this one"
/// button, and the distinction is load-bearing: changing effort does not
/// preserve the session's cached prefix, so the cost is paid on every *change*.
/// The phone therefore sends the same value on every message and the switch is
/// paid only when Mike actually moves the picker. Do not evolve this into a
/// per-message control without re-reading that section of the task doc.
enum ThinkingEffort: String, CaseIterable, Identifiable, Sendable {
    case low
    case medium
    case high

    var id: String { rawValue }

    /// What goes on the wire. Matches the server's allowlist exactly.
    var wireValue: String { rawValue }

    /// Sentence-case for the picker in Settings.
    var displayName: String {
        switch self {
        case .low: return "Low"
        case .medium: return "Medium"
        case .high: return "High"
        }
    }

    /// Matches the server-side default from piece 1, so an untouched app and an
    /// unconfigured server agree rather than disagreeing invisibly. Medium
    /// since 2026-10-02: usefulness on day one beats latency (server/config.py).
    static let `default`: ThinkingEffort = .medium
}

/// Where the chosen level lives. `UserDefaults` rather than anything larger:
/// it survives relaunch and the value rides on every message. Since
/// 2026-10-16 the box also keeps a copy so every client shows the same level
/// (`PreferenceSync`), but the turn still runs on what the message carries.
///
/// A plain type rather than only `@AppStorage` because `ChatStore` has to read
/// it at send time and is not a view. Both sides go through this one key, so
/// the picker and the send path cannot drift apart.
enum EffortPreference {
    static let storageKey = "thinkingEffort"

    static func current(in defaults: UserDefaults = .standard) -> ThinkingEffort {
        guard let raw = defaults.string(forKey: storageKey),
              let effort = ThinkingEffort(rawValue: raw)
        else {
            // Covers first launch and a value written by a future build that
            // knows a level this one doesn't. Falling back beats persisting a
            // level this app can't display in the picker.
            return .default
        }
        return effort
    }

    static func set(_ effort: ThinkingEffort, in defaults: UserDefaults = .standard) {
        defaults.set(effort.rawValue, forKey: storageKey)
    }
}

extension String {
    /// `"claude-sonnet-5"` -> `"Sonnet"`, `"sonnet"` -> `"Sonnet"`. The server
    /// pins the `sonnet` *alias* rather than a version string, so what arrives
    /// is usually already short; this also handles the full name the CLI
    /// records, since `/health` reports whatever `settings.model` holds and a
    /// `MARLOW_MODEL` override may spell it either way.
    ///
    /// Falls back to the whole string untouched rather than guessing, so an
    /// unfamiliar model name shows up as itself instead of being mangled into
    /// something that reads authoritative and is wrong.
    var capitalizedModelName: String {
        let parts = split(separator: "-")
        // The tier is the segment after a leading "claude", when there is one.
        if parts.count >= 2, parts[0].lowercased() == "claude" {
            return parts[1].capitalized
        }
        if parts.count == 1, let only = parts.first {
            return only.capitalized
        }
        return self
    }
}
