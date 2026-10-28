import Foundation

/// The calls `ChatStore` actually makes. Narrow on purpose: it exists so the
/// chat state machine can be driven by a fake in tests without a URLSession or
/// a wire format in the loop. `APIClient` conforms as-is — everything else
/// (pairing, APNs registration) keeps talking to `APIClient` directly, since
/// nothing there needs a seam yet. Permission calls are here because the
/// in-app approval sheet (docs/tasks/ios-permission-round-trip-ui.md phase 1)
/// is driven from the same poll loop as `getTurn`.
protocol ChatAPI {
    func sendMessage(
        text: String, clientMsgId: String?,
        imagesBase64: [String], effort: ThinkingEffort,
        location: OutgoingLocation?, ideaId: String?, spoken: Bool, timeout: TimeInterval?, deviceToken: String
    ) async throws -> MessageResponse

    func getTurn(_ turnId: String, deviceToken: String) async throws -> Turn

    /// The server-owned conversation (docs/tasks/ios-server-transcript.md).
    /// Pass only the cursor that is set: neither is the newest page,
    /// descending; `after` is catch-up, ascending; `before` is scrollback,
    /// descending. Non-consuming: a retry or a second device reading is safe.
    func transcript(after: Int?, before: Int?, limit: Int, deviceToken: String) async throws -> TranscriptPage

    /// The raw bytes of one attachment a transcript row carries.
    func transcriptAttachment(id: String, deviceToken: String) async throws -> Data

    func health(deviceToken: String) async throws -> HealthResponse

    /// Only for the >= 90% status row (docs/tasks/usage-visibility.md); the
    /// sheet's own panel goes through `UsageAPI`. Not a health signal.
    func usage(deviceToken: String) async throws -> UsageReport

    func permissions(deviceToken: String) async throws -> PermissionsResponse

    func answerPermission(requestId: String, decision: String, deviceToken: String) async throws -> AnswerPermissionResponse
}

extension APIClient: ChatAPI {}
