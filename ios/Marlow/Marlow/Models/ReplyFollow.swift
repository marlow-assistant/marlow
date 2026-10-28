import CoreGraphics
import Foundation

/// Whether the transcript should move to keep a reply in view while it
/// streams, after a send from this phone.
///
/// The problem it fixes (Mike, 2026-09-29): with a long scrollback, a send
/// scrolled to his bubble and the reply's first line, then the reply ran off
/// the bottom — the transcript only scrolled when a *row* was added, and a
/// streaming reply grows inside one. He was managing the scroll by hand.
///
/// The rule Mike agreed: follow the growing reply until the start of the
/// exchange reaches the top of the visible transcript (just under the
/// floating header), then leave the scroll alone and let the rest stream in
/// below the fold. The start of the exchange is his own message, unless that
/// message is by itself taller than the viewport — a long dictation — where
/// pinning its top would scroll *back up* and hide the reply; then the
/// reply's own top is the pin point.
///
/// Stopping is a pin, not a last follow step: once one more follow would
/// carry the anchor above the top, the anchor is pinned to the top exactly,
/// so the view lands on the point instead of overshooting and snapping back.
///
/// Pure so it can be tested without a scroll view; `ChatView` supplies the
/// geometry and carries out the answer. Everything is in one coordinate
/// space — the transcript's — where y grows downward.
enum ReplyFollow {
    struct Geometry: Equatable {
        /// Mike's message row.
        var userTop: CGFloat
        var userHeight: CGFloat
        /// The reply row (the placeholder that fills as text streams).
        var replyTop: CGFloat
        var replyBottom: CGFloat
        /// The top and bottom of what can be read. In `ChatView` that is
        /// `0...` the scroll view's height: its coordinate space already
        /// starts below the floating header (see `readableHeight` there).
        var visibleTop: CGFloat
        var visibleBottom: CGFloat

        var visibleHeight: CGFloat { visibleBottom - visibleTop }
    }

    enum Action: Equatable {
        /// Nothing to do: the reply's bottom is on screen already.
        case hold
        /// Scroll the reply's bottom to the bottom of the viewport.
        case followReply
        /// Pin Mike's message to the top. Following ends here.
        case pinUser
        /// Pin the reply's top to the top (long dictation). Following ends.
        case pinReply

        var endsFollowing: Bool {
            switch self {
            case .pinUser, .pinReply: return true
            case .hold, .followReply: return false
            }
        }
    }

    /// Half a point: layout reports fractional positions, and a comparison
    /// that flips on rounding noise would scroll by nothing, or pin a frame
    /// early.
    static let tolerance: CGFloat = 0.5

    static func action(for g: Geometry) -> Action {
        // Unmeasured — the viewport reader or header reader hasn't reported.
        // Moving on a guess is worse than a frame of not moving.
        guard g.visibleHeight > 0 else { return .hold }

        let userIsTallerThanViewport = g.userHeight > g.visibleHeight + tolerance
        let anchorTop = userIsTallerThanViewport ? g.replyTop : g.userTop
        // How much has to be on screen at once to show the exchange from its
        // start to the reply's latest line. Independent of scroll position:
        // scrolling moves both ends together.
        let span = g.replyBottom - anchorTop

        if span > g.visibleHeight + tolerance {
            return userIsTallerThanViewport ? .pinReply : .pinUser
        }
        if g.replyBottom > g.visibleBottom + tolerance {
            return .followReply
        }
        return .hold
    }
}
