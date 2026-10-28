import Foundation

/// The calls `IdeasStore` makes, as a seam for tests and `UIFixture` (same
/// shape as `UsageAPI`).
protocol IdeasAPI {
    func ideas(deviceToken: String) async throws -> [Idea]
    func dismissIdea(id: String, reason: String?, deviceToken: String) async throws
}

extension APIClient: IdeasAPI {}

/// What the Ideas panel and the spotlight card above the composer show
/// (docs/tasks/ideas-panel.md, "v1"). Unlike the Marlow sheet's stores this one
/// lives as long as the chat screen: the lightbulb's dot and the spotlight card
/// are visible without the panel open, so something has to have fetched.
///
/// **Quiet on failure, by design** (Mike: nothing off-putting in chat). A failed
/// fetch keeps the last good list and says nothing anywhere; there is no error
/// state to render. The panel is a side surface, not something Mike is waiting
/// on.
///
/// **Optimistic removal.** Do it and Not now take effect on the phone at once.
/// The ids are remembered for the app's lifetime (`removedIds`), so a fetch that
/// was already in flight when the tap landed cannot bring the card back. A
/// dismiss the server then refuses or never receives is not retried or shown:
/// the card stays gone until the next launch, when the server's list is the
/// truth again. A speculative retry queue would be more machinery than a
/// dismissal is worth.
@MainActor
final class IdeasStore: ObservableObject {
    /// The server's open ideas in store order, minus those removed here, plus
    /// the ones collapsed by Not now until the panel closes.
    @Published private(set) var ideas: [Idea] = []
    /// Not now has been tapped on these; the panel shows each as a one-line
    /// row (with a reason field) until it closes.
    @Published private(set) var collapsedIds: Set<String> = []
    /// Reasons already sent, by id, so the collapsed row can say so.
    @Published private(set) var reasons: [String: String] = [:]
    /// When the panel was last opened. Drives the lightbulb's dot, and
    /// persisted so a relaunch does not light it again for ideas already seen.
    @Published private(set) var lastOpened: Date?

    static let lastOpenedKey = "ideasPanelLastOpened"

    private let api: IdeasAPI
    private let deviceToken: String
    private let defaults: UserDefaults
    private let now: () -> Date
    private let calendar: Calendar
    private var removedIds: Set<String> = []
    private var isFetching = false
    /// Collapsed rows belong to one opening of the panel. A Not now on the
    /// spotlight card, with the panel shut, just removes the idea: a later
    /// opening must not start with a stale "Dismissed" row (as on web).
    private var isPanelOpen = false

    init(
        api: IdeasAPI = APIClient(), deviceToken: String, defaults: UserDefaults = .standard,
        now: @escaping () -> Date = { Date() }, calendar: Calendar = .current
    ) {
        self.api = api
        self.deviceToken = deviceToken
        self.defaults = defaults
        self.now = now
        self.calendar = calendar
        self.lastOpened = defaults.object(forKey: Self.lastOpenedKey) as? Date
    }

    // MARK: Derived

    /// Ideas that are not collapsed: what the lightbulb and the spotlight
    /// judge by. Expiry is `IdeaFormat`'s to apply.
    private var openIdeas: [Idea] {
        ideas.filter { !collapsedIds.contains($0.id) }
    }

    /// The panel's sections. Includes collapsed ideas (as rows in place);
    /// ones past `until` are hidden whatever the server last said.
    var sections: [IdeaFormat.Group] {
        IdeaFormat.sections(ideas, today: now(), calendar: calendar)
    }

    var spotlight: Idea? {
        IdeaFormat.spotlight(openIdeas, today: now(), calendar: calendar)
    }

    var showsDot: Bool {
        IdeaFormat.showsDot(openIdeas, lastOpened: lastOpened, today: now(), calendar: calendar)
    }

    func untilText(_ idea: Idea) -> String? {
        IdeaFormat.untilText(idea, today: now(), calendar: calendar)
    }

    // MARK: Fetch

    /// On panel open and on app foreground. A failure keeps what was shown and
    /// shows nothing (type comment).
    func refresh() async {
        guard !isFetching else { return }
        isFetching = true
        defer { isFetching = false }
        guard let fetched = try? await api.ideas(deviceToken: deviceToken) else { return }
        var merged = fetched.filter { !removedIds.contains($0.id) }
        // A collapsed row is already gone server-side, so a fetch while the
        // panel is open would drop it from under a half-typed reason. Put it
        // back where it was.
        for (index, idea) in ideas.enumerated() where collapsedIds.contains(idea.id) {
            merged.insert(idea, at: min(index, merged.count))
        }
        ideas = merged
    }

    // MARK: Panel

    /// Marks the panel opened now (clearing the dot) and refreshes.
    func panelOpened() async {
        isPanelOpen = true
        let opened = now()
        lastOpened = opened
        defaults.set(opened, forKey: Self.lastOpenedKey)
        await refresh()
    }

    /// Collapsed rows go away when the panel closes.
    func panelClosed() {
        isPanelOpen = false
        guard !collapsedIds.isEmpty else { return }
        ideas.removeAll { collapsedIds.contains($0.id) }
        collapsedIds = []
    }

    // MARK: Responses

    /// Do it: the card goes at once. The acceptance itself reaches the server
    /// on the chat message (`idea_id`), not through here.
    func accept(_ idea: Idea) {
        removedIds.insert(idea.id)
        collapsedIds.remove(idea.id)
        ideas.removeAll { $0.id == idea.id }
    }

    /// Not now, or the reason that follows it. The first call (no reason)
    /// collapses the card at once and posts; a later call with a reason
    /// re-posts, which the server takes as replacing the reason. Returns
    /// whether the server took it, so the row can show the reason as sent.
    @discardableResult
    func dismiss(_ idea: Idea, reason: String? = nil) async -> Bool {
        let trimmed = reason?.trimmingCharacters(in: .whitespacesAndNewlines)
        let sent = (trimmed?.isEmpty ?? true) ? nil : trimmed
        removedIds.insert(idea.id)
        if isPanelOpen {
            collapsedIds.insert(idea.id)
        } else {
            ideas.removeAll { $0.id == idea.id }
        }
        do {
            try await api.dismissIdea(id: idea.id, reason: sent, deviceToken: deviceToken)
        } catch {
            return false
        }
        if let sent { reasons[idea.id] = sent }
        return true
    }
}
