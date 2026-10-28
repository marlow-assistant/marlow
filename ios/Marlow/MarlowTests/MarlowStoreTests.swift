import Foundation
import Testing
@testable import Marlow

/// The Marlow sheet's data (docs/tasks/ux-lane.md §3a): wire format, the
/// store, and the file loader.
struct IdentityDecodingTests {
    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder().decode(type, from: Data(json.utf8))
    }

    @Test func indexDecodesFilesAndSchedulesIncludingNulls() throws {
        let index = try decode(IdentityIndex.self, """
        {"files": [
          {"id": "SOUL.md", "kind": "soul", "modified_at": 1759000000.0, "size": 3191, "description": null},
          {"id": "memory/travel.md", "kind": "memory", "modified_at": 1759000100.5, "size": 24495,
           "description": "Trips and bookings"},
          {"id": "x.md", "kind": "from_the_future", "modified_at": 1.0, "size": 1}
        ],
        "schedules": {"timezone": "America/Toronto", "rejected": null, "entries": [
          {"id": "morning", "cron": "0 8 * * *", "at": null, "recurrence": {"type": "daily", "times": ["08:00"]},
           "next_fire_at": 1759000000.0, "notify": null, "prompt": null, "job": "morning",
           "job_file": "cron/morning.md"},
          {"id": "starlink", "cron": null, "at": "2026-10-20T08:15-0400", "recurrence": {"type": "once"},
           "next_fire_at": null, "notify": "Downgrade Starlink.", "prompt": null, "job": null, "job_file": null}
        ]}}
        """)
        #expect(index.files.map(\.kind) == [.soul, .memory, .unknown])
        #expect(index.files[0].description == nil)
        // A missing `description` key decodes like an explicit null.
        #expect(index.files[2].description == nil)
        #expect(index.files[1].modifiedAt == 1759000100.5)
        let entries = index.schedules.entries
        #expect(index.schedules.timezone == "America/Toronto")
        #expect(index.schedules.rejected == nil)
        #expect(entries[0].jobFile == "cron/morning.md")
        #expect(entries[0].nextFireAt == 1759000000.0)
        #expect(entries[1].cron == nil)
        #expect(entries[1].nextFireAt == nil)
        #expect(entries[1].notify == "Downgrade Starlink.")
        #expect(entries[0].recurrence == .daily(times: ["08:00"]))
        #expect(entries[1].recurrence == .once)
    }

    @Test func recurrenceDecodesEveryShapeAndToleratesTheUnknown() throws {
        func rec(_ json: String) throws -> Recurrence { try decode(Recurrence.self, json) }
        #expect(try rec(#"{"type":"daily","times":["08:00","20:00"]}"#) == .daily(times: ["08:00", "20:00"]))
        #expect(try rec(#"{"type":"weekly","weekdays":[1],"times":["08:03"]}"#) == .weekly(weekdays: [1], times: ["08:03"]))
        #expect(try rec(#"{"type":"monthly","days":[1,15],"times":["08:00"]}"#) == .monthly(days: [1, 15], times: ["08:00"]))
        #expect(try rec(#"{"type":"once"}"#) == .once)
        #expect(try rec(#"{"type":"other"}"#) == .other)
        // A type from a newer server, and no type at all, read as `other`.
        #expect(try rec(#"{"type":"hourly","times":["08:00"]}"#) == .other)
        #expect(try rec("{}") == .other)
        // Missing arrays decode empty.
        #expect(try rec(#"{"type":"daily"}"#) == .daily(times: []))
        #expect(try rec(#"{"type":"weekly"}"#) == .weekly(weekdays: [], times: []))
        #expect(try rec(#"{"type":"monthly"}"#) == .monthly(days: [], times: []))
    }

    @Test func anEntryWithoutARecurrenceReadsAsOther() throws {
        let entry = try decode(ScheduleEntry.self, #"{"id":"x","cron":"0 8 1 6 *"}"#)
        #expect(entry.recurrence == .other)
    }

    @Test func aRejectedStoreDecodesItsReason() throws {
        let index = try decode(IdentityIndex.self, """
        {"files": [], "schedules": {"timezone": null, "rejected": "line 3: bad cron", "entries": []}}
        """)
        #expect(index.schedules.rejected == "line 3: bad cron")
        #expect(index.schedules.entries.isEmpty)
    }

    @Test func aMissingSchedulesKeyStillShowsTheFiles() throws {
        let index = try decode(IdentityIndex.self, #"{"files": []}"#)
        #expect(index.schedules == .empty)
    }

    @Test func fileDecodes() throws {
        let file = try decode(IdentityFile.self, """
        {"id": "memory/travel.md", "kind": "memory", "modified_at": 5.0, "text": "# Hi", "truncated": true}
        """)
        #expect(file == IdentityFile(id: "memory/travel.md", kind: .memory, modifiedAt: 5.0, text: "# Hi", truncated: true))
    }
}

// MARK: - Store

private final class FakeMarlowAPI: MarlowAPI, @unchecked Sendable {
    var index: IdentityIndex
    var files: [String: IdentityFile] = [:]
    var error: Error?
    var requestedIDs: [String] = []
    var identityCalls = 0

    init(index: IdentityIndex) { self.index = index }

    func identity(deviceToken: String) async throws -> IdentityIndex {
        identityCalls += 1
        if let error { throw error }
        return index
    }

    func identityFile(id: String, deviceToken: String) async throws -> IdentityFile {
        requestedIDs.append(id)
        if let error { throw error }
        guard let file = files[id] else { throw URLError(.fileDoesNotExist) }
        return file
    }
}

private func info(_ id: String, _ kind: IdentityKind, _ modified: Double, description: String? = nil) -> IdentityFileInfo {
    IdentityFileInfo(id: id, kind: kind, modifiedAt: modified, size: 10, description: description)
}

@MainActor
struct MarlowStoreTests {
    private let index = IdentityIndex(
        files: [
            info("SOUL.md", .soul, 1), info("CLAUDE.md", .instructions, 2),
            info("memory/old.md", .memory, 10), info("memory/new.md", .memory, 30),
            info("cron/morning.md", .job, 5),
        ],
        schedules: .empty
    )

    @Test func refreshLoadsTheIndexAndSortsMemoryNewestFirst() async {
        let api = FakeMarlowAPI(index: index)
        let store = MarlowStore(api: api, deviceToken: "tok")
        #expect(!store.hasLoaded)
        await store.refresh()
        #expect(store.hasLoaded)
        #expect(store.errorMessage == nil)
        #expect(store.memoryFiles.map(\.id) == ["memory/new.md", "memory/old.md"])
        #expect(store.file(of: .soul)?.id == "SOUL.md")
        #expect(store.file(of: .user) == nil)
    }

    @Test func aFirstFailureIsAnErrorWithNothingLoaded() async {
        let api = FakeMarlowAPI(index: index)
        api.error = URLError(.notConnectedToInternet)
        let store = MarlowStore(api: api, deviceToken: "tok")
        await store.refresh()
        #expect(!store.hasLoaded)
        #expect(store.errorMessage != nil)
    }

    @Test func aLaterFailureKeepsWhatWasShownAndSaysSo() async {
        let api = FakeMarlowAPI(index: index)
        let store = MarlowStore(api: api, deviceToken: "tok")
        await store.refresh()
        api.error = URLError(.timedOut)
        await store.refresh()
        #expect(store.index == index)
        #expect(store.errorMessage != nil)
        api.error = nil
        await store.refresh()
        #expect(store.errorMessage == nil)
    }

    @Test func loadersAreSharedPerFileSoPanelsDoNotRefetchIntoASpinner() async {
        let api = FakeMarlowAPI(index: index)
        let store = MarlowStore(api: api, deviceToken: "tok")
        #expect(store.loader(for: "SOUL.md") === store.loader(for: "SOUL.md"))
        #expect(store.loader(for: "SOUL.md") !== store.loader(for: "CLAUDE.md"))
    }

    @Test func aFileLoadsThenKeepsItsTextIfARefreshFails() async {
        let api = FakeMarlowAPI(index: index)
        let file = IdentityFile(id: "SOUL.md", kind: .soul, modifiedAt: 1, text: "hello", truncated: false)
        api.files["SOUL.md"] = file
        let loader = IdentityFileLoader(id: "SOUL.md", api: api, deviceToken: "tok")
        #expect(loader.state == .loading)
        await loader.load()
        #expect(loader.state == .loaded(file))
        #expect(api.requestedIDs == ["SOUL.md"])

        api.error = URLError(.notConnectedToInternet)
        await loader.load()
        #expect(loader.state == .loaded(file))
    }

    @Test func aFileThatFailsToLoadSaysSo() async {
        let api = FakeMarlowAPI(index: index)
        let loader = IdentityFileLoader(id: "nope.md", api: api, deviceToken: "tok")
        await loader.load()
        guard case .failed = loader.state else {
            Issue.record("expected .failed, got \(loader.state)")
            return
        }
    }
}

// MARK: - Wire format

extension APIClientTests {
    private func identityClient() -> APIClient {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [StubURLProtocol.self]
        return APIClient(
            baseURL: URL(string: "https://marlow.example.ts.net:8443")!,
            session: URLSession(configuration: config)
        )
    }

    @Test func identityFileSendsTheIdAsAnEncodedQueryValue() async throws {
        StubURLProtocol.respond(json: """
        {"id": "memory/a b.md", "kind": "memory", "modified_at": 1.0, "text": "x", "truncated": false}
        """)
        let file = try await identityClient().identityFile(id: "memory/a b.md", deviceToken: "tok")
        #expect(file.text == "x")

        let request = try #require(StubURLProtocol.lastRequest)
        #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer tok")
        let url = try #require(request.url)
        let components = try #require(URLComponents(url: url, resolvingAgainstBaseURL: false))
        #expect(components.path == "/v1/identity/file")
        #expect(components.queryItems?.first { $0.name == "id" }?.value == "memory/a b.md")
        // A space must not survive raw into the query string.
        #expect(!url.absoluteString.contains(" "))
    }

    @Test func identityHitsItsPath() async throws {
        StubURLProtocol.respond(json: #"{"files": [], "schedules": {"entries": []}}"#)
        let index = try await identityClient().identity(deviceToken: "tok")
        #expect(index.files.isEmpty)
        #expect(StubURLProtocol.lastRequest?.url?.path == "/v1/identity")
    }
}
