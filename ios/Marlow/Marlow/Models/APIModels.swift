import Foundation

struct PairResponse: Decodable {
    let deviceToken: String
    enum CodingKeys: String, CodingKey { case deviceToken = "device_token" }
}

struct MessageResponse: Decodable {
    let turnId: String
    enum CodingKeys: String, CodingKey { case turnId = "turn_id" }
}

/// `at` looks unused — nothing renders it — but it is load-bearing for
/// `ActivityPlaybackState.ingest`: that cursor finds the longest overlap
/// between the previous window and the next one, and the timestamp is the only
/// thing making two otherwise-identical events (two `Read`s, two `tool_end`s)
/// distinguishable. Drop it from `Equatable` and a sliding window of repeated
/// events over-matches and silently swallows the new ones.
struct TurnActivity: Decodable, Equatable {
    let kind: String?
    let tool: String?
    let at: Double?

    enum CodingKeys: String, CodingKey {
        case kind
        case tool
        case at
    }

    init(kind: String?, tool: String?, at: Double? = nil) {
        self.kind = kind
        self.tool = tool
        self.at = at
    }

    /// Friendly, bounded copy for the UI. Tool names select known wording but
    /// are never rendered: MCP connectors control those names, so an unknown
    /// value is arbitrary semi-trusted text rather than user-facing copy.
    /// The two labels that describe work with nothing to show for it. Named
    /// rather than written inline because the view has to recognise them: once
    /// the reply is typing itself in the bubble, these say strictly less than
    /// the text above them and are hidden. A tool label is the opposite — it
    /// is the only thing explaining why the text has stopped.
    static let thinking = "Thinking…"
    static let writing = "Writing…"

    static func describesUnseenWork(_ label: String?) -> Bool {
        guard let label else { return true }
        return label == thinking || label == writing
    }

    var label: String {
        guard kind == "tool_start" else {
            // A `text` event means the model is writing the reply — and since
            // reply streaming shipped, that reply is visible in the bubble
            // directly above this line. A spinner reading "Thinking…" over
            // text that is actively typing itself contradicts what the user
            // can see, and read as a glitch on the first evening it was used
            // (docs/tasks/streaming-reply-text.md). Every other kind —
            // thinking, tool_end, an unknown future one — stays "Thinking…",
            // which is still the honest word for them.
            return kind == "text" ? Self.writing : Self.thinking
        }
        guard let tool else { return "Using a tool…" }

        let normalized = tool.lowercased()
        if normalized.contains("calendar") {
            return "Checking your calendar…"
        }
        // Our own weather server (docs/tasks/weather-mcp.md): `mcp__weather__*`.
        // Its route tool routes and times a drive before any weather, so it
        // gets its own words (Mike, 2026-10-14).
        if normalized.contains("route_forecast") {
            return "Planning the route…"
        }
        if normalized.contains("weather") {
            return "Checking the weather…"
        }
        // Connector-named web search, e.g. `mcp__brave__web_search`. The exact
        // `WebSearch`/`WebFetch` cases below only catch the CLI's own tools, so
        // a connector doing the same job fell through to "Using a tool…" — the
        // least informative label available, on the leg that takes the longest
        // and most needs explaining. Both halves required, so `webflow` and
        // friends do not become a web search.
        if normalized.contains("web")
            && (normalized.contains("search") || normalized.contains("fetch")
                || normalized.contains("browse")) {
            return "Searching the web…"
        }
        if normalized.contains("gmail") || normalized.contains("mail") {
            return "Checking your mail…"
        }

        switch tool {
        case "Read": return "Reading a file…"
        case "Write", "Edit": return "Writing a file…"
        case "Bash": return "Running a command…"
        case "Grep", "Glob": return "Searching…"
        case "WebSearch", "WebFetch": return "Searching the web…"
        default: return "Using a tool…"
        }
    }
}

/// What the server calls a turn a person sent from a paired device (as opposed
/// to `cron`, `system` and `compaction`). The server now writes `chat`; it used
/// to write `ios`, and records stored before the rename (transcript rows, the
/// activity log) keep that value for 30 days to 12 months. So every check for
/// "is this a chat turn" goes through here and accepts both, for good. A literal
/// `== "chat"` would quietly demote every old record to "not a chat turn".
enum TurnSource {
    static func isChat(_ value: String?) -> Bool {
        value == "chat" || value == "ios"
    }
}

struct Turn: Decodable, Identifiable {
    let turnId: String
    let status: String
    let source: String
    let resultText: String?
    let hadPermissionDenial: Bool
    let error: String?
    let createdAt: Double
    let activities: [TurnActivity]
    /// The reply as far as the model has written it, cumulative and whole on
    /// every poll (docs/tasks/streaming-reply-text.md). Empty once the turn is
    /// terminal, and `resultText` is authoritative from that moment — this is
    /// an approximation assembled from deltas and must never be what a
    /// finished turn is left showing.
    let partialText: String
    /// Character offsets into `partialText` where a tool call happened, so an
    /// interleaved turn does not fuse two thoughts into one sentence.
    let partialTextMarks: [Int]
    let mergedInto: String?

    var id: String { turnId }

    enum CodingKeys: String, CodingKey {
        case turnId = "turn_id"
        case status
        case source
        case resultText = "result_text"
        case hadPermissionDenial = "had_permission_denial"
        case error
        case createdAt = "created_at"
        case activities
        case partialText = "partial_text"
        case partialTextMarks = "partial_text_marks"
        case mergedInto = "merged_into"
    }

    init(
        turnId: String, status: String, source: String, resultText: String?,
        hadPermissionDenial: Bool, error: String?,
        createdAt: Double, activities: [TurnActivity], partialText: String = "",
        partialTextMarks: [Int] = [], mergedInto: String? = nil
    ) {
        self.turnId = turnId
        self.status = status
        self.source = source
        self.resultText = resultText
        self.hadPermissionDenial = hadPermissionDenial
        self.error = error
        self.createdAt = createdAt
        self.activities = activities
        self.partialText = partialText
        self.partialTextMarks = partialTextMarks
        self.mergedInto = mergedInto
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        turnId = try container.decode(String.self, forKey: .turnId)
        status = try container.decode(String.self, forKey: .status)
        source = try container.decode(String.self, forKey: .source)
        resultText = try container.decodeIfPresent(String.self, forKey: .resultText)
        hadPermissionDenial = try container.decode(Bool.self, forKey: .hadPermissionDenial)
        error = try container.decodeIfPresent(String.self, forKey: .error)
        createdAt = try container.decode(Double.self, forKey: .createdAt)
        // Servers predating the cumulative contract omit this key. Treat that
        // exactly like an empty array so the client remains safe to ship first.
        activities = try container.decodeIfPresent([TurnActivity].self, forKey: .activities) ?? []
        // Absent from a server predating reply streaming; "nothing streamed
        // yet" is exactly what that means, so decode it as empty rather than
        // failing the whole turn.
        partialText = try container.decodeIfPresent(String.self, forKey: .partialText) ?? ""
        partialTextMarks = try container.decodeIfPresent([Int].self, forKey: .partialTextMarks) ?? []
        mergedInto = try container.decodeIfPresent(String.self, forKey: .mergedInto)
    }
}

/// One page of `GET /v1/transcript` (docs/tasks/server-transcript.md).
///
/// Decoded leniently, on the same reasoning as `ChatMessage.Status`: one value
/// this build does not know must not fail the page, because a page that fails
/// to decode is retried forever against the same bytes and the cursor never
/// moves. Every Optional or defaulted field below is a field the server may
/// grow, drop or null without that being this client's problem.
struct TranscriptPage: Decodable {
    let rows: [TranscriptRow]
    /// The server's newest `seq` when the page was read. NOT the cursor: a page
    /// that stopped at `limit` ends before it, so the cursor is the last row's
    /// own `seq` (`ChatStore.syncTranscript`).
    let latestSeq: Int
    let hasMore: Bool

    enum CodingKeys: String, CodingKey {
        case rows
        case latestSeq = "latest_seq"
        case hasMore = "has_more"
    }

    init(rows: [TranscriptRow], latestSeq: Int, hasMore: Bool) {
        self.rows = rows
        self.latestSeq = latestSeq
        self.hasMore = hasMore
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        rows = try container.decode([TranscriptRow].self, forKey: .rows)
        latestSeq = try container.decode(Int.self, forKey: .latestSeq)
        hasMore = try container.decodeIfPresent(Bool.self, forKey: .hasMore) ?? false
    }
}

/// The idea a "Do it" answers, quoted above Mike's bubble
/// (docs/tasks/ideas-panel.md). Server rows carry it as `quote` once the server
/// has matched `idea_id` to an open idea; the phone also builds one itself for
/// the optimistic bubble, so it looks right before the transcript syncs. One
/// snake_case shape both ways, because `ChatMessage` persists it.
struct IdeaQuote: Codable, Equatable {
    let ideaId: String
    let emoji: String
    let title: String

    enum CodingKeys: String, CodingKey {
        case ideaId = "idea_id"
        case emoji, title
    }

    init(ideaId: String, emoji: String, title: String) {
        self.ideaId = ideaId
        self.emoji = emoji
        self.title = title
    }

    init(_ idea: Idea) {
        self.init(ideaId: idea.id, emoji: idea.emoji, title: idea.title)
    }
}

/// One bubble of the server-owned conversation. Immutable once written.
struct TranscriptRow: Decodable, Equatable {
    /// A role this build does not know decodes as `.other` and is skipped by
    /// `TranscriptMerge` (its `seq` still advances the cursor), rather than
    /// failing the whole page.
    enum Role: String, Decodable {
        case user
        case assistant
        case other

        init(from decoder: any Decoder) throws {
            let raw = try decoder.singleValueContainer().decode(String.self)
            self = Role(rawValue: raw) ?? .other
        }
    }

    /// Assistant rows only (`null` on a user row). An unknown value is `.error`
    /// for the reason `ChatMessage.Status` gives: it was written by a newer
    /// server, and showing the row beats discarding the page.
    enum Status: String, Decodable {
        case done
        case error

        init(from decoder: any Decoder) throws {
            let raw = try decoder.singleValueContainer().decode(String.self)
            self = Status(rawValue: raw) ?? .error
        }
    }

    struct Attachment: Decodable, Equatable {
        let id: String
        let mime: String
    }

    let seq: Int
    let id: String
    let role: Role
    let source: String
    /// User rows: the turn they started. Assistant rows: the turn they finish.
    let turnId: String
    /// User rows only; the sending client's own id for the message.
    let clientMsgId: String?
    let text: String
    let status: Status?
    let createdAt: Double
    /// Assistant rows: the user-row `turn_id`s this reply resolves. A user row
    /// is in flight if and only if no assistant row lists its `turn_id` here.
    let answers: [String]
    let attachments: [Attachment]
    /// User rows from a "Do it": the idea quoted above the text. Null or absent
    /// otherwise (an older server never sends it); a malformed one reads as
    /// absent rather than failing the page.
    let quote: IdeaQuote?

    enum CodingKeys: String, CodingKey {
        case seq, id, role, source, text, status, answers, attachments, quote
        case turnId = "turn_id"
        case clientMsgId = "client_msg_id"
        case createdAt = "created_at"
    }

    init(
        seq: Int, id: String, role: Role, source: String = "chat", turnId: String,
        clientMsgId: String? = nil, text: String = "", status: Status? = nil,
        createdAt: Double = 0, answers: [String] = [], attachments: [Attachment] = [],
        quote: IdeaQuote? = nil
    ) {
        self.seq = seq
        self.id = id
        self.role = role
        self.source = source
        self.turnId = turnId
        self.clientMsgId = clientMsgId
        self.text = text
        self.status = status
        self.createdAt = createdAt
        self.answers = answers
        self.attachments = attachments
        self.quote = quote
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        seq = try container.decode(Int.self, forKey: .seq)
        id = try container.decode(String.self, forKey: .id)
        role = try container.decode(Role.self, forKey: .role)
        source = try container.decodeIfPresent(String.self, forKey: .source) ?? ""
        turnId = try container.decodeIfPresent(String.self, forKey: .turnId) ?? ""
        clientMsgId = try container.decodeIfPresent(String.self, forKey: .clientMsgId)
        text = try container.decodeIfPresent(String.self, forKey: .text) ?? ""
        status = try container.decodeIfPresent(Status.self, forKey: .status)
        createdAt = try container.decodeIfPresent(Double.self, forKey: .createdAt) ?? 0
        answers = try container.decodeIfPresent([String].self, forKey: .answers) ?? []
        attachments = try container.decodeIfPresent([Attachment].self, forKey: .attachments) ?? []
        quote = (try? container.decodeIfPresent(IdeaQuote.self, forKey: .quote)) ?? nil
    }
}

struct PermissionRequestInfo: Decodable, Identifiable {
    let requestId: String
    let tool: String
    let toolInput: [String: AnyCodableValue]
    let createdAt: Double
    /// When the server's own `t_hook_seconds` wait gives up on this request —
    /// computed server-side (`server/app.py._serialize_permission_request`)
    /// so the client renders its countdown against the same deadline the
    /// server actually enforces, rather than keeping its own copy of the
    /// duration that could drift out of sync.
    let expiresAt: Double
    /// The server's plain-language reading of the request. Optional because
    /// an older server does not send it; the card then falls back to the tool
    /// name and the raw input, which is what the sheet showed before.
    let summary: PermissionSummary?

    var id: String { requestId }

    enum CodingKeys: String, CodingKey {
        case requestId = "request_id"
        case tool
        case toolInput = "tool_input"
        case createdAt = "created_at"
        case expiresAt = "expires_at"
        case summary
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        requestId = try container.decode(String.self, forKey: .requestId)
        tool = try container.decode(String.self, forKey: .tool)
        toolInput = try container.decode([String: AnyCodableValue].self, forKey: .toolInput)
        createdAt = try container.decode(Double.self, forKey: .createdAt)
        expiresAt = try container.decode(Double.self, forKey: .expiresAt)
        summary = try container.decodeIfPresent(PermissionSummary.self, forKey: .summary)
    }

    /// True once the server's own deadline for this request has passed —
    /// used to keep an already-timed-out request from ever being published as
    /// `pendingPermission` in the first place (rather than relying on the
    /// card's own countdown to notice and self-clear after the fact).
    func isExpired(asOf date: Date = Date()) -> Bool {
        date.timeIntervalSince1970 >= expiresAt
    }
}

/// What the approval card says about a request (`server/approval_summary.py`).
/// The split matters and the card keeps it visible: `title` and `preview` are
/// facts the server lifted from the tool input; `marlowSays` is the model's
/// own account of why, which an injected prompt could write too, so it is
/// only ever shown as a quotation.
struct PermissionSummary: Decodable, Equatable {
    let title: String
    let preview: [PermissionPreviewLine]
    let marlowSays: String?
    /// Non-nil when the server offers "Allow similar for this reply" for this
    /// request. The server answers 400 to `allow_similar` without one, so the
    /// button exists only when this does.
    let similar: PermissionSimilar?

    enum CodingKeys: String, CodingKey {
        case title, preview, similar
        case marlowSays = "marlow_says"
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        title = try container.decode(String.self, forKey: .title)
        preview = try container.decodeIfPresent([PermissionPreviewLine].self, forKey: .preview) ?? []
        marlowSays = try container.decodeIfPresent(String.self, forKey: .marlowSays)
        similar = try container.decodeIfPresent(PermissionSimilar.self, forKey: .similar)
    }

    init(title: String, preview: [PermissionPreviewLine] = [], marlowSays: String? = nil, similar: PermissionSimilar? = nil) {
        self.title = title
        self.preview = preview
        self.marlowSays = marlowSays
        self.similar = similar
    }
}

struct PermissionPreviewLine: Decodable, Equatable {
    /// `remove`, `add` or `text`; any other value the server may grow later
    /// decodes and renders as plain text rather than failing the whole list.
    enum Op: Equatable {
        case remove, add, text
    }

    let op: Op
    let text: String

    enum CodingKeys: String, CodingKey {
        case op, text
    }

    init(op: Op, text: String) {
        self.op = op
        self.text = text
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let raw = try container.decodeIfPresent(String.self, forKey: .op)
        switch raw {
        case "remove": op = .remove
        case "add": op = .add
        default: op = .text
        }
        text = try container.decode(String.self, forKey: .text)
    }
}

struct PermissionSimilar: Decodable, Equatable {
    let label: String
}

/// Backs `GET /v1/permissions`, which has no side effects. It was chosen
/// over `/v1/pending` because that one consumed a turn's single-use delivered
/// flag; `/v1/pending` itself was removed on 2026-10-01.
struct PermissionsResponse: Decodable {
    let permissionRequests: [PermissionRequestInfo]

    enum CodingKeys: String, CodingKey {
        case permissionRequests = "permission_requests"
    }
}

struct AnswerPermissionResponse: Decodable {
    let accepted: Bool
}

struct HealthResponse: Decodable {
    let status: String
    let authOk: Bool
    let reauthPrewarn: Bool
    let queueDepth: Int
    let queuePaused: Bool
    let oldestPendingPermissionAge: Double?
    /// Display-only, and optional for the same reason `Turn.activities` is:
    /// a server predating this field must keep working, so the client stays
    /// safe to ship first. Read from the server rather than hardcoded in the
    /// app precisely so a `MARLOW_MODEL` override can't leave the header
    /// confidently stating something untrue.
    ///
    /// There is deliberately no model *picker* — a model switch pays a full
    /// cold prefix read where an effort switch does not
    /// (docs/tasks/model-and-effort-config.md).
    let model: String?
    /// What the SERVER PROCESS is running — frozen at import server-side, so it
    /// reports the code actually loaded rather than whatever is on disk. That
    /// distinction is the point: a merge that lands without a successful
    /// restart shows up here as a commit that disagrees with the checkout.
    /// Optional for the same shipping reason as `model`.
    let gitBranch: String?
    let gitCommit: String?

    enum CodingKeys: String, CodingKey {
        case status
        case authOk = "auth_ok"
        case reauthPrewarn = "reauth_prewarn"
        case queueDepth = "queue_depth"
        case queuePaused = "queue_paused"
        case oldestPendingPermissionAge = "oldest_pending_permission_age"
        case model
        case gitBranch = "git_branch"
        case gitCommit = "git_commit"
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        status = try container.decode(String.self, forKey: .status)
        authOk = try container.decode(Bool.self, forKey: .authOk)
        reauthPrewarn = try container.decode(Bool.self, forKey: .reauthPrewarn)
        queueDepth = try container.decode(Int.self, forKey: .queueDepth)
        queuePaused = try container.decode(Bool.self, forKey: .queuePaused)
        oldestPendingPermissionAge = try container.decodeIfPresent(
            Double.self, forKey: .oldestPendingPermissionAge)
        model = try container.decodeIfPresent(String.self, forKey: .model)
        gitBranch = try container.decodeIfPresent(String.self, forKey: .gitBranch)
        gitCommit = try container.decodeIfPresent(String.self, forKey: .gitCommit)
    }

    init(
        status: String, authOk: Bool, reauthPrewarn: Bool, queueDepth: Int,
        queuePaused: Bool, oldestPendingPermissionAge: Double?, model: String? = nil,
        gitBranch: String? = nil, gitCommit: String? = nil
    ) {
        self.status = status
        self.authOk = authOk
        self.reauthPrewarn = reauthPrewarn
        self.queueDepth = queueDepth
        self.queuePaused = queuePaused
        self.oldestPendingPermissionAge = oldestPendingPermissionAge
        self.model = model
        self.gitBranch = gitBranch
        self.gitCommit = gitCommit
    }
}

/// Minimal decode-only JSON value box for `tool_input`, whose shape varies by tool.
enum AnyCodableValue: Decodable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case object([String: AnyCodableValue])
    case array([AnyCodableValue])
    case null

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([String: AnyCodableValue].self) {
            self = .object(value)
        } else if let value = try? container.decode([AnyCodableValue].self) {
            self = .array(value)
        } else {
            self = .null
        }
    }

    /// Compact single-line rendering for display in the permission-approval UI.
    var displayString: String {
        switch self {
        case .string(let s): return s
        case .number(let n): return n.truncatingRemainder(dividingBy: 1) == 0 ? String(Int(n)) : String(n)
        case .bool(let b): return String(b)
        case .null: return "null"
        case .array(let items): return "[" + items.map(\.displayString).joined(separator: ", ") + "]"
        case .object(let dict):
            return "{" + AnyCodableValue.formattedPairs(dict).joined(separator: ", ") + "}"
        }
    }

    /// `"key: value"` for every entry, sorted by key. The one place this
    /// dict->lines transform is implemented — the approval card's raw
    /// details and any future reader of a tool input call this rather than
    /// each re-deriving the same map/sort, so a change to how a value
    /// renders can't update one and silently miss the other.
    static func formattedPairs(_ dict: [String: AnyCodableValue]) -> [String] {
        dict.map { "\($0.key): \($0.value.displayString)" }.sorted()
    }
}

// MARK: - Activity log (docs/tasks/ux-lane.md §2)

/// One page of `GET /v1/activity`, newest first. `nextBefore` is nil when this
/// was the last page — the server only sets it when the page came back full.
struct ActivityPage: Decodable {
    let entries: [ActivityEntry]
    let nextBefore: Double?

    enum CodingKeys: String, CodingKey {
        case entries
        case nextBefore = "next_before"
    }
}

/// One turn's record, as the server wrote it at turn end. Every field but the
/// id and time is optional, and decoding never depends on `v`: the record is a
/// file on the box that outlives app versions, and an entry the app half
/// understands is worth more than a page that fails to decode.
struct ActivityEntry: Decodable, Identifiable, Equatable {
    let turnId: String
    let source: String?
    let label: String?
    let createdAt: Double?
    let startedAt: Double?
    let finishedAt: Double
    let status: String?
    let tools: [ActivityToolCall]
    let toolsDropped: Int
    let approvals: [ActivityApproval]
    let denials: [String]
    let files: [String]
    /// The model id the API answered with ("claude-sonnet-5-5") and the effort
    /// the server passed. Raw facts; `ActivityFormat.runLine` words them. nil
    /// on records written before 2026-10-18.
    let model: String?
    let effort: String?
    /// "plumbing" for the harness's own upkeep (nightly tidy, overnight
    /// ideas), which the panel files under "background jobs"
    /// (docs/tasks/default-infrastructure.md). nil for everything else and
    /// for every record written before 2026-10-21.
    let tier: String?
    /// The server kept what this turn said because it never reached the chat;
    /// fetched with `activityOutput` when the row is expanded.
    let hasOutput: Bool

    var id: String { turnId }

    enum CodingKeys: String, CodingKey {
        case turnId = "turn_id"
        case source, label, status, tools, approvals, denials, files, model, effort, tier
        case createdAt = "created_at"
        case startedAt = "started_at"
        case finishedAt = "finished_at"
        case toolsDropped = "tools_dropped"
        case hasOutput = "has_output"
    }

    init(
        turnId: String, source: String?, label: String?, createdAt: Double? = nil,
        startedAt: Double? = nil, finishedAt: Double, status: String? = "done",
        tools: [ActivityToolCall] = [], toolsDropped: Int = 0,
        approvals: [ActivityApproval] = [], denials: [String] = [], files: [String] = [],
        model: String? = nil, effort: String? = nil, tier: String? = nil, hasOutput: Bool = false
    ) {
        self.turnId = turnId
        self.source = source
        self.label = label
        self.createdAt = createdAt
        self.startedAt = startedAt
        self.finishedAt = finishedAt
        self.status = status
        self.tools = tools
        self.toolsDropped = toolsDropped
        self.approvals = approvals
        self.denials = denials
        self.files = files
        self.model = model
        self.effort = effort
        self.tier = tier
        self.hasOutput = hasOutput
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        turnId = try c.decode(String.self, forKey: .turnId)
        finishedAt = try c.decode(Double.self, forKey: .finishedAt)
        source = try? c.decodeIfPresent(String.self, forKey: .source)
        label = try? c.decodeIfPresent(String.self, forKey: .label)
        status = try? c.decodeIfPresent(String.self, forKey: .status)
        createdAt = try? c.decodeIfPresent(Double.self, forKey: .createdAt)
        startedAt = try? c.decodeIfPresent(Double.self, forKey: .startedAt)
        tools = (try? c.decodeIfPresent([ActivityToolCall].self, forKey: .tools)) ?? []
        toolsDropped = (try? c.decodeIfPresent(Int.self, forKey: .toolsDropped)) ?? 0
        approvals = (try? c.decodeIfPresent([ActivityApproval].self, forKey: .approvals)) ?? []
        denials = (try? c.decodeIfPresent([String].self, forKey: .denials)) ?? []
        files = (try? c.decodeIfPresent([String].self, forKey: .files)) ?? []
        model = try? c.decodeIfPresent(String.self, forKey: .model)
        effort = try? c.decodeIfPresent(String.self, forKey: .effort)
        tier = try? c.decodeIfPresent(String.self, forKey: .tier)
        hasOutput = (try? c.decodeIfPresent(Bool.self, forKey: .hasOutput)) ?? false
    }
}

/// `GET /v1/activity/{turn_id}/output`: what a background job said.
struct ActivityOutput: Decodable, Equatable {
    let text: String
}

struct ActivityToolCall: Decodable, Equatable {
    let name: String
    let target: String?
    let at: Double?
}

struct ActivityApproval: Decodable, Equatable {
    let tool: String
    /// "allow" | "deny" | "timeout"
    let decision: String
}


// MARK: - Marlow sheet (docs/tasks/ux-lane.md §3a)

/// What a file is for; picks the "About this file" sentence. An unknown value
/// decodes to `.unknown` so a newer server can add kinds without breaking the
/// whole sheet.
enum IdentityKind: String, Decodable, Equatable {
    case soul, user, instructions, memory, job, unknown

    init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = IdentityKind(rawValue: raw) ?? .unknown
    }
}

/// One entry of `GET /v1/identity`'s `files`. `description` is only set for
/// memory files (the index line Marlow's prompt carries).
struct IdentityFileInfo: Decodable, Identifiable, Equatable {
    let id: String
    let kind: IdentityKind
    let modifiedAt: Double
    let size: Int
    let description: String?

    enum CodingKeys: String, CodingKey {
        case id, kind, size, description
        case modifiedAt = "modified_at"
    }
}

struct ScheduleEntry: Decodable, Identifiable, Equatable {
    let id: String
    let cron: String?
    let at: String?
    let recurrence: Recurrence
    let nextFireAt: Double?
    let notify: String?
    let prompt: String?
    let job: String?
    let jobFile: String?

    enum CodingKeys: String, CodingKey {
        case id, cron, at, recurrence, notify, prompt, job
        case nextFireAt = "next_fire_at"
        case jobFile = "job_file"
    }
}

extension ScheduleEntry {
    /// A missing `recurrence` reads as `.other` (the raw cron is shown).
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        cron = try c.decodeIfPresent(String.self, forKey: .cron)
        at = try c.decodeIfPresent(String.self, forKey: .at)
        recurrence = try c.decodeIfPresent(Recurrence.self, forKey: .recurrence) ?? .other
        nextFireAt = try c.decodeIfPresent(Double.self, forKey: .nextFireAt)
        notify = try c.decodeIfPresent(String.self, forKey: .notify)
        prompt = try c.decodeIfPresent(String.self, forKey: .prompt)
        job = try c.decodeIfPresent(String.self, forKey: .job)
        jobFile = try c.decodeIfPresent(String.self, forKey: .jobFile)
    }
}

/// Structure only; the server sends no wording (docs/tasks/ux-lane.md §3a).
/// Times are "HH:MM" store wall-clock, weekdays 0-6 with Sunday 0. A type
/// this build doesn't know decodes as `.other`.
enum Recurrence: Decodable, Equatable {
    case daily(times: [String])
    case weekly(weekdays: [Int], times: [String])
    case monthly(days: [Int], times: [String])
    case once
    case other

    private enum Keys: String, CodingKey { case type, times, weekdays, days }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        let times = (try? c.decodeIfPresent([String].self, forKey: .times)) ?? []
        switch (try? c.decodeIfPresent(String.self, forKey: .type)) ?? "" {
        case "daily":
            self = .daily(times: times)
        case "weekly":
            self = .weekly(weekdays: (try? c.decodeIfPresent([Int].self, forKey: .weekdays)) ?? [], times: times)
        case "monthly":
            self = .monthly(days: (try? c.decodeIfPresent([Int].self, forKey: .days)) ?? [], times: times)
        case "once":
            self = .once
        default:
            self = .other
        }
    }
}

struct ScheduleIndex: Decodable, Equatable {
    let timezone: String?
    /// Why the schedule file was refused; the last good version keeps running.
    let rejected: String?
    let entries: [ScheduleEntry]

    static let empty = ScheduleIndex(timezone: nil, rejected: nil, entries: [])
}

/// `GET /v1/identity`.
struct IdentityIndex: Decodable, Equatable {
    let files: [IdentityFileInfo]
    let schedules: ScheduleIndex

    enum CodingKeys: String, CodingKey { case files, schedules }
}

extension IdentityIndex {
    /// Tolerant of a missing `schedules`: the files are still worth showing.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        files = try c.decode([IdentityFileInfo].self, forKey: .files)
        schedules = (try? c.decodeIfPresent(ScheduleIndex.self, forKey: .schedules)) ?? .empty
    }
}

/// `GET /v1/identity/file`. `truncated` means the server cut the text at its cap.
struct IdentityFile: Decodable, Equatable {
    let id: String
    let kind: IdentityKind
    let modifiedAt: Double
    let text: String
    let truncated: Bool

    enum CodingKeys: String, CodingKey {
        case id, kind, text, truncated
        case modifiedAt = "modified_at"
    }
}

// MARK: - Usage (docs/tasks/usage-visibility.md)

/// `GET /v1/usage`: facts only, no wording. Every block can be null (nothing
/// observed yet), and a block that fails to decode reads as null rather than
/// failing the whole response: a half-empty sheet beats an error for one odd
/// field, and the server's shape is allowed to grow.
struct UsageReport: Decodable, Equatable {
    let plan: UsagePlan?
    let conversation: UsageConversation?
    /// Window name ("five_hour", "seven_day") -> Marlow's own activity in it.
    let marlow: [String: UsageBuckets]?
    /// The raw plan tier ("pro", "max"); nil when the server doesn't know. The
    /// sheet words it (`UsageFormat.planName`), so a new tier needs no box deploy.
    let subscriptionType: String?

    enum CodingKeys: String, CodingKey {
        case plan, conversation, marlow
        case subscriptionType = "subscription_type"
    }

    init(
        plan: UsagePlan? = nil, conversation: UsageConversation? = nil, marlow: [String: UsageBuckets]? = nil,
        subscriptionType: String? = nil
    ) {
        self.plan = plan
        self.conversation = conversation
        self.marlow = marlow
        self.subscriptionType = subscriptionType
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        plan = (try? c.decodeIfPresent(UsagePlan.self, forKey: .plan)) ?? nil
        conversation = (try? c.decodeIfPresent(UsageConversation.self, forKey: .conversation)) ?? nil
        marlow = (try? c.decodeIfPresent([String: Lossy<UsageBuckets>].self, forKey: .marlow))?
            .compactMapValues(\.value)
        subscriptionType = (try? c.decodeIfPresent(String.self, forKey: .subscriptionType)) ?? nil
    }
}

/// Decodes to nil instead of throwing, so one bad entry of a dictionary drops
/// alone.
struct Lossy<T: Decodable>: Decodable {
    let value: T?
    init(from decoder: Decoder) throws { value = try? T(from: decoder) }
}

private extension KeyedDecodingContainer {
    /// A whole number the server may have written as `61000` or `61000.0`.
    func lenientInt(forKey key: Key) -> Int? {
        if let int = try? decodeIfPresent(Int.self, forKey: key) { return int }
        if let double = try? decodeIfPresent(Double.self, forKey: key), double.isFinite { return Int(double) }
        return nil
    }
}

/// The subscription's rate-limit windows, as of the last time Marlow ran.
struct UsagePlan: Decodable, Equatable {
    let observedAt: Double?
    let status: String?
    /// Every window the server passed through; the sheet only shows the
    /// names it has wording for.
    let windows: [String: UsageWindow]

    enum CodingKeys: String, CodingKey {
        case status, windows
        case observedAt = "observed_at"
    }

    init(observedAt: Double?, status: String? = "allowed", windows: [String: UsageWindow]) {
        self.observedAt = observedAt
        self.status = status
        self.windows = windows
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        observedAt = try? c.decodeIfPresent(Double.self, forKey: .observedAt)
        status = try? c.decodeIfPresent(String.self, forKey: .status)
        windows = ((try? c.decodeIfPresent([String: Lossy<UsageWindow>].self, forKey: .windows)) ?? nil)?
            .compactMapValues(\.value) ?? [:]
    }
}

struct UsageWindow: Decodable, Equatable {
    /// A fraction, 0...1 (the server sends what the CLI reported).
    let utilization: Double
    let resetsAt: Double?

    enum CodingKeys: String, CodingKey {
        case utilization
        case resetsAt = "resets_at"
    }

    init(utilization: Double, resetsAt: Double?) {
        self.utilization = utilization
        self.resetsAt = resetsAt
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        utilization = try c.decode(Double.self, forKey: .utilization)
        resetsAt = try? c.decodeIfPresent(Double.self, forKey: .resetsAt)
    }
}

/// The attended conversation's size and history.
struct UsageConversation: Decodable, Equatable {
    let contextTokens: Int?
    let observedAt: Double?
    let compactionThresholdTokens: Int?
    let startedAt: Double?
    let compactions: Int
    /// Absent on a server that predates the field, which reads as not retired.
    let retired: Bool

    enum CodingKeys: String, CodingKey {
        case compactions, retired
        case contextTokens = "context_tokens"
        case observedAt = "observed_at"
        case compactionThresholdTokens = "compaction_threshold_tokens"
        case startedAt = "started_at"
    }

    init(
        contextTokens: Int?, observedAt: Double? = nil, compactionThresholdTokens: Int?,
        startedAt: Double?, compactions: Int = 0, retired: Bool = false
    ) {
        self.contextTokens = contextTokens
        self.observedAt = observedAt
        self.compactionThresholdTokens = compactionThresholdTokens
        self.startedAt = startedAt
        self.compactions = compactions
        self.retired = retired
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        contextTokens = c.lenientInt(forKey: .contextTokens)
        observedAt = try? c.decodeIfPresent(Double.self, forKey: .observedAt)
        compactionThresholdTokens = c.lenientInt(forKey: .compactionThresholdTokens)
        startedAt = try? c.decodeIfPresent(Double.self, forKey: .startedAt)
        compactions = c.lenientInt(forKey: .compactions) ?? 0
        retired = (try? c.decodeIfPresent(Bool.self, forKey: .retired)) ?? false
    }
}

/// Marlow's turns in one window, by what started them.
struct UsageBuckets: Decodable, Equatable {
    let chat: UsageBucket
    let scheduled: UsageBucket
    let compaction: UsageBucket

    enum CodingKeys: String, CodingKey { case chat, scheduled, compaction }

    init(chat: UsageBucket = .zero, scheduled: UsageBucket = .zero, compaction: UsageBucket = .zero) {
        self.chat = chat
        self.scheduled = scheduled
        self.compaction = compaction
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        chat = (try? c.decodeIfPresent(UsageBucket.self, forKey: .chat)) ?? .zero
        scheduled = (try? c.decodeIfPresent(UsageBucket.self, forKey: .scheduled)) ?? .zero
        compaction = (try? c.decodeIfPresent(UsageBucket.self, forKey: .compaction)) ?? .zero
    }
}

struct UsageBucket: Decodable, Equatable {
    let turns: Int
    /// A relative proxy for work done, not a cost and not a share of the plan.
    let weightedTokens: Int

    static let zero = UsageBucket(turns: 0, weightedTokens: 0)

    enum CodingKeys: String, CodingKey {
        case turns
        case weightedTokens = "weighted_tokens"
    }

    init(turns: Int, weightedTokens: Int) {
        self.turns = turns
        self.weightedTokens = weightedTokens
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        turns = c.lenientInt(forKey: .turns) ?? 0
        weightedTokens = c.lenientInt(forKey: .weightedTokens) ?? 0
    }
}

// MARK: - Ideas (docs/tasks/ideas-panel.md)

/// One open idea from `GET /v1/ideas`. Facts only: the grouping, the expiry
/// and the wording are `IdeaFormat`'s. Dates are the server's bare
/// `YYYY-MM-DD` strings, left as sent. `section` stays a string so a value this
/// build doesn't know decodes (and is dropped by `IdeaFormat`) rather than
/// failing the list.
struct Idea: Decodable, Equatable, Identifiable {
    let id: String
    let section: String
    let emoji: String
    let title: String
    let plan: String
    let category: String
    /// What grounded it; the server requires it for `now` ideas only.
    let source: String?
    /// The last day the idea is any use.
    let until: String?
    let added: String
    /// At most one idea at a time is the spotlight.
    let spotlight: Bool

    init(
        id: String, section: String, emoji: String, title: String, plan: String, category: String,
        source: String? = nil, until: String? = nil, added: String, spotlight: Bool = false
    ) {
        self.id = id
        self.section = section
        self.emoji = emoji
        self.title = title
        self.plan = plan
        self.category = category
        self.source = source
        self.until = until
        self.added = added
        self.spotlight = spotlight
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        section = try c.decode(String.self, forKey: .section)
        emoji = try c.decode(String.self, forKey: .emoji)
        title = try c.decode(String.self, forKey: .title)
        plan = try c.decode(String.self, forKey: .plan)
        category = try c.decode(String.self, forKey: .category)
        source = try c.decodeIfPresent(String.self, forKey: .source)
        until = try c.decodeIfPresent(String.self, forKey: .until)
        added = try c.decode(String.self, forKey: .added)
        // Always a bool from the server; a missing one reads as "not".
        spotlight = try c.decodeIfPresent(Bool.self, forKey: .spotlight) ?? false
    }

    enum CodingKeys: String, CodingKey {
        case id, section, emoji, title, plan, category, source, until, added, spotlight
    }
}

/// `GET /v1/preferences`: what every client shares (`PreferenceSync`).
struct Preferences: Decodable, Equatable {
    let face: Int?
    let effort: String?
    /// "Your day" ("HH:MM", 24h; `YourDay`). Optional so a box that predates
    /// the setting still decodes; a current one never sends null.
    let dayStart: String?
    let dayEnd: String?
    /// Hours between check-ins, 0 = off. Nil from a box that predates it.
    let checkinEveryHours: Int?

    init(
        face: Int?, effort: String?, dayStart: String? = nil, dayEnd: String? = nil, checkinEveryHours: Int? = nil
    ) {
        self.face = face
        self.effort = effort
        self.dayStart = dayStart
        self.dayEnd = dayEnd
        self.checkinEveryHours = checkinEveryHours
    }

    enum CodingKeys: String, CodingKey {
        case face, effort
        case dayStart = "day_start"
        case dayEnd = "day_end"
        case checkinEveryHours = "checkin_every_hours"
    }
}

/// `GET /v1/ideas`. A malformed idea drops alone: a panel missing one card
/// beats an empty one for the sake of an odd field.
struct IdeasResponse: Decodable {
    let ideas: [Idea]

    enum CodingKeys: String, CodingKey { case ideas }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ideas = try c.decode([Lossy<Idea>].self, forKey: .ideas).compactMap(\.value)
    }
}
