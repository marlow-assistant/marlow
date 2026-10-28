import Foundation

/// The call `ActivityStore` makes, as a seam for tests and `UIFixture`.
protocol ActivityAPI {
    func activity(limit: Int, before: Double?, deviceToken: String) async throws -> ActivityPage
    func activityOutput(turnId: String, deviceToken: String) async throws -> ActivityOutput
}

extension APIClient: ActivityAPI {}

/// Pages through `GET /v1/activity` for `ActivityList`. Fetched fresh each
/// time the pane opens and never persisted: the server's file is the record,
/// and a cached copy on the phone would only be a staler one.
@MainActor
final class ActivityStore: ObservableObject {
    @Published private(set) var entries: [ActivityEntry] = []
    @Published private(set) var isLoading = false
    @Published private(set) var hasMore = false
    @Published private(set) var errorMessage: String?
    /// True once a first page has come back, so the empty state is not shown
    /// for the half-second before anything has loaded.
    @Published private(set) var hasLoaded = false

    static let pageSize = 50

    private let api: ActivityAPI
    private let deviceToken: String
    private var nextBefore: Double?

    init(api: ActivityAPI = APIClient(), deviceToken: String) {
        self.api = api
        self.deviceToken = deviceToken
    }

    /// What a background job said, fetched when its row is first expanded.
    enum Output: Equatable {
        case loading
        case failed
        case loaded(String)
    }

    @Published private(set) var outputs: [String: Output] = [:]

    /// The rows worth showing — see `ActivityFormat.isVisible`.
    func visibleEntries(showBackground: Bool) -> [ActivityEntry] {
        entries.filter { ActivityFormat.isVisible($0, showBackground: showBackground) }
    }

    /// Fetch what `entry` said, once. A failed fetch is retried by expanding
    /// the row again.
    func loadOutput(for entry: ActivityEntry) async {
        guard entry.hasOutput else { return }
        switch outputs[entry.turnId] {
        case .loading, .loaded: return
        case .failed, nil: break
        }
        outputs[entry.turnId] = .loading
        do {
            let output = try await api.activityOutput(turnId: entry.turnId, deviceToken: deviceToken)
            outputs[entry.turnId] = .loaded(output.text)
        } catch {
            outputs[entry.turnId] = .failed
        }
    }

    func refresh() async {
        await load(before: nil, replacing: true)
    }

    func loadMore() async {
        guard hasMore, let nextBefore else { return }
        await load(before: nextBefore, replacing: false)
    }

    private func load(before: Double?, replacing: Bool) async {
        guard !isLoading else { return }
        isLoading = true
        defer { isLoading = false }
        do {
            let page = try await api.activity(limit: Self.pageSize, before: before, deviceToken: deviceToken)
            if replacing {
                entries = page.entries
            } else {
                // De-duplicated by id anyway: the cursor is exact, but a row
                // shown twice is the one paging bug a user would notice.
                let seen = Set(entries.map(\.turnId))
                entries += page.entries.filter { !seen.contains($0.turnId) }
            }
            nextBefore = page.nextBefore
            hasMore = page.nextBefore != nil
            errorMessage = nil
            hasLoaded = true
        } catch {
            errorMessage = "Couldn't load activity. \(error.localizedDescription)"
        }
    }
}
