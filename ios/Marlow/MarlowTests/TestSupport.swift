import Foundation
import Testing
@testable import Marlow

/// Checks a collection's length and hands back the collection, so the elements
/// can be indexed safely afterwards. Use this rather than `#expect(x.count ==
/// n)` followed by `x[i]`: `#expect` records a failure and *keeps going*, so
/// the subscript then traps on an out-of-bounds index and takes the whole test
/// process down with it — turning one clean failure into a crash that loses the
/// results of every test that hadn't run yet.
func requireCount<T>(
    _ items: [T], _ expected: Int, sourceLocation: SourceLocation = #_sourceLocation
) throws -> [T] {
    try #require(
        items.count == expected ? items : nil,
        "expected \(expected) element(s), got \(items.count)",
        sourceLocation: sourceLocation
    )
}

// MARK: - Fake transport

/// Scriptable stand-in for the calls `ChatStore` makes. An actor rather
/// than a lock-guarded class because `ChatAPI`'s requirements are all `async`,
/// which an actor's isolated methods satisfy directly — so recording and
/// scripting are race-free without any `@unchecked Sendable` hand-waving.
actor FakeChatAPI: ChatAPI {
    // Scripted results.
    private var sendScript: [Result<MessageResponse, Error>] = []
    private var sendFallback: Result<MessageResponse, Error> = .success(MessageResponse(turnId: "turn-1"))
    private var turnScript: [Result<Turn, Error>] = []
    private var turnFallback: Result<Turn, Error> = .success(.fixture(status: "running"))
    private var healthResult: Result<HealthResponse, Error> = .success(.fixture())
    private var usageResult: Result<UsageReport, Error> = .success(UsageReport())
    private(set) var usageCallCount = 0
    private var permissionsScript: [Result<PermissionsResponse, Error>] = []
    private var permissionsFallback: Result<PermissionsResponse, Error> = .success(PermissionsResponse(permissionRequests: []))
    private var answerPermissionResult: Result<AnswerPermissionResponse, Error> = .success(AnswerPermissionResponse(accepted: true))
    /// Transcript pages, handed out in order; once exhausted, an empty page
    /// at `transcriptFallbackSeq` repeats (nothing new on the server). Each
    /// scripted page is returned regardless of the cursor asked for, so a test
    /// reads `transcriptCalls` to pin what was asked.
    private var transcriptScript: [Result<TranscriptPage, Error>] = []
    private var transcriptFallbackSeq = 0
    /// Attachment bytes by id. An id with no entry answers 404.
    private var attachmentResults: [String: Result<Data, Error>] = [:]

    /// Called at the top of every `getTurn`, with the 1-based call index.
    /// Lets a test observe or intervene (cancel, snapshot the persisted file)
    /// at an exact point in the poll loop.
    private var onGetTurn: ((Int) async -> Void)?

    /// Called after scripting is resolved but before `permissions()` returns,
    /// with the 1-based call index — lets a test hold a `checkPermissions()`
    /// call mid-flight to land a concurrent `answerPermission()` in the same
    /// window, the interleaving `ChatStore`'s stale-response guard exists for.
    private var onPermissions: ((Int) async -> Void)?

    /// Called inside `transcript`, with the 1-based call index, before its
    /// scripted result is returned — lets a test hold a sync mid-flight, or
    /// cancel the store while a fetch is in the air.
    private var onTranscript: ((Int) async -> Void)?

    /// Called at the top of every `sendMessage`, with the 1-based call index —
    /// lets a test observe state between one retry attempt
    /// and the next, before `submit`'s backoff sleep and next attempt run.
    private var onSend: ((Int) async -> Void)?

    // Recorded calls.
    private(set) var sentMessages: [(
        text: String, clientMsgId: String?,
        imagesBase64: [String], effort: ThinkingEffort,
        location: OutgoingLocation?, ideaId: String?, spoken: Bool, timeout: TimeInterval?, deviceToken: String
    )] = []
    private(set) var getTurnCalls: [(turnId: String, deviceToken: String)] = []
    private(set) var permissionsCallCount = 0
    private(set) var transcriptCalls: [(after: Int?, before: Int?, limit: Int)] = []
    private(set) var attachmentCalls: [String] = []
    /// "transcript" and "health", in the order the store called them — the only
    /// way to pin that the sync runs before `/health` on the tick.
    private(set) var callOrder: [String] = []
    private(set) var answerPermissionCalls: [(requestId: String, decision: String, deviceToken: String)] = []

    var getTurnCallCount: Int { getTurnCalls.count }
    var transcriptCallCount: Int { transcriptCalls.count }

    // MARK: Scripting

    func stubSend(_ result: Result<MessageResponse, Error>) { sendFallback = result }

    /// Results returned in order; once exhausted, `fallback` repeats forever.
    /// Lets a test simulate a connection that drops N times before recovering.
    func stubSendScript(_ script: [Result<MessageResponse, Error>], fallback: Result<MessageResponse, Error>? = nil) {
        sendScript = script
        if let fallback { sendFallback = fallback }
    }
    func stubHealth(_ result: Result<HealthResponse, Error>) { healthResult = result }
    func stubUsage(_ result: Result<UsageReport, Error>) { usageResult = result }
    func stubTranscript(_ pages: [Result<TranscriptPage, Error>], fallbackSeq: Int = 0) {
        transcriptScript = pages
        transcriptFallbackSeq = fallbackSeq
    }
    func stubAttachment(id: String, _ result: Result<Data, Error>) { attachmentResults[id] = result }

    /// Results returned in order; once exhausted, `fallback` repeats forever.
    func stubTurns(_ script: [Result<Turn, Error>], fallback: Result<Turn, Error>? = nil) {
        turnScript = script
        if let fallback { turnFallback = fallback }
    }

    func setOnGetTurn(_ hook: @escaping (Int) async -> Void) { onGetTurn = hook }
    func setOnPermissions(_ hook: @escaping (Int) async -> Void) { onPermissions = hook }
    func setOnTranscript(_ hook: @escaping (Int) async -> Void) { onTranscript = hook }
    func setOnSend(_ hook: @escaping (Int) async -> Void) { onSend = hook }

    /// Results returned in order; once exhausted, `fallback` repeats forever.
    /// Defaults to an empty permission list, so tests unrelated to the
    /// permission sheet don't need to stub this at all.
    func stubPermissions(_ script: [Result<PermissionsResponse, Error>], fallback: Result<PermissionsResponse, Error>? = nil) {
        permissionsScript = script
        if let fallback { permissionsFallback = fallback }
    }

    func stubAnswerPermission(_ result: Result<AnswerPermissionResponse, Error>) { answerPermissionResult = result }

    // MARK: ChatAPI

    func sendMessage(
        text: String, clientMsgId: String?,
        imagesBase64: [String], effort: ThinkingEffort,
        location: OutgoingLocation?, ideaId: String?, spoken: Bool, timeout: TimeInterval?, deviceToken: String
    ) async throws -> MessageResponse {
        sentMessages.append((
            text: text, clientMsgId: clientMsgId,
            imagesBase64: imagesBase64, effort: effort,
            location: location, ideaId: ideaId, spoken: spoken, timeout: timeout, deviceToken: deviceToken
        ))
        if let onSend { await onSend(sentMessages.count) }
        let result = sendScript.isEmpty ? sendFallback : sendScript.removeFirst()
        return try result.get()
    }

    func getTurn(_ turnId: String, deviceToken: String) async throws -> Turn {
        getTurnCalls.append((turnId: turnId, deviceToken: deviceToken))
        if let onGetTurn { await onGetTurn(getTurnCalls.count) }
        let result = turnScript.isEmpty ? turnFallback : turnScript.removeFirst()
        return try result.get()
    }

    func health(deviceToken: String) async throws -> HealthResponse {
        callOrder.append("health")
        return try healthResult.get()
    }

    func usage(deviceToken: String) async throws -> UsageReport {
        usageCallCount += 1
        return try usageResult.get()
    }

    func permissions(deviceToken: String) async throws -> PermissionsResponse {
        permissionsCallCount += 1
        let result = permissionsScript.isEmpty ? permissionsFallback : permissionsScript.removeFirst()
        if let onPermissions { await onPermissions(permissionsCallCount) }
        return try result.get()
    }

    func transcript(after: Int?, before: Int?, limit: Int, deviceToken: String) async throws -> TranscriptPage {
        callOrder.append("transcript")
        transcriptCalls.append((after: after, before: before, limit: limit))
        let result = transcriptScript.isEmpty ? nil : transcriptScript.removeFirst()
        if let onTranscript { await onTranscript(transcriptCalls.count) }
        guard let result else {
            return TranscriptPage(rows: [], latestSeq: transcriptFallbackSeq, hasMore: false)
        }
        return try result.get()
    }

    func transcriptAttachment(id: String, deviceToken: String) async throws -> Data {
        attachmentCalls.append(id)
        guard let result = attachmentResults[id] else { throw APIError.server(status: 404, detail: nil) }
        return try result.get()
    }

    func answerPermission(requestId: String, decision: String, deviceToken: String) async throws -> AnswerPermissionResponse {
        answerPermissionCalls.append((requestId: requestId, decision: decision, deviceToken: deviceToken))
        return try answerPermissionResult.get()
    }
}

// MARK: - A one-shot async gate

/// Lets a test block the fake at a chosen point and release it on demand —
/// the only way to land a `cancelActiveWork()` *while* a call is genuinely in
/// flight rather than racing a sleep.
actor Gate {
    private var isOpen = false
    private var waiters: [CheckedContinuation<Void, Never>] = []

    func open() {
        isOpen = true
        for waiter in waiters { waiter.resume() }
        waiters = []
    }

    /// Blocks until opened, with no deadline. Safe *only* inside a fake's hook,
    /// where the task is tracked by `activeTasks` and so a gate that never
    /// opens is caught by `awaitPendingWork(timeout:)`.
    func wait() async {
        if isOpen { return }
        await withCheckedContinuation { waiters.append($0) }
    }

    /// Blocks until opened or `timeout` elapses; returns whether it opened.
    /// Use this from a *test body* — that suspension point has nothing
    /// watching it, so a bare `wait()` there hangs the whole `xcodebuild test`
    /// run until the CI job's 30-minute timeout, burning ~300 billed minutes on
    /// a 10x-priced macOS runner and reporting a timeout instead of a
    /// diagnosable failure. Same trap `awaitPendingWork(timeout:)` closes.
    func waitOpened(timeout: Duration = .seconds(10)) async -> Bool {
        let deadline = ContinuousClock.now + timeout
        while ContinuousClock.now < deadline {
            if isOpen { return true }
            try? await Task.sleep(for: .milliseconds(1))
        }
        return isOpen
    }
}

/// Waits for a gate the code under test is expected to reach, failing the test
/// rather than hanging if it never gets there. The test-body counterpart to
/// `awaitSettled`.
func awaitOpened(
    _ gate: Gate, _ comment: Comment, sourceLocation: SourceLocation = #_sourceLocation
) async {
    let opened = await gate.waitOpened()
    #expect(opened, comment, sourceLocation: sourceLocation)
}

/// Collects snapshots taken from inside a fake's hook, off the main actor.
actor Recorder {
    private(set) var transcripts: [[ChatMessage]] = []
    func record(_ messages: [ChatMessage]) { transcripts.append(messages) }
}

// MARK: - Fixtures

extension Turn {
    static func fixture(
        turnId: String = "turn-1",
        status: String,
        resultText: String? = nil,
        error: String? = nil,
        activities: [TurnActivity] = [],
        partialText: String = "",
        partialTextMarks: [Int] = [],
        // "chat" is what server/app.py actually enqueues for a message sent from a paired
        // device (it was "ios" until the 2026-10 rename; TurnSource accepts both).
        // This said "device" until 2026-08-07, which was never a real value --
        // harmless while nothing read the field, and actively misleading now
        // that ingestion keys on it.
        source: String = "chat",
        createdAt: Double = 0
    ) -> Turn {
        Turn(
            turnId: turnId,
            status: status,
            source: source,
            resultText: resultText,
            hadPermissionDenial: false,
            error: error,
            createdAt: createdAt,
            activities: activities,
            partialText: partialText,
            partialTextMarks: partialTextMarks
        )
    }
}

extension PermissionRequestInfo {
    static func fixture(
        requestId: String = "req-1", tool: String = "Bash",
        createdAt: Double = Date().timeIntervalSince1970,
        expiresAt: Double? = nil, summaryJSON: String? = nil
    ) -> PermissionRequestInfo {
        // Decoded rather than constructed directly: PermissionRequestInfo has
        // no memberwise initializer available here (Decodable-only), and
        // round-tripping through JSON keeps this fixture honest about the
        // wire shape.
        let expires = expiresAt ?? createdAt + 55
        let json = """
        {"request_id": "\(requestId)", "tool": "\(tool)", "tool_input": {"command": "ls"}, \
        "created_at": \(createdAt), "expires_at": \(expires)\(summaryJSON.map { ", \"summary\": \($0)" } ?? "")}
        """
        return try! JSONDecoder().decode(PermissionRequestInfo.self, from: Data(json.utf8))
    }
}

extension HealthResponse {
    static func fixture(
        status: String = "ok", queuePaused: Bool = false, reauthPrewarn: Bool = false,
        authOk: Bool? = nil
    ) -> HealthResponse {
        HealthResponse(
            status: status,
            authOk: authOk ?? (status == "ok"),
            reauthPrewarn: reauthPrewarn,
            queueDepth: 0,
            queuePaused: queuePaused,
            oldestPendingPermissionAge: nil
        )
    }
}

extension PollingPolicy {
    /// Zero-delay intervals so the state machine runs at full speed, small
    /// attempt caps so exhaustion paths are reachable, and a health interval
    /// long enough that `/health` is polled exactly once per test.
    static func test(
        fastAttempts: Int = 20, slowAttempts: Int = 20,
        sendMaxAttempts: Int = 5, activityDwell: UInt64 = 0,
        sendAttemptTimeout: TimeInterval = 1,
        partialTextTick: UInt64 = 0,
        // Effectively never, so the connection banner stays out of every test
        // that isn't about it. A test that wants the notice sets 0 and holds a
        // call open; one that wants to prove a healthy link stays quiet leaves
        // this alone.
        connectionNoticeDelay: UInt64 = 1_000_000_000_000,
        // Off by default: with a zero interval a live burst would spin for its
        // whole wall-clock duration against the fake. Tests that want it set
        // a duration explicitly.
        burstDuration: Duration = .zero
    ) -> PollingPolicy {
        PollingPolicy(
            burstInterval: 0,
            burstDuration: burstDuration,
            fastInterval: 0,
            fastAttempts: fastAttempts,
            slowInterval: 0,
            slowAttempts: slowAttempts,
            healthInterval: 1_000_000_000_000,
            activityDwell: activityDwell,
            connectionNoticeDelay: connectionNoticeDelay,
            partialTextTick: partialTextTick,
            sendMaxAttempts: sendMaxAttempts,
            sendBaseInterval: 0,
            sendMaxInterval: 0,
            sendAttemptTimeout: sendAttemptTimeout
        )
    }
}

/// A fresh transcript path per test — Swift Testing runs tests in parallel, and
/// the production path is a single shared file in Application Support.
/// Lets a `FakeChatAPI` hook reach a store that does not exist yet when the
/// hook is installed. `ChatStore` starts polling in `init`, so a test wanting
/// to intervene on the very first tick has no window to install a hook after
/// construction — it has to hand the fake something it can fill in later.
@MainActor
final class StoreBox {
    var store: ChatStore?
}

func makeTempTranscriptURL() -> URL {
    let dir = URL(fileURLWithPath: NSTemporaryDirectory())
        .appendingPathComponent("MarlowTests-\(UUID().uuidString)")
    try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    return dir.appendingPathComponent("chat-transcript-v2.json")
}

/// A fresh attachments directory per test, same isolation reasoning as
/// `makeTempTranscriptURL` — a test's `ChatStore` must never read or write
/// the app's real Application Support directory.
func makeTempAttachmentsDirectoryURL() -> URL {
    let dir = URL(fileURLWithPath: NSTemporaryDirectory())
        .appendingPathComponent("MarlowTests-attachments-\(UUID().uuidString)")
    try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    return dir
}

/// Writes the cache file `ChatStore` loads. `latestSeq: nil` is a store that has
/// never synced, so its first sync bootstraps.
func writeTranscript(_ messages: [ChatMessage], latestSeq: Int? = nil, to url: URL) throws {
    let cache = ChatStore.TranscriptCache(latestSeq: latestSeq, hasOlderOnServer: nil, messages: messages)
    try JSONEncoder().encode(cache).write(to: url, options: .atomic)
}

func readCache(at url: URL) -> ChatStore.TranscriptCache? {
    guard let data = try? Data(contentsOf: url) else { return nil }
    return try? JSONDecoder().decode(ChatStore.TranscriptCache.self, from: data)
}

func readTranscript(at url: URL) -> [ChatMessage] {
    readCache(at: url)?.messages ?? []
}

/// Spins the cooperative pool until `condition` holds. Used for state that
/// `awaitPendingWork()` deliberately doesn't track (the `/health` banner task)
/// and for waiting on a fake's call count mid-flight. The condition is `async`
/// so it can read an actor-isolated fake as well as main-actor store state.
func waitUntil(
    timeout: Duration = .seconds(5), _ condition: () async -> Bool
) async -> Bool {
    let deadline = ContinuousClock.now + timeout
    while ContinuousClock.now < deadline {
        if await condition() { return true }
        try? await Task.sleep(for: .milliseconds(5))
    }
    return false
}

/// Waits for the store's state machine to settle, failing the test rather than
/// hanging if it doesn't. Always use this instead of calling
/// `awaitPendingWork()` bare — its return value is the only thing standing
/// between a stuck task and a 30-minute CI timeout.
@MainActor
func awaitSettled(
    _ store: ChatStore, sourceLocation: SourceLocation = #_sourceLocation
) async {
    let settled = await store.awaitPendingWork()
    #expect(settled, "ChatStore still had work in flight after the timeout", sourceLocation: sourceLocation)
}

/// Bools appended from a fake's async hook and read back by the test.
final class LockedFlags: @unchecked Sendable {
    private let lock = NSLock()
    private var stored: [Bool] = []
    func append(_ value: Bool) { lock.withLock { stored.append(value) } }
    var values: [Bool] { lock.withLock { stored } }
}
