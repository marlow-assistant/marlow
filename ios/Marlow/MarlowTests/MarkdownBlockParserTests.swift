import Testing
@testable import Marlow

/// Numbered lists carry their source start number
/// (docs/tasks/ios-chat-ux-gaps.md §5) — before, the renderer counted from 1
/// for every block, so a list resumed after a paragraph restarted at "1.".
struct MarkdownBlockParserTests {

    @Test func aListStartingAtOneStartsAtOne() {
        #expect(MarkdownBlockParser.parse("1. first\n2. second")
                == [.numbered(start: 1, items: ["first", "second"])])
    }

    @Test func aListResumedAfterAParagraphKeepsItsNumber() {
        let source = "1. one\n2. two\n3. three\n\nAn aside.\n\n4. four\n5. five"
        #expect(MarkdownBlockParser.parse(source) == [
            .numbered(start: 1, items: ["one", "two", "three"]),
            .paragraph("An aside."),
            .numbered(start: 4, items: ["four", "five"]),
        ])
    }

    /// CommonMark takes only the first number; later items count up from it,
    /// so the lazy "1. 1. 1." style still renders 1, 2, 3.
    @Test func laterItemNumbersAreIgnored() {
        #expect(MarkdownBlockParser.parse("1. a\n1. b\n1. c")
                == [.numbered(start: 1, items: ["a", "b", "c"])])
    }

    /// The renderer computes `start + index`; an uncapped start near
    /// `Int.max` would trap there on the second item.
    @Test func aStartPastNineDigitsFallsBackToOne() {
        #expect(MarkdownBlockParser.parse("9223372036854775807. a\n2. b")
                == [.numbered(start: 1, items: ["a", "b"])])
        #expect(MarkdownBlockParser.parse("99999999999999999999. big")
                == [.numbered(start: 1, items: ["big"])])
    }

    @Test func nineDigitsIsStillAStart() {
        #expect(MarkdownBlockParser.parse("123456789. a")
                == [.numbered(start: 123456789, items: ["a"])])
    }
}

/// GFM tables (2026-10-14): before, these fell through to a paragraph and
/// showed as raw pipe text.
struct MarkdownTableParserTests {

    @Test func aTableParsesHeaderAlignmentsAndRows() {
        let source = """
        | Insurer | Annual | Deductible | Notes |
        |:--------|-------:|:----------:|-------|
        | **Current** | $1,240 | $500 | Auto-renews |
        | Northline | $1,130 | $500 | Valid until `Oct 30` |
        """
        #expect(MarkdownBlockParser.parse(source) == [
            .table(
                header: ["Insurer", "Annual", "Deductible", "Notes"],
                alignments: [.left, .right, .center, .none],
                // Inline markdown stays in the cell text, for the inline parse.
                rows: [
                    ["**Current**", "$1,240", "$500", "Auto-renews"],
                    ["Northline", "$1,130", "$500", "Valid until `Oct 30`"],
                ]
            ),
        ])
    }

    @Test func outerPipesAreOptional() {
        #expect(MarkdownBlockParser.parse("a | b\n--|--\n1 | 2") == [
            .table(header: ["a", "b"], alignments: [.none, .none], rows: [["1", "2"]]),
        ])
    }

    /// GFM: missing cells are empty, extra cells are dropped — so the
    /// renderer can index every row by column.
    @Test func raggedRowsArePaddedOrCutToTheHeader() {
        let source = "| a | b | c |\n|---|---|---|\n| 1 |\n| 1 | 2 | 3 | 4 |"
        #expect(MarkdownBlockParser.parse(source) == [
            .table(
                header: ["a", "b", "c"], alignments: [.none, .none, .none],
                rows: [["1", "", ""], ["1", "2", "3"]]
            ),
        ])
    }

    @Test func anEscapedPipeIsCellText() {
        let source = "| cmd | note |\n|---|---|\n| `a \\| b` | x\\|y \\* z |"
        #expect(MarkdownBlockParser.parse(source) == [
            .table(header: ["cmd", "note"], alignments: [.none, .none],
                   rows: [["`a | b`", "x|y \\* z"]]),
        ])
    }

    @Test func emptyCellsSurvive() {
        #expect(MarkdownBlockParser.parse("| a | b |\n|---|---|\n|  | 2 |") == [
            .table(header: ["a", "b"], alignments: [.none, .none], rows: [["", "2"]]),
        ])
    }

    /// The header line ends whatever paragraph was above it, and the first
    /// line without a pipe ends the table.
    @Test func aTableSitsBetweenParagraphsWithoutBlankLines() {
        let source = "Here they are:\n| a | b |\n|---|---|\n| 1 | 2 |\nThat's all."
        #expect(MarkdownBlockParser.parse(source) == [
            .paragraph("Here they are:"),
            .table(header: ["a", "b"], alignments: [.none, .none], rows: [["1", "2"]]),
            .paragraph("That's all."),
        ])
    }

    /// GFM ends a table where another block starts, pipe or not.
    @Test func aListOrHeadingRightAfterATableEndsIt() {
        let source = "| a | b |\n|---|---|\n| 1 | 2 |\n- note: x | y\n## Next | up"
        #expect(MarkdownBlockParser.parse(source) == [
            .table(header: ["a", "b"], alignments: [.none, .none], rows: [["1", "2"]]),
            .bullet(items: ["note: x | y"]),
            .heading(level: 2, text: "Next | up"),
        ])
    }

    @Test func aHeaderWithNoBodyIsStillATable() {
        #expect(MarkdownBlockParser.parse("| a | b |\n|:-:|--:|") == [
            .table(header: ["a", "b"], alignments: [.center, .right], rows: []),
        ])
    }

    /// Not tables: GFM requires the delimiter row's cell count to match the
    /// header's, and dashes in every delimiter cell.
    @Test func nearMissesStayParagraphs() {
        #expect(MarkdownBlockParser.parse("| a | b |\n|---|")
                == [.paragraph("| a | b |\n|---|")])
        #expect(MarkdownBlockParser.parse("| a | b |\n|---|:x:|")
                == [.paragraph("| a | b |\n|---|:x:|")])
        #expect(MarkdownBlockParser.parse("| a | b |\n| : | : |")
                == [.paragraph("| a | b |\n| : | : |")])
    }

    /// A reply mid-stream: the header has arrived but not the delimiter, so
    /// it reads as a paragraph until the next tick brings the rest.
    @Test func aHeaderAloneIsAParagraph() {
        #expect(MarkdownBlockParser.parse("| a | b |") == [.paragraph("| a | b |")])
    }

    @Test func aPipeInProseIsNotATable() {
        #expect(MarkdownBlockParser.parse("this | that\nand more")
                == [.paragraph("this | that\nand more")])
    }
}

