import UIKit

/// The taps that accompany the chat sounds — and, at the start of a reply,
/// one that has no sound to accompany. Plus the two that mark the microphone
/// going live and going off, at the bottom.
///
/// **Why this exists at all: the silent switch.** `ChatSoundPlayer` uses
/// `AudioServicesPlaySystemSound`, which iOS silences when the ringer switch
/// is off — so on a phone that lives on silent (Mike's does) the send and
/// receive tones have never once played, and there was no feedback that a
/// turn had started or landed short of watching the screen. Haptics are the
/// half of that pair the ringer switch does NOT suppress.
///
/// **So the rule here is: fire unconditionally, never "only when silent."**
/// That reads backwards until you notice iOS exposes no public way to read
/// the ringer switch — every approach is a private API or an audio-session
/// inference that Apple has broken before. It also turns out not to be
/// needed: sound and haptic are already complementary, because the system
/// suppresses exactly one of them. Calling both at each point gives "a tone
/// when the phone can make noise, a tap when it can't" for free, which is the
/// behaviour asked for, and is what Messages and the Claude iOS app do. Do
/// not add a silent-mode check here; there is nothing correct to check.
///
/// Every generator is created, used and dropped per call rather than kept
/// warm with `prepare()`. Warming matters where a haptic must land inside a
/// few milliseconds of a touch — a drag crossing a detent. Nothing here is
/// tied to a finger: these fire off network events that already took
/// hundreds of milliseconds to arrive, and a taut coupling to a gesture is
/// not a property any of them have to preserve.
///
/// All of it is a no-op when the app is backgrounded, which is correct and is
/// why there is no scene-phase guard inside: a turn that lands while the app
/// is away is announced by APNs, and `ChatView` already drains those ids
/// without sounding them.
enum ChatHaptics {
    /// Mike's own message entering the transcript. Light, because it confirms
    /// something he just did deliberately — the news is only "it went."
    static func sent() {
        UIImpactFeedbackGenerator(style: .light).impactOccurred()
    }

    /// The first streamed characters of a reply. **The one Mike actually
    /// asked for** (2026-09-11): the gap between tapping send and any visible
    /// answer is the whole of Marlow's thinking time, and on a silent phone
    /// nothing marked its end. Soft rather than light — this is an "it's
    /// starting" cue under an answer he is about to read, and it should not
    /// compete with the one below.
    ///
    /// Fires once per turn; the latch is `ChatStore.startedAssistantReplies`,
    /// not anything here.
    static func replyStarted() {
        UIImpactFeedbackGenerator(style: .soft).impactOccurred()
    }

    /// The turn reaching a terminal state. Medium, deliberately the firmest of
    /// the three: it is the one that means "done, you can stop waiting."
    ///
    /// An impact rather than `UINotificationFeedbackGenerator.success` — that
    /// one is a three-part rise, which is a lot of ceremony to spend on the
    /// ordinary end of every single turn, and it is the pattern the system
    /// reserves for the completion of something the user was *told* to wait
    /// for (Apple Pay, a save). A reply arriving is routine.
    static func replyFinished() {
        UIImpactFeedbackGenerator(style: .medium).impactOccurred()
    }

    /// The microphone is live. Fired by `DictationController` once the engine
    /// is actually running, not on the tap: `start()` awaits two permission
    /// round-trips first, and a tap that lands before an error message would
    /// be saying something false. Rigid rather than any of the chat styles
    /// above, so a mic event never reads as a turn event.
    ///
    /// **This one only plays because of the audio session.** iOS suppresses
    /// haptics while a `.record` session is active unless the session opts in
    /// with `setAllowHapticsAndSystemSoundsDuringRecording(true)`, which
    /// `DictationController.start()` sets. Remove that and this goes silent
    /// with nothing failing.
    static func dictationStarted() {
        UIImpactFeedbackGenerator(style: .rigid).impactOccurred()
    }

    /// The microphone is off — whichever way it ended: the mic button, send,
    /// or the length cap. Fired after the session is handed back, so it
    /// plays without the opt-in the start needs.
    static func dictationStopped() {
        UIImpactFeedbackGenerator(style: .rigid).impactOccurred()
    }
}
