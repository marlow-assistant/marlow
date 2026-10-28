import { describe, expect, it } from "vitest";
import {
  backlog,
  createPartialTextState,
  firstCharactersBecameVisible,
  ingest,
  ReplyStartAnnouncer,
  revealedMarks,
  segments,
  tick,
  visiblePieces,
  visibleText,
  type PartialTextState,
} from "./partialText";

const ing = (s: PartialTextState, text: string, marks: number[] = [], ticksPerPoll = 40) =>
  ingest(s, text, marks, undefined, ticksPerPoll);
const adv = (s: PartialTextState, jump = 1000) => tick(s, 0, jump);
const rep = (ch: string, n: number) => ch.repeat(n);

describe("PartialTextPlaybackState", () => {
  it("reveals a growing prefix of what the server sent", () => {
    let s = ing(createPartialTextState(), "hello there", [], 4);
    s = adv(s);
    const first = visibleText(s);
    s = adv(s);
    const second = visibleText(s);
    expect(first).toBe("hel");
    // ceil(11/4) = 3 characters a tick, held steady rather than recomputed from
    // the shrinking backlog.
    expect(second).toBe("hello ");
    expect("hello there".startsWith(second)).toBe(true);
  });

  it("drains the backlog within one poll interval", () => {
    // Whatever has arrived is fully shown by the time the next poll's slab lands.
    let s = ing(createPartialTextState(), rep("x", 430), [], 40);
    for (let i = 0; i < 40; i++) s = adv(s);
    expect(backlog(s)).toBe(0);
  });

  it("holds the stride per slab rather than recomputing it from the shrinking backlog", () => {
    // 430 chars, 40 ticks: stride ceil(430/40) = 11, held. A recomputing
    // implementation would reveal 11, then ceil(419/40)=11, ... decaying to 1 and
    // not finishing in 40 ticks. Check the exact per-tick count stays 11.
    let s = ing(createPartialTextState(), rep("x", 430), [], 40);
    const counts: number[] = [];
    let prev = 0;
    for (let i = 0; i < 39; i++) {
      s = adv(s);
      counts.push(s.revealedCount - prev);
      prev = s.revealedCount;
    }
    expect(counts.every((c) => c === 11)).toBe(true);
  });

  it("returns idle (the same state) with nothing left to reveal", () => {
    let s = ing(createPartialTextState(), "hi", [], 1);
    s = adv(s);
    expect(adv(s)).toBe(s);
  });

  it("a large backlog is shown at once rather than performed", () => {
    // A phone back from a tunnel with the whole answer buffered.
    const essay = rep("y", 3000);
    const s = tick(ing(createPartialTextState(), essay, [], 40), 0, 2000);
    expect(visibleText(s)).toBe(essay);
  });

  it("the jump threshold is the backlog at which it jumps: 1999 types, 2000 jumps", () => {
    const typed = tick(ing(createPartialTextState(), rep("y", 1999), [], 40), 0, 2000);
    expect(typed.revealedCount).toBe(50); // ceil(1999/40)
    const jumped = tick(ing(createPartialTextState(), rep("y", 2000), [], 40), 0, 2000);
    expect(jumped.revealedCount).toBe(2000);
  });

  it("a late joiner shows what it missed rather than typing it", () => {
    // The 2026-09-05 bug: open the app on an unread message and watch a reply
    // that finished long ago type itself out from nothing.
    const already = rep("z", 300);
    let s = ing(createPartialTextState({ joinedLate: true }), already, [], 40);
    // Well under the jump threshold, so the large-backlog escape hatch is not
    // what is being exercised here.
    s = tick(s, 0, 2000);
    expect(visibleText(s)).toBe(already);
  });

  it("a late joiner still types what arrives after it", () => {
    // Catching up is a one-time concession to having arrived late, not a mode.
    let s = ing(createPartialTextState({ joinedLate: true }), rep("z", 300), [], 40);
    s = tick(s, 0, 2000);
    s = ing(s, rep("z", 300) + rep("a", 40), [], 40);
    s = tick(s, 0, 2000);
    expect(Array.from(visibleText(s)).length).toBe(301); // ceil(40/40) = one a tick
  });

  it("a turn watched from the start is still typed out", () => {
    // The guard on the fix: "first ingest" is ALSO true of a live turn's first
    // text, and keying the catch-up on that would flatten the feature.
    const firstSlab = rep("z", 300);
    let s = ing(createPartialTextState(), firstSlab, [], 40);
    s = tick(s, 0, 2000);
    expect(visibleText(s)).not.toBe(firstSlab);
  });

  it("a shorter payload never rewinds the bubble", () => {
    let s = ing(createPartialTextState(), "the whole answer", [], 1);
    s = adv(s);
    s = ing(s, "the", [], 1);
    expect(visibleText(s)).toBe("the whole answer");
    expect(backlog(s)).toBe(0);
  });

  it("repeated identical payloads add nothing", () => {
    let s = ing(createPartialTextState(), "same", [], 1);
    s = ing(s, "same", [], 1);
    expect(backlog(s)).toBe(4);
  });

  it("an equal-length payload does not recompute the stride", () => {
    let s = ing(createPartialTextState(), rep("x", 430), [], 40);
    const stride = s.stride;
    s = adv(s);
    s = ing(s, rep("x", 430), [], 40);
    expect(s.stride).toBe(stride);
  });
});

describe("splitting at tool marks", () => {
  it("text is broken where a tool call interrupted it", () => {
    expect(segments("I'll check your calendar.You have three meetings.", [25])).toEqual([
      "I'll check your calendar.",
      "You have three meetings.",
    ]);
  });

  it("a reply with no tool call is one piece", () => {
    expect(segments("just an answer", [])).toEqual(["just an answer"]);
    expect(segments("", [])).toEqual([]);
  });

  it("marks are counted in unicode code points like the server counts", () => {
    // Python len() counts code points. Counting UTF-16 units would put the break
    // one early here, because the thumbs-up is two units.
    const text = "done 👍 next";
    const mark = Array.from("done 👍 ").length; // 7 code points, 8 UTF-16 units
    expect("done 👍 ".length).toBe(8);
    expect(segments(text, [mark])).toEqual(["done 👍 ", "next"]);
  });

  it("astral characters before the mark do not drift it (many emoji)", () => {
    const text = "😀😀😀😀. then 😀 more";
    const mark = Array.from("😀😀😀😀.").length; // 5
    expect(segments(text, [mark])).toEqual(["😀😀😀😀.", " then 😀 more"]);
    // A UTF-16 split at 5 would land inside the third emoji.
    expect(text.slice(0, 5)).not.toBe("😀😀😀😀.");
  });

  it("marks at the edges and duplicates are ignored", () => {
    expect(segments("abcdef", [0, 3, 3, 6, 99, -1])).toEqual(["abc", "def"]);
  });

  it("whitespace-only pieces are dropped", () => {
    expect(segments("abc   def", [3, 6])).toEqual(["abc", "def"]);
    expect(segments("abc\n\ndef", [3, 5])).toEqual(["abc", "def"]);
  });

  it("only marks the typing cursor has reached are exposed", () => {
    // A mark past what has been revealed would split text the reader cannot see.
    let s = ing(createPartialTextState(), "first part.second part.", [12], 4);
    s = adv(s);
    expect(revealedMarks(s)).toEqual([]); // 6 characters in, the mark at 12 is ahead
    for (let i = 0; i < 4; i++) s = adv(s);
    expect(revealedMarks(s)).toEqual([12]);
  });

  it("visiblePieces applies only the passed marks, in code points", () => {
    // 17 code points (the text has 4 emoji), stride ceil(17/17) = 1 a tick.
    let s = ing(createPartialTextState(), "😀😀😀😀. then more", [5], 17);
    for (let i = 0; i < 5; i++) s = adv(s); // 5 code points: the mark at 5 is not < 5
    expect(visibleText(s)).toBe("😀😀😀😀.");
    expect(visiblePieces(s)).toEqual(["😀😀😀😀."]);
    s = adv(s); // 6 shown: the mark is passed, the second piece is whitespace only
    expect(visiblePieces(s)).toEqual(["😀😀😀😀."]);
    for (let i = 0; i < 20; i++) s = adv(s);
    expect(visibleText(s)).toBe("😀😀😀😀. then more");
    expect(visiblePieces(s)).toEqual(["😀😀😀😀.", " then more"]);
  });

  it("replaces marks only if the new list is at least as long", () => {
    let s = ing(createPartialTextState(), "abcdef", [2, 4], 1);
    expect(s.marks).toEqual([2, 4]);
    s = ing(s, "abcdefg", [4], 1); // the server evicted its oldest mark
    expect(s.marks).toEqual([2, 4]);
    s = ing(s, "abcdefgh", [2, 4, 6], 1);
    expect(s.marks).toEqual([2, 4, 6]);
    s = ing(s, "abcdefgh", [1, 3, 5], 1); // equal length replaces (even without growth)
    expect(s.marks).toEqual([1, 3, 5]);
  });

  it("counts reveal progress in code points, not UTF-16 units", () => {
    let s = ing(createPartialTextState(), "😀😀😀😀", [], 4); // stride 1
    s = adv(s);
    expect(visibleText(s)).toBe("😀");
    expect(s.revealedCount).toBe(1);
    expect(backlog(s)).toBe(3);
  });
});

describe("FBK-1: reply started", () => {
  it("tick reports when characters first became visible, with the tick's clock", () => {
    let s = ing(createPartialTextState(), "I'll check.", [], 4);
    expect(s.firstVisibleAt).toBeNull();
    const next = tick(s, 1234);
    expect(firstCharactersBecameVisible(s, next)).toBe(true);
    expect(next.firstVisibleAt).toBe(1234);
    s = next;
    const after = tick(s, 1284);
    expect(firstCharactersBecameVisible(s, after)).toBe(false);
    expect(after.firstVisibleAt).toBe(1234);
  });

  it("nothing is visible before the first tick even though text has arrived", () => {
    const s = ing(createPartialTextState(), "hello", [], 4);
    expect(visibleText(s)).toBe("");
    expect(s.firstVisibleAt).toBeNull();
  });

  it("announces once per placeholder however often the text is republished", () => {
    const a = new ReplyStartAnnouncer();
    expect(a.announce("p1", "")).toBe(false);
    expect(a.announce("p1", "o")).toBe(true);
    expect(a.announce("p1", "on")).toBe(false);
    expect(a.announce("p1", "one two")).toBe(false);
    expect(a.announce("p2", "x")).toBe(true);
  });

  it("a turn that never streams anything announces nothing", () => {
    const a = new ReplyStartAnnouncer();
    expect(a.announce("p1", "")).toBe(false);
  });
});
