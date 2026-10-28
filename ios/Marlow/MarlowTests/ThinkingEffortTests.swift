import Foundation
import Testing
@testable import Marlow

/// docs/tasks/model-and-effort-config.md piece 2 — the phone owns the level and
/// sends it with every message.
struct ThinkingEffortTests {

    /// A private suite, not `.standard`: these write the same key the running
    /// app uses, and clobbering a real preference from a test run is the kind
    /// of thing only noticed much later.
    private func makeDefaults() -> (UserDefaults, String) {
        let name = "ThinkingEffortTests-\(UUID().uuidString)"
        return (UserDefaults(suiteName: name)!, name)
    }

    /// The wire values ARE the server's allowlist. If this changes, the
    /// server's allowed set has to change with it — an unrecognised level is
    /// silently replaced by the server's default, so drift shows up as "the
    /// picker does nothing" rather than as an error anyone can see.
    @Test func wireValuesMatchTheServerAllowlist() {
        #expect(ThinkingEffort.allCases.map(\.wireValue) == ["low", "medium", "high"])
    }

    /// `xhigh` and `max` exist in the CLI and are deliberately not offered —
    /// they only add latency on this workload.
    @Test func onlyThreeLevelsAreExposed() {
        #expect(ThinkingEffort.allCases.count == 3)
        #expect(ThinkingEffort(rawValue: "xhigh") == nil)
        #expect(ThinkingEffort(rawValue: "max") == nil)
    }

    /// Must match the server-side pin from piece 1, so an untouched app and an
    /// unconfigured server agree rather than disagreeing invisibly.
    @Test func defaultIsMedium() {
        let (defaults, name) = makeDefaults()
        defer { defaults.removePersistentDomain(forName: name) }
        #expect(ThinkingEffort.default == .medium)
        #expect(EffortPreference.current(in: defaults) == .medium)
    }

    @Test func roundTripsThroughDefaults() {
        let (defaults, name) = makeDefaults()
        defer { defaults.removePersistentDomain(forName: name) }
        EffortPreference.set(.high, in: defaults)
        #expect(EffortPreference.current(in: defaults) == .high)
        EffortPreference.set(.medium, in: defaults)
        #expect(EffortPreference.current(in: defaults) == .medium)
    }

    /// A value written by a future build that knows a level this one doesn't.
    /// Falling back beats persisting a level the picker can't display.
    @Test func unknownStoredValueFallsBackToDefault() {
        let (defaults, name) = makeDefaults()
        defer { defaults.removePersistentDomain(forName: name) }
        defaults.set("xhigh", forKey: EffortPreference.storageKey)
        #expect(EffortPreference.current(in: defaults) == .default)
        defaults.set("", forKey: EffortPreference.storageKey)
        #expect(EffortPreference.current(in: defaults) == .default)
    }

    // MARK: - Model name rendering (Settings)

    @Test func modelNameRendersTheTier() {
        #expect("claude-sonnet-5".capitalizedModelName == "Sonnet")
        #expect("claude-opus-5".capitalizedModelName == "Opus")
        #expect("sonnet".capitalizedModelName == "Sonnet")
    }

    /// An unfamiliar name shows as itself rather than being mangled into
    /// something that reads authoritative and is wrong.
    @Test func unfamiliarModelNamePassesThrough() {
        #expect("some-future-thing".capitalizedModelName == "some-future-thing")
    }

    // MARK: - Server build identity (Settings "Build" row)

    /// The whole point of the row is telling you what the box is ACTUALLY
    /// running once a branch deploy is possible, so a partial payload must not
    /// render as a half-truth like "feature/x@".
    @Test func serverBuildNeedsBothHalvesToRender() throws {
        let complete = try decodeHealth(#"{"status":"ok","auth_ok":true,"reauth_prewarn":false,"queue_depth":0,"queue_paused":false,"oldest_pending_permission_age":null,"git_branch":"feature/effort-toggle-server","git_commit":"abc1234"}"#)
        #expect(complete.gitBranch == "feature/effort-toggle-server")
        #expect(complete.gitCommit == "abc1234")

        // A server predating these fields must still decode -- this client
        // ships before the server half is deployed.
        let older = try decodeHealth(#"{"status":"ok","auth_ok":true,"reauth_prewarn":false,"queue_depth":0,"queue_paused":false,"oldest_pending_permission_age":null}"#)
        #expect(older.gitBranch == nil)
        #expect(older.gitCommit == nil)
        #expect(older.model == nil)
    }

    private func decodeHealth(_ json: String) throws -> HealthResponse {
        try JSONDecoder().decode(HealthResponse.self, from: Data(json.utf8))
    }

    // MARK: - What actually reaches the wire

    @MainActor
    @Test func everyMessageCarriesTheSelectedEffort() async throws {
        let (defaults, name) = makeDefaults()
        defer { defaults.removePersistentDomain(forName: name) }
        EffortPreference.set(.high, in: defaults)

        let fake = FakeChatAPI()
        await fake.stubSend(.success(MessageResponse(turnId: "t1")))
        await fake.stubTurns([.success(.fixture(status: "done", resultText: "ok"))])
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(),
            policy: .test(), preferences: defaults
        )
        defer { store.cancelActiveWork() }

        store.send(text: "first")
        await awaitSettled(store)
        store.send(text: "second")
        await awaitSettled(store)

        let sent = try requireCount(await fake.sentMessages, 2)
        // On EVERY message, not only the one after the picker moved.
        #expect(sent.map(\.effort) == [.high, .high])
    }

    @MainActor
    @Test func defaultsToMediumWhenNeverSet() async throws {
        let (defaults, name) = makeDefaults()
        defer { defaults.removePersistentDomain(forName: name) }

        let fake = FakeChatAPI()
        await fake.stubSend(.success(MessageResponse(turnId: "t1")))
        await fake.stubTurns([.success(.fixture(status: "done", resultText: "ok"))])
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(),
            policy: .test(), preferences: defaults
        )
        defer { store.cancelActiveWork() }

        store.send(text: "hello")
        await awaitSettled(store)

        let sent = try requireCount(await fake.sentMessages, 1)
        #expect(sent[0].effort == .medium)
    }

    /// A retry that happens after the picker moves should carry what the picker
    /// is now showing, not a level captured when the message was submitted.
    @MainActor
    @Test func effortIsReadPerAttemptNotPerSubmit() async throws {
        let (defaults, name) = makeDefaults()
        defer { defaults.removePersistentDomain(forName: name) }
        EffortPreference.set(.low, in: defaults)

        let fake = FakeChatAPI()
        await fake.stubSendScript(
            [.failure(URLError(.timedOut))],
            fallback: .success(MessageResponse(turnId: "t1"))
        )
        await fake.stubTurns([.success(.fixture(status: "done", resultText: "ok"))])
        await fake.setOnSend { index in
            if index == 1 { EffortPreference.set(.high, in: defaults) }
        }
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(),
            policy: .test(), preferences: defaults
        )
        defer { store.cancelActiveWork() }

        store.send(text: "retry me")
        await awaitSettled(store)

        let sent = try requireCount(await fake.sentMessages, 2)
        #expect(sent.map(\.effort) == [.low, .high])
    }
}
