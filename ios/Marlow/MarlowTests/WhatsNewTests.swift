import Foundation
import Testing
@testable import Marlow

// MARK: - Fixtures

private func entry(_ id: Int, _ part: String = "app", category: String = "feature") -> ChangeEntry {
    ChangeEntry(id: id, date: "2026-10-23", part: part, category: category, text: "change \(id)")
}

private final class FakeChanges: ChangesAPI, @unchecked Sendable {
    var result: Result<[ChangeEntry], Error> = .success([])
    var onboardingComplete = true
    func changes(deviceToken: String) async throws -> ChangesResponse {
        ChangesResponse(changes: try result.get(), onboardingComplete: onboardingComplete)
    }
}

/// A fresh suite per store test: Swift Testing runs tests in parallel.
private func freshDefaults() -> UserDefaults {
    UserDefaults(suiteName: "WhatsNewTests-\(UUID().uuidString)")!
}

// MARK: - Rules

struct WhatsNewRuleTests {
    @Test func eachHalfContributesOnlyItsOwnPart() {
        // An app built from a newer main than its box bundles server entries
        // the box isn't running yet; those must not be announced.
        let merged = WhatsNew.merge(
            app: [entry(1, "app"), entry(5, "server")],
            server: [entry(2, "server"), entry(6, "app")]
        )
        #expect(merged.map(\.id) == [2, 1])
    }

    @Test func firstRunShowsOnlyTheNewestFew() {
        let entries = (1...7).reversed().map { entry($0) }
        #expect(WhatsNew.unseen(entries, seen: nil).map(\.id) == [7, 6, 5, 4, 3])
    }

    @Test func unseenIsASetNotAHighWaterMark() {
        let entries = [entry(12, "app"), entry(11, "server")]
        #expect(WhatsNew.unseen(entries, seen: [12]).map(\.id) == [11])
    }

    @Test func dateText() {
        #expect(WhatsNew.dateText("2026-10-23") == "Oct 23")
        #expect(WhatsNew.dateText("soon") == "soon")
    }

    @Test func theBundledFileIsInTheApp() {
        // project.yml bundles the repo-root changes.json; without it the app's
        // half silently never shows.
        let bundled = WhatsNew.bundled()
        #expect(!bundled.isEmpty)
        #expect(bundled.allSatisfy { $0.part == "app" })
    }
}

// MARK: - Store

@MainActor
struct WhatsNewStoreTests {
    private let defaults = freshDefaults()

    private func makeStore(_ api: FakeChanges, bundled: [ChangeEntry]) -> WhatsNewStore {
        WhatsNewStore(api: api, deviceToken: "t", defaults: defaults, bundled: bundled)
    }

    @Test func anAcknowledgedCardDoesNotComeBack() async {
        let api = FakeChanges()
        api.result = .success([entry(1, "server")])
        let store = makeStore(api, bundled: [entry(2)])
        await store.refresh()
        #expect(store.pending.map(\.id) == [2, 1])
        store.acknowledge()
        #expect(store.pending.isEmpty)

        let relaunched = makeStore(api, bundled: [entry(2)])
        await relaunched.refresh()
        #expect(relaunched.pending.isEmpty)
    }

    @Test func firstRunHistoryIsNotShownOnTheNextLaunch() async {
        let api = FakeChanges()
        let bundled = (1...7).map { entry($0) }
        let store = makeStore(api, bundled: bundled)
        await store.refresh()
        #expect(store.pending.map(\.id) == [7, 6, 5, 4, 3])
        store.acknowledge()

        let relaunched = makeStore(api, bundled: bundled)
        await relaunched.refresh()
        #expect(relaunched.pending.isEmpty)
    }

    @Test func anOlderServerEntryArrivingLaterStillShows() async {
        let api = FakeChanges()
        let store = makeStore(api, bundled: [entry(1), entry(12)])
        await store.refresh()
        store.acknowledge()

        // The box deploys afterwards, with an entry numbered below the app's.
        api.result = .success([entry(11, "server")])
        await store.refresh()
        #expect(store.pending.map(\.id) == [11])
    }

    @Test func aFailedFetchStillShowsTheAppsHalf() async {
        let api = FakeChanges()
        api.result = .failure(URLError(.notConnectedToInternet))
        let store = makeStore(api, bundled: [entry(3)])
        await store.refresh()
        #expect(store.pending.map(\.id) == [3])
    }

    @Test func mutedShowsNothingAndUnmutingSkipsTheBacklog() async {
        let api = FakeChanges()
        let store = makeStore(api, bundled: [entry(1)])
        await store.refresh()
        store.setMuted(true)
        // The card that was up stays up for its confirmation.
        #expect(store.pending.map(\.id) == [1])
        store.acknowledge()

        api.result = .success([entry(2, "server")])
        await store.refresh()
        #expect(store.pending.isEmpty)
        #expect(store.entries.map(\.id) == [2, 1], "Settings' list still has everything")

        store.setMuted(false)
        api.result = .success([entry(2, "server"), entry(3, "server")])
        await store.refresh()
        #expect(store.pending.map(\.id) == [3])
    }

    @Test func nothingShowsBeforeOnboardingEndsAndTheHeldBackEntriesAreHistory() async {
        let api = FakeChanges()
        api.onboardingComplete = false
        api.result = .success([entry(1, "server")])
        let store = makeStore(api, bundled: [entry(2)])
        await store.refresh()
        #expect(store.pending.isEmpty)
        #expect(store.entries.map(\.id) == [2, 1], "Settings' list still has everything")

        // Onboarding ends and a relaunch brings only what is new after.
        api.onboardingComplete = true
        let relaunched = makeStore(api, bundled: [entry(2)])
        await relaunched.refresh()
        #expect(relaunched.pending.isEmpty)
        api.result = .success([entry(1, "server"), entry(3, "server")])
        await relaunched.refresh()
        #expect(relaunched.pending.map(\.id) == [3])
    }

    @Test func aBoxWithoutTheFlagHasNoOnboardingToWaitFor() throws {
        let json = #"{"changes": []}"#.data(using: .utf8)!
        #expect(try JSONDecoder().decode(ChangesResponse.self, from: json).onboardingComplete)
        let held = #"{"changes": [], "onboarding_complete": false}"#.data(using: .utf8)!
        #expect(try JSONDecoder().decode(ChangesResponse.self, from: held).onboardingComplete == false)
    }

    @Test func mutedSurvivesARelaunch() {
        makeStore(FakeChanges(), bundled: []).setMuted(true)
        #expect(makeStore(FakeChanges(), bundled: []).muted)
    }
}
