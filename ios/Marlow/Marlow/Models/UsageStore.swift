import Foundation

/// The call `UsageStore` makes, as a seam for tests and `UIFixture` (same
/// shape as `ActivityAPI`).
protocol UsageAPI {
    func usage(deviceToken: String) async throws -> UsageReport
}

extension APIClient: UsageAPI {}

/// What the Usage panel shows, from one `GET /v1/usage`
/// (docs/tasks/usage-visibility.md). Fetched fresh each time the sheet opens
/// and never persisted, like `MarlowStore`: the server's reading is the
/// record, and a copy on the phone would only be an older one.
@MainActor
final class UsageStore: ObservableObject {
    @Published private(set) var report: UsageReport?
    @Published private(set) var isLoading = false
    @Published private(set) var errorMessage: String?

    private let api: UsageAPI
    private let deviceToken: String

    init(api: UsageAPI = APIClient(), deviceToken: String) {
        self.api = api
        self.deviceToken = deviceToken
    }

    /// True once a report has come back, so the empty lines are not shown for
    /// the half-second before anything has loaded.
    var hasLoaded: Bool { report != nil }

    /// A failure keeps what was already shown; only a first load with nothing
    /// to show becomes an error state.
    func refresh() async {
        guard !isLoading else { return }
        isLoading = true
        defer { isLoading = false }
        do {
            report = try await api.usage(deviceToken: deviceToken)
            errorMessage = nil
        } catch {
            errorMessage = "Couldn't load usage. \(error.localizedDescription)"
        }
    }
}
