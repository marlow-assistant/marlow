import Foundation

/// The calls the Marlow sheet makes, as a seam for tests and `UIFixture`
/// (same shape as `ActivityAPI`).
protocol MarlowAPI {
    func identity(deviceToken: String) async throws -> IdentityIndex
    func identityFile(id: String, deviceToken: String) async throws -> IdentityFile
}

extension APIClient: MarlowAPI {}

/// Everything the Marlow sheet shows except the activity list, from one
/// `GET /v1/identity` (docs/tasks/ux-lane.md §3a). Fetched fresh each time the
/// sheet opens and never persisted, like `ActivityStore`: the box's files are
/// the record, and a copy on the phone would only be a staler one.
@MainActor
final class MarlowStore: ObservableObject {
    @Published private(set) var index: IdentityIndex?
    @Published private(set) var isLoading = false
    @Published private(set) var errorMessage: String?

    private let api: MarlowAPI
    private let deviceToken: String
    private var loaders: [String: IdentityFileLoader] = [:]

    init(api: MarlowAPI = APIClient(), deviceToken: String) {
        self.api = api
        self.deviceToken = deviceToken
    }

    /// True once an index has come back, so an empty panel is not shown for
    /// the half-second before anything has loaded.
    var hasLoaded: Bool { index != nil }

    /// A failure keeps what was already shown and says so; only a first
    /// load with nothing to show becomes an error state.
    func refresh() async {
        guard !isLoading else { return }
        isLoading = true
        defer { isLoading = false }
        do {
            index = try await api.identity(deviceToken: deviceToken)
            errorMessage = nil
        } catch {
            errorMessage = "Couldn't load Marlow's files. \(error.localizedDescription)"
        }
    }

    /// Newest change first: what Marlow has just learned is what Mike is most
    /// likely to open the panel to check.
    var memoryFiles: [IdentityFileInfo] {
        (index?.files ?? []).filter { $0.kind == .memory }.sorted { $0.modifiedAt > $1.modifiedAt }
    }

    func file(of kind: IdentityKind) -> IdentityFileInfo? {
        index?.files.first { $0.kind == kind }
    }

    /// One loader per file id for the sheet's lifetime, so switching panels
    /// back to Soul shows the text already fetched while it refreshes,
    /// instead of a spinner every time.
    func loader(for id: String) -> IdentityFileLoader {
        if let existing = loaders[id] { return existing }
        let made = IdentityFileLoader(id: id, api: api, deviceToken: deviceToken)
        loaders[id] = made
        return made
    }
}

/// Loads one file's text for `IdentityFileView`.
@MainActor
final class IdentityFileLoader: ObservableObject {
    enum State: Equatable {
        case loading
        case loaded(IdentityFile)
        case failed(String)
    }

    @Published private(set) var state: State = .loading

    let id: String
    private let api: MarlowAPI
    private let deviceToken: String

    init(id: String, api: MarlowAPI, deviceToken: String) {
        self.id = id
        self.api = api
        self.deviceToken = deviceToken
    }

    /// Fetches fresh each time the screen appears. Text already on screen
    /// stays if the refresh fails: stale words beat a blank page.
    func load() async {
        do {
            state = .loaded(try await api.identityFile(id: id, deviceToken: deviceToken))
        } catch {
            if case .loaded = state { return }
            state = .failed("Couldn't load this file. \(error.localizedDescription)")
        }
    }
}
