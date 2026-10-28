import Foundation
import Testing
@testable import Marlow

/// `MarkdownParseCache` exists purely to stop `MarkdownMessageView` re-parsing
/// on every body evaluation, so the only way it can go wrong is by returning
/// something a fresh parse wouldn't. Every test here is a variant of "the
/// cached answer agrees with the parser" — that is the whole contract.
///
/// Each test builds its own instance rather than using `.shared`: a bug that
/// only shows on a cold cache would otherwise be invisible to every test after
/// the first one warmed it.
struct MarkdownParseCacheTests {

    /// One of each block type the parser recognizes, so a caching mistake
    /// that only affects one shape (fenced code, say) can't hide.
    private static let sources = [
        "plain paragraph text",
        "# Heading\n\nbody after it",
        "- one\n- two\n- three",
        "1. first\n2. second",
        "```swift\nlet x = 1\n```",
        "mixed **bold** and `code` in a paragraph\n\n## then a heading",
    ]

    @Test func cachedBlocksMatchAFreshParse() {
        let cache = MarkdownParseCache()
        for source in Self.sources {
            #expect(cache.blocks(for: source) == MarkdownBlockParser.parse(source))
        }
    }

    @Test func secondCallReturnsTheSameBlocks() {
        let cache = MarkdownParseCache()
        for source in Self.sources {
            let first = cache.blocks(for: source)
            #expect(cache.blocks(for: source) == first)
        }
    }

    /// Distinct texts must not read each other's entries — keying is the one
    /// place a memoization like this can silently render the wrong message.
    @Test func differentSourcesDoNotShareAnEntry() {
        let cache = MarkdownParseCache()
        #expect(cache.blocks(for: "# One") == [.heading(level: 1, text: "One")])
        #expect(cache.blocks(for: "# Two") == [.heading(level: 1, text: "Two")])
        #expect(cache.blocks(for: "# One") == [.heading(level: 1, text: "One")])
    }

    /// A streamed reply's text grows between ticks. That must miss and
    /// re-parse, not return the prefix it was called with a moment ago.
    @Test func growingTextReparsesRatherThanReturningThePrefix() {
        let cache = MarkdownParseCache()
        #expect(cache.blocks(for: "partial") == [.paragraph("partial")])
        #expect(cache.blocks(for: "partial reply, now complete")
                == [.paragraph("partial reply, now complete")])
    }

    @Test func cachedInlineAttributedMatchesAFreshParse() {
        let cache = MarkdownParseCache()
        let raw = "some **bold** and `code`"
        let expected = try? AttributedString(
            markdown: raw,
            options: AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        )
        #expect(cache.inlineAttributed(for: raw) == expected)
        #expect(cache.inlineAttributed(for: raw) == expected)
    }

    /// The streaming path (`MarkdownMessageView(cached: false)`) must produce
    /// exactly what the cached path does — the flag is a memoization switch,
    /// never a rendering difference.
    @Test func theUncachedInlineParseAgreesWithTheCachedOne() {
        let cache = MarkdownParseCache()
        for source in ["plain", "some **bold**", "a `code` span", "[link](https://example.com)"] {
            #expect(cache.inlineAttributed(for: source) == MarkdownParseCache.parseInline(source))
        }
    }

    @Test func differentInlineSourcesDoNotShareAnEntry() {
        let cache = MarkdownParseCache()
        let one = cache.inlineAttributed(for: "alpha")
        let two = cache.inlineAttributed(for: "beta")
        #expect(one != two)
        #expect(cache.inlineAttributed(for: "alpha") == one)
    }
}
