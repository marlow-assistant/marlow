import Foundation
import Testing
@testable import Marlow

@Suite("Pairing link")
struct PairingLinkTests {
    static let invite = "k3J9xQ2mZpL0vR7tYb5nWc8dHs1fGaU4eXoI6jMqKzE"

    @Test func splitsTheLinkIntoHostAndInvite() {
        let link = PairingLink.parse("https://box.example.ts.net:8443/#pair=\(Self.invite)")
        #expect(link == PairingLink(host: "https://box.example.ts.net:8443", invite: Self.invite))
    }

    @Test func toleratesWhitespaceAndOtherFragmentKeys() {
        let link = PairingLink.parse("  https://box.example.ts.net/#x=1&pair=\(Self.invite)\n")
        #expect(link == PairingLink(host: "https://box.example.ts.net", invite: Self.invite))
    }

    @Test func rejectsWhatIsNotAPairingLink() {
        #expect(PairingLink.parse("") == nil)
        #expect(PairingLink.parse("123456") == nil)
        #expect(PairingLink.parse("https://box.example.ts.net:8443/") == nil)
        #expect(PairingLink.parse("https://box.example.ts.net:8443/#pair=short") == nil)
        #expect(PairingLink.parse("https://box.example.ts.net:8443/#pair=<script>aaaaaaaaaaaaaaaaaaaaaa") == nil)
        #expect(PairingLink.parse(Self.invite) == nil)
    }

    @Test func theInviteIsNeverTakenFromAQueryString() {
        #expect(PairingLink.parse("https://box.example.ts.net:8443/?pair=\(Self.invite)") == nil)
    }

    @Test func plainHttpOnlyToTheMachineItself() {
        #expect(PairingLink.parse("http://box.example.ts.net:8443/#pair=\(Self.invite)") == nil)
        #expect(PairingLink.parse("http://localhost:8080/#pair=\(Self.invite)")?.host == "http://localhost:8080")
    }
}

/// `ChatStoreTests` drives the state machine through a fake, which leaves the
/// wire format itself untested. This file is the counterpart: it pins the
/// encoding, the snake_case decoding, the auth header, and — load-bearing —
/// that a 404 arrives as `APIError.server(status: 404, ...)`, which is the exact
/// shape `ChatStore.pollLoop` pattern-matches to distinguish a permanently gone
/// turn from a network blip. Change that mapping and the permanent-error branch
/// silently becomes dead code, so it gets a test rather than an assumption.
@Suite(.serialized) // StubURLProtocol.handler is process-global
struct APIClientTests {

    private func makeClient() -> APIClient {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [StubURLProtocol.self]
        return APIClient(
            baseURL: URL(string: "https://marlow.example.ts.net:8443")!,
            session: URLSession(configuration: config)
        )
    }

    // MARK: - Decoding

    @Test func getTurnDecodesTheServersSnakeCasePayload() async throws {
        StubURLProtocol.respond(json: """
        {"turn_id": "t1", "status": "done", "source": "device", "result_text": "hello",
         "session_id": "s1", "had_permission_denial": true, "error": null, "created_at": 1234.5}
        """)

        let turn = try await makeClient().getTurn("t1", deviceToken: "tok")

        #expect(turn.turnId == "t1")
        #expect(turn.status == "done")
        #expect(turn.resultText == "hello")
        #expect(turn.hadPermissionDenial)
        #expect(turn.error == nil)
        #expect(turn.createdAt == 1234.5)
    }

    @Test func getTurnDecodesCumulativeActivities() async throws {
        StubURLProtocol.respond(json: """
        {"turn_id": "t1", "status": "running", "source": "chat", "result_text": null,
         "session_id": null, "had_permission_denial": false, "error": null, "created_at": 0,
         "activities": [
           {"kind": "tool_start", "tool": "Read", "at": 1755900001.1},
           {"kind": "tool_end", "tool": null, "at": 1755900001.3},
           {"kind": "text", "tool": null, "at": 1755900002.5}
         ]}
        """)

        let activities = try await makeClient().getTurn("t1", deviceToken: "tok").activities
        #expect(activities.count == 3)
        #expect(activities[0].kind == "tool_start")
        #expect(activities[0].tool == "Read")
        #expect(activities[0].at == 1755900001.1)
        #expect(activities[2].kind == "text")
    }

    @Test func getTurnDecodesMergedSurvivor() async throws {
        StubURLProtocol.respond(json: """
        {"turn_id": "absorbed", "status": "merged", "source": "chat", "result_text": null,
         "session_id": "s1", "had_permission_denial": false, "error": null, "created_at": 0,
         "merged_into": "survivor"}
        """)

        let turn = try await makeClient().getTurn("absorbed", deviceToken: "tok")
        #expect(turn.status == "merged")
        #expect(turn.mergedInto == "survivor")
    }

    @Test(arguments: ["[]", nil] as [String?])
    func emptyAndAbsentActivitiesDecodeIdentically(activitiesJSON: String?) async throws {
        let activitiesMember = activitiesJSON.map { ", \"activities\": \($0)" } ?? ""
        StubURLProtocol.respond(json: """
        {"turn_id": "t1", "status": "running", "source": "chat", "result_text": null,
         "session_id": null, "had_permission_denial": false, "error": null, "created_at": 0\(activitiesMember)}
        """)

        let turn = try await makeClient().getTurn("t1", deviceToken: "tok")
        #expect(turn.activities.isEmpty)
    }

    @Test func unknownActivityKindStillDecodes() async throws {
        StubURLProtocol.respond(json: """
        {"turn_id": "t1", "status": "running", "source": "chat", "result_text": null,
         "session_id": null, "had_permission_denial": false, "error": null, "created_at": 0,
         "activities": [{"kind": "future_cli_event", "tool": "untrusted", "at": 1}]}
        """)

        let activity = try #require(try await makeClient().getTurn("t1", deviceToken: "tok").activities.first)
        #expect(activity.kind == "future_cli_event")
        #expect(activity.label == "Thinking…")
    }

    @Test(arguments: [
        ("Read", "Reading a file…"),
        ("Write", "Writing a file…"),
        ("Edit", "Writing a file…"),
        ("Bash", "Running a command…"),
        ("Grep", "Searching…"),
        ("Glob", "Searching…"),
        ("WebSearch", "Searching the web…"),
        ("WebFetch", "Searching the web…"),
        ("mcp__brave__web_search", "Searching the web…"),
        ("mcp__exa__web_fetch", "Searching the web…"),
        ("mcp__webflow__list_sites", "Using a tool…"),
        ("mcp__google_calendar__list_events", "Checking your calendar…"),
        ("mcp__GMAIL__search_messages", "Checking your mail…"),
        ("mcp__weather__forecast", "Checking the weather…"),
        ("mcp__weather__route_forecast", "Planning the route…"),
        ("UnknownTool", "Using a tool…"),
    ])
    func toolActivityUsesFriendlyLabels(tool: String, expected: String) {
        #expect(TurnActivity(kind: "tool_start", tool: tool).label == expected)
    }

    @Test func untrustedToolNamesNeverBecomeVisibleCopy() {
        let tool = "<script>alert('x')</script>" + String(repeating: "x", count: 10_000)
        let label = TurnActivity(kind: "tool_start", tool: tool).label

        #expect(label == "Using a tool…")
        #expect(!label.contains(tool))
    }

    @Test func aTextEventSaysWritingNotThinking() {
        /// The reply is visible in the bubble above the status line while this
        /// label shows, so "Thinking…" over text that is typing itself
        /// contradicts what the user can see.
        #expect(TurnActivity(kind: "text", tool: nil).label == "Writing…")
    }

    @Test func everyOtherKindStillSaysThinking() {
        for kind in ["thinking", "tool_end", "future_cli_event"] {
            #expect(TurnActivity(kind: kind, tool: nil).label == "Thinking…", "kind=\(kind)")
        }
    }

    @Test func toolStartWithoutANameUsesTheGenericToolLabel() {
        #expect(TurnActivity(kind: "tool_start", tool: nil).label == "Using a tool…")
    }

    @Test func healthDecodesTheBannerFields() async throws {
        StubURLProtocol.respond(json: """
        {"status": "reauth_needed", "auth_ok": false, "reauth_prewarn": true,
         "queue_depth": 3, "queue_paused": true, "oldest_pending_permission_age": 12.5}
        """)

        let health = try await makeClient().health(deviceToken: "tok")

        #expect(health.status == "reauth_needed")
        #expect(health.authOk == false)
        #expect(health.queueDepth == 3)
        #expect(health.queuePaused)
        #expect(health.oldestPendingPermissionAge == 12.5)
    }

    @Test func aMalformedBodyBecomesADecodingError() async throws {
        StubURLProtocol.respond(json: #"{"unexpected": true}"#)

        await #expect(throws: APIError.self) {
            _ = try await self.makeClient().getTurn("t1", deviceToken: "tok")
        }
    }

    // MARK: - Requests

    @Test func authenticatedCallsCarryTheBearerToken() async throws {
        StubURLProtocol.respond(json: """
        {"turn_id": "t1", "status": "done", "source": "device", "result_text": null,
         "session_id": null, "had_permission_denial": false, "error": null, "created_at": 0}
        """)

        _ = try await makeClient().getTurn("t1", deviceToken: "secret-token")

        let request = try #require(StubURLProtocol.lastRequest)
        #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer secret-token")
        #expect(request.url?.path == "/v1/turn/t1")
        #expect(request.httpMethod == "GET")
    }

    // MARK: - 401: the token is gone

    @Test func aRevokedTokenIsReportedWithTheTokenItCarried() async throws {
        var reported: [String] = []
        APIClient.onUnauthorized = { reported.append($0) }
        defer { APIClient.onUnauthorized = nil }
        StubURLProtocol.respond(json: #"{"detail": "invalid device token"}"#, status: 401)

        await #expect(throws: APIError.self) {
            _ = try await self.makeClient().getTurn("t1", deviceToken: "revoked-token")
        }

        #expect(reported == ["revoked-token"])
    }

    @Test func aRefusedPairingIsNotAnUnauthorizedToken() async throws {
        var reported: [String] = []
        APIClient.onUnauthorized = { reported.append($0) }
        defer { APIClient.onUnauthorized = nil }
        StubURLProtocol.respond(json: #"{"detail": "pairing failed"}"#, status: 401)

        await #expect(throws: APIError.self) {
            _ = try await self.makeClient().pair(deviceName: "x", invite: "y")
        }

        #expect(reported.isEmpty)
    }

    @Test func unpairHandlesItsOwn401() async throws {
        var reported: [String] = []
        APIClient.onUnauthorized = { reported.append($0) }
        defer { APIClient.onUnauthorized = nil }
        StubURLProtocol.respond(json: #"{"detail": "invalid device token"}"#, status: 401)

        await #expect(throws: APIError.self) {
            try await self.makeClient().unpair(deviceToken: "already-gone")
        }

        #expect(reported.isEmpty)
    }

    @Test func otherFailuresAreNotUnauthorized() async throws {
        var reported: [String] = []
        APIClient.onUnauthorized = { reported.append($0) }
        defer { APIClient.onUnauthorized = nil }
        StubURLProtocol.respond(json: #"{"detail": "boom"}"#, status: 500)

        await #expect(throws: APIError.self) {
            _ = try await self.makeClient().getTurn("t1", deviceToken: "tok")
        }

        #expect(reported.isEmpty)
    }

    @Test func healthCarriesTheBearerToken() async throws {
        StubURLProtocol.respond(json: """
        {"status": "ok", "auth_ok": true, "reauth_prewarn": false, "queue_depth": 0,
         "queue_paused": false, "oldest_pending_permission_age": null}
        """)

        _ = try await makeClient().health(deviceToken: "secret-token")

        let request = try #require(StubURLProtocol.lastRequest)
        #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer secret-token")
    }

    @Test func sendMessagePostsTheClientMsgIdForIdempotency() async throws {
        StubURLProtocol.respond(json: #"{"turn_id": "t7"}"#, status: 202)

        let response = try await makeClient().sendMessage(
            text: "check my email", clientMsgId: "cmid-1", deviceToken: "tok"
        )

        #expect(response.turnId == "t7")
        let body = try #require(StubURLProtocol.lastBodyJSON)
        #expect(body["text"] as? String == "check my email")
        #expect(body["client_msg_id"] as? String == "cmid-1")
    }

    @Test func sendMessageOmitsImageFieldsWhenThereIsNoImage() async throws {
        StubURLProtocol.respond(json: #"{"turn_id": "t9"}"#, status: 202)

        _ = try await makeClient().sendMessage(text: "hi", deviceToken: "tok")

        let body = try #require(StubURLProtocol.lastBodyJSON)
        #expect(body["images"] == nil)
        #expect(body["image_b64"] == nil)
        #expect(body["image_mime"] == nil)
    }

    @Test func sendMessagePostsTheImageAsBase64WithItsMimeType() async throws {
        StubURLProtocol.respond(json: #"{"turn_id": "t10"}"#, status: 202)
        let imageData = Data([0xFF, 0xD8, 0xFF, 0xD9])

        _ = try await makeClient().sendMessage(
            text: "what's this?", imagesBase64: [imageData.base64EncodedString()], deviceToken: "tok"
        )

        let body = try #require(StubURLProtocol.lastBodyJSON)
        let images = try #require(body["images"] as? [[String: String]])
        #expect(images == [["b64": imageData.base64EncodedString(), "mime": "image/jpeg"]])
        #expect(body["image_b64"] == nil, "the single-image pair is for older clients; this one sends the list")
    }

    @Test func sendMessagePostsSeveralImagesInOrder() async throws {
        StubURLProtocol.respond(json: #"{"turn_id": "t11"}"#, status: 202)
        let blobs = [Data([1]), Data([2]), Data([3])].map { $0.base64EncodedString() }

        _ = try await makeClient().sendMessage(text: "three", imagesBase64: blobs, deviceToken: "tok")

        let body = try #require(StubURLProtocol.lastBodyJSON)
        let images = try #require(body["images"] as? [[String: String]])
        #expect(images.map { $0["b64"] } == blobs)
        #expect(images.allSatisfy { $0["mime"] == "image/jpeg" })
    }

    @Test func sendMessageAlwaysCarriesTheCurrentTimezone() async throws {
        StubURLProtocol.respond(json: #"{"turn_id": "t8"}"#, status: 202)

        _ = try await makeClient().sendMessage(text: "hi", deviceToken: "tok")

        let body = try #require(StubURLProtocol.lastBodyJSON)
        #expect(
            body["timezone"] as? String == TimeZone.current.identifier,
            "sent fresh on every message, not just at pairing — a phone that changed zones must report the new one"
        )
    }

    @Test func sendMessageCarriesTheLocationWhenGiven() async throws {
        StubURLProtocol.respond(json: #"{"turn_id": "t12"}"#, status: 202)

        _ = try await makeClient().sendMessage(
            text: "hi",
            location: OutgoingLocation(latitude: 46.81, longitude: -71.21, accuracyM: 65, place: "Québec, QC, Canada"),
            deviceToken: "tok"
        )

        let body = try #require(StubURLProtocol.lastBodyJSON)
        let location = try #require(body["location"] as? [String: Any])
        #expect(location["latitude"] as? Double == 46.81)
        #expect(location["longitude"] as? Double == -71.21)
        #expect(location["accuracy_m"] as? Double == 65)
        #expect(location["place"] as? String == "Québec, QC, Canada")
    }

    @Test func sendMessageLeavesOutAccuracyAndPlaceWhenUnknown() async throws {
        StubURLProtocol.respond(json: #"{"turn_id": "t13"}"#, status: 202)

        _ = try await makeClient().sendMessage(
            text: "hi",
            location: OutgoingLocation(latitude: 1, longitude: 2, accuracyM: nil, place: nil),
            deviceToken: "tok"
        )

        let body = try #require(StubURLProtocol.lastBodyJSON)
        let location = try #require(body["location"] as? [String: Any])
        #expect(Set(location.keys) == ["latitude", "longitude"])
    }

    /// Absence is the signal: the server clears what it holds for this device
    /// when the key is missing, so Off must not send null or an empty object.
    @Test func sendMessageHasNoLocationKeyWhenThereIsNone() async throws {
        StubURLProtocol.respond(json: #"{"turn_id": "t14"}"#, status: 202)

        _ = try await makeClient().sendMessage(text: "hi", deviceToken: "tok")

        let body = try #require(StubURLProtocol.lastBodyJSON)
        #expect(!body.keys.contains("location"))
    }

    @Test func sendMessageAppliesAnExplicitRequestTimeout() async throws {
        StubURLProtocol.respond(json: #"{"turn_id": "t11"}"#, status: 202)

        _ = try await makeClient().sendMessage(text: "hi", timeout: 12.5, deviceToken: "tok")

        let request = try #require(StubURLProtocol.lastRequest)
        #expect(request.timeoutInterval == 12.5)
    }

    // MARK: - Transcript (docs/tasks/ios-server-transcript.md)

    /// The server rejects both cursors at once with a 422, and neither means
    /// the newest page, so only the one that is set may go on the wire.
    @Test func transcriptSendsOnlyTheCursorThatIsSet() async throws {
        let empty = #"{"rows": [], "latest_seq": 0, "has_more": false}"#

        StubURLProtocol.respond(json: empty)
        _ = try await makeClient().transcript(after: 7, before: nil, limit: 200, deviceToken: "tok")
        var request = try #require(StubURLProtocol.lastRequest)
        var query = Set((URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []).map { "\($0.name)=\($0.value ?? "")" })
        #expect(request.url?.path == "/v1/transcript")
        #expect(query == ["after=7", "limit=200"])

        StubURLProtocol.respond(json: empty)
        _ = try await makeClient().transcript(after: nil, before: 51, limit: 200, deviceToken: "tok")
        request = try #require(StubURLProtocol.lastRequest)
        query = Set((URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []).map { "\($0.name)=\($0.value ?? "")" })
        #expect(query == ["before=51", "limit=200"])

        StubURLProtocol.respond(json: empty)
        _ = try await makeClient().transcript(after: nil, before: nil, limit: 100, deviceToken: "tok")
        request = try #require(StubURLProtocol.lastRequest)
        query = Set((URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []).map { "\($0.name)=\($0.value ?? "")" })
        #expect(query == ["limit=100"])
        #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer tok")
        #expect(request.httpMethod == "GET")
    }

    @Test func transcriptDecodesTheServersPage() async throws {
        let id = UUID().uuidString
        StubURLProtocol.respond(json: """
        {"rows": [{"seq": 12, "id": "\(id)", "role": "assistant", "source": "cron", "turn_id": "c1",
                   "client_msg_id": null, "text": "Morning.", "status": "done", "created_at": 1234.5,
                   "answers": [], "attachments": []}],
         "latest_seq": 12, "has_more": true}
        """)

        let page = try await makeClient().transcript(after: 11, before: nil, limit: 200, deviceToken: "tok")

        let row = try #require(page.rows.first)
        #expect(row.seq == 12)
        #expect(row.id == id)
        #expect(row.role == .assistant)
        #expect(row.source == "cron")
        #expect(row.turnId == "c1")
        #expect(row.clientMsgId == nil)
        #expect(row.status == .done)
        #expect(row.createdAt == 1234.5)
        #expect(page.latestSeq == 12)
        #expect(page.hasMore)
    }

    @Test func transcriptAttachmentReturnsTheRawBodyAuthenticated() async throws {
        let id = UUID().uuidString
        StubURLProtocol.respond(json: "not json at all, just bytes")

        let data = try await makeClient().transcriptAttachment(id: id, deviceToken: "secret")

        #expect(String(data: data, encoding: .utf8) == "not json at all, just bytes", "returned as-is, never decoded")
        let request = try #require(StubURLProtocol.lastRequest)
        #expect(request.url?.path == "/v1/transcript/attachment/\(id)")
        #expect(request.httpMethod == "GET")
        #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer secret")
    }

    @Test func aMissingAttachmentIsA404() async throws {
        StubURLProtocol.respond(json: #"{"detail": "unknown attachment"}"#, status: 404)

        do {
            _ = try await makeClient().transcriptAttachment(id: UUID().uuidString, deviceToken: "tok")
            Issue.record("expected a 404 to throw")
        } catch APIError.server(let status, let detail) {
            #expect(status == 404)
            #expect(detail == "unknown attachment")
        }
    }

    // MARK: - Errors

    @Test func a404BecomesServerError404() async throws {
        StubURLProtocol.respond(json: #"{"detail": "unknown turn"}"#, status: 404)

        do {
            _ = try await makeClient().getTurn("gone", deviceToken: "tok")
            Issue.record("expected a 404 to throw")
        } catch APIError.server(let status, let detail) {
            #expect(status == 404, "ChatStore.pollLoop matches on exactly this")
            #expect(detail == "unknown turn")
        }
    }

    @Test func otherFailuresKeepTheirStatusCode() async throws {
        StubURLProtocol.respond(json: #"{"detail": "queue paused"}"#, status: 503)

        do {
            _ = try await makeClient().getTurn("t1", deviceToken: "tok")
            Issue.record("expected a 503 to throw")
        } catch APIError.server(let status, _) {
            #expect(status == 503, "must not be conflated with the permanent 404 case")
        }
    }

    @Test func aTransportFailurePropagates() async throws {
        StubURLProtocol.fail(with: URLError(.cannotConnectToHost))

        await #expect(throws: (any Error).self) {
            _ = try await self.makeClient().health(deviceToken: "tok")
        }
    }

    // MARK: - Ideas (docs/tasks/ideas-panel.md)

    @Test func sendMessageCarriesTheIdeaIdWhenGiven() async throws {
        StubURLProtocol.respond(json: #"{"turn_id": "t20"}"#, status: 202)

        _ = try await makeClient().sendMessage(text: "Do it: Draft the invites", ideaId: "dinner-invites", deviceToken: "tok")

        let body = try #require(StubURLProtocol.lastBodyJSON)
        #expect(body["idea_id"] as? String == "dinner-invites")
    }

    // MARK: - Voice mode (docs/tasks/voice-mode-hands-free.md)

    @Test func sendMessageMarksASpokenMessage() async throws {
        StubURLProtocol.respond(json: #"{"turn_id": "t30"}"#, status: 202)

        _ = try await makeClient().sendMessage(text: "what's on today", spoken: true, deviceToken: "tok")

        let body = try #require(StubURLProtocol.lastBodyJSON)
        #expect(body["spoken"] as? Bool == true)
    }

    @Test func sendMessageHasNoSpokenKeyOnATypedMessage() async throws {
        StubURLProtocol.respond(json: #"{"turn_id": "t31"}"#, status: 202)

        _ = try await makeClient().sendMessage(text: "hi", deviceToken: "tok")

        let body = try #require(StubURLProtocol.lastBodyJSON)
        #expect(body["spoken"] == nil, "absent rather than false: an older server never sees the key")
    }

    @Test func sendMessageHasNoIdeaIdKeyOnANormalSend() async throws {
        StubURLProtocol.respond(json: #"{"turn_id": "t21"}"#, status: 202)

        _ = try await makeClient().sendMessage(text: "hi", deviceToken: "tok")

        let body = try #require(StubURLProtocol.lastBodyJSON)
        #expect(body["idea_id"] == nil, "absent, never null: the server reads a present key as an acceptance")
    }

    @Test func ideasGetsTheOpenIdeasInOrder() async throws {
        StubURLProtocol.respond(json: """
        {"ideas": [
          {"id": "a", "section": "now", "emoji": "x", "title": "A", "plan": "p", "category": "Van",
           "source": "an email", "until": "2026-10-13", "added": "2026-10-12", "spotlight": true},
          {"id": "b", "section": "standing", "emoji": "y", "title": "B", "plan": "q", "category": "Travel",
           "source": null, "until": null, "added": "2026-10-01", "spotlight": false}
        ]}
        """)

        let ideas = try await makeClient().ideas(deviceToken: "tok")

        #expect(ideas.map(\.id) == ["a", "b"])
        let request = try #require(StubURLProtocol.lastRequest)
        #expect(request.url?.path == "/v1/ideas")
        #expect(request.httpMethod == "GET")
        #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer tok")
    }

    @Test func dismissPostsANullReasonWhenThereIsNone() async throws {
        StubURLProtocol.respond(json: #"{"ok": true}"#)

        try await makeClient().dismissIdea(id: "van-wiring", reason: nil, deviceToken: "tok")

        let request = try #require(StubURLProtocol.lastRequest)
        #expect(request.url?.path == "/v1/ideas/van-wiring/dismiss")
        #expect(request.httpMethod == "POST")
        let body = try #require(StubURLProtocol.lastBodyJSON)
        #expect(body.keys.contains("reason"))
        #expect(body["reason"] is NSNull)
    }

    @Test func dismissPostsTheReasonWhenGiven() async throws {
        StubURLProtocol.respond(json: #"{"ok": true}"#)

        try await makeClient().dismissIdea(id: "van-wiring", reason: "Already did it", deviceToken: "tok")

        let body = try #require(StubURLProtocol.lastBodyJSON)
        #expect(body["reason"] as? String == "Already did it")
    }

    @Test func dismissOfAnIdeaThatIsNoLongerOpenThrowsThe404() async throws {
        StubURLProtocol.respond(json: #"{"detail": "unknown idea"}"#, status: 404)

        do {
            try await makeClient().dismissIdea(id: "gone", reason: nil, deviceToken: "tok")
            Issue.record("expected a 404 to throw")
        } catch APIError.server(let status, _) {
            #expect(status == 404)
        }
    }
}

// MARK: - Stub transport

final class StubURLProtocol: URLProtocol {
    private nonisolated(unsafe) static var handler: ((URLRequest) throws -> (HTTPURLResponse, Data))?
    nonisolated(unsafe) static var lastRequest: URLRequest?
    nonisolated(unsafe) static var lastBody: Data?

    /// `URLProtocol` strips `httpBody` in favour of `httpBodyStream`, so POST
    /// bodies have to be drained from the stream to be inspected.
    static var lastBodyJSON: [String: Any]? {
        guard let lastBody else { return nil }
        return try? JSONSerialization.jsonObject(with: lastBody) as? [String: Any]
    }

    /// Installs a handler and **clears the recorded request**. Every entry
    /// point goes through here: `lastRequest`/`lastBody` are process-global, so
    /// leaving a previous test's values in place lets a test whose own request
    /// never reached the stub assert against stale data and pass falsely.
    private static func install(_ newHandler: @escaping (URLRequest) throws -> (HTTPURLResponse, Data)) {
        lastRequest = nil
        lastBody = nil
        handler = newHandler
    }

    static func respond(json: String, status: Int = 200) {
        install { request in
            let response = HTTPURLResponse(
                url: request.url!, statusCode: status, httpVersion: "HTTP/1.1", headerFields: nil
            )!
            return (response, Data(json.utf8))
        }
    }

    static func fail(with error: Error) {
        install { _ in throw error }
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        Self.lastRequest = request
        Self.lastBody = request.httpBodyStream.map(Self.drain) ?? request.httpBody

        guard let handler = Self.handler else {
            client?.urlProtocol(self, didFailWithError: URLError(.unsupportedURL))
            return
        }
        do {
            let (response, data) = try handler(request)
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}

    private static func drain(_ stream: InputStream) -> Data {
        stream.open()
        defer { stream.close() }
        var data = Data()
        let size = 4096
        var buffer = [UInt8](repeating: 0, count: size)
        while stream.hasBytesAvailable {
            let read = stream.read(&buffer, maxLength: size)
            if read <= 0 { break }
            data.append(buffer, count: read)
        }
        return data
    }
}
