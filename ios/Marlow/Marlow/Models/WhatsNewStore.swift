import Foundation

/// The call `WhatsNewStore` makes, as a seam for tests and `UIFixture` (same
/// shape as `IdeasAPI`).
protocol ChangesAPI {
    func changes(deviceToken: String) async throws -> ChangesResponse
}

extension APIClient: ChangesAPI {}

/// The "Marlow was updated" card and Settings' What's new list
/// (docs/tasks/whats-new.md). Lives as long as the chat screen, like
/// `IdeasStore`, because the card shows without anything else open.
///
/// What this device has shown is remembered here, per device, on purpose: a
/// second person on the box learns of an update on their own phone, not
/// because someone else opened theirs. Quiet on failure, as ideas are: a fetch
/// that fails keeps the last server half and the app's own entries still show.
@MainActor
final class WhatsNewStore: ObservableObject {
    /// Everything known, newest first: Settings' list.
    @Published private(set) var entries: [ChangeEntry] = []
    /// What the card shows. Stays put until `acknowledge()`, so "Don't show
    /// again" can swap the card to its confirmation without the list emptying
    /// underneath it.
    @Published private(set) var pending: [ChangeEntry] = []
    @Published private(set) var muted: Bool

    static let seenKey = "whatsNewSeenIds"
    static let mutedKey = "whatsNewMuted"

    private let api: ChangesAPI
    private let deviceToken: String
    private let defaults: UserDefaults
    private let bundled: [ChangeEntry]
    private var server: [ChangeEntry] = []
    private var onboardingComplete = true
    private var isFetching = false

    init(
        api: ChangesAPI = APIClient(), deviceToken: String, defaults: UserDefaults = .standard,
        bundled: [ChangeEntry] = WhatsNew.bundled()
    ) {
        self.api = api
        self.deviceToken = deviceToken
        self.defaults = defaults
        self.bundled = bundled
        self.muted = defaults.bool(forKey: Self.mutedKey)
        self.entries = WhatsNew.merge(app: bundled, server: [])
    }

    private var seen: Set<Int>? {
        (defaults.array(forKey: Self.seenKey) as? [Int]).map(Set.init)
    }

    private func setSeen(_ ids: Set<Int>) {
        defaults.set(ids.sorted(), forKey: Self.seenKey)
    }

    /// On launch and on foreground.
    func refresh() async {
        guard !isFetching else { return }
        isFetching = true
        defer { isFetching = false }
        if let fetched = try? await api.changes(deviceToken: deviceToken) {
            server = fetched.changes
            onboardingComplete = fetched.onboardingComplete
        }
        entries = WhatsNew.merge(app: bundled, server: server)
        if muted || !onboardingComplete {
            // Counted as seen while muted, so turning the card back on later
            // brings only what is new from then on, not the backlog. The same
            // before onboarding ends: a fresh install's first screen is
            // onboarding, not "Marlow was updated", and what would have shown
            // is history by the time it finishes.
            setSeen((seen ?? []).union(entries.map(\.id)))
            pending = []
            return
        }
        let previouslySeen = seen
        let unseen = WhatsNew.unseen(entries, seen: previouslySeen)
        if previouslySeen == nil {
            // First run: everything older than what the card shows is history,
            // not news. Recorded now, or the next launch would show it.
            let shown = Set(unseen.map(\.id))
            setSeen(Set(entries.map(\.id)).subtracting(shown))
        }
        pending = unseen
    }

    /// The card was closed, by OK or by swipe.
    func acknowledge() {
        guard !pending.isEmpty else { return }
        setSeen((seen ?? []).union(pending.map(\.id)))
        pending = []
    }

    /// Settings' switch. Leaves `pending` alone: a card that is up closes
    /// through `acknowledge()`.
    func setMuted(_ value: Bool) {
        muted = value
        defaults.set(value, forKey: Self.mutedKey)
        if value {
            setSeen((seen ?? []).union(entries.map(\.id)))
        }
    }
}
