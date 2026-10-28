import Foundation
import Testing
@testable import Marlow

// MARK: - Fixtures

/// Monday 2026-10-12, in UTC and en_US so the strings are the same on every
/// machine.
private let utc: Calendar = {
    var c = Calendar(identifier: .gregorian)
    c.timeZone = TimeZone(identifier: "UTC")!
    c.locale = Locale(identifier: "en_US")
    return c
}()

private func at(_ month: Int, _ day: Int, _ hour: Int = 15, year: Int = 2026) -> Date {
    utc.date(from: DateComponents(year: year, month: month, day: day, hour: hour))!
}

/// Mid-afternoon, so "today" has to be taken as the whole day.
private let today = at(10, 12)

private func idea(
    _ id: String, section: String = "now", until: String? = nil, added: String = "2026-10-01",
    spotlight: Bool = false, source: String? = "an email"
) -> Idea {
    Idea(
        id: id, section: section, emoji: "x", title: "Title \(id)", plan: "Plan \(id).", category: "Van",
        source: source, until: until, added: added, spotlight: spotlight
    )
}

// MARK: - Expiry and the nearing marker

struct IdeaExpiryTests {
    @Test func anIdeaPastItsUntilIsHidden() {
        #expect(IdeaFormat.isHidden(idea("a", until: "2026-10-11"), today: today, calendar: utc))
        #expect(IdeaFormat.isHidden(idea("a", until: "2026-09-30"), today: today, calendar: utc))
    }

    @Test func untilIsTheLastDayItShows() {
        let onTheDay = idea("a", until: "2026-10-12")
        #expect(!IdeaFormat.isHidden(onTheDay, today: today, calendar: utc))
        #expect(!IdeaFormat.isHidden(onTheDay, today: at(10, 12, 23), calendar: utc), "late on the last day is still the last day")
        #expect(IdeaFormat.isHidden(onTheDay, today: at(10, 13, 0), calendar: utc))
    }

    @Test func noUntilNeverHidesOrMarks() {
        let open = idea("a")
        #expect(!IdeaFormat.isHidden(open, today: today, calendar: utc))
        #expect(!IdeaFormat.isNearing(open, today: today, calendar: utc))
        #expect(IdeaFormat.untilText(open, today: today, calendar: utc) == nil)
    }

    @Test func aMalformedUntilIsTreatedAsAbsent() {
        for bad in ["soon", "2026-10", "2026-2-3", "2026-02-31", "2026-13-01", ""] {
            let odd = idea("a", until: bad)
            #expect(!IdeaFormat.isHidden(odd, today: today, calendar: utc), "\(bad)")
            #expect(!IdeaFormat.isNearing(odd, today: today, calendar: utc), "\(bad)")
        }
    }

    @Test func nearingIsTodayTomorrowAndTheDayAfter() {
        func nearing(_ until: String) -> Bool {
            IdeaFormat.isNearing(idea("a", until: until), today: today, calendar: utc)
        }
        #expect(nearing("2026-10-12"), "today")
        #expect(nearing("2026-10-13"), "tomorrow")
        #expect(nearing("2026-10-14"), "the day after: the edge that is in")
        #expect(!nearing("2026-10-15"), "three days out is not yet")
        #expect(!nearing("2026-10-11"), "yesterday is hidden, not nearing")
    }

    @Test func nearingSpansAMonthEnd() {
        let end = at(10, 31)
        let spans = idea("a", until: "2026-11-02")
        #expect(IdeaFormat.isNearing(spans, today: end, calendar: utc))
        #expect(IdeaFormat.untilText(spans, today: end, calendar: utc) == "until Nov 2")
    }

    @Test func untilTextIsTheDateOnlyWhileNearing() {
        #expect(IdeaFormat.untilText(idea("a", until: "2026-10-13"), today: today, calendar: utc) == "until Oct 13")
        #expect(IdeaFormat.untilText(idea("a", until: "2026-10-12"), today: today, calendar: utc) == "until Oct 12")
        #expect(IdeaFormat.untilText(idea("a", until: "2026-10-20"), today: today, calendar: utc) == nil)
    }
}

// MARK: - Sections and the spotlight

struct IdeaSelectionTests {
    @Test func sectionsAreForYouNowThenThingsICanDoWhateverTheStoreOrder() {
        let ideas = [
            idea("s1", section: "standing"), idea("n1"), idea("s2", section: "standing"), idea("n2"),
        ]
        let groups = IdeaFormat.sections(ideas, today: today, calendar: utc)
        #expect(groups.map(\.title) == ["For you now", "Things I can do"])
        #expect(groups.map { $0.ideas.map(\.id) } == [["n1", "n2"], ["s1", "s2"]], "store order kept inside a section")
    }

    @Test func anEmptySectionIsOmitted() {
        let onlyStanding = IdeaFormat.sections([idea("s", section: "standing")], today: today, calendar: utc)
        #expect(onlyStanding.map(\.section) == [.standing])
        #expect(IdeaFormat.sections([], today: today, calendar: utc).isEmpty)
    }

    @Test func expiredIdeasAreLeftOutOfTheSectionsAndCanEmptyOne() {
        let ideas = [idea("old", until: "2026-10-10"), idea("s", section: "standing")]
        let groups = IdeaFormat.sections(ideas, today: today, calendar: utc)
        #expect(groups.map(\.section) == [.standing])
    }

    @Test func anUnknownSectionIsDroppedNotGuessed() {
        let ideas = [idea("a", section: "someday"), idea("b")]
        #expect(IdeaFormat.sections(ideas, today: today, calendar: utc).flatMap(\.ideas).map(\.id) == ["b"])
    }

    @Test func theSpotlightIsTheFirstMarkedIdea() {
        let ideas = [idea("a"), idea("b", spotlight: true), idea("c", spotlight: true)]
        #expect(IdeaFormat.spotlight(ideas, today: today, calendar: utc)?.id == "b")
        #expect(IdeaFormat.spotlight([idea("a")], today: today, calendar: utc) == nil)
    }

    @Test func anExpiredSpotlightIsSkippedNotShown() {
        let ideas = [idea("old", until: "2026-10-11", spotlight: true), idea("fresh", spotlight: true)]
        #expect(IdeaFormat.spotlight(ideas, today: today, calendar: utc)?.id == "fresh", "skips the hidden one")
        #expect(IdeaFormat.spotlight([ideas[0]], today: today, calendar: utc) == nil, "and with no other, there is none")
    }
}

// MARK: - The dot

struct IdeaDotTests {
    private func dot(_ ideas: [Idea], lastOpened: Date?) -> Bool {
        IdeaFormat.showsDot(ideas, lastOpened: lastOpened, today: today, calendar: utc)
    }

    /// The dot asks "has the list been read", and the spotlight's card is
    /// its own signal (Mike, 2026-10-12).
    @Test func aSpotlightAlreadyReadPastDoesNotLightIt() {
        #expect(!dot([idea("a", added: "2026-09-01", spotlight: true)], lastOpened: at(10, 12)))
    }

    @Test func anIdeaAddedAfterTheLastOpenLightsIt() {
        #expect(dot([idea("a", added: "2026-10-12")], lastOpened: at(10, 11, 22)))
    }

    @Test func comparesByDayAnIdeaAddedTheDayItWasOpenedWasAlreadySeen() {
        #expect(!dot([idea("a", added: "2026-10-12")], lastOpened: at(10, 12, 6)))
        #expect(!dot([idea("a", added: "2026-10-11")], lastOpened: at(10, 12, 6)))
    }

    @Test func neverOpenedMeansAnyIdeaIsNew() {
        #expect(dot([idea("a", added: "2020-01-01")], lastOpened: nil))
        #expect(!dot([], lastOpened: nil))
    }

    @Test func nothingNewMeansNoDot() {
        #expect(!dot([idea("a", added: "2026-10-01"), idea("b", section: "standing", added: "2026-10-02")], lastOpened: at(10, 5)))
    }

    @Test func anExpiredNewIdeaDoesNotLightIt() {
        #expect(!dot([idea("a", until: "2026-10-11", added: "2026-10-10")], lastOpened: at(10, 5)))
    }
}

// MARK: - Wording

struct IdeaWordingTests {
    @Test func doItSendsJustTheWordsTheIdeaIsQuotedNotRepeated() {
        #expect(IdeaFormat.doItMessage == "Do it")
        #expect(IdeaQuote(idea("a")) == IdeaQuote(ideaId: "a", emoji: "x", title: "Title a"))
    }
}

// MARK: - Decoding

struct IdeaDecodingTests {
    private func decode(_ json: String) throws -> [Idea] {
        try JSONDecoder().decode(IdeasResponse.self, from: Data(json.utf8)).ideas
    }

    @Test func decodesTheContractWithNullsForAbsentOptionals() throws {
        let ideas = try decode("""
        {"ideas": [
          {"id": "dinner-invites", "section": "now", "emoji": "x", "title": "Draft the invites", "plan": "I will.",
           "category": "People", "source": "Sam's email", "until": "2026-10-13", "added": "2026-10-12", "spotlight": true},
          {"id": "trip-plan", "section": "standing", "emoji": "y", "title": "Plan a trip", "plan": "I can.",
           "category": "Travel", "source": null, "until": null, "added": "2026-10-01", "spotlight": false}
        ]}
        """)
        #expect(ideas.count == 2)
        #expect(ideas[0] == Idea(
            id: "dinner-invites", section: "now", emoji: "x", title: "Draft the invites", plan: "I will.",
            category: "People", source: "Sam's email", until: "2026-10-13", added: "2026-10-12", spotlight: true
        ))
        #expect(ideas[1].source == nil)
        #expect(ideas[1].until == nil)
        #expect(ideas[1].section == "standing")
        #expect(!ideas[1].spotlight)
    }

    @Test func missingOptionalKeysDecodeLikeNulls() throws {
        let ideas = try decode("""
        {"ideas": [{"id": "a", "section": "standing", "emoji": "x", "title": "T", "plan": "P",
                    "category": "Van", "added": "2026-10-01"}]}
        """)
        #expect(ideas[0].source == nil && ideas[0].until == nil && !ideas[0].spotlight)
    }

    @Test func anEmptyStoreIsAnEmptyList() throws {
        #expect(try decode(#"{"ideas": []}"#).isEmpty)
    }

    @Test func aMalformedIdeaDropsAloneAndKeepsTheRest() throws {
        let ideas = try decode("""
        {"ideas": [
          {"id": "good", "section": "now", "emoji": "x", "title": "T", "plan": "P", "category": "Van", "added": "2026-10-01"},
          {"id": "no-title", "section": "now", "emoji": "x", "plan": "P", "category": "Van", "added": "2026-10-01"}
        ]}
        """)
        #expect(ideas.map(\.id) == ["good"])
    }
}

// MARK: - The store

@MainActor
final class FakeIdeasAPI: IdeasAPI {
    var result: Result<[Idea], Error> = .success([])
    private(set) var fetchCount = 0
    private(set) var dismissals: [(id: String, reason: String?)] = []
    var dismissResult: Result<Void, Error> = .success(())

    func ideas(deviceToken: String) async throws -> [Idea] {
        fetchCount += 1
        return try result.get()
    }

    func dismissIdea(id: String, reason: String?, deviceToken: String) async throws {
        dismissals.append((id, reason))
        try dismissResult.get()
    }
}

@MainActor
struct IdeasStoreTests {
    private func makeDefaults() -> UserDefaults {
        UserDefaults(suiteName: "IdeasStoreTests-\(UUID().uuidString)")!
    }

    private func makeStore(_ api: FakeIdeasAPI, defaults: UserDefaults? = nil) -> IdeasStore {
        IdeasStore(
            api: api, deviceToken: "tok", defaults: defaults ?? makeDefaults(), now: { today }, calendar: utc
        )
    }

    @Test func refreshLoadsTheIdeasInStoreOrder() async {
        let api = FakeIdeasAPI()
        api.result = .success([idea("a"), idea("b", section: "standing")])
        let store = makeStore(api)

        await store.refresh()

        #expect(store.ideas.map(\.id) == ["a", "b"])
        #expect(store.sections.map(\.title) == ["For you now", "Things I can do"])
    }

    @Test func aFailedFetchKeepsTheLastGoodListAndSaysNothing() async {
        let api = FakeIdeasAPI()
        api.result = .success([idea("a")])
        let store = makeStore(api)
        await store.refresh()

        api.result = .failure(URLError(.notConnectedToInternet))
        await store.refresh()

        #expect(store.ideas.map(\.id) == ["a"])
    }

    @Test func spotlightAndDotComeFromTheFetchedList() async {
        let api = FakeIdeasAPI()
        api.result = .success([idea("a"), idea("hot", spotlight: true)])
        let store = makeStore(api)
        #expect(store.spotlight == nil && !store.showsDot, "nothing before the first fetch")

        await store.refresh()

        #expect(store.spotlight?.id == "hot")
        #expect(store.showsDot)
    }

    @Test func openingThePanelClearsTheDotAndItStaysClearedAcrossARelaunch() async {
        let defaults = makeDefaults()
        let api = FakeIdeasAPI()
        api.result = .success([idea("a", added: "2026-10-11")])
        let store = makeStore(api, defaults: defaults)
        await store.refresh()
        #expect(store.showsDot)

        await store.panelOpened()

        #expect(!store.showsDot)
        #expect(api.fetchCount == 2, "opening the panel fetches")
        let relaunched = makeStore(api, defaults: defaults)
        await relaunched.refresh()
        #expect(!relaunched.showsDot, "the opened date is persisted")
    }

    @Test func acceptRemovesTheCardAtOnceAndAFetchCannotBringItBack() async {
        let api = FakeIdeasAPI()
        api.result = .success([idea("a"), idea("b")])
        let store = makeStore(api)
        await store.refresh()

        store.accept(store.ideas[0])
        #expect(store.ideas.map(\.id) == ["b"])

        await store.refresh() // the server still lists "a" until the message lands
        #expect(store.ideas.map(\.id) == ["b"])
    }

    @Test func notNowPostsAtOnceWithNoReasonAndCollapsesTheCard() async {
        let api = FakeIdeasAPI()
        api.result = .success([idea("a"), idea("b", spotlight: true)])
        let store = makeStore(api)
        await store.panelOpened()

        let took = await store.dismiss(store.ideas[1])

        #expect(took)
        #expect(api.dismissals.count == 1)
        #expect(api.dismissals[0].id == "b" && api.dismissals[0].reason == nil)
        #expect(store.collapsedIds == ["b"])
        #expect(store.ideas.map(\.id) == ["a", "b"], "still there, as a row, until the panel closes")
        #expect(store.spotlight == nil, "a dismissed spotlight leaves the chat")
    }

    /// Not now on the spotlight card, with the panel shut: the idea just goes.
    /// A row would greet the next opening as stale news (and web shows none).
    @Test func notNowWithThePanelShutLeavesNoCollapsedRow() async {
        let api = FakeIdeasAPI()
        api.result = .success([idea("a"), idea("b", spotlight: true)])
        let store = makeStore(api)
        await store.refresh()

        await store.dismiss(store.ideas[1])

        #expect(store.collapsedIds.isEmpty)
        #expect(store.ideas.map(\.id) == ["a"])
        #expect(store.spotlight == nil)
    }

    @Test func aReasonRePostsTheDismissalAndIsRemembered() async {
        let api = FakeIdeasAPI()
        api.result = .success([idea("a")])
        let store = makeStore(api)
        await store.panelOpened()
        await store.dismiss(store.ideas[0])

        await store.dismiss(store.ideas[0], reason: "  Already did it  ")

        #expect(api.dismissals.map(\.reason) == [nil, "Already did it"])
        #expect(store.reasons["a"] == "Already did it")
    }

    @Test func aBlankReasonIsPostedAsNoReason() async {
        let api = FakeIdeasAPI()
        api.result = .success([idea("a")])
        let store = makeStore(api)
        await store.refresh()

        await store.dismiss(store.ideas[0], reason: "   ")

        #expect(api.dismissals.map(\.reason) == [nil])
        #expect(store.reasons["a"] == nil)
    }

    @Test func aFailedReasonIsNotRememberedSoTheFieldStaysForAnotherTry() async {
        let api = FakeIdeasAPI()
        api.result = .success([idea("a")])
        let store = makeStore(api)
        await store.panelOpened()
        await store.dismiss(store.ideas[0])

        api.dismissResult = .failure(URLError(.timedOut))
        let took = await store.dismiss(store.ideas[0], reason: "Nope")

        #expect(!took)
        #expect(store.reasons["a"] == nil)
        #expect(store.collapsedIds == ["a"])
    }

    @Test func aCollapsedRowSurvivesARefreshWhilePanelIsOpenThenGoesWhenItCloses() async {
        let api = FakeIdeasAPI()
        api.result = .success([idea("a"), idea("b"), idea("c")])
        let store = makeStore(api)
        await store.panelOpened()
        await store.dismiss(store.ideas[1])

        api.result = .success([idea("a"), idea("c")]) // the server no longer lists "b"
        await store.refresh()
        #expect(store.ideas.map(\.id) == ["a", "b", "c"], "kept in place")

        store.panelClosed()
        #expect(store.ideas.map(\.id) == ["a", "c"])
        #expect(store.collapsedIds.isEmpty)
    }

    @Test func anExpiredIdeaIsHiddenWithoutWaitingForTheServer() async {
        let api = FakeIdeasAPI()
        api.result = .success([idea("old", until: "2026-10-11", spotlight: true), idea("a")])
        let store = makeStore(api)

        await store.refresh()

        #expect(store.sections.flatMap(\.ideas).map(\.id) == ["a"])
        #expect(store.spotlight == nil)
    }
}

// MARK: - Do it through the chat path

@MainActor
struct IdeaSendTests {
    @Test func doItSendsTheIdeaIdAndAnOrdinarySendDoesNot() async throws {
        let fake = FakeChatAPI()
        await fake.stubSend(.success(MessageResponse(turnId: "t1")))
        await fake.stubTurns([.success(.fixture(turnId: "t1", status: "done", resultText: "On it."))])
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test()
        )
        defer { store.cancelActiveWork() }

        store.send(text: IdeaFormat.doItMessage, ideaId: "a", quote: IdeaQuote(idea("a")))
        await awaitSettled(store)
        store.send(text: "thanks")
        await awaitSettled(store)

        let sent = try requireCount(await fake.sentMessages, 2)
        #expect(sent[0].text == "Do it")
        #expect(sent[0].ideaId == "a")
        #expect(sent[1].ideaId == nil)
        let first = try #require(store.messages.first)
        #expect(first.text == "Do it")
        #expect(first.quote == IdeaQuote(ideaId: "a", emoji: "x", title: "Title a"), "the bubble quotes the idea at once, before any sync")
        #expect(store.messages.first(where: { $0.text == "thanks" })?.quote == nil, "an ordinary send quotes nothing")
    }

    @Test func aRetryResendsUnderTheSameIdeaId() async throws {
        let fake = FakeChatAPI()
        await fake.stubSend(.failure(URLError(.notConnectedToInternet)))
        let store = ChatStore(
            deviceToken: "tok", client: fake, persistURL: makeTempTranscriptURL(), policy: .test(sendMaxAttempts: 1)
        )
        defer { store.cancelActiveWork() }

        store.send(text: "Do it", ideaId: "a", quote: IdeaQuote(idea("a")))
        await awaitSettled(store)
        let failed = try requireCount(store.messages, 2)
        #expect(failed[1].status == .error)

        await fake.stubSend(.success(MessageResponse(turnId: "t2")))
        await fake.stubTurns([.success(.fixture(turnId: "t2", status: "done", resultText: "On it."))])
        store.retry(failed[1].id)
        await awaitSettled(store)

        let sent = try requireCount(await fake.sentMessages, 2)
        #expect(sent[1].ideaId == "a", "the acceptance must not be lost to a flaky first attempt")
    }
}
