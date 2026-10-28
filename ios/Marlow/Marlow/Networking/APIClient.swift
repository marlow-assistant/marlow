import Foundation

enum APIError: Error, LocalizedError {
    case server(status: Int, detail: String?)
    case notPaired
    case decoding(Error)

    var errorDescription: String? {
        switch self {
        case .server(let status, let detail):
            return "Server error \(status)" + (detail.map { ": \($0)" } ?? "")
        case .notPaired:
            return "Not paired with the server yet."
        case .decoding(let error):
            return "Couldn't read the server's response: \(error.localizedDescription)"
        }
    }
}

/// URLSession-based client for the six endpoints the app needs. Always the
/// MagicDNS name, never a 100.x address — the TLS cert is issued for the name.
final class APIClient {
    /// The paired server (`PairedHost`), read per request rather than once at
    /// init: `APNsRegistrar.shared` builds its client at launch, before a
    /// first pairing, and a host fixed then would send the new APNs token to
    /// the fallback server instead of the one just paired.
    static var host: String { PairedHost.current() }

    /// Called with the bearer token a request carried when the box answered it
    /// 401: that token is revoked or unknown (`deploy/devices.sh revoke`, an
    /// unpair from another place, a reset token file). A global rather than a
    /// parameter because clients are built all over the app, and what to do
    /// about it is one app-wide thing (`SessionStore`). Carries the token so a
    /// slow request from an earlier pairing can't sign out a newer one. The web
    /// client does the same (WEB-8).
    nonisolated(unsafe) static var onUnauthorized: (@Sendable (String) -> Void)?

    private let fixedBaseURL: URL?
    private let hostDefaults: UserDefaults
    private let session: URLSession
    private let decoder: JSONDecoder

    private var baseURL: URL { fixedBaseURL ?? PairedHost.url(in: hostDefaults) }

    /// `baseURL` only for the pairing call, which targets the host being
    /// tried before it is saved.
    init(
        baseURL: URL? = nil, hostDefaults: UserDefaults = PairedHost.defaults,
        session: URLSession = .shared
    ) {
        self.fixedBaseURL = baseURL
        self.hostDefaults = hostDefaults
        self.session = session
        self.decoder = JSONDecoder()
    }

    // MARK: - Pairing (no auth)

    /// Redeems a single-use invite (`PairingLink`) for a device token. Every
    /// refusal is the same 400, whatever the cause.
    func pair(deviceName: String, invite: String) async throws -> PairResponse {
        try await post("/v1/pair", body: ["device_name": deviceName, "invite": invite], auth: nil)
    }

    // MARK: - Authenticated calls

    /// Revokes this device's token on the box, and its push registration with
    /// it. Unpair calls this before clearing local state.
    func unpair(deviceToken: String, timeout: TimeInterval? = nil) async throws {
        let _: EmptyResponse = try await post(
            "/v1/device/unpair", body: [:], auth: deviceToken, timeout: timeout
        )
    }

    func registerAPNsToken(_ apnsToken: String, deviceToken: String) async throws {
        let _: EmptyResponse = try await post(
            "/v1/device/apns-token", body: ["apns_token": apnsToken], auth: deviceToken
        )
    }

    func sendMessage(
        text: String, clientMsgId: String? = nil,
        imagesBase64: [String] = [],
        effort: ThinkingEffort = .default,
        location: OutgoingLocation? = nil,
        ideaId: String? = nil,
        spoken: Bool = false,
        timeout: TimeInterval? = nil, deviceToken: String
    ) async throws -> MessageResponse {
        var body: [String: Any] = ["text": text]
        if let clientMsgId { body["client_msg_id"] = clientMsgId }
        // Absent when there are none, never an empty list: server/app.py
        // rejects `images: []` with a 400. Always the `images` form now; the
        // server still accepts the old `image_b64`/`image_mime` pair for older
        // clients, but this client no longer sends it. Every image is JPEG
        // (the downscaler's only output).
        if !imagesBase64.isEmpty {
            body["images"] = imagesBase64.map { ["b64": $0, "mime": "image/jpeg"] }
        }
        // Sent fresh on every message, not registered once at pairing — a zone
        // captured at pairing is wrong after the first flight, and only the
        // phone knows where it currently is. Optional server-side, so this is
        // still safe to send even if `TimeZone.current.identifier` ever
        // returned something the server can't resolve.
        body["timezone"] = TimeZone.current.identifier
        // Sent on EVERY message, not only when it changes. A stateless field is
        // far easier to reason about than a sticky one, and there is no
        // server-side effort state to drift out of sync with. Optional
        // server-side, so this is safe to ship before the server half deploys —
        // an older server ignores the key rather than rejecting the message.
        body["effort"] = effort.wireValue
        // Present only when there is a fresh fix the user agreed to share, and
        // ABSENT otherwise — never null or an empty object. The server reads a
        // missing key as "no location from this device right now" and clears
        // what it held, which is how Off, a denied permission and a stale fix
        // all stop a previously sent position from lingering.
        if let location {
            var wire: [String: Any] = ["latitude": location.latitude, "longitude": location.longitude]
            if let accuracy = location.accuracyM { wire["accuracy_m"] = accuracy }
            if let place = location.place { wire["place"] = place }
            body["location"] = wire
        }
        // Present only on a "Do it" from the Ideas panel, ABSENT on every other
        // send. The server records the acceptance and tells Marlow which idea
        // it was; an id it doesn't recognise is dropped and the message still
        // sends (docs/tasks/ideas-panel.md).
        if let ideaId { body["idea_id"] = ideaId }
        // Present only when said aloud in voice mode, ABSENT otherwise; the
        // server then tells Marlow its reply will be read out
        // (docs/tasks/voice-mode-hands-free.md).
        if spoken { body["spoken"] = true }
        return try await post("/v1/message", body: body, auth: deviceToken, timeout: timeout)
    }

    func getTurn(_ turnId: String, deviceToken: String) async throws -> Turn {
        try await get("/v1/turn/\(turnId)", auth: deviceToken)
    }

    /// The server-owned conversation (docs/tasks/ios-server-transcript.md).
    /// Only the cursor that is set goes on the wire: the server rejects both at
    /// once with a 422, and neither means the newest page, descending.
    func transcript(after: Int?, before: Int?, limit: Int, deviceToken: String) async throws -> TranscriptPage {
        var query = [URLQueryItem(name: "limit", value: String(limit))]
        if let after { query.append(URLQueryItem(name: "after", value: String(after))) }
        if let before { query.append(URLQueryItem(name: "before", value: String(before))) }
        return try await get("/v1/transcript", auth: deviceToken, query: query)
    }

    /// One file a user row carries, as the raw bytes the server stored.
    func transcriptAttachment(id: String, deviceToken: String) async throws -> Data {
        var request = URLRequest(url: baseURL.appendingPathComponent("/v1/transcript/attachment/\(id)"))
        request.httpMethod = "GET"
        applyAuth(deviceToken, to: &request)
        return try await sendData(request)
    }

    func permissions(deviceToken: String) async throws -> PermissionsResponse {
        try await get("/v1/permissions", auth: deviceToken)
    }

    func answerPermission(requestId: String, decision: String, deviceToken: String) async throws -> AnswerPermissionResponse {
        try await post("/v1/permission/\(requestId)", body: ["decision": decision], auth: deviceToken)
    }

    /// The activity log, newest first (docs/tasks/ux-lane.md §2). `before`
    /// is the previous page's `nextBefore`; nil asks for the newest page.
    func activity(limit: Int, before: Double?, deviceToken: String) async throws -> ActivityPage {
        var query = [URLQueryItem(name: "limit", value: String(limit))]
        if let before {
            // Shortest round-trip form, not a fixed number of places: the
            // server pages on `finished_at < before`, and a cursor rounded UP
            // would hand back the boundary row a second time.
            query.append(URLQueryItem(name: "before", value: String(before)))
        }
        return try await get("/v1/activity", auth: deviceToken, query: query)
    }

    /// What a background job said (docs/tasks/default-infrastructure.md).
    /// 404 when the turn kept nothing. The id is the server's own, from the
    /// activity record, as `/v1/turn/{id}` takes it.
    func activityOutput(turnId: String, deviceToken: String) async throws -> ActivityOutput {
        try await get("/v1/activity/\(turnId)/output", auth: deviceToken)
    }

    /// Marlow's files and schedules in one call (docs/tasks/ux-lane.md §3a).
    func identity(deviceToken: String) async throws -> IdentityIndex {
        try await get("/v1/identity", auth: deviceToken)
    }

    /// One file's text. The id goes through `URLComponents`, so the `/` in
    /// "memory/travel.md" is encoded as the query value it is, never a path.
    func identityFile(id: String, deviceToken: String) async throws -> IdentityFile {
        try await get("/v1/identity/file", auth: deviceToken, query: [URLQueryItem(name: "id", value: id)])
    }

    /// Plan windows, conversation size and Marlow's own activity
    /// (docs/tasks/usage-visibility.md). Facts only; the wording is the phone's.
    func usage(deviceToken: String) async throws -> UsageReport {
        try await get("/v1/usage", auth: deviceToken)
    }

    /// Open ideas, in the store's order (docs/tasks/ideas-panel.md).
    func ideas(deviceToken: String) async throws -> [Idea] {
        let response: IdeasResponse = try await get("/v1/ideas", auth: deviceToken)
        return response.ideas
    }

    /// The box's half of What's new: `server` entries only, newest first
    /// (docs/tasks/whats-new.md), and whether first-run onboarding has ended.
    /// The app's half is bundled.
    func changes(deviceToken: String) async throws -> ChangesResponse {
        try await get("/v1/changes", auth: deviceToken)
    }

    /// Repeatable: a later call with a reason replaces the one before, which is
    /// how "Not now" stays one tap and the reason comes after. The reason key
    /// is always sent, null when there is none. A 404 means the idea is no
    /// longer open.
    func dismissIdea(id: String, reason: String?, deviceToken: String) async throws {
        let _: EmptyResponse = try await post(
            "/v1/ideas/\(id)/dismiss", body: ["reason": reason.map { $0 as Any } ?? NSNull()], auth: deviceToken
        )
    }

    /// Choices every client shares (server/preferences.py). Each is nil until
    /// picked anywhere.
    func preferences(deviceToken: String) async throws -> Preferences {
        try await get("/v1/preferences", auth: deviceToken)
    }

    func setPreferences(_ update: PreferencesUpdate, deviceToken: String) async throws {
        var body: [String: Any] = [:]
        if let face = update.face { body["face"] = face }
        if let effort = update.effort { body["effort"] = effort }
        let _: EmptyResponse = try await post("/v1/preferences", body: body, auth: deviceToken)
    }

    /// "Your day", only the field that changed. Answers with the box's whole
    /// object; a refusal is `APIError.server(status: 422, detail: code)`.
    /// Not part of `PreferenceSync`: the box is the only copy.
    func setDay(
        start: String? = nil, end: String? = nil, checkinEveryHours: Int? = nil, deviceToken: String
    ) async throws -> Preferences {
        var body: [String: Any] = [:]
        if let start { body["day_start"] = start }
        if let end { body["day_end"] = end }
        if let checkinEveryHours { body["checkin_every_hours"] = checkinEveryHours }
        return try await post("/v1/preferences", body: body, auth: deviceToken)
    }

    /// Behind the device token since docs/tasks/funnel-ingress.md step 4.
    func health(deviceToken: String) async throws -> HealthResponse {
        try await get("/health", auth: deviceToken)
    }

    // MARK: - Plumbing

    private struct EmptyResponse: Decodable {}

    private func get<T: Decodable>(
        _ path: String, auth deviceToken: String?, timeout: TimeInterval? = nil, query: [URLQueryItem] = []
    ) async throws -> T {
        var url = baseURL.appendingPathComponent(path)
        if !query.isEmpty {
            var components = URLComponents(url: url, resolvingAgainstBaseURL: false)!
            components.queryItems = query
            url = components.url!
        }
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        if let timeout { request.timeoutInterval = timeout }
        applyAuth(deviceToken, to: &request)
        return try await send(request)
    }

    private func post<T: Decodable>(
        _ path: String, body: [String: Any], auth deviceToken: String?, timeout: TimeInterval? = nil
    ) async throws -> T {
        var request = URLRequest(url: baseURL.appendingPathComponent(path))
        request.httpMethod = "POST"
        if let timeout { request.timeoutInterval = timeout }
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        applyAuth(deviceToken, to: &request)
        return try await send(request)
    }

    private func applyAuth(_ deviceToken: String?, to request: inout URLRequest) {
        guard let deviceToken else { return }
        request.setValue("Bearer \(deviceToken)", forHTTPHeaderField: "Authorization")
    }

    private func send<T: Decodable>(_ request: URLRequest) async throws -> T {
        let data = try await sendData(request)
        if T.self == EmptyResponse.self {
            return EmptyResponse() as! T
        }
        do {
            return try decoder.decode(T.self, from: data)
        } catch {
            throw APIError.decoding(error)
        }
    }

    /// The status check shared by the JSON calls and the raw-body attachment
    /// GET, so both map a failure to the same `APIError`.
    private func sendData(_ request: URLRequest) async throws -> Data {
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw APIError.server(status: 0, detail: "no HTTP response")
        }
        guard (200..<300).contains(http.statusCode) else {
            // Not for unpair, which handles its own 401 (already revoked is the
            // outcome it asked for), and not for the pairing call, which sends
            // no token.
            if http.statusCode == 401, request.url?.path != "/v1/device/unpair",
               let bearer = request.value(forHTTPHeaderField: "Authorization"),
               bearer.hasPrefix("Bearer ") {
                Self.onUnauthorized?(String(bearer.dropFirst("Bearer ".count)))
            }
            let detail = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["detail"] as? String
            throw APIError.server(status: http.statusCode, detail: detail)
        }
        return data
    }
}
