import { describe, expect, it } from "vitest";
import { decodeIdeas, type Idea } from "./apiModels";
import {
  addDays,
  isExpired,
  isNearing,
  keepDismissed,
  localToday,
  sectionsOf,
  showDot,
  spotlightOf,
  untilText,
  visibleIdeas,
} from "./ideas";

const TODAY = "2026-10-12";

function idea(id: string, over: Partial<Idea> = {}): Idea {
  return {
    id,
    section: "now",
    emoji: "💡",
    title: `Title ${id}`,
    plan: "A plan.",
    category: "Travel",
    source: "an email",
    until: null,
    added: "2026-10-01",
    spotlight: false,
    ...over,
  };
}

describe("local days", () => {
  it("localToday uses the local calendar fields, not UTC", () => {
    // 23:30 local on Oct 12: whatever the zone, the local date is the 12th.
    expect(localToday(new Date(2026, 9, 12, 23, 30))).toBe("2026-10-12");
    expect(localToday(new Date(2026, 0, 5, 0, 5))).toBe("2026-01-05");
  });

  it("addDays crosses month and year ends", () => {
    expect(addDays("2026-10-30", 2)).toBe("2026-11-01");
    expect(addDays("2026-12-31", 1)).toBe("2027-01-01");
    expect(addDays("2028-02-28", 1)).toBe("2028-02-29");
  });
});

describe("until", () => {
  it("hides an idea only once its last day has passed", () => {
    expect(isExpired(idea("a", { until: "2026-10-11" }), TODAY)).toBe(true);
    expect(isExpired(idea("a", { until: TODAY }), TODAY)).toBe(false);
    expect(isExpired(idea("a", { until: null }), TODAY)).toBe(false);
    expect(visibleIdeas([idea("a", { until: "2026-10-11" }), idea("b", { until: TODAY }), idea("c")], TODAY).map((i) => i.id)).toEqual(["b", "c"]);
  });

  it("nearing is today, tomorrow and the day after, and not the day after that", () => {
    const near = (until: string | null) => isNearing(idea("a", { until }), TODAY);
    expect(near("2026-10-12")).toBe(true);
    expect(near("2026-10-13")).toBe(true);
    expect(near("2026-10-14")).toBe(true);
    expect(near("2026-10-15")).toBe(false);
    expect(near("2026-10-11")).toBe(false); // expired is hidden, not nearing
    expect(near(null)).toBe(false);
  });

  it("nearing counts across a month end", () => {
    expect(isNearing(idea("a", { until: "2026-11-01" }), "2026-10-30")).toBe(true);
    expect(isNearing(idea("a", { until: "2026-11-02" }), "2026-10-30")).toBe(false);
  });

  it("the marker reads 'until Oct 13', without a leading zero", () => {
    expect(untilText("2026-10-13")).toBe("until Oct 13");
    expect(untilText("2026-11-05")).toBe("until Nov 5");
  });
});

describe("sections", () => {
  it("For you now, then Things I can do, each in the store's order", () => {
    const ideas = [
      idea("s1", { section: "standing" }),
      idea("n1"),
      idea("s2", { section: "standing" }),
      idea("n2"),
    ];
    const out = sectionsOf(ideas, TODAY);
    expect(out.map((s) => [s.title, s.ideas.map((i) => i.id)])).toEqual([
      ["For you now", ["n1", "n2"]],
      ["Things I can do", ["s1", "s2"]],
    ]);
  });

  it("leaves out an empty section, and hides expired ideas first", () => {
    const out = sectionsOf([idea("n1", { until: "2026-10-01" }), idea("s1", { section: "standing" })], TODAY);
    expect(out.map((s) => s.id)).toEqual(["standing"]);
    expect(sectionsOf([], TODAY)).toEqual([]);
  });
});

describe("spotlight", () => {
  it("is the first spotlight idea", () => {
    expect(spotlightOf([idea("a"), idea("b", { spotlight: true }), idea("c", { spotlight: true })], TODAY)?.id).toBe("b");
    expect(spotlightOf([idea("a")], TODAY)).toBeNull();
  });

  it("skips a spotlight that has expired", () => {
    const ideas = [idea("old", { spotlight: true, until: "2026-10-11" }), idea("new", { spotlight: true })];
    expect(spotlightOf(ideas, TODAY)?.id).toBe("new");
    expect(spotlightOf([ideas[0]], TODAY)).toBeNull();
  });
});

describe("dot", () => {
  it("is not raised by a spotlight the list has already been read past", () => {
    expect(showDot([idea("a", { spotlight: true, added: "2026-10-01" })], TODAY, TODAY)).toBe(false);
  });

  it("is on for an idea added after the last visit, by day", () => {
    expect(showDot([idea("a", { added: "2026-10-12" })], TODAY, "2026-10-11")).toBe(true);
    expect(showDot([idea("a", { added: "2026-10-12" })], TODAY, "2026-10-12")).toBe(false);
    expect(showDot([idea("a", { added: "2026-10-01" })], TODAY, "2026-10-12")).toBe(false);
  });

  it("counts everything as new when the panel was never opened", () => {
    expect(showDot([idea("a", { added: "2020-01-01" })], TODAY, null)).toBe(true);
    expect(showDot([], TODAY, null)).toBe(false);
  });

  it("ignores an expired idea, even a new or spotlighted one", () => {
    const gone = idea("a", { added: "2026-10-12", spotlight: true, until: "2026-10-11" });
    expect(showDot([gone], TODAY, "2026-10-01")).toBe(false);
  });
});

describe("keepDismissed", () => {
  it("puts back a dismissed idea the server no longer lists, where it was", () => {
    const prev = [idea("a"), idea("b"), idea("c")];
    const fresh = [idea("a"), idea("c")];
    expect(keepDismissed(prev, fresh, new Set(["b"])).map((i) => i.id)).toEqual(["a", "b", "c"]);
  });

  it("does not resurrect an idea that is gone but was not dismissed here", () => {
    expect(keepDismissed([idea("a"), idea("b")], [idea("a")], new Set()).map((i) => i.id)).toEqual(["a"]);
  });

  it("does not duplicate a dismissed idea the server still lists", () => {
    expect(keepDismissed([idea("a")], [idea("a")], new Set(["a"])).map((i) => i.id)).toEqual(["a"]);
  });
});

describe("decodeIdeas", () => {
  const wire = {
    id: "dinner-invites",
    section: "now",
    emoji: "🍽",
    title: "Draft the dinner invites",
    plan: "I will draft them.",
    category: "People",
    source: "Calendar",
    until: "2026-10-13",
    added: "2026-10-11",
    spotlight: true,
  };

  it("reads the contract's shape", () => {
    expect(decodeIdeas({ ideas: [wire] })).toEqual([wire]);
  });

  it("absent optionals are null, and a bad idea drops alone", () => {
    const out = decodeIdeas({
      ideas: [
        { ...wire, source: null, until: null, spotlight: undefined },
        { ...wire, id: "x", section: "later" },
        { title: "no id", section: "now" },
        null,
        { ...wire, id: "y", until: "next week" },
      ],
    });
    expect(out.map((i) => i.id)).toEqual(["dinner-invites", "y"]);
    expect(out[0]).toMatchObject({ source: null, until: null, spotlight: false });
    expect(out[1].until).toBeNull();
  });

  it("anything that is not a list is empty", () => {
    expect(decodeIdeas(null)).toEqual([]);
    expect(decodeIdeas({ ideas: "nope" })).toEqual([]);
  });
});
