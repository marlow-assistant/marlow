import UserNotifications

/// Replaces "Marlow has something for you..." with the actual reply, fetched
/// over the tailnet before the banner is shown
/// (docs/tasks/ios-notification-open-and-content-swap.md Half B).
///
/// The privacy boundary this preserves: APNs carries only `{aps, type, ref}`,
/// never the message. The content is fetched by the phone, from the tailnet
/// server, and the permission to rewrite the body locally is the only thing
/// Apple's infrastructure is told.
///
/// It also leaves what it fetched in `TurnInbox`, so tapping the banner does
/// not make the app re-download the same reply before it can render it.
///
/// **The generic APNs alert is the fail-safe, not a loading state.** The
/// original content is
/// copied before any work begins and returned unchanged on every failure. This
/// extension never substitutes a blank body or an error string. It reads
/// only `/v1/turn/{id}`, which has no side effects. (It once had to avoid
/// `/v1/pending`, which consumed a turn on read; that endpoint is gone.)
final class NotificationService: UNNotificationServiceExtension {
    private var contentHandler: ((UNNotificationContent) -> Void)?
    /// The fail-safe copy, captured first thing.
    private var original: UNNotificationContent?
    /// `serviceExtensionTimeWillExpire()` can land while the fetch is still in
    /// flight, so both paths race to deliver. Whoever gets here first wins;
    /// calling the handler twice is a system-level misuse.
    private var handled = false
    private let lock = NSLock()

    override func didReceive(
        _ request: UNNotificationRequest,
        withContentHandler contentHandler: @escaping (UNNotificationContent) -> Void
    ) {
        self.contentHandler = contentHandler
        self.original = request.content

        let started = Date()
        let userInfo = request.content.userInfo

        // Only `turn_ready`. A `notify` reminder already carries its real body
        // and has nothing to fetch; a `permission_request` is on a 55s human
        // deadline that a network stall here would spend.
        guard userInfo["type"] as? String == "turn_ready",
              let ref = userInfo["ref"] as? String,
              !ref.isEmpty,
              // The ref goes straight into a URL path. A turn ID is a UUID;
              // anything else is either a protocol change or something we
              // should not be pasting into a request.
              ref.allSatisfy({ $0.isHexDigit || $0 == "-" })
        else {
            finish(outcome: "no_ref", started: started)
            return
        }

        guard let deviceToken = SharedCredential.load() else {
            // Expected on a build installed before the app has launched once
            // to mirror the credential — a real answer, not a bug.
            finish(outcome: "no_credential", started: started)
            return
        }

        Task { await fetch(ref: ref, deviceToken: deviceToken, started: started) }
    }

    override func serviceExtensionTimeWillExpire() {
        // Apple is about to kill us; hand back the best content we have,
        // which at this point is always the original.
        finish(outcome: "expired", started: nil)
    }

    // MARK: - Fetch

    private func fetch(ref: String, deviceToken: String, started: Date) async {
        guard let url = URL(string: "\(PairedHost.current())/v1/turn/\(ref)") else {
            finish(outcome: "bad_url", started: started)
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.setValue("Bearer \(deviceToken)", forHTTPHeaderField: "Authorization")
        // Our own deadline, well under Apple's budget: being killed by the
        // system leaves no diagnostic, timing out here leaves one.
        request.timeoutInterval = NotificationShared.requestTimeout
        // The response body is the reply text. `/v1/turn/{id}` sends no
        // Cache-Control, so a default URLSession would heuristically write it
        // to an on-disk URL cache in this extension's container and leave it
        // there — message content in a second, unmanaged place on disk that
        // nothing ever prunes. `TurnInbox` below is the deliberate copy, with
        // a bound and a delete path; a URL cache would be an accidental one
        // with neither. Belt and braces: an ephemeral session has no disk
        // cache to begin with, and the policy stops a cached response being
        // read back as a "successful fetch" that never touched the network.
        request.cachePolicy = .reloadIgnoringLocalAndRemoteCacheData

        let configuration = URLSessionConfiguration.ephemeral
        configuration.urlCache = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            // The answer the spike exists for lives in this branch: with the
            // app suspended and the phone locked, is there a route at all?
            let timedOut = (error as? URLError)?.code == .timedOut
            finish(outcome: timedOut ? "timeout" : "network_error", started: started)
            return
        }

        guard let http = response as? HTTPURLResponse else {
            finish(outcome: "network_error", started: started)
            return
        }
        guard http.statusCode == 200 else {
            finish(outcome: "http_\(http.statusCode)", started: started)
            return
        }

        // Only the two fields the banner needs. The app decodes the same
        // bytes with the full `Turn`, so this extension never has to be kept
        // in step with that model — see `TurnInbox.store`.
        struct FetchedTurn: Decodable {
            let status: String
            let resultText: String?

            enum CodingKeys: String, CodingKey {
                case status
                case resultText = "result_text"
            }
        }

        guard let turn = try? JSONDecoder().decode(FetchedTurn.self, from: data) else {
            finish(outcome: "decode_error", started: started)
            return
        }

        // Stored before the body check, not after: an `error` turn has no
        // result text to display, but the app still has a spinning placeholder
        // for it and can resolve that from disk instead of polling. Only
        // terminal turns — a `running` turn would resolve the placeholder to a
        // state it has already moved on from.
        if turn.status == "done" || turn.status == "error" {
            TurnInbox.store(turnId: ref, response: data)
        }
        // A turn that is ready but has no usable text is not a failure of the
        // transport, and it is also not something to display — the generic
        // body stays. The inbox entry above still stands: the app can render
        // that turn properly, a 40-character banner cannot.
        guard let body = turn.resultText.flatMap(NotificationShared.alertBody(from:)) else {
            finish(outcome: "no_text_\(turn.status)", started: started)
            return
        }

        finish(outcome: "ok", started: started, body: body)
    }

    // MARK: - Delivery

    /// The single exit. Every path above ends here, and only the first call
    /// does anything.
    private func finish(outcome: String, started: Date?, body: String? = nil) {
        lock.lock()
        if handled {
            lock.unlock()
            return
        }
        handled = true
        let handler = contentHandler
        let original = self.original
        lock.unlock()

        let milliseconds = started.map { Int(Date().timeIntervalSince($0) * 1000) } ?? 0
        NotificationDiagnostics.record(
            outcome: outcome,
            milliseconds: milliseconds,
            bodyCharacters: body?.count
        )

        guard let handler, let original else { return }
        guard let body, let mutable = original.mutableCopy() as? UNMutableNotificationContent else {
            handler(original)
            return
        }
        mutable.body = body
        handler(mutable)
    }
}
