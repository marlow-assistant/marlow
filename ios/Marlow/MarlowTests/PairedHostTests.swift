import Foundation
import Testing
@testable import Marlow

/// The app and the notification extension both read the server from
/// `PairedHost`. Before it existed the host typed at pairing was used for the
/// pairing call only, so a second instance's phone paired and then talked to
/// the first instance (docs/tasks/marlow-for-taylor.md piece 5).
///
/// Each test gets its own defaults suite: the real one is the App Group's,
/// shared with the app installed on the same simulator.
struct PairedHostTests {

    static func freshDefaults() -> UserDefaults {
        let name = "paired-host-tests-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: name)!
        defaults.removePersistentDomain(forName: name)
        return defaults
    }

    /// Mike's phone paired before the host was stored. It must keep reaching
    /// his server without a re-pair.
    @Test func aPhonePairedBeforeThisKeepsTheOriginalServer() {
        #expect(PairedHost.current(in: Self.freshDefaults()) == "https://marlow.tail1234.ts.net:8443")
    }

    @Test func theSavedHostIsTheOneUsed() {
        let defaults = Self.freshDefaults()
        PairedHost.save("https://marlow.tail1234.ts.net:8444", in: defaults)
        #expect(PairedHost.current(in: defaults) == "https://marlow.tail1234.ts.net:8444")
    }

    /// The extension builds `host + "/v1/turn/…"` by interpolation.
    @Test func aTrailingSlashAndWhitespaceAreTrimmed() {
        let defaults = Self.freshDefaults()
        PairedHost.save("  https://box.example:8444/ \n", in: defaults)
        #expect(PairedHost.current(in: defaults) == "https://box.example:8444")
    }

    @Test func somethingThatIsNotAServerURLIsNeitherSavedNorUsed() {
        let defaults = Self.freshDefaults()
        PairedHost.save("https://box.example:8444", in: defaults)
        PairedHost.save("not a url", in: defaults)
        #expect(PairedHost.current(in: defaults) == "https://box.example:8444")

        // Written behind the API's back, e.g. by an older build.
        defaults.set("box.example:8444", forKey: PairedHost.storageKey)
        #expect(PairedHost.current(in: defaults) == PairedHost.fallback)
    }
}

/// In `APIClientTests`'s serialized suite because it uses the process-global
/// `StubURLProtocol`.
extension APIClientTests {
    /// The bug that kept her pushes from ever registering: `APNsRegistrar`
    /// makes its client at launch, before the first pairing, so the client
    /// has to read the host when it sends, not when it was made.
    @Test func aClientMadeBeforePairingUsesTheHostSavedAfterIt() async throws {
        let defaults = PairedHostTests.freshDefaults()
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [StubURLProtocol.self]
        let client = APIClient(hostDefaults: defaults, session: URLSession(configuration: config))

        PairedHost.save("https://marlow.tail1234.ts.net:8444", in: defaults)
        StubURLProtocol.respond(json: """
        {"turn_id": "t1", "status": "done", "source": "chat", "result_text": "hi",
         "session_id": null, "had_permission_denial": false, "error": null, "created_at": 0}
        """)
        _ = try await client.getTurn("t1", deviceToken: "tok")

        let url = try #require(StubURLProtocol.lastRequest?.url)
        #expect(url.host == "marlow.tail1234.ts.net")
        #expect(url.port == 8444)
        #expect(url.path == "/v1/turn/t1")
    }
}
