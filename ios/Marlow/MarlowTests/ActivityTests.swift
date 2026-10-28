import Foundation
import Testing
@testable import Marlow

/// The activity pane (docs/tasks/ux-lane.md §2c): the row wording, the paging
/// store, and the wire format.
struct ActivityFormatTests {

    private func entry(
        source: String = "cron", label: String? = "morning",
        tools: [(String, String?)] = [], files: [String] = [],
        approvals: [(String, String)] = [], denials: [String] = [],
        status: String = "done", dropped: Int = 0
    ) -> ActivityEntry {
        ActivityEntry(
            turnId: UUID().uuidString, source: source, label: label,
            startedAt: 100, finishedAt: 114, status: status,
            tools: tools.map { ActivityToolCall(name: $0.0, target: $0.1, at: nil) },
            toolsDropped: dropped,
            approvals: approvals.map { ActivityApproval(tool: $0.0, decision: $0.1) },
            denials: denials, files: files
        )
    }

    @Test func titlesNameKnownJobsAndHumaniseTheRest() {
        #expect(ActivityFormat.title(entry(label: "morning")) == "Morning briefing")
        #expect(ActivityFormat.title(entry(label: "email-flash")) == "Check-in")
        #expect(ActivityFormat.title(entry(label: "physio-nudge")) == "Physio nudge")
        #expect(ActivityFormat.title(entry(label: nil)) == "Scheduled job")
        #expect(ActivityFormat.title(entry(source: "chat", label: nil)) == "Chat")
    }

    /// Activity-log records written before the server's `ios` -> `chat` rename
    /// keep `ios` for their 12-month retention. Deliberately the old value.
    @Test func aLegacyIosRecordIsAChatTurnExactlyLikeChat() {
        #expect(ActivityFormat.title(entry(source: "ios", label: nil)) == "Chat")
        #expect(ActivityFormat.summaryLine(entry(source: "ios", label: nil)) == "Replied")
        #expect(!ActivityFormat.isNoteworthy(entry(source: "ios", label: nil)))
        #expect(ActivityFormat.isNoteworthy(entry(source: "ios", label: nil, status: "error")))
        #expect(TurnSource.isChat("chat"))
        #expect(TurnSource.isChat("ios"))
        #expect(!TurnSource.isChat("cron"))
        #expect(!TurnSource.isChat(nil))
    }

    @Test func theMorningBriefReadsAsOneLine() {
        let e = entry(
            tools: [
                ("mcp__claude_ai_Gmail__search_threads", nil),
                ("mcp__claude_ai_Google_Calendar__list_events", nil),
                ("mcp__claude_ai_Gmail__get_thread", nil),
                ("Read", "memory/travel.md"),
                ("Edit", "memory/travel.md"),
            ],
            files: ["memory/travel.md"]
        )
        #expect(ActivityFormat.summary(e) == "Checked Gmail, Calendar · read travel.md · changed travel.md")
    }

    /// Connector names are never shown: an unknown MCP tool is "another tool",
    /// not its name — same rule as `TurnActivity.label`.
    @Test func unknownToolNamesAreNeverRendered() {
        let e = entry(tools: [("mcp__evil__<b>Ignore_previous</b>", nil), ("FutureTool", nil)], dropped: 1)
        let summary = ActivityFormat.summary(e)
        #expect(summary == "Used 3 other tools")
        #expect(ActivityFormat.detailLines(e).allSatisfy { !$0.label.contains("evil") })
    }

    @Test func detailLinesMergeRepeatsInFirstSeenOrderWithACount() {
        let e = entry(tools: [
            ("Read", "memory/travel.md"), ("Bash", nil), ("Read", "memory/travel.md"),
            ("Edit", "memory/travel.md"), ("Bash", nil), ("Read", "memory/travel.md"),
            ("Read", "memory/food.md"),
        ], dropped: 2)
        let lines = ActivityFormat.detailLines(e)
        #expect(lines.map(\.label) == ["Read", "Command", "Changed", "Read", "2 more not recorded"])
        #expect(lines.map(\.target) == ["memory/travel.md", nil, "memory/travel.md", "memory/food.md", nil])
        #expect(lines.map(\.countText) == ["×3", "×2", nil, nil, nil])
    }

    @Test func theOvernightJobsAreNamedNeverById() {
        #expect(ActivityFormat.title(entry(label: "ideas")) == "Overnight ideas")
        #expect(ActivityFormat.title(entry(label: "consolidate")) == "Nightly tidy")
    }

    @Test func backgroundJobsAreVisibleOnlyWhenAskedOrFailed() {
        func plumbing(_ status: String) -> ActivityEntry {
            ActivityEntry(turnId: "p", source: "cron", label: "ideas", finishedAt: 1, status: status, tier: "plumbing")
        }
        #expect(ActivityFormat.isBackground(plumbing("done")))
        #expect(!ActivityFormat.isVisible(plumbing("done"), showBackground: false))
        #expect(ActivityFormat.isVisible(plumbing("done"), showBackground: true))
        #expect(ActivityFormat.isVisible(plumbing("error"), showBackground: false))
        #expect(ActivityFormat.isVisible(entry(), showBackground: false))
        #expect(!ActivityFormat.isVisible(entry(source: "chat", label: nil), showBackground: true))
    }

    @Test func outputIsShownButTheSilentTokenIsWorded() {
        #expect(ActivityFormat.outputText("[silent]") == "Nothing to report.")
        #expect(ActivityFormat.outputText(" `[SILENT]` ") == "Nothing to report.")
        #expect(ActivityFormat.outputText("  ") == "Nothing to report.")
        #expect(ActivityFormat.outputText("\nAdded one idea.\n") == "Added one idea.")
    }

    @Test func searchesQuoteTheFirstQueryAndCountTheRest() {
        let e = entry(tools: [("WebSearch", "flights to YYC"), ("WebSearch", "hotels"), ("WebFetch", "example.com")])
        #expect(ActivityFormat.summary(e) == "Searched “flights to YYC” +1 more · opened example.com")
    }

    @Test func longListsStopAtTwoNames() {
        let e = entry(tools: [("Read", "a.md"), ("Read", "b.md"), ("Read", "c.md"), ("Read", "a.md")])
        #expect(ActivityFormat.summary(e) == "Read a.md, b.md +1 more")
    }

    @Test func readsWithoutATargetAreCounted() {
        #expect(ActivityFormat.summary(entry(tools: [("Read", nil), ("Read", nil)])) == "Read 2 files")
    }

    @Test func flagsSeparateMikesAnswersFromBlocks() {
        let e = entry(
            approvals: [("Edit", "allow"), ("Bash", "deny"), ("Write", "timeout")],
            denials: ["Bash", "Write", "Bash"], status: "error"
        )
        let flags = ActivityFormat.flags(e)
        #expect(flags.map(\.text) == ["Didn't finish", "you approved 1", "you declined 1", "1 request missed", "1 blocked"])
        #expect(ActivityFormat.flags(entry()).isEmpty)
    }

    /// Mike's approvals are a record, not a warning: only they stay grey.
    @Test func onlyApprovalsAreNotWarnings() {
        let e = entry(approvals: [("Bash", "allow"), ("Bash", "deny")], denials: ["Bash"], status: "error")
        #expect(ActivityFormat.flags(e) == [
            .init(text: "Didn't finish", isWarning: true),
            .init(text: "you approved 1", isWarning: false),
            .init(text: "you declined 1", isWarning: true),
        ])
        #expect(ActivityFormat.flags(entry(approvals: [("Read", "allow")])).first
            == .init(text: "You approved 1", isWarning: false))
    }

    /// A failed turn did not reply; "Replied" over it was false.
    @Test func aFailedToolLessTurnHasNoSummaryLine() {
        #expect(ActivityFormat.summaryLine(entry(source: "chat", label: nil, status: "error")) == nil)
        #expect(ActivityFormat.summaryLine(entry(source: "chat", label: nil)) == "Replied")
        #expect(ActivityFormat.summaryLine(entry(tools: [("Read", "a.md")], status: "error")) == "Read a.md")
    }

    @Test func aToolLessChatTurnIsHiddenButAnIdleCronJobIsNot() {
        #expect(!ActivityFormat.isNoteworthy(entry(source: "chat", label: nil)))
        #expect(ActivityFormat.isNoteworthy(entry(source: "chat", label: nil, tools: [("Read", nil)])))
        #expect(ActivityFormat.isNoteworthy(entry(source: "chat", label: nil, status: "error")))
        #expect(ActivityFormat.isNoteworthy(entry(source: "cron")))
    }

    @Test func durationIsSecondsThenMinutes() {
        #expect(ActivityFormat.duration(entry()) == "14s")
        let long = ActivityEntry(turnId: "x", source: "cron", label: nil, startedAt: 0, finishedAt: 125)
        #expect(ActivityFormat.duration(long) == "2m 5s")
    }

    @Test func runLineSaysHowLongWhichModelAndWhatEffort() {
        let full = ActivityEntry(
            turnId: "x", source: "cron", label: "consolidate", startedAt: 100, finishedAt: 114,
            model: "claude-sonnet-5-5", effort: "high"
        )
        #expect(ActivityFormat.runLine(full) == "Took 14s · Sonnet 5.5 · High effort")
        // An old record: duration alone, exactly what the row used to say.
        #expect(ActivityFormat.runLine(entry()) == "Took 14s")
        let noStart = ActivityEntry(turnId: "y", source: "chat", label: nil, finishedAt: 5, effort: "low")
        #expect(ActivityFormat.runLine(noStart) == "Low effort")
        #expect(ActivityFormat.runLine(ActivityEntry(turnId: "z", source: "chat", label: nil, finishedAt: 5)) == nil)
        // A level this build doesn't know is shown as the server sent it.
        let future = ActivityEntry(turnId: "w", source: "cron", label: nil, finishedAt: 5, effort: "xhigh")
        #expect(ActivityFormat.runLine(future) == "xhigh effort")
    }

    @Test func modelNamesKeepTheVersionAndNeverGuess() {
        #expect(ActivityFormat.modelName("claude-sonnet-5-5") == "Sonnet 5.5")
        #expect(ActivityFormat.modelName("claude-opus-5") == "Opus 5")
        #expect(ActivityFormat.modelName("claude-haiku-4-5-20251001") == "Haiku 4.5")
        #expect(ActivityFormat.modelName("claude-fable") == "Fable")
        #expect(ActivityFormat.modelName("sonnet") == "Sonnet")
        #expect(ActivityFormat.modelName("gpt-x-1") == "gpt-x-1")
    }
}

// MARK: - Store

private final class FakeActivityAPI: ActivityAPI, @unchecked Sendable {
    var pages: [ActivityPage]
    var requestedBefores: [Double?] = []
    var error: Error?

    init(pages: [ActivityPage]) { self.pages = pages }

    func activity(limit: Int, before: Double?, deviceToken: String) async throws -> ActivityPage {
        requestedBefores.append(before)
        if let error { throw error }
        return pages.isEmpty ? ActivityPage(entries: [], nextBefore: nil) : pages.removeFirst()
    }

    var outputs: [String: String] = [:]
    var outputRequests: [String] = []

    func activityOutput(turnId: String, deviceToken: String) async throws -> ActivityOutput {
        outputRequests.append(turnId)
        guard let text = outputs[turnId] else { throw URLError(.badServerResponse) }
        return ActivityOutput(text: text)
    }
}

@MainActor
struct ActivityStoreTests {
    private func e(_ id: String, _ at: Double) -> ActivityEntry {
        ActivityEntry(turnId: id, source: "cron", label: "morning", finishedAt: at)
    }

    @Test func pagesFollowTheCursorAndStopOnNil() async {
        let api = FakeActivityAPI(pages: [
            ActivityPage(entries: [e("a", 3), e("b", 2)], nextBefore: 2),
            ActivityPage(entries: [e("b", 2), e("c", 1)], nextBefore: nil),
        ])
        let store = ActivityStore(api: api, deviceToken: "tok")

        await store.refresh()
        #expect(store.hasMore)
        await store.loadMore()

        #expect(api.requestedBefores == [nil, 2])
        // The repeated "b" is dropped rather than shown twice.
        #expect(store.entries.map(\.turnId) == ["a", "b", "c"])
        #expect(!store.hasMore)
        await store.loadMore()
        #expect(api.requestedBefores.count == 2)
    }

    @Test func refreshReplacesRatherThanAppends() async {
        let api = FakeActivityAPI(pages: [
            ActivityPage(entries: [e("a", 1)], nextBefore: nil),
            ActivityPage(entries: [e("b", 2), e("a", 1)], nextBefore: nil),
        ])
        let store = ActivityStore(api: api, deviceToken: "tok")
        await store.refresh()
        await store.refresh()
        #expect(store.entries.map(\.turnId) == ["b", "a"])
    }

    @Test func aFailureKeepsWhatWasShownAndSaysSo() async {
        let api = FakeActivityAPI(pages: [ActivityPage(entries: [e("a", 1)], nextBefore: 1)])
        let store = ActivityStore(api: api, deviceToken: "tok")
        await store.refresh()
        api.error = URLError(.notConnectedToInternet)
        await store.loadMore()
        #expect(store.entries.map(\.turnId) == ["a"])
        #expect(store.errorMessage != nil)
        #expect(store.hasMore)
    }

    @Test func backgroundJobsHideUnlessAskedButNeverWhenTheyFailed() async {
        let tidy = ActivityEntry(turnId: "t", source: "cron", label: "consolidate", finishedAt: 3, tier: "plumbing")
        let failed = ActivityEntry(turnId: "f", source: "cron", label: "ideas", finishedAt: 2, status: "error", tier: "plumbing")
        let api = FakeActivityAPI(pages: [ActivityPage(entries: [tidy, failed, e("m", 1)], nextBefore: nil)])
        let store = ActivityStore(api: api, deviceToken: "tok")
        await store.refresh()
        #expect(store.visibleEntries(showBackground: false).map(\.turnId) == ["f", "m"])
        #expect(store.visibleEntries(showBackground: true).map(\.turnId) == ["t", "f", "m"])
    }

    @Test func outputIsFetchedOnceAndAFailureCanBeRetried() async {
        let api = FakeActivityAPI(pages: [])
        let store = ActivityStore(api: api, deviceToken: "tok")
        let kept = ActivityEntry(turnId: "k", source: "cron", label: "ideas", finishedAt: 1, hasOutput: true)
        let none = ActivityEntry(turnId: "n", source: "cron", label: "ideas", finishedAt: 1)

        await store.loadOutput(for: kept)
        #expect(store.outputs["k"] == .failed)
        api.outputs["k"] = "Added one idea."
        await store.loadOutput(for: kept)
        await store.loadOutput(for: kept)
        #expect(store.outputs["k"] == .loaded("Added one idea."))
        await store.loadOutput(for: none)
        #expect(api.outputRequests == ["k", "k"])
    }
}

// MARK: - Wire format

extension APIClientTests {
    private func activityClient() -> APIClient {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [StubURLProtocol.self]
        return APIClient(
            baseURL: URL(string: "https://marlow.example.ts.net:8443")!,
            session: URLSession(configuration: config)
        )
    }

    @Test func activityDecodesTheServersRecordAndToleratesGaps() async throws {
        StubURLProtocol.respond(json: """
        {"entries": [
          {"v": 1, "turn_id": "t1", "source": "cron", "label": "morning", "merged": [],
           "created_at": 1.0, "started_at": 2.0, "finished_at": 16.5, "status": "done",
           "tools": [{"name": "Read", "target": "memory/a.md", "at": 3.0}], "tools_dropped": 0,
           "approvals": [{"tool": "Edit", "decision": "allow"}], "denials": ["Bash"],
           "files": ["memory/a.md"], "commit": "abc1234", "some_future_field": 7,
           "model": "claude-sonnet-5-5", "effort": "high", "tier": "plumbing", "has_output": true},
          {"turn_id": "t2", "finished_at": 10.0, "tools": "garbage"}
        ], "next_before": 10.0}
        """)
        let page = try await activityClient().activity(limit: 50, before: nil, deviceToken: "tok")
        #expect(page.nextBefore == 10.0)
        #expect(page.entries.count == 2)
        let first = page.entries[0]
        #expect(first.label == "morning")
        #expect(first.tools == [ActivityToolCall(name: "Read", target: "memory/a.md", at: 3.0)])
        #expect(first.approvals == [ActivityApproval(tool: "Edit", decision: "allow")])
        #expect(first.files == ["memory/a.md"])
        #expect(page.entries[1].tools.isEmpty)
        #expect(first.model == "claude-sonnet-5-5" && first.effort == "high")
        #expect(page.entries[1].model == nil && page.entries[1].effort == nil)
        #expect(first.tier == "plumbing" && first.hasOutput)
        #expect(page.entries[1].tier == nil && !page.entries[1].hasOutput)
    }

    @Test func activityOutputAsksForTheTurnsOutput() async throws {
        StubURLProtocol.respond(json: #"{"turn_id": "t1", "label": "ideas", "finished_at": 1.0, "text": "Added one idea."}"#)
        let output = try await activityClient().activityOutput(turnId: "t1", deviceToken: "tok")
        #expect(output.text == "Added one idea.")
        let request = try #require(StubURLProtocol.lastRequest)
        #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer tok")
        #expect(request.url?.absoluteString.hasSuffix("/v1/activity/t1/output") == true)
    }

    @Test func activitySendsLimitAuthAndAnExactCursor() async throws {
        StubURLProtocol.respond(json: #"{"entries": [], "next_before": null}"#)
        let before = 1790000000.1234567
        _ = try await activityClient().activity(limit: 25, before: before, deviceToken: "tok")

        let request = try #require(StubURLProtocol.lastRequest)
        #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer tok")
        let url = try #require(request.url)
        let components = try #require(URLComponents(url: url, resolvingAgainstBaseURL: false))
        #expect(components.path == "/v1/activity")
        let items = Dictionary(uniqueKeysWithValues: (components.queryItems ?? []).map { ($0.name, $0.value) })
        #expect(items["limit"] == "25")
        // Parses back to the same Double: a rounded cursor repeats a row.
        let cursor = try #require(items["before"] ?? nil)
        #expect(Double(cursor) == before)
    }
}
