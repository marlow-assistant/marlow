import Foundation
import NaturalLanguage

/// Turns a reply that is still being written into whole sentences to speak,
/// one poll at a time (docs/tasks/voice-mode-hands-free.md, "Speaking the
/// reply"). Fed the server's cumulative `partial_text` and its tool-call
/// offsets on every poll, then the authoritative `result_text` once.
///
/// The one judgement here is when the trailing sentence is finished. Speaking
/// it early cuts a sentence in half; holding it costs time before Marlow's
/// first words — the device spike (2026-10-20) measured 1–2.5s of exactly
/// that on short replies. A trailing sentence is spoken when it visibly ended
/// (a terminator, then whitespace), when it ends in a terminator and the next
/// poll didn't extend it, when a tool call starts after it, or at the end.
struct SentenceChunker {
    /// Unicode scalars of the reply already handed out — scalars because the
    /// server's tool-call offsets are (see `PartialTextPlaybackState`).
    private(set) var consumed = 0
    private var consumedText = ""
    /// The text as of the last poll, to tell "stopped growing" from "growing".
    private var lastSeen = ""

    /// Sentences newly ready to speak.
    mutating func ingest(_ text: String, marks: [Int] = []) -> [String] {
        defer { lastSeen = text }
        return take(Array(text.unicodeScalars), marks: marks, final: false, stable: text == lastSeen)
    }

    /// The finished reply. `result_text` is authoritative but not guaranteed
    /// to extend the streamed text (`server/queue.py`, `Turn.partial_text`), so
    /// carry on after what was already spoken, found by its last words; if
    /// they aren't there, stop rather than reread or guess.
    mutating func finish(_ text: String) -> [String] {
        if !text.hasPrefix(consumedText) {
            let anchor = String(consumedText.suffix(40))
            guard !anchor.isEmpty, let hit = text.range(of: anchor, options: .backwards) else { return [] }
            consumed = text[text.startIndex..<hit.upperBound].unicodeScalars.count
        }
        return take(Array(text.unicodeScalars), marks: [], final: true, stable: true)
    }

    private mutating func take(_ scalars: [Unicode.Scalar], marks: [Int], final: Bool, stable: Bool) -> [String] {
        guard scalars.count > consumed else { return [] }
        // Text either side of a tool call is two thoughts even with no
        // punctuation between them, so every piece before a mark is complete.
        let cuts = marks.filter { $0 > consumed && $0 < scalars.count }.sorted() + [scalars.count]
        var sentences: [String] = []
        var start = consumed
        for (index, cut) in cuts.enumerated() {
            let piece = String(String.UnicodeScalarView(scalars[start..<cut]))
            let complete = final || index < cuts.count - 1
            let (found, used) = Self.sentences(in: piece, complete: complete, stable: stable)
            sentences += found
            consumed = start + used
            if used < piece.unicodeScalars.count { break }
            start = cut
        }
        consumedText = String(String.UnicodeScalarView(scalars[0..<consumed]))
        return sentences
    }

    /// The whole sentences in `piece`, and how many scalars they used.
    private static func sentences(in piece: String, complete: Bool, stable: Bool) -> ([String], Int) {
        let tokenizer = NLTokenizer(unit: .sentence)
        tokenizer.string = piece
        var ranges: [Range<String.Index>] = []
        tokenizer.enumerateTokens(in: piece.startIndex..<piece.endIndex) { range, _ in
            ranges.append(range)
            return true
        }
        if !complete, let last = ranges.last {
            let tail = piece[last]
            let trimmed = tail.trimmingCharacters(in: .whitespacesAndNewlines)
            let terminated = trimmed.last.map { ".!?…".contains($0) } == true
            let ended = terminated && (tail.last?.isWhitespace == true || stable)
            if !ended { ranges.removeLast() }
        }
        let found = ranges.map { String(piece[$0]) }
        // A complete piece is used whole, whitespace-only included, or the
        // cursor would park before a mark forever.
        if complete { return (found, piece.unicodeScalars.count) }
        guard let last = ranges.last else { return ([], 0) }
        return (found, piece[piece.startIndex..<last.upperBound].unicodeScalars.count)
    }
}

/// Keeps fenced code out of speech. A code block spans several sentences, so
/// stripping it per sentence would remove only the fragment holding the
/// opening fence and read the rest out (`/code-review high`, 2026-10-20);
/// this carries "inside a fence" from one sentence to the next.
struct CodeFenceFilter {
    private(set) var inFence = false

    /// The parts of `sentence` outside any fence, with one "The code is in
    /// the chat." where a block begins.
    mutating func filter(_ sentence: String) -> String {
        let parts = sentence.components(separatedBy: "```")
        var kept = ""
        for (index, part) in parts.enumerated() {
            if index > 0 {
                inFence.toggle()
                if inFence { kept += " The code is in the chat. " }
            }
            if !inFence { kept += part }
        }
        return kept
    }
}

/// Markdown to something worth hearing. Marlow is told not to format a spoken
/// reply (`SPOKEN_TURN_NOTE`), but won't always comply, and "asterisk
/// asterisk" read aloud is worse than nothing.
enum SpeechText {
    static func plain(_ markdown: String) -> String {
        var s = markdown
        let rules: [(String, String)] = [
            (#"!?\[([^\]]+)\]\([^)]*\)"#, "$1"),
            (#"https?://\S+"#, "a link"),
            (#"(?m)^\s*#{1,6}\s*"#, ""),
            (#"(?m)^\s*>\s?"#, ""),
            (#"(?m)^\s*([-*+•]|\d+[.)])\s+"#, ""),
            (#"(?m)^\s*\|?\s*:?-{3,}.*$"#, ""),
            (#"\|"#, ", "),
            (#"`"#, ""),
            (#"(\*{1,3}|_{2,3})([^*_]+)\1"#, "$2"),
            (#"\*+"#, ""),
            (#"[ \t]{2,}"#, " "),
        ]
        for (pattern, template) in rules {
            s = s.replacingOccurrences(of: pattern, with: template, options: .regularExpression)
        }
        return s.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}
