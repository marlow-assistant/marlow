import Foundation
import Testing
@testable import Marlow

@MainActor
struct PreferenceSyncTests {
    private func makeDefaults() -> UserDefaults {
        UserDefaults(suiteName: "PreferenceSyncTests-\(UUID().uuidString)")!
    }

    @Test func choicesMadeElsewhereAreTakenUpOnce() async {
        let defaults = makeDefaults()
        let api = FakePreferencesAPI(face: 7, effort: "high")
        #expect(await PreferenceSync.pull(api: api, deviceToken: "t", defaults: defaults) == [.face, .effort])
        #expect(FacePreference.current(in: defaults).id == 7)
        #expect(EffortPreference.current(in: defaults) == .high)
        #expect(await PreferenceSync.pull(api: api, deviceToken: "t", defaults: defaults).isEmpty)
    }

    @Test func aPickIsSentAndThenNoLongerPending() async {
        let defaults = makeDefaults()
        let api = FakePreferencesAPI(face: 2, effort: "low")
        FacePreference.set(.with(id: 9), in: defaults)
        PreferenceSync.picked(.face, in: defaults)
        await PreferenceSync.send(api: api, deviceToken: "t", defaults: defaults)
        #expect(api.sent == [PreferencesUpdate(face: 9, effort: nil)])
        #expect(defaults.bool(forKey: PreferenceSync.Item.face.pendingKey) == false)
        #expect(api.effort == "low")
    }

    // The bug this guards: a pick made offline, then the box's older value
    // put back over it on the next foreground.
    @Test func anUnsentPickIsSentOnPullNotOverwritten() async {
        let defaults = makeDefaults()
        let api = FakePreferencesAPI(face: 2, effort: "low")
        api.reachable = false
        EffortPreference.set(.high, in: defaults)
        PreferenceSync.picked(.effort, in: defaults)
        await PreferenceSync.send(api: api, deviceToken: "t", defaults: defaults)
        #expect(defaults.bool(forKey: PreferenceSync.Item.effort.pendingKey))

        api.reachable = true
        #expect(await PreferenceSync.pull(api: api, deviceToken: "t", defaults: defaults) == [.face])
        #expect(EffortPreference.current(in: defaults) == .high)
        #expect(api.effort == "high")
        #expect(defaults.bool(forKey: PreferenceSync.Item.effort.pendingKey) == false)
    }

    // Devices that chose before sync existed: the first to reach an empty box
    // sets it, and the rest take that up.
    @Test func anEmptyBoxTakesThisDevicesChoice() async {
        let defaults = makeDefaults()
        let api = FakePreferencesAPI(face: nil, effort: nil)
        FacePreference.set(.with(id: 4), in: defaults)
        #expect(await PreferenceSync.pull(api: api, deviceToken: "t", defaults: defaults).isEmpty)
        #expect(api.face == 4)
        #expect(api.effort == nil)
        #expect(FacePreference.current(in: defaults).id == 4)
    }

    @Test func anEmptyBoxAndNoChoiceSendNothing() async {
        let defaults = makeDefaults()
        let api = FakePreferencesAPI(face: nil, effort: nil)
        #expect(await PreferenceSync.pull(api: api, deviceToken: "t", defaults: defaults).isEmpty)
        #expect(api.sent.isEmpty)
    }

    @Test func valuesThisBuildDoesNotKnowStayTheChoice() async {
        let defaults = makeDefaults()
        let api = FakePreferencesAPI(face: 42, effort: "max")
        #expect(await PreferenceSync.pull(api: api, deviceToken: "t", defaults: defaults) == [.face, .effort])
        #expect(defaults.integer(forKey: FacePreference.storageKey) == 42)
        #expect(FacePreference.current(in: defaults) == .default)
        #expect(EffortPreference.current(in: defaults) == .default)
    }
}

@MainActor
private final class FakePreferencesAPI: PreferencesAPI {
    var face: Int?
    var effort: String?
    var reachable = true
    private(set) var sent: [PreferencesUpdate] = []

    init(face: Int?, effort: String?) {
        self.face = face
        self.effort = effort
    }

    func preferences(deviceToken: String) async throws -> Preferences {
        guard reachable else { throw URLError(.notConnectedToInternet) }
        return Preferences(face: face, effort: effort)
    }

    func setPreferences(_ update: PreferencesUpdate, deviceToken: String) async throws {
        guard reachable else { throw URLError(.notConnectedToInternet) }
        sent.append(update)
        if let f = update.face { face = f }
        if let e = update.effort { effort = e }
    }
}
