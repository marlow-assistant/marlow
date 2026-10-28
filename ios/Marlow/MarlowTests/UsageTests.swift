import Foundation
import Testing
@testable import Marlow

// MARK: - Fixtures

/// Sunday 2026-10-04, in UTC and en_US so the strings are the same on every
/// machine.
private let utc: Calendar = {
    var c = Calendar(identifier: .gregorian)
    c.timeZone = TimeZone(identifier: "UTC")!
    c.locale = Locale(identifier: "en_US")
    return c
}()

private func at(_ day: Int, _ hour: Int, _ minute: Int = 0) -> Date {
    utc.date(from: DateComponents(year: 2026, month: 10, day: day, hour: hour, minute: minute))!
}

private func epoch(_ day: Int, _ hour: Int, _ minute: Int = 0) -> Double {
    at(day, hour, minute).timeIntervalSince1970
}

private let now = at(4, 19, 50)

private func report(five: Double?, fiveResets: Double? = nil, seven: Double? = nil, sevenResets: Double? = nil) -> UsageReport {
    var windows: [String: UsageWindow] = [:]
    if let five { windows["five_hour"] = UsageWindow(utilization: five, resetsAt: fiveResets ?? epoch(4, 20, 59)) }
    if let seven { windows["seven_day"] = UsageWindow(utilization: seven, resetsAt: sevenResets ?? epoch(9, 4, 59)) }
    return UsageReport(plan: UsagePlan(observedAt: epoch(4, 19, 44), windows: windows))
}

private func bucket(_ turns: Int, _ weighted: Int = 0) -> UsageBucket {
    UsageBucket(turns: turns, weightedTokens: weighted)
}

// MARK: - Wording

struct UsageFormatTests {
    @Test func windowTitles() {
        #expect(UsageFormat.windowTitle("five_hour") == "This 5 hours")
        #expect(UsageFormat.windowTitle("seven_day") == "This week")
        #expect(UsageFormat.windowTitle("thirty_day") == nil)
    }

    @Test func planRowsShowPercentAndResetTodayOrWithTheWeekday() {
        let rows = UsageFormat.planRows(report(five: 0.14, seven: 0.08).plan!, now: now, calendar: utc)
        #expect(rows.map(\.title) == ["This 5 hours", "This week"])
        #expect(rows[0].detail == "14% used · resets 8:59 PM")
        #expect(rows[1].detail == "8% used · resets Fri 4:59 AM")
        #expect(rows[0].fraction == 0.14)
        #expect(rows.allSatisfy { !$0.isHigh })
    }

    @Test func unknownWindowsAreIgnoredAndOrderIsFixed() {
        let plan = UsagePlan(observedAt: nil, windows: [
            "seven_day": UsageWindow(utilization: 0.5, resetsAt: nil),
            "thirty_day": UsageWindow(utilization: 0.9, resetsAt: nil),
            "five_hour": UsageWindow(utilization: 0.1, resetsAt: nil),
        ])
        let rows = UsageFormat.planRows(plan, now: now, calendar: utc)
        #expect(rows.map(\.id) == ["five_hour", "seven_day"])
        // No reset time: the percentage alone.
        #expect(rows[0].detail == "10% used")
    }

    @Test func aHighWindowTintsItsBarAndAWindowThatHasResetDoesNot() {
        let high = UsageFormat.planRows(report(five: 0.93).plan!, now: now, calendar: utc)
        #expect(high[0].isHigh)
        let reset = UsageFormat.planRows(report(five: 0.93, fiveResets: epoch(4, 19, 0)).plan!, now: now, calendar: utc)
        #expect(!reset[0].isHigh)
        #expect(reset[0].detail == "93% at last reading · has since reset")
    }

    @Test func asOfTimeIsTheBareTimeAndGainsTheWeekdayWhenNotToday() {
        let today = UsagePlan(observedAt: epoch(4, 19, 44), windows: [:])
        #expect(UsageFormat.asOfTime(today, now: now, calendar: utc) == "7:44 PM")
        let old = UsagePlan(observedAt: epoch(2, 9, 5), windows: [:])
        #expect(UsageFormat.asOfTime(old, now: now, calendar: utc) == "Fri 9:05 AM")
        #expect(UsageFormat.asOfTime(UsagePlan(observedAt: nil, windows: [:]), now: now, calendar: utc) == nil)
    }

    @Test func planNameMapsKnownTiersAndFallsBackForTheRest() {
        #expect(UsageFormat.planName("pro") == "Claude Pro")
        #expect(UsageFormat.planName("max") == "Claude Max")
        #expect(UsageFormat.planName("PRO") == "Claude Pro")
        #expect(UsageFormat.planName("  Max\n") == "Claude Max")
        #expect(UsageFormat.planName(nil) == "Your Claude plan")
        #expect(UsageFormat.planName("") == "Your Claude plan")
        #expect(UsageFormat.planName("team") == "Your Claude plan")
    }

    @Test func planFootnoteNamesThePlanOrReadsGenerically() {
        #expect(UsageFormat.planFootnote(planName: UsageFormat.planName("pro"))
            == "Marlow shares your Claude Pro limits with everything else you use Claude for.")
        #expect(UsageFormat.planFootnote(planName: UsageFormat.planName("max"))
            == "Marlow shares your Claude Max limits with everything else you use Claude for.")
        #expect(UsageFormat.planFootnote(planName: UsageFormat.planName(nil))
            == "Marlow shares your Claude plan's limits with everything else you use Claude for.")
    }

    // MARK: Conversation

    @Test func tokenCountRoundsToKAndShowsSmallNumbersAsIs() {
        #expect(UsageFormat.tokenCount(36_938) == "37K")
        #expect(UsageFormat.tokenCount(110_000) == "110K")
        #expect(UsageFormat.tokenCount(999) == "999")
        #expect(UsageFormat.tokenCount(0) == "0")
        #expect(UsageFormat.tokenCount(1_000) == "1K")
        #expect(UsageFormat.tokenCount(1_499) == "1K")
        #expect(UsageFormat.tokenCount(1_500) == "2K")
    }

    @Test func conversationSizeAgainstTheThreshold() {
        let c = UsageConversation(
            contextTokens: 36_938, compactionThresholdTokens: 110_000,
            startedAt: epoch(4, 6, 0), compactions: 0
        )
        let s = UsageFormat.conversation(c, now: now, calendar: utc)
        #expect(s.size == "37K tokens")
        #expect(abs((s.fraction ?? 0) - 36_938.0 / 110_000.0) < 1e-9)
        #expect(s.thresholdText == "Marlow tidies up around 110K")
        #expect(s.notice == nil)
        #expect(s.started == "Started 6:00 AM")
        #expect(s.tidiedUp == nil)
    }

    @Test func aSizePastTheThresholdFillsTheBarNoFurther() {
        let c = UsageConversation(contextTokens: 150_000, compactionThresholdTokens: 110_000, startedAt: nil)
        #expect(UsageFormat.conversation(c, now: now, calendar: utc).fraction == 1)
    }

    @Test func aSmallSizeIsShownAsTheNumber() {
        let c = UsageConversation(contextTokens: 640, compactionThresholdTokens: 110_000, startedAt: nil)
        #expect(UsageFormat.conversation(c, now: now, calendar: utc).size == "640 tokens")
    }

    @Test func anUnmeasuredSizeSaysWhenItWillBe() {
        let c = UsageConversation(contextTokens: nil, compactionThresholdTokens: 110_000, startedAt: epoch(4, 6, 0))
        let s = UsageFormat.conversation(c, now: now, calendar: utc)
        #expect(s.size == nil)
        #expect(s.fraction == nil)
        #expect(s.notice == "Size shows after your next message")
        // The rest still reads: the conversation exists, only its size is unknown.
        #expect(s.started == "Started 6:00 AM")
    }

    @Test func startAndTidyUpsShareOneLine() {
        func history(tokens: Int?, started: Double?, compactions: Int) -> String? {
            let c = UsageConversation(
                contextTokens: tokens, compactionThresholdTokens: 110_000, startedAt: started, compactions: compactions
            )
            return UsageFormat.conversation(c, now: now, calendar: utc).history
        }
        #expect(history(tokens: 40_000, started: epoch(4, 6, 0), compactions: 1) == "Started 6:00 AM · tidied up once")
        #expect(history(tokens: 40_000, started: epoch(4, 6, 0), compactions: 3) == "Started 6:00 AM · tidied up 3 times")
        #expect(history(tokens: 40_000, started: epoch(4, 6, 0), compactions: 0) == "Started 6:00 AM")
        #expect(history(tokens: 40_000, started: nil, compactions: 2) == "Tidied up 2 times")
        #expect(history(tokens: 40_000, started: nil, compactions: 0) == nil)
    }

    @Test func anUnmeasuredSizeAfterATidyUpSaysSoAndDoesNotRepeatTheCount() {
        // Mike read the plain "measured on your next message" as a bug right
        // after a compaction: the notice has to say why the size is missing.
        let c = UsageConversation(
            contextTokens: nil, compactionThresholdTokens: 110_000, startedAt: epoch(4, 6, 0), compactions: 2
        )
        let s = UsageFormat.conversation(c, now: now, calendar: utc)
        #expect(s.size == nil)
        #expect(s.notice == "Tidied up · new size after your next message")
        #expect(s.tidiedUp == nil)
        #expect(s.started == "Started 6:00 AM")
    }

    @Test func aRetiredConversationOnlySaysAFreshOneIsComing() {
        let c = UsageConversation(
            contextTokens: nil, compactionThresholdTokens: 110_000, startedAt: epoch(3, 21, 12),
            compactions: 2, retired: true
        )
        let s = UsageFormat.conversation(c, now: now, calendar: utc)
        #expect(s.notice == "A fresh conversation starts with your next message.")
        #expect(s.size == nil && s.fraction == nil && s.started == nil && s.tidiedUp == nil)
        // Takes precedence over a size too, not only over a missing one.
        let measured = UsageConversation(
            contextTokens: 50_000, compactionThresholdTokens: 110_000, startedAt: nil, retired: true
        )
        let m = UsageFormat.conversation(measured, now: now, calendar: utc)
        #expect(m.notice == "A fresh conversation starts with your next message.")
        #expect(m.size == nil)
    }

    @Test func startedReadsTodayYesterdayOrWithTheWeekday() {
        func started(_ date: Date) -> String? {
            let c = UsageConversation(contextTokens: 1, compactionThresholdTokens: nil, startedAt: date.timeIntervalSince1970)
            return UsageFormat.conversation(c, now: now, calendar: utc).started
        }
        #expect(started(at(4, 6, 0)) == "Started 6:00 AM")
        #expect(started(at(3, 21, 12)) == "Started yesterday 9:12 PM")
        #expect(started(at(2, 9, 5)) == "Started Fri 9:05 AM")
    }

    @Test func tidyUpsAreCountedInWords() {
        func tidied(_ n: Int) -> String? {
            UsageFormat.conversation(
                UsageConversation(contextTokens: 1, compactionThresholdTokens: nil, startedAt: nil, compactions: n),
                now: now, calendar: utc
            ).tidiedUp
        }
        #expect(tidied(0) == nil)
        #expect(tidied(1) == "Tidied up once")
        #expect(tidied(3) == "Tidied up 3 times")
    }

    @Test func aMissingThresholdDropsTheBarNotTheSize() {
        let c = UsageConversation(contextTokens: 61_000, compactionThresholdTokens: nil, startedAt: nil)
        let s = UsageFormat.conversation(c, now: now, calendar: utc)
        #expect(s.size == "61K tokens")
        #expect(s.fraction == nil)
        #expect(s.thresholdText == nil)
    }

    // MARK: Marlow's part

    @Test func countsOmitZeroBucketsAndNameTidyUpsOnlyWhenThereAreAny() {
        #expect(UsageFormat.counts(UsageBuckets(chat: bucket(12), scheduled: bucket(6))) == "12 chat · 6 scheduled")
        #expect(UsageFormat.counts(UsageBuckets(scheduled: bucket(4))) == "4 scheduled")
        #expect(UsageFormat.counts(UsageBuckets(chat: bucket(2), compaction: bucket(1))) == "2 chat · 1 tidy-up")
        #expect(UsageFormat.counts(UsageBuckets(compaction: bucket(3))) == "3 tidy-ups")
        #expect(UsageFormat.counts(UsageBuckets()) == nil)
    }

    @Test func marlowPartsFollowWindowOrderAndSkipUnknownWindows() {
        let parts = UsageFormat.marlowParts([
            "seven_day": UsageBuckets(chat: bucket(5, 50)),
            "five_hour": UsageBuckets(),
            "thirty_day": UsageBuckets(chat: bucket(1, 1)),
        ])
        #expect(parts.map(\.title) == ["This 5 hours", "This week"])
        #expect(parts[0].counts == nil)
        #expect(parts[1].counts == "5 chat")
    }

    // MARK: High usage

    @Test func theThresholdBoundaryIsNinetyPercentInclusive() {
        #expect(UsageFormat.highUsageThreshold == 0.90)
        #expect(UsageFormat.highUsage(report(five: 0.899), now: now, calendar: utc) == nil)
        #expect(UsageFormat.highUsage(report(five: 0.90), now: now, calendar: utc) != nil)
    }

    @Test func highUsagePicksTheMostUsedWindowAndFormatsIt() throws {
        let r = report(five: 0.92, seven: 0.97)
        let high = try #require(UsageFormat.highUsage(r, now: now, calendar: utc))
        #expect(high == UsageFormat.HighUsage(windowTitle: "This week", percent: 97, resetText: "Fri 4:59 AM"))
        #expect(UsageFormat.highUsageText(high) == "97% of this week used — resets Fri 4:59 AM.")

        let five = try #require(UsageFormat.highUsage(report(five: 0.93), now: now, calendar: utc))
        #expect(UsageFormat.highUsageText(five) == "93% of this 5 hours used — resets 8:59 PM.")
    }

    @Test func aHighWindowThatHasResetDoesNotCount() {
        let r = report(five: 0.95, fiveResets: epoch(4, 19, 0))
        #expect(UsageFormat.highUsage(r, now: now, calendar: utc) == nil)
        // The other window still can.
        let both = report(five: 0.95, fiveResets: epoch(4, 19, 0), seven: 0.91)
        #expect(UsageFormat.highUsage(both, now: now, calendar: utc)?.windowTitle == "This week")
    }

    @Test func aHighWindowWithNoResetTimeDoesNotCount() {
        let plan = UsagePlan(observedAt: nil, windows: ["five_hour": UsageWindow(utilization: 0.99, resetsAt: nil)])
        #expect(UsageFormat.highUsage(UsageReport(plan: plan), now: now, calendar: utc) == nil)
    }

    @Test func noReportOrPlanIsNoHighUsage() {
        #expect(UsageFormat.highUsage(nil, now: now, calendar: utc) == nil)
        #expect(UsageFormat.highUsage(UsageReport(), now: now, calendar: utc) == nil)
    }

    @Test func unknownWindowsNeverTriggerTheRow() {
        let plan = UsagePlan(observedAt: nil, windows: ["thirty_day": UsageWindow(utilization: 0.99, resetsAt: epoch(9, 1))])
        #expect(UsageFormat.highUsage(UsageReport(plan: plan), now: now, calendar: utc) == nil)
    }
}

// MARK: - Decoding

struct UsageDecodingTests {
    private func decode(_ json: String) throws -> UsageReport {
        try JSONDecoder().decode(UsageReport.self, from: Data(json.utf8))
    }

    @Test func theServersExamplePayloadDecodes() throws {
        let r = try decode("""
        {"plan": {"observed_at": 1791163586.47, "status": "allowed",
                  "windows": {"five_hour": {"utilization": 0.14, "resets_at": 1791167246},
                              "seven_day": {"utilization": 0.08, "resets_at": 1791250046}}},
         "conversation": {"session_id": "abc", "context_tokens": 61000, "observed_at": 1791163616.47,
                          "compaction_threshold_tokens": 110000, "started_at": 1791140000.0,
                          "compactions": 1, "retired": false},
         "marlow": {"five_hour": {"chat": {"turns": 2, "weighted_tokens": 400},
                                  "scheduled": {"turns": 2, "weighted_tokens": 17},
                                  "compaction": {"turns": 1, "weighted_tokens": 1000}},
                    "seven_day": {"chat": {"turns": 9, "weighted_tokens": 4000},
                                  "scheduled": {"turns": 0, "weighted_tokens": 0},
                                  "compaction": {"turns": 0, "weighted_tokens": 0}}}}
        """)
        #expect(r.plan?.observedAt == 1791163586.47)
        #expect(r.plan?.status == "allowed")
        #expect(r.plan?.windows["five_hour"] == UsageWindow(utilization: 0.14, resetsAt: 1791167246))
        #expect(r.plan?.windows["seven_day"]?.utilization == 0.08)
        #expect(r.conversation == UsageConversation(
            contextTokens: 61000, observedAt: 1791163616.47, compactionThresholdTokens: 110000,
            startedAt: 1791140000.0, compactions: 1, retired: false
        ))
        #expect(r.marlow?["five_hour"] == UsageBuckets(chat: bucket(2, 400), scheduled: bucket(2, 17), compaction: bucket(1, 1000)))
        #expect(r.marlow?["seven_day"]?.chat == bucket(9, 4000))
    }

    @Test func subscriptionTypeDecodesWhenPresentAndIsNilWhenAbsentOrNull() throws {
        #expect(try decode(#"{"subscription_type": "max"}"#).subscriptionType == "max")
        #expect(try decode("{}").subscriptionType == nil)
        #expect(try decode(#"{"subscription_type": null}"#).subscriptionType == nil)
    }

    @Test func aWrongTypedSubscriptionTypeDecodesAsNilWithoutSinkingTheReport() throws {
        let r = try decode(#"{"subscription_type": 7, "plan": {"observed_at": 1.0, "windows": {}}}"#)
        #expect(r.subscriptionType == nil)
        #expect(r.plan?.observedAt == 1.0)
    }

    @Test func nullBlocksAndAnEmptyObjectDecodeAsNil() throws {
        let nulls = try decode(#"{"plan": null, "conversation": null, "marlow": null}"#)
        #expect(nulls == UsageReport())
        #expect(try decode("{}") == UsageReport())
    }

    @Test func nullOptionalFieldsDecodeAsNil() throws {
        let r = try decode("""
        {"plan": null, "marlow": null,
         "conversation": {"session_id": "abc", "context_tokens": null, "observed_at": null,
                          "compaction_threshold_tokens": 110000, "started_at": null,
                          "compactions": 0, "retired": true}}
        """)
        #expect(r.conversation?.contextTokens == nil)
        #expect(r.conversation?.startedAt == nil)
        #expect(r.conversation?.retired == true)
    }

    @Test func anUnknownWindowNameDecodesAndIsIgnoredByTheFormatter() throws {
        let r = try decode("""
        {"plan": {"observed_at": 1.0, "status": "allowed",
                  "windows": {"thirty_day": {"utilization": 0.5, "resets_at": 9},
                              "five_hour": {"utilization": 0.2, "resets_at": 99999999999}}}}
        """)
        #expect(r.plan?.windows.count == 2)
        #expect(UsageFormat.planRows(try #require(r.plan), now: now, calendar: utc).map(\.id) == ["five_hour"])
    }

    @Test func aMissingRetiredKeyFromAnOlderServerMeansNotRetired() throws {
        let r = try decode("""
        {"conversation": {"session_id": "abc", "context_tokens": 5, "observed_at": 1.0,
                          "compaction_threshold_tokens": 110000, "started_at": 2.0, "compactions": 0}}
        """)
        #expect(r.conversation?.retired == false)
    }

    @Test func oneOddFieldDoesNotSinkTheRest() throws {
        let r = try decode("""
        {"plan": {"observed_at": "yesterday", "status": 7,
                  "windows": {"five_hour": {"utilization": "lots"},
                              "seven_day": {"utilization": 0.3, "resets_at": null}}},
         "conversation": "garbage",
         "marlow": {"five_hour": {"chat": {"turns": "x"}, "scheduled": null}, "seven_day": 4}}
        """)
        #expect(r.plan?.observedAt == nil)
        #expect(r.plan?.status == nil)
        #expect(r.plan?.windows.keys.sorted() == ["seven_day"])
        #expect(r.plan?.windows["seven_day"]?.resetsAt == nil)
        #expect(r.conversation == nil)
        #expect(r.marlow?.keys.sorted() == ["five_hour"])
        #expect(r.marlow?["five_hour"] == UsageBuckets())
    }

    @Test func wholeNumbersWrittenAsFloatsStillRead() throws {
        let r = try decode(#"{"conversation": {"context_tokens": 61000.0, "compaction_threshold_tokens": 110000.0}}"#)
        #expect(r.conversation?.contextTokens == 61000)
        #expect(r.conversation?.compactionThresholdTokens == 110000)
    }
}

// MARK: - Store

private final class FakeUsageAPI: UsageAPI, @unchecked Sendable {
    var result: Result<UsageReport, Error> = .success(UsageReport())
    var calls = 0
    var tokens: [String] = []

    func usage(deviceToken: String) async throws -> UsageReport {
        calls += 1
        tokens.append(deviceToken)
        return try result.get()
    }
}

@MainActor
struct UsageStoreTests {
    @Test func refreshLoadsTheReport() async {
        let api = FakeUsageAPI()
        api.result = .success(report(five: 0.14))
        let store = UsageStore(api: api, deviceToken: "tok")
        #expect(!store.hasLoaded)

        await store.refresh()

        #expect(store.report == report(five: 0.14))
        #expect(store.hasLoaded)
        #expect(store.errorMessage == nil)
        #expect(!store.isLoading)
        #expect(api.tokens == ["tok"])
    }

    @Test func aFirstFailureIsAnErrorStateWithNothingToShow() async {
        let api = FakeUsageAPI()
        api.result = .failure(URLError(.notConnectedToInternet))
        let store = UsageStore(api: api, deviceToken: "tok")

        await store.refresh()

        #expect(store.report == nil)
        #expect(!store.hasLoaded)
        #expect(store.errorMessage != nil)
    }

    @Test func aLaterFailureKeepsWhatWasShownAndASuccessClearsTheError() async {
        let api = FakeUsageAPI()
        api.result = .success(report(five: 0.14))
        let store = UsageStore(api: api, deviceToken: "tok")
        await store.refresh()

        api.result = .failure(URLError(.timedOut))
        await store.refresh()
        #expect(store.report == report(five: 0.14))
        #expect(store.errorMessage != nil)

        api.result = .success(report(five: 0.5))
        await store.refresh()
        #expect(store.report == report(five: 0.5))
        #expect(store.errorMessage == nil)
    }
}

// MARK: - Wire format

extension APIClientTests {
    private func usageClient() -> APIClient {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [StubURLProtocol.self]
        return APIClient(
            baseURL: URL(string: "https://marlow.example.ts.net:8443")!,
            session: URLSession(configuration: config)
        )
    }

    @Test func usageGetsTheRightPathWithAuthAndDecodes() async throws {
        StubURLProtocol.respond(json: #"{"plan": null, "conversation": null, "marlow": null}"#)
        let r = try await usageClient().usage(deviceToken: "tok")
        #expect(r == UsageReport())

        let request = try #require(StubURLProtocol.lastRequest)
        #expect(request.httpMethod == "GET")
        #expect(request.url?.path == "/v1/usage")
        #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer tok")
    }
}

// MARK: - Status row feed (ChatStore)

@MainActor
struct UsageChatStoreTests {
    private func makeStore(_ fake: FakeChatAPI, usageInterval: TimeInterval = 300) -> ChatStore {
        // A real, short health interval so several passes happen in a test;
        // `.test()`'s is effectively never.
        var policy = PollingPolicy.test()
        policy.healthInterval = 2_000_000
        policy.usageInterval = usageInterval
        return ChatStore(deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: policy)
    }

    @Test func usageIsReadOnTheFirstPassAndNotAgainWithinTheInterval() async throws {
        let fake = FakeChatAPI()
        await fake.stubUsage(.success(report(five: 0.93)))
        let store = makeStore(fake)
        defer { store.cancelActiveWork() }

        #expect(await waitUntil { store.usageReport != nil })
        // Let several health passes go by; none may read usage again.
        #expect(await waitUntil { await fake.callOrder.filter { $0 == "health" }.count >= 4 })
        #expect(await fake.usageCallCount == 1)
        #expect(store.usageReport == report(five: 0.93))
    }

    @Test func aFailedFirstReadIsRetriedOnTheNextPassNotAfterTheInterval() async throws {
        // Review 2026-10-04: the throttle used to start before the read, so a
        // failure at launch left the row dark for the whole interval.
        let fake = FakeChatAPI()
        await fake.stubUsage(.failure(URLError(.timedOut)))
        let store = makeStore(fake) // 300s interval
        defer { store.cancelActiveWork() }
        #expect(await waitUntil { await fake.usageCallCount >= 1 })

        await fake.stubUsage(.success(report(five: 0.93)))
        #expect(await waitUntil { store.usageReport == report(five: 0.93) })
    }

    @Test func usageIsReadAgainOnceTheIntervalHasPassed() async throws {
        let fake = FakeChatAPI()
        await fake.stubUsage(.success(report(five: 0.5)))
        let store = makeStore(fake, usageInterval: 0)
        defer { store.cancelActiveWork() }

        #expect(await waitUntil { await fake.usageCallCount >= 3 })
    }

    @Test func eachRunReadsUsageOnItsFirstPass() async throws {
        let fake = FakeChatAPI()
        let store = makeStore(fake)
        defer { store.cancelActiveWork() }
        #expect(await waitUntil { await fake.usageCallCount == 1 })

        // A foreground restart is a new run: the pass someone is waiting on.
        store.handleDidBecomeActive()
        #expect(await waitUntil { await fake.usageCallCount == 2 })
    }

    @Test func usageIsReadAfterHealth() async throws {
        let fake = FakeChatAPI()
        let store = makeStore(fake)
        defer { store.cancelActiveWork() }
        #expect(await waitUntil { await fake.usageCallCount >= 1 })
        // Health ran before it; the sync is first, as it always was.
        #expect(await fake.callOrder.prefix(2) == ["transcript", "health"])
    }

    @Test func aFailedReadKeepsThePreviousReportAndIsNotAnUnreachableSignal() async throws {
        let fake = FakeChatAPI()
        await fake.stubUsage(.success(report(five: 0.93)))
        let store = makeStore(fake, usageInterval: 0)
        defer { store.cancelActiveWork() }
        #expect(await waitUntil { store.usageReport != nil })

        await fake.stubUsage(.failure(URLError(.notConnectedToInternet)))
        let before = await fake.usageCallCount
        #expect(await waitUntil { await fake.usageCallCount >= before + 2 })

        #expect(store.usageReport == report(five: 0.93), "a failed read must not blank the row")
        #expect(store.connectionActivity == .idle, "usage is not a health signal")
    }
}
