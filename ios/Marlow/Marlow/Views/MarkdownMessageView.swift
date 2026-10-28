import SwiftUI

/// Renders a message's markdown with real block structure, not just the inline
/// styling `AttributedString(markdown:)` gives for free.
///
/// Block-level decision (docs/tasks/ios-chat-ux-gaps.md §4): a lightweight,
/// hand-rolled line scanner recognizing fenced code, headings (`#`…`######`),
/// bullet/numbered lists and GFM tables — everything else is a paragraph.
/// That covers what Marlow actually writes by default (headings, bullets,
/// bold, fenced code) without pulling in a markdown parser dependency for
/// block types (nested blockquotes, nested lists) it doesn't use. Tables were
/// first in that "doesn't use" list and turned out not to be: Marlow writes
/// them for comparisons, and they showed as raw pipe text (Mike, 2026-10-14).
/// Inline runs
/// within each block still go through `AttributedString(markdown:)`, so bold,
/// italic, inline code spans and links all work inside headings and list
/// items too, not just plain paragraphs.
///
/// Fenced code is deliberately its own case, not folded into inline code
/// spans: it renders monospaced with `.textSelection(.enabled)` and its own
/// horizontal scroll, so a long line doesn't wrap and lose formatting — and,
/// load-bearing per the doc, stays selectable/copyable exactly like every
/// other message (task 3's whole-message copy still covers the raw text too).
enum MarkdownBlock: Equatable {
    case heading(level: Int, text: String)
    case bullet(items: [String])
    /// `start` is the first item's source number; later items count up from
    /// it, as in CommonMark. Carried rather than re-derived so a list that
    /// resumes at "4." after an intervening paragraph doesn't restart at "1."
    /// (the paragraph closes the first block and opens a fresh one) —
    /// docs/tasks/ios-chat-ux-gaps.md §5.
    case numbered(start: Int, items: [String])
    case code(text: String, language: String?)
    /// Every row, header included, has exactly `alignments.count` cells:
    /// short rows are padded with empty cells and long ones cut, as GFM does,
    /// so the renderer never has to index past a row's end.
    case table(header: [String], alignments: [TableAlignment], rows: [[String]])
    case paragraph(String)
}

/// A table column's alignment, from its delimiter cell (`:--`, `:-:`, `--:`).
enum TableAlignment: Equatable {
    case none, left, center, right
}

enum MarkdownBlockParser {
    static func parse(_ source: String) -> [MarkdownBlock] {
        var blocks: [MarkdownBlock] = []
        let lines = source.components(separatedBy: "\n")
        var i = 0

        var paragraphLines: [String] = []
        func flushParagraph() {
            guard !paragraphLines.isEmpty else { return }
            let text = paragraphLines.joined(separator: "\n")
            if !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                blocks.append(.paragraph(text))
            }
            paragraphLines.removeAll()
        }

        while i < lines.count {
            let line = lines[i]
            let trimmed = line.trimmingCharacters(in: .whitespaces)

            if trimmed.hasPrefix("```") {
                flushParagraph()
                let language = String(trimmed.dropFirst(3)).trimmingCharacters(in: .whitespaces)
                var codeLines: [String] = []
                i += 1
                while i < lines.count && !lines[i].trimmingCharacters(in: .whitespaces).hasPrefix("```") {
                    codeLines.append(lines[i])
                    i += 1
                }
                // consume the closing fence, if there was one
                if i < lines.count { i += 1 }
                blocks.append(.code(text: codeLines.joined(separator: "\n"), language: language.isEmpty ? nil : language))
                continue
            }

            if let headingMatch = headingLevel(of: trimmed) {
                flushParagraph()
                blocks.append(.heading(level: headingMatch.level, text: headingMatch.text))
                i += 1
                continue
            }

            if isBulletLine(trimmed) {
                flushParagraph()
                var items: [String] = []
                while i < lines.count, isBulletLine(lines[i].trimmingCharacters(in: .whitespaces)) {
                    items.append(bulletText(lines[i].trimmingCharacters(in: .whitespaces)))
                    i += 1
                }
                blocks.append(.bullet(items: items))
                continue
            }

            if isNumberedLine(trimmed) {
                flushParagraph()
                let start = number(of: trimmed)
                var items: [String] = []
                while i < lines.count, isNumberedLine(lines[i].trimmingCharacters(in: .whitespaces)) {
                    items.append(numberedText(lines[i].trimmingCharacters(in: .whitespaces)))
                    i += 1
                }
                blocks.append(.numbered(start: start, items: items))
                continue
            }

            // A table can interrupt a paragraph: its header is the line
            // before the delimiter row, whatever came above it.
            if i + 1 < lines.count, let alignments = tableAlignments(header: line, delimiter: lines[i + 1]) {
                flushParagraph()
                let header = tableCells(line, count: alignments.count)
                var rows: [[String]] = []
                i += 2
                while i < lines.count, isTableRow(lines[i]), !startsOtherBlock(lines[i]) {
                    rows.append(tableCells(lines[i], count: alignments.count))
                    i += 1
                }
                blocks.append(.table(header: header, alignments: alignments, rows: rows))
                continue
            }

            if trimmed.isEmpty {
                flushParagraph()
                i += 1
                continue
            }

            paragraphLines.append(line)
            i += 1
        }

        flushParagraph()
        return blocks
    }

    private static func headingLevel(of line: String) -> (level: Int, text: String)? {
        guard line.hasPrefix("#") else { return nil }
        var level = 0
        var chars = Substring(line)
        while chars.first == "#" && level < 6 {
            level += 1
            chars = chars.dropFirst()
        }
        guard chars.first == " " else { return nil }
        return (level, String(chars.dropFirst()).trimmingCharacters(in: .whitespaces))
    }

    private static func isBulletLine(_ line: String) -> Bool {
        (line.hasPrefix("- ") || line.hasPrefix("* ") || line.hasPrefix("+ "))
    }

    private static func bulletText(_ line: String) -> String {
        String(line.dropFirst(2)).trimmingCharacters(in: .whitespaces)
    }

    private static func isNumberedLine(_ line: String) -> Bool {
        guard let dotIndex = line.firstIndex(of: ".") else { return false }
        let prefix = line[line.startIndex..<dotIndex]
        return !prefix.isEmpty && prefix.allSatisfy(\.isNumber) && line[dotIndex...].hasPrefix(". ")
    }

    /// Capped at nine digits, CommonMark's own limit, and falls back to 1
    /// past it. Not just tidiness: the renderer adds the item index to this,
    /// and an uncapped "9223372036854775807." would overflow that addition and
    /// trap — in a view body, on a persisted message, so every launch.
    private static func number(of line: String) -> Int {
        guard let dotIndex = line.firstIndex(of: ".") else { return 1 }
        let digits = line[line.startIndex..<dotIndex]
        guard digits.count <= 9 else { return 1 }
        return Int(digits) ?? 1
    }

    /// The column alignments if `header` and `delimiter` open a GFM table:
    /// both contain a pipe, every delimiter cell is dashes with optional
    /// colons, and the counts match. A mismatch is not a table in GFM either —
    /// it stays a paragraph rather than guessing which columns line up.
    private static func tableAlignments(header: String, delimiter: String) -> [TableAlignment]? {
        guard isTableRow(header), isTableRow(delimiter) else { return nil }
        let cells = splitTableRow(delimiter)
        var alignments: [TableAlignment] = []
        for cell in cells {
            let leading = cell.hasPrefix(":")
            let trailing = cell.hasSuffix(":") && cell.count > 1
            let dashes = cell.dropFirst(leading ? 1 : 0).dropLast(trailing ? 1 : 0)
            guard !dashes.isEmpty, dashes.allSatisfy({ $0 == "-" }) else { return nil }
            switch (leading, trailing) {
            case (true, true): alignments.append(.center)
            case (true, false): alignments.append(.left)
            case (false, true): alignments.append(.right)
            case (false, false): alignments.append(.none)
            }
        }
        guard splitTableRow(header).count == alignments.count else { return nil }
        return alignments
    }

    /// A body row is any non-blank line with an unescaped pipe; the table
    /// ends at the first line without one. (GFM keeps going until a blank
    /// line or another block, but a pipeless line is almost always the prose
    /// that follows, and swallowing it into a one-cell row reads worse.)
    private static func isTableRow(_ line: String) -> Bool {
        var escaped = false
        for ch in line {
            if ch == "|" && !escaped { return true }
            escaped = ch == "\\" && !escaped
        }
        return false
    }

    /// A table ends where another block starts, as in GFM, even if that
    /// line has a pipe: "- x | y" straight after a table is a bullet.
    private static func startsOtherBlock(_ line: String) -> Bool {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        return trimmed.hasPrefix("```") || headingLevel(of: trimmed) != nil
            || isBulletLine(trimmed) || isNumberedLine(trimmed)
    }

    /// `row`'s cells padded or cut to `count`.
    private static func tableCells(_ row: String, count: Int) -> [String] {
        let cells = splitTableRow(row)
        return Array(cells.prefix(count)) + Array(repeating: "", count: max(0, count - cells.count))
    }

    /// Splits on unescaped pipes, dropping the optional outer ones, and
    /// unescapes `\|` to a literal pipe in the cell text.
    private static func splitTableRow(_ row: String) -> [String] {
        var cells: [String] = []
        var current = ""
        var escaped = false
        let trimmedRow = row.trimmingCharacters(in: .whitespaces)
        for ch in trimmedRow {
            if escaped {
                // Only `\|` is ours to unescape; any other escape is left
                // for the inline parse.
                if ch != "|" { current.append("\\") }
                current.append(ch)
                escaped = false
            } else if ch == "\\" {
                escaped = true
            } else if ch == "|" {
                cells.append(current)
                current = ""
            } else {
                current.append(ch)
            }
        }
        if escaped { current.append("\\") }
        cells.append(current)
        if trimmedRow.hasPrefix("|") { cells.removeFirst() }
        // A closing pipe leaves an empty last cell; an escaped one (`\|`)
        // doesn't, because it never split.
        if trimmedRow.hasSuffix("|"), cells.last?.isEmpty == true { cells.removeLast() }
        return cells.map { $0.trimmingCharacters(in: .whitespaces) }
    }

    private static func numberedText(_ line: String) -> String {
        guard let dotIndex = line.firstIndex(of: ".") else { return line }
        return String(line[line.index(after: dotIndex)...]).trimmingCharacters(in: .whitespaces)
    }
}

/// Memoizes both halves of rendering a message body, because `body` runs far
/// more often than a message's text changes: `ChatStore.messages` is
/// `@Published`, so every poll tick while a turn is in flight re-evaluates
/// every visible row, and any `ChatView` state write does the same. Before
/// this, each of those re-scanned the whole message *and* rebuilt every
/// inline run through `AttributedString(markdown:)` — a Foundation parse —
/// on the main thread (found on device 2026-09-06: enough main-thread work
/// during a scroll to leave the LazyVStack's rows unlaid-out, so the
/// transcript rendered blank while the scroll indicator still showed a
/// correct content size).
///
/// Keyed by the source text, which is what makes it safe: a streamed reply
/// whose text grows simply misses and re-parses. `NSCache` rather than a
/// dictionary, for the same reason `ChatStore.imageCache` is one — it evicts
/// under memory pressure, which a long transcript's worth of parses would
/// otherwise never do.
///
/// Not `private` only so `MarkdownParseCacheTests` can check that a cache hit
/// still returns what a fresh parse would.
final class MarkdownParseCache {
    static let shared = MarkdownParseCache()

    private final class Box<T> { let value: T; init(_ value: T) { self.value = value } }

    private let blockCache = NSCache<NSString, Box<[MarkdownBlock]>>()
    private let inlineCache = NSCache<NSString, Box<AttributedString?>>()

    func blocks(for text: String) -> [MarkdownBlock] {
        let key = text as NSString
        if let hit = blockCache.object(forKey: key) { return hit.value }
        let parsed = MarkdownBlockParser.parse(text)
        blockCache.setObject(Box(parsed), forKey: key)
        return parsed
    }

    /// Caches the failure too (`nil`), so unparseable text doesn't retry the
    /// throwing initializer on every single body evaluation.
    func inlineAttributed(for raw: String) -> AttributedString? {
        let key = raw as NSString
        if let hit = inlineCache.object(forKey: key) { return hit.value }
        let parsed = Self.parseInline(raw)
        inlineCache.setObject(Box(parsed), forKey: key)
        return parsed
    }

    /// The uncached parse, shared so the streaming path and the cache fill
    /// cannot drift to different `MarkdownParsingOptions`.
    static func parseInline(_ raw: String) -> AttributedString? {
        try? AttributedString(
            markdown: raw,
            options: AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        )
    }
}

/// Renders one message body as its parsed markdown blocks.
struct MarkdownMessageView: View {
    let text: String

    /// False for a reply still being streamed. `ChatStore` advances
    /// `streamedText` every 50ms (`policy.partialTextTick`), so a
    /// thirty-second answer renders through ~600 *different* strings, each one
    /// a prefix of the next. Memoizing those would be all cost and no benefit:
    /// a growing string never hits its own earlier entry, so every tick still
    /// parses — and each dead prefix is then retained holding a full parse of
    /// everything before it, which is quadratic in the reply's length for a
    /// value nothing will ever read again. Cache the settled text, which is
    /// what the transcript actually re-renders; parse the moving one.
    var cached = true

    /// See `TableCellWidth`. Scaled, so a larger text size wraps at the same
    /// number of words rather than squeezing into a column sized for 17pt.
    @ScaledMetric(relativeTo: .subheadline) private var tableCellMaxWidth: CGFloat = 220

    private var blocks: [MarkdownBlock] {
        cached ? MarkdownParseCache.shared.blocks(for: text) : MarkdownBlockParser.parse(text)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                blockView(block)
            }
        }
    }

    @ViewBuilder
    private func blockView(_ block: MarkdownBlock) -> some View {
        switch block {
        case .heading(let level, let text):
            inlineText(text)
                .font(headingFont(level))

        case .paragraph(let text):
            inlineText(text)

        case .bullet(let items):
            VStack(alignment: .leading, spacing: 4) {
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    listItem(marker: "\u{2022}", text: item)
                }
            }

        case .numbered(let start, let items):
            VStack(alignment: .leading, spacing: 4) {
                ForEach(Array(items.enumerated()), id: \.offset) { index, item in
                    listItem(marker: "\(start + index).", text: item)
                }
            }

        case .code(let code, _):
            ScrollView(.horizontal, showsIndicators: false) {
                Text(code)
                    .font(.system(.footnote, design: .monospaced))
                    .textSelection(.enabled)
                    .padding(8)
            }
            .background(Color(.tertiarySystemBackground))
            .clipShape(RoundedRectangle(cornerRadius: 8))

        case .table(let header, let alignments, let rows):
            tableView(header: header, alignments: alignments, rows: rows)
        }
    }

    /// A `Grid`, so every cell in a column shares its width, inside its own
    /// horizontal scroll like fenced code: a table wider than the bubble
    /// scrolls instead of squeezing every column to a word per line. Each
    /// cell wraps at `tableCellMaxWidth`, so one long note can't stretch its
    /// column (and the scroll) to a paragraph's length on a single line.
    private func tableView(header: [String], alignments: [TableAlignment], rows: [[String]]) -> some View {
        ScrollView(.horizontal, showsIndicators: false) {
            Grid(alignment: .topLeading, horizontalSpacing: 0, verticalSpacing: 0) {
                GridRow {
                    ForEach(Array(header.enumerated()), id: \.offset) { column, cell in
                        tableCell(cell, alignment: alignments[column], isHeader: true)
                    }
                }
                ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                    Divider()
                    GridRow {
                        ForEach(Array(row.enumerated()), id: \.offset) { column, cell in
                            tableCell(cell, alignment: alignments[column], isHeader: false)
                        }
                    }
                }
            }
            .font(.subheadline)
            // Its ideal height, no more. The header cells are flexible
            // vertically (below), and without this the grid hands them any
            // height the scroll view offers — a header row twice too tall.
            .fixedSize(horizontal: false, vertical: true)
            // Clip first, then stroke: the other way round the clip takes
            // the stroke's outer half.
            .clipShape(RoundedRectangle(cornerRadius: 8))
            .overlay(RoundedRectangle(cornerRadius: 8).stroke(Color(.separator)))
            // Room for the stroke, which the scroll view would otherwise clip.
            .padding(1)
        }
    }

    /// The header's tint fills the whole cell, not just its text, because
    /// the cell is flexible both ways: the grid stretches it to its column's
    /// width and its row's height.
    private func tableCell(_ raw: String, alignment: TableAlignment, isHeader: Bool) -> some View {
        TableCellWidth(max: tableCellMaxWidth) {
            inlineText(raw)
                .fontWeight(isHeader ? .semibold : nil)
                .multilineTextAlignment(alignment.textAlignment)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 6)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: alignment.frameAlignment)
        // A `Rectangle` rather than `.background(Color)`: that's the
        // ShapeStyle overload, which on iOS 26 rounds the tint to the
        // bubble's container shape — a rounded corner on every header cell.
        .background { Rectangle().fill(isHeader ? Color(.tertiarySystemBackground) : Color.clear) }
    }

    /// A list item's text sits beside its marker in an HStack, and that is
    /// what truncated items with "…" instead of wrapping (Mike's screenshot,
    /// 2026-09-21) while paragraphs — direct children of the VStack — wrapped
    /// fine. An HStack sizes its children with the width it has left after
    /// the others, and inside the transcript's lazy stack the row's height can
    /// be settled at a width the text then doesn't get, leaving it one line
    /// tall. `fixedSize(vertical:)` makes the text always take the height its
    /// wrapped lines need; the marker is fixed both ways so it can neither be
    /// squeezed nor wrap its own "10." onto two lines.
    private func listItem(marker: String, text: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text(marker)
                .fixedSize()
            inlineText(text)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func inlineText(_ raw: String) -> Text {
        if let attributed = cached
            ? MarkdownParseCache.shared.inlineAttributed(for: raw)
            : MarkdownParseCache.parseInline(raw) {
            return Text(attributed)
        }
        return Text(raw)
    }

    private func headingFont(_ level: Int) -> Font {
        switch level {
        case 1: return .title2.bold()
        case 2: return .title3.bold()
        default: return .headline
        }
    }
}

/// Caps the width a table cell's text is offered. Inside a horizontal scroll
/// the grid proposes no width at all, so the text would take its one-line
/// ideal — and `.frame(maxWidth:)` doesn't help, because it passes the nil
/// proposal through and only clamps the result, leaving the text unwrapped
/// and overflowing its frame. This offers at most `max`, so text that fits
/// keeps its natural width and longer text wraps.
private struct TableCellWidth: Layout {
    let max: CGFloat

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = min(proposal.width ?? max, max)
        return subviews.first?.sizeThatFits(ProposedViewSize(width: width, height: nil)) ?? .zero
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        subviews.first?.place(at: bounds.origin, proposal: ProposedViewSize(bounds.size))
    }
}

private extension TableAlignment {
    var textAlignment: TextAlignment {
        switch self {
        case .none, .left: .leading
        case .center: .center
        case .right: .trailing
        }
    }

    var frameAlignment: Alignment {
        switch self {
        case .none, .left: .topLeading
        case .center: .top
        case .right: .topTrailing
        }
    }
}
