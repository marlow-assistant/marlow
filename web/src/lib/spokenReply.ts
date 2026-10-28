// Voice mode's reading of a reply (docs/tasks/voice-mode-android.md), ported
// from iOS `SpokenReply.swift`, whose tests are the cases in
// spokenReply.test.ts. The iOS comments carry the reasons; they are kept here.
//
// One difference: iOS cuts sentences with NLTokenizer, the web with
// `Intl.Segmenter`. Both put trailing whitespace on a sentence and keep "3.5"
// or "e.g." inside one; neither is identical on every edge, and the cases
// below are the ones that matter.

const TERMINATORS = ".!?…";

/** Code points, because the server's tool-call offsets count Unicode scalars
 * (`PartialTextPlaybackState` on iOS, partialText.ts here), not UTF-16 units. */
function scalars(text: string): string[] {
  return Array.from(text);
}

let segmenter: Intl.Segmenter | null = null;
function sentenceRanges(piece: string): string[] {
  segmenter ??= new Intl.Segmenter(undefined, { granularity: "sentence" });
  return Array.from(segmenter.segment(piece), (s) => s.segment);
}

/** Turns a reply that is still being written into whole sentences to speak,
 * one poll at a time. Fed the server's cumulative `partial_text` and its
 * tool-call offsets on every poll, then the authoritative `result_text` once.
 *
 * The one judgement here is when the trailing sentence is finished. Speaking
 * it early cuts a sentence in half; holding it costs time before Marlow's
 * first words — the device spike (2026-10-20) measured 1–2.5s of exactly
 * that on short replies. A trailing sentence is spoken when it visibly ended
 * (a terminator, then whitespace), when it ends in a terminator and the next
 * poll didn't extend it, when a tool call starts after it, or at the end. */
export class SentenceChunker {
  /** Code points of the reply already handed out. */
  consumed = 0;
  private consumedText = "";
  /** The text as of the last poll, to tell "stopped growing" from "growing". */
  private lastSeen = "";

  /** Sentences newly ready to speak. */
  ingest(text: string, marks: number[] = []): string[] {
    const stable = text === this.lastSeen;
    this.lastSeen = text;
    return this.take(scalars(text), marks, false, stable);
  }

  /** The finished reply. `result_text` is authoritative but not guaranteed to
   * extend the streamed text (server/queue.py, `Turn.partial_text`), so carry
   * on after what was already spoken, found by its last words; if they aren't
   * there, stop rather than reread or guess. */
  finish(text: string): string[] {
    if (!text.startsWith(this.consumedText)) {
      const anchor = scalars(this.consumedText).slice(-40).join("");
      const at = anchor ? text.lastIndexOf(anchor) : -1;
      if (at < 0) return [];
      this.consumed = scalars(text.slice(0, at + anchor.length)).length;
    }
    return this.take(scalars(text), [], true, true);
  }

  private take(all: string[], marks: number[], final: boolean, stable: boolean): string[] {
    if (all.length <= this.consumed) return [];
    // Text either side of a tool call is two thoughts even with no
    // punctuation between them, so every piece before a mark is complete.
    const cuts = [...marks.filter((m) => m > this.consumed && m < all.length).sort((a, b) => a - b), all.length];
    const sentences: string[] = [];
    let start = this.consumed;
    for (let i = 0; i < cuts.length; i++) {
      const piece = all.slice(start, cuts[i]);
      const complete = final || i < cuts.length - 1;
      const [found, used] = sentencesIn(piece.join(""), piece.length, complete, stable);
      sentences.push(...found);
      this.consumed = start + used;
      if (used < piece.length) break;
      start = cuts[i];
    }
    this.consumedText = all.slice(0, this.consumed).join("");
    return sentences.map((s) => s.trim()).filter((s) => s !== "");
  }
}

/** The whole sentences in `piece`, and how many code points they used. */
function sentencesIn(piece: string, length: number, complete: boolean, stable: boolean): [string[], number] {
  const found = sentenceRanges(piece);
  if (!complete && found.length > 0) {
    const tail = found[found.length - 1];
    const trimmed = tail.trim();
    const terminated = trimmed !== "" && TERMINATORS.includes(trimmed[trimmed.length - 1]);
    const ended = terminated && (/\s$/.test(tail) || stable);
    if (!ended) found.pop();
  }
  // A complete piece is used whole, whitespace-only included, or the cursor
  // would park before a mark forever.
  if (complete) return [found, length];
  return [found, scalars(found.join("")).length];
}

/** Keeps fenced code out of speech. A code block spans several sentences, so
 * stripping it per sentence would remove only the fragment holding the
 * opening fence and read the rest out (`/code-review high`, 2026-10-20);
 * this carries "inside a fence" from one sentence to the next. */
export class CodeFenceFilter {
  inFence = false;

  /** The parts of `sentence` outside any fence, with one "The code is in the
   * chat." where a block begins. */
  filter(sentence: string): string {
    const parts = sentence.split("```");
    let kept = "";
    parts.forEach((part, index) => {
      if (index > 0) {
        this.inFence = !this.inFence;
        if (this.inFence) kept += " The code is in the chat. ";
      }
      if (!this.inFence) kept += part;
    });
    return kept;
  }
}

const PLAIN_RULES: [RegExp, string][] = [
  [/!?\[([^\]]+)\]\([^)]*\)/g, "$1"],
  [/https?:\/\/\S+/g, "a link"],
  [/^\s*#{1,6}\s*/gm, ""],
  [/^\s*>\s?/gm, ""],
  [/^\s*([-*+•]|\d+[.)])\s+/gm, ""],
  [/^\s*\|?\s*:?-{3,}.*$/gm, ""],
  [/\|/g, ", "],
  [/`/g, ""],
  [/(\*{1,3}|_{2,3})([^*_]+)\1/g, "$2"],
  [/\*+/g, ""],
  [/[ \t]{2,}/g, " "],
];

/** Markdown to something worth hearing. Marlow is told not to format a
 * spoken reply (`SPOKEN_TURN_NOTE`), but won't always comply, and "asterisk
 * asterisk" read aloud is worse than nothing. */
export function plainSpeech(markdown: string): string {
  let s = markdown;
  for (const [pattern, template] of PLAIN_RULES) s = s.replace(pattern, template);
  return s.trim();
}
