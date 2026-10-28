import Foundation
import Testing
@testable import Marlow

/// The window decides how many rows `ChatView` builds before its first frame,
/// so its failure modes are both directions: too many is the cold-launch pause
/// coming back, too few (or the wrong ones) is a message Mike can't see.
struct TranscriptWindowTests {

    private static let now = Date(timeIntervalSince1970: 1_800_000_000)
    private static let hour: TimeInterval = 60 * 60
    private static let day: TimeInterval = 24 * hour

    /// `count` messages, `spacing` apart, the newest `newestAge` before `now`.
    private static func transcript(
        count: Int, spacing: TimeInterval, newestAge: TimeInterval = 0
    ) -> [ChatMessage] {
        (0..<count).map { i in
            let age = newestAge + Double(count - 1 - i) * spacing
            return ChatMessage(
                id: UUID(), role: i.isMultiple(of: 2) ? .user : .assistant,
                text: "m\(i)", createdAt: now.addingTimeInterval(-age),
                turnId: nil, status: .done, clientMsgId: nil
            )
        }
    }

    private static func visibleCount(_ messages: [ChatMessage], floor: Date?) -> Int {
        messages.count - TranscriptWindow.startIndex(of: messages, floor: floor)
    }

    @Test func anEmptyTranscriptShowsEverything() {
        #expect(TranscriptWindow.floor(for: [], before: 0, reference: Self.now) == nil)
        #expect(TranscriptWindow.startIndex(of: [], floor: nil) == 0)
    }

    /// Everything inside 48 hours renders, however many rows that is — the
    /// time rule is the promise, the count is only a floor under it.
    @Test func aBusyTwoDaysIsShownWhole() {
        let messages = Self.transcript(count: 200, spacing: 10 * 60)  // ~33h
        #expect(TranscriptWindow.floor(for: messages, before: messages.count, reference: Self.now) == nil)
    }

    @Test func olderThanFortyEightHoursIsWindowedOut() {
        let messages = Self.transcript(count: 100, spacing: Self.hour)
        let floor = TranscriptWindow.floor(for: messages, before: messages.count, reference: Self.now)
        // Hourly for 100 hours: ages 0...48h inclusive is 49 rows.
        #expect(Self.visibleCount(messages, floor: floor) == 49)
        #expect(floor == Self.now.addingTimeInterval(-48 * Self.hour))
    }

    /// A quiet weekend must not open onto a lone message.
    @Test func aQuietStretchStillShowsTheMinimumCount() {
        let messages = Self.transcript(count: 100, spacing: Self.hour, newestAge: 10 * Self.day)
        let floor = TranscriptWindow.floor(for: messages, before: messages.count, reference: Self.now)
        #expect(Self.visibleCount(messages, floor: floor) == TranscriptWindow.minimumCount)
    }

    @Test func aShortOldTranscriptIsShownWhole() {
        let messages = Self.transcript(count: TranscriptWindow.minimumCount, spacing: Self.day)
        #expect(TranscriptWindow.floor(for: messages, before: messages.count, reference: Self.now) == nil)
    }

    /// "Show earlier" steps back from the oldest row on screen, by the same
    /// rule, until it reaches the start — never straight to the whole history.
    @Test func steppingBackRevealsOneWindowAtATime() {
        let messages = Self.transcript(count: 100, spacing: Self.day)
        var floor = TranscriptWindow.floor(for: messages, before: messages.count, reference: Self.now)
        var shown = [Self.visibleCount(messages, floor: floor)]
        while floor != nil {
            let start = TranscriptWindow.startIndex(of: messages, floor: floor)
            floor = TranscriptWindow.floor(for: messages, before: start, reference: messages[start].createdAt)
            shown.append(Self.visibleCount(messages, floor: floor))
        }
        #expect(shown == [30, 60, 90, 100])
    }

    /// The floor is a date so a row inserted by time (a late cron turn) lands
    /// on the correct side of it without any index bookkeeping.
    @Test func aRowInsertedInsideTheWindowIsShown() {
        var messages = Self.transcript(count: 100, spacing: Self.hour)
        let floor = TranscriptWindow.floor(for: messages, before: messages.count, reference: Self.now)
        let before = Self.visibleCount(messages, floor: floor)
        let late = ChatMessage(
            id: UUID(), role: .assistant, text: "late cron",
            createdAt: Self.now.addingTimeInterval(-5.5 * Self.hour),
            turnId: "cron-late", status: .done, clientMsgId: nil
        )
        let index = messages.lastIndex { $0.createdAt <= late.createdAt }.map { $0 + 1 } ?? 0
        messages.insert(late, at: index)
        #expect(Self.visibleCount(messages, floor: floor) == before + 1)
    }
}
