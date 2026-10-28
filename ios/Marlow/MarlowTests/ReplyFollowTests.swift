import CoreGraphics
import Testing
@testable import Marlow

/// The rule that decides whether the transcript chases a streaming reply.
/// Both directions fail visibly: too little and the reply runs off the
/// bottom (the bug), too much and the start of the exchange scrolls away
/// above the header before Mike has read it.
struct ReplyFollowTests {

    /// A phone-ish viewport: header bottom at 100, viewport bottom at 800,
    /// so 700pt readable.
    private static func geometry(
        userTop: CGFloat, userHeight: CGFloat = 60,
        replyHeight: CGFloat, gap: CGFloat = 12
    ) -> ReplyFollow.Geometry {
        let replyTop = userTop + userHeight + gap
        return ReplyFollow.Geometry(
            userTop: userTop, userHeight: userHeight,
            replyTop: replyTop, replyBottom: replyTop + replyHeight,
            visibleTop: 100, visibleBottom: 800
        )
    }

    @Test func holdsWhileTheReplyIsOnScreen() {
        let g = Self.geometry(userTop: 500, replyHeight: 100)
        #expect(ReplyFollow.action(for: g) == .hold)
    }

    @Test func followsAReplyThatHasRunOffTheBottom() {
        // The reported bug: bubble and one line on screen, the rest below.
        let g = Self.geometry(userTop: 600, replyHeight: 300)
        #expect(ReplyFollow.action(for: g) == .followReply)
    }

    @Test func pinsTheUserMessageOnceTheExchangeOutgrowsTheViewport() {
        // 60 + 12 + 640 = 712 > 700: one more follow would hide his message.
        let g = Self.geometry(userTop: 500, replyHeight: 640)
        #expect(ReplyFollow.action(for: g) == .pinUser)
        #expect(ReplyFollow.action(for: g).endsFollowing)
    }

    @Test func stillFollowsWhenTheExchangeExactlyFills() {
        // 60 + 12 + 628 = 700: fits to the point, so it is still a follow —
        // pinning here would be the same position, one step early.
        let g = Self.geometry(userTop: 150, replyHeight: 628)
        #expect(ReplyFollow.action(for: g) == .followReply)
    }

    @Test func pinDecisionDoesNotDependOnScrollPosition() {
        // Same exchange, scrolled differently: the span is what decides.
        for userTop: CGFloat in [-400, 100, 900] {
            let g = Self.geometry(userTop: userTop, replyHeight: 640)
            #expect(ReplyFollow.action(for: g) == .pinUser)
        }
    }

    @Test func longDictationFollowsTheReplyInsteadOfPinningTheMessage() {
        // His message alone is taller than the viewport. Pinning its top
        // would scroll back up and hide the reply entirely.
        let g = Self.geometry(userTop: -300, userHeight: 900, replyHeight: 200)
        #expect(ReplyFollow.action(for: g) == .followReply)
    }

    @Test func longDictationPinsTheReplyOnceTheReplyOutgrowsTheViewport() {
        let g = Self.geometry(userTop: -1000, userHeight: 900, replyHeight: 710)
        #expect(ReplyFollow.action(for: g) == .pinReply)
        #expect(ReplyFollow.action(for: g).endsFollowing)
    }

    @Test func longDictationHoldsWhileTheReplyFits() {
        // Reply bottom at 700, inside the 800 bottom edge.
        let g = Self.geometry(userTop: -300, userHeight: 900, replyHeight: 88)
        #expect(ReplyFollow.action(for: g) == .hold)
    }

    @Test func subPointNoiseIsNotAMove() {
        // Layout reports fractions; a quarter point past the edge is rounding.
        var g = Self.geometry(userTop: 500, replyHeight: 100)
        g.replyBottom = 800.25
        #expect(ReplyFollow.action(for: g) == .hold)
    }

    @Test func unmeasuredViewportHolds() {
        var g = Self.geometry(userTop: 600, replyHeight: 300)
        g.visibleTop = 0
        g.visibleBottom = 0
        #expect(ReplyFollow.action(for: g) == .hold)
    }

    @Test func onlyPinsEndFollowing() {
        #expect(!ReplyFollow.Action.hold.endsFollowing)
        #expect(!ReplyFollow.Action.followReply.endsFollowing)
    }
}
