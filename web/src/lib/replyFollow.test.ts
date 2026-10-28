import { describe, expect, it } from "vitest";
import {
  action,
  endsFollowing,
  FollowTracker,
  isFarFromBottom,
  replyHasWords,
  shouldStartFollowing,
  type Geometry,
} from "./replyFollow";

// A phone-ish viewport: header bottom at 100, viewport bottom at 800, so 700pt
// readable.
function geometry(o: {
  userTop: number;
  userHeight?: number;
  replyHeight: number;
  gap?: number;
}): Geometry {
  const userHeight = o.userHeight ?? 60;
  const gap = o.gap ?? 12;
  const replyTop = o.userTop + userHeight + gap;
  return {
    userTop: o.userTop,
    userHeight,
    replyTop,
    replyBottom: replyTop + o.replyHeight,
    visibleTop: 100,
    visibleBottom: 800,
  };
}

describe("ReplyFollow", () => {
  it("holds while the reply is on screen", () => {
    expect(action(geometry({ userTop: 500, replyHeight: 100 }))).toBe("hold");
  });

  it("follows a reply that has run off the bottom", () => {
    // The reported bug: bubble and one line on screen, the rest below.
    expect(action(geometry({ userTop: 600, replyHeight: 300 }))).toBe("followReply");
  });

  it("pins the user message once the exchange outgrows the viewport", () => {
    // 60 + 12 + 640 = 712 > 700: one more follow would hide his message.
    const g = geometry({ userTop: 500, replyHeight: 640 });
    expect(action(g)).toBe("pinUser");
    expect(endsFollowing(action(g))).toBe(true);
  });

  it("still follows when the exchange exactly fills", () => {
    // 60 + 12 + 628 = 700: fits to the point, so it is still a follow.
    expect(action(geometry({ userTop: 150, replyHeight: 628 }))).toBe("followReply");
  });

  it("pin decision does not depend on scroll position", () => {
    for (const userTop of [-400, 100, 900]) {
      expect(action(geometry({ userTop, replyHeight: 640 }))).toBe("pinUser");
    }
  });

  it("long dictation follows the reply instead of pinning the message", () => {
    const g = geometry({ userTop: -300, userHeight: 900, replyHeight: 200 });
    expect(action(g)).toBe("followReply");
  });

  it("long dictation pins the reply once the reply outgrows the viewport", () => {
    const g = geometry({ userTop: -1000, userHeight: 900, replyHeight: 710 });
    expect(action(g)).toBe("pinReply");
    expect(endsFollowing(action(g))).toBe(true);
  });

  it("long dictation holds while the reply fits", () => {
    // Reply bottom at 700, inside the 800 bottom edge.
    expect(action(geometry({ userTop: -300, userHeight: 900, replyHeight: 88 }))).toBe("hold");
  });

  it("sub-point noise is not a move", () => {
    const g = geometry({ userTop: 500, replyHeight: 100 });
    g.replyBottom = 800.25;
    expect(action(g)).toBe("hold");
  });

  it("unmeasured viewport holds", () => {
    const g = geometry({ userTop: 600, replyHeight: 300 });
    g.visibleTop = 0;
    g.visibleBottom = 0;
    expect(action(g)).toBe("hold");
  });

  it("only pins end following", () => {
    expect(endsFollowing("hold")).toBe(false);
    expect(endsFollowing("followReply")).toBe(false);
    expect(endsFollowing("pinUser")).toBe(true);
    expect(endsFollowing("pinReply")).toBe(true);
  });

  // SCR-6 worked cases, verbatim from the contract (visible 100..800).
  it("SCR-6 worked cases", () => {
    expect(action(geometry({ userTop: 500, replyHeight: 100 }))).toBe("hold");
    expect(action(geometry({ userTop: 600, replyHeight: 300 }))).toBe("followReply");
    expect(action(geometry({ userTop: 500, replyHeight: 640 }))).toBe("pinUser");
    expect(action(geometry({ userTop: 150, replyHeight: 628 }))).toBe("followReply");
    expect(action(geometry({ userTop: 150, userHeight: 900, replyHeight: 200 }))).toBe("followReply");
    expect(action(geometry({ userTop: 150, userHeight: 900, replyHeight: 710 }))).toBe("pinReply");
  });

  it("the 0.5 tolerance is the boundary: 700.5 follows, 700.6 pins", () => {
    const at = (replyHeight: number) => action(geometry({ userTop: 150, replyHeight }));
    expect(at(628.5)).toBe("followReply"); // span 700.5, not > 700.5
    expect(at(628.6)).toBe("pinUser"); // span 700.6
  });
});

describe("FollowTracker (SCR-8)", () => {
  it("the first measurement is a baseline, not news", () => {
    const t = new FollowTracker();
    expect(t.isNews(geometry({ userTop: 500, replyHeight: 100 }))).toBe(false);
  });

  it("a scroll that moves both rows together is not news", () => {
    const t = new FollowTracker();
    t.isNews(geometry({ userTop: 500, replyHeight: 100 }));
    expect(t.isNews(geometry({ userTop: 300, replyHeight: 100 }))).toBe(false);
  });

  it("growth beyond the tolerance is news, sub-point jitter is not", () => {
    const t = new FollowTracker();
    t.isNews(geometry({ userTop: 500, replyHeight: 100 }));
    expect(t.isNews(geometry({ userTop: 500, replyHeight: 100.4 }))).toBe(false);
    expect(t.isNews(geometry({ userTop: 500, replyHeight: 101 }))).toBe(true);
  });

  it("a change of visible bounds is news", () => {
    const t = new FollowTracker();
    t.isNews(geometry({ userTop: 500, replyHeight: 100 }));
    const g = geometry({ userTop: 500, replyHeight: 100 });
    g.visibleBottom = 600;
    expect(t.isNews(g)).toBe(true);
    expect(t.isNews(g)).toBe(false); // the baseline moved
  });

  it("the baseline keeps moving when a frame is news", () => {
    const t = new FollowTracker();
    t.isNews(geometry({ userTop: 500, replyHeight: 100 }));
    expect(t.isNews(geometry({ userTop: 500, replyHeight: 200 }))).toBe(true);
    expect(t.isNews(geometry({ userTop: 500, replyHeight: 200 }))).toBe(false);
  });
});

describe("shouldStartFollowing (SCR-5)", () => {
  const msgs = [
    { id: "old", role: "assistant" as const },
    { id: "u1", role: "user" as const },
    { id: "a1", role: "assistant" as const },
  ];

  it("starts when the newest two are the user message and placeholder this client added", () => {
    expect(shouldStartFollowing(msgs, new Set(["u1", "a1"]))).toEqual({ userId: "u1", replyId: "a1" });
  });

  it("never starts for another device's send or a briefing", () => {
    expect(shouldStartFollowing(msgs, new Set())).toBeNull();
    expect(shouldStartFollowing(msgs, new Set(["u1"]))).toBeNull();
    expect(shouldStartFollowing([{ id: "b", role: "assistant" }], new Set(["b"]))).toBeNull();
  });

  it("needs a user message directly before the assistant reply", () => {
    const m = [
      { id: "x", role: "assistant" as const },
      { id: "a1", role: "assistant" as const },
    ];
    expect(shouldStartFollowing(m, new Set(["x", "a1"]))).toBeNull();
    expect(shouldStartFollowing([], new Set())).toBeNull();
  });

  it("matches ids case-insensitively against a lowercase set", () => {
    const m = [
      { id: "ABC", role: "user" as const },
      { id: "DEF", role: "assistant" as const },
    ];
    expect(shouldStartFollowing(m, new Set(["abc", "def"]))).toEqual({ userId: "ABC", replyId: "DEF" });
  });
});

describe("replyHasWords (SCR-9)", () => {
  it("waits for streamed text, done or error", () => {
    expect(replyHasWords({ status: "running" })).toBe(false);
    expect(replyHasWords({ status: "running", streamedText: "" })).toBe(false);
    expect(replyHasWords({ status: "running", streamedText: "hi" })).toBe(true);
    expect(replyHasWords({ status: "done" })).toBe(true);
    expect(replyHasWords({ status: "error" })).toBe(true);
    expect(replyHasWords({ status: "stalled" })).toBe(false);
  });
});

describe("isFarFromBottom (SCR-4)", () => {
  it("is not far until the height is measured", () => {
    expect(isFarFromBottom(100000, 0, 0)).toBe(false);
    expect(isFarFromBottom(100000, 0, -1)).toBe(false);
  });

  it("uses a 160 floor on short viewports", () => {
    // clientHeight 300: threshold max(160, 100) = 160.
    expect(isFarFromBottom(1000, 540, 300)).toBe(false); // 160 below: not more than
    expect(isFarFromBottom(1000, 539, 300)).toBe(true); // 161 below
  });

  it("uses a third of the viewport on tall ones", () => {
    // clientHeight 900: threshold 300.
    expect(isFarFromBottom(2000, 800, 900)).toBe(false); // exactly 300
    expect(isFarFromBottom(2000, 799, 900)).toBe(true);
  });

  it("is not far at the bottom", () => {
    expect(isFarFromBottom(1000, 600, 400)).toBe(false);
  });
});
