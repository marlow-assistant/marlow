import Foundation
import Testing
@testable import Marlow

/// Covers only what a unit test can actually decide. Whether the extension
/// can reach the tailnet from a locked phone is not testable here by
/// construction — it was answered on Mike's phone on 2026-09-08, and mocking
/// it would have produced exactly the false confidence that gate existed to
/// prevent. The diagnostics and inbox files are likewise device-only: they
/// need a provisioned App Group container, and a test that skips when the
/// container is absent reports green for having done nothing. The app's side
/// of the inbox is covered in `TurnPrefetchTests` through an injected reader
/// for that reason.
struct NotificationSharedTests {

    /// The entitlement says `$(AppIdentifierPrefix)com.mikemason.Marlow.shared`;
    /// a Swift string that differs by a character makes the extension's
    /// Keychain read fail. Built from the same build setting since 2026-10-26,
    /// when the team ID left the repo, so it can't drift from the signing team.
    @Test func theSharedKeychainGroupCarriesTheSigningTeamsPrefix() throws {
        let prefix = try #require(Bundle.main.object(forInfoDictionaryKey: "AppIdentifierPrefix") as? String)
        #expect(prefix.count == 11 && prefix.hasSuffix("."), "a ten-character team ID and its dot")
        #expect(NotificationShared.keychainAccessGroup == prefix + "com.mikemason.Marlow.shared")
    }

    @Test func bodyCollapsesWhitespaceSoAMarkdownReplyIsNotAWallOfText() {
        let reply = "Two things:\n\n- the first\n- the second\n"
        // The bullets survive as real ones. Collapsed onto one line without
        // them, "the first the second" reads as a sentence.
        #expect(NotificationShared.alertBody(from: reply)
                == "Two things: \u{2022} the first \u{2022} the second")
    }

    /// What Mike actually saw on his lock screen on 2026-09-09: a morning
    /// briefing with its markup intact. A notification body is plain text.
    @Test func markupDoesNotReachTheLockScreen() {
        let reply = """
        ## Morning Briefing

        **Calendar** is clear. See [the plan](https://example.com/plan) and the
        `deploy` script.
        """

        #expect(NotificationShared.alertBody(from: reply)
                == "Morning Briefing Calendar is clear. See the plan and the deploy script.")
    }

    /// A URL is allowed to contain parentheses. Taking the first `)` as the
    /// end of the link left the tail of the URL on the banner as a stray
    /// bracket — found by running a realistic briefing through it.
    @Test func aLinkWhoseUrlContainsParenthesesIsFullyRemoved() {
        let reply = "See [the plan](https://en.wikipedia.org/wiki/Foo_(bar)) today."
        #expect(NotificationShared.alertBody(from: reply) == "See the plan today.")
    }

    /// The rules only fire on things that are markup. Eating the underscore
    /// out of an identifier, or the star out of an arithmetic expression,
    /// would misreport what Marlow said — a worse bug than the one being
    /// fixed, because it is invisible.
    @Test func proseThatMerelyLooksLikeMarkupIsLeftAlone() {
        #expect(NotificationShared.alertBody(from: "turn_ready fired and 2 * 3 = 6")
                == "turn_ready fired and 2 * 3 = 6")
        #expect(NotificationShared.alertBody(from: "See the [TODO] item")
                == "See the [TODO] item")
        // An opener with no closer was never emphasis.
        #expect(NotificationShared.alertBody(from: "a *lone star") == "a *lone star")
    }

    @Test func emphasisComesOffButItalicisedWordsStay() {
        #expect(NotificationShared.alertBody(from: "that is *really* important")
                == "that is really important")
        #expect(NotificationShared.alertBody(from: "that is __really__ important")
                == "that is really important")
    }

    /// A fence's contents are the message; only the markers go. Emphasis
    /// rules must not fire inside code, where `*` and `_` are literal.
    @Test func codeSurvivesItsFence() {
        let reply = "Run:\n```\nls -la *.py\n```"
        #expect(NotificationShared.alertBody(from: reply) == "Run: ls -la *.py")
    }

    /// A rule carries no words, and once the newlines collapse it would land
    /// as a row of dashes in the middle of a sentence.
    @Test func aHorizontalRuleIsNotContent() {
        #expect(NotificationShared.alertBody(from: "Before\n\n---\n\nAfter")
                == "Before After")
        // The spaced spelling too: its first two characters are also a valid
        // list marker, so the bullet rule used to eat it into "\u{2022} -".
        #expect(NotificationShared.alertBody(from: "Before\n\n- - -\n\nAfter")
                == "Before After")
        #expect(NotificationShared.alertBody(from: "Before\n\n***\n\nAfter")
                == "Before After")
    }

    /// The cap counts the text the user will read, so stripping happens
    /// first — otherwise 300 characters of briefing could be mostly asterisks.
    @Test func theCapAppliesToTheStrippedText() {
        let reply = String(repeating: "**bold** ", count: 200)
        let body = try! #require(NotificationShared.alertBody(from: reply))

        // The exact length is `bodyIsCappedWithAnEllipsis`'s business; what
        // this one pins is that the 300 characters are text and not markup.
        #expect(!body.contains("*"))
        #expect(body.hasSuffix("\u{2026}"))
        #expect(body.count <= NotificationShared.maxBodyCharacters + 1)
    }

    /// Markup with nothing inside it is not a message, and the generic
    /// fail-safe body is the right answer rather than an empty banner.
    @Test func aReplyThatIsOnlyMarkupYieldsNoBody() {
        #expect(NotificationShared.alertBody(from: "###\n\n---\n") == nil)
    }

    @Test func bodyIsCappedWithAnEllipsis() {
        let long = String(repeating: "a", count: NotificationShared.maxBodyCharacters * 3)
        let body = try! #require(NotificationShared.alertBody(from: long))

        #expect(body.count == NotificationShared.maxBodyCharacters + 1)
        #expect(body.hasSuffix("\u{2026}"))
    }

    /// The cap counts characters, not bytes, so an emoji-heavy reply must not
    /// be cut mid-grapheme — Swift's `prefix` on a `String` is grapheme-safe
    /// and this pins that it stays a `String` operation.
    @Test func cappingDoesNotSplitAGrapheme() {
        let long = String(repeating: "👍🏽", count: NotificationShared.maxBodyCharacters * 2)
        let body = try! #require(NotificationShared.alertBody(from: long))

        #expect(body.count == NotificationShared.maxBodyCharacters + 1)
        #expect(body.dropLast().allSatisfy { $0 == "👍🏽" })
    }

    /// A turn can be terminal with nothing worth showing. The generic body is
    /// the fail-safe, so returning nil here is what keeps the extension from
    /// displaying an empty notification.
    @Test func emptyResultTextYieldsNoBody() {
        #expect(NotificationShared.alertBody(from: "") == nil)
        #expect(NotificationShared.alertBody(from: "   \n\t ") == nil)
    }

    /// Not an aesthetic assertion: iOS collapses the banner to roughly two
    /// lines anyway, so a cap far above that would only matter on the expanded
    /// view, where a morning briefing is the case being defended against.
    @Test func theCapIsTheAgreedThreeHundred() {
        #expect(NotificationShared.maxBodyCharacters == 300)
    }
}
