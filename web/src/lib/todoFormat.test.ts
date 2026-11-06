import { describe, expect, it } from "vitest";
import { decodeTodo, decodeTodos, type Todo } from "./apiModels";
import {
  anchorAt,
  badgeCount,
  dropIndex,
  dueLine,
  isOverdue,
  localInputToIso,
  moveChoices,
  reordered,
  sectionsOf,
  snoozeIso,
  snoozePresets,
  snoozedLine,
} from "./todoFormat";

// Wed 4 Nov 2026, 10:00 on the machine's own clock: every case builds its
// dates from local fields, so the zone the tests run in does not matter.
const NOW = new Date(2026, 10, 4, 10, 0);

/** An ISO instant with the local offset, as the box stores a timed due. */
function iso(y: number, mo: number, d: number, h: number, mi = 0): string {
  const date = new Date(y, mo - 1, d, h, mi);
  const off = -date.getTimezoneOffset();
  const p = (n: number) => String(Math.abs(n)).padStart(2, "0");
  return (
    `${date.getFullYear()}-${p(date.getMonth() + 1)}-${p(date.getDate())}T${p(date.getHours())}:${p(date.getMinutes())}:00` +
    `${off < 0 ? "-" : "+"}${p(Math.trunc(Math.abs(off) / 60))}:${p(Math.abs(off) % 60)}`
  );
}

function todo(id: string, over: Partial<Todo> = {}): Todo {
  return {
    id,
    title: `Title ${id}`,
    notes: null,
    due: null,
    state: "open",
    source: "user",
    created: iso(2026, 11, 1, 8),
    closed: null,
    snoozedUntil: null,
    ...over,
  };
}

describe("decoding", () => {
  it("reads the wire shape, snake_case to camelCase", () => {
    expect(
      decodeTodo({
        id: "t1",
        title: "Book the dentist",
        notes: "the cleaning",
        due: "2026-11-06",
        state: "proposed",
        source: "inferred",
        created: "2026-11-01T08:00:00-05:00",
        closed: null,
        snoozed_until: "2026-11-05T09:00:00-05:00",
      }),
    ).toEqual({
      id: "t1",
      title: "Book the dentist",
      notes: "the cleaning",
      due: "2026-11-06",
      state: "proposed",
      source: "inferred",
      created: "2026-11-01T08:00:00-05:00",
      closed: null,
      snoozedUntil: "2026-11-05T09:00:00-05:00",
    });
  });

  it("skips an item it cannot show and keeps the rest in order", () => {
    const good = { id: "t1", title: "a", state: "open", source: "user", created: "2026-11-01T08:00:00-05:00" };
    const todos = decodeTodos({
      todos: [good, { ...good, id: "" }, { ...good, id: "t3", state: "weird" }, { ...good, id: "t4", title: "" }, { ...good, id: "t5" }],
    });
    expect(todos.map((t) => t.id)).toEqual(["t1", "t5"]);
    expect(decodeTodos(null)).toEqual([]);
  });

  it("a malformed optional field falls back to null", () => {
    const t = decodeTodo({ id: "t1", title: "a", state: "open", source: "user", created: "2026-11-01T08:00:00-05:00", due: 5, notes: 3 });
    expect(t).toMatchObject({ due: null, notes: null, closed: null, snoozedUntil: null });
  });
});

describe("overdue", () => {
  it("a timed due before now is overdue; one after is not", () => {
    expect(isOverdue(todo("a", { due: iso(2026, 11, 4, 9) }), NOW)).toBe(true);
    expect(isOverdue(todo("a", { due: iso(2026, 11, 4, 11) }), NOW)).toBe(false);
  });

  it("a date-only due is overdue from the day after, not on the day itself", () => {
    expect(isOverdue(todo("a", { due: "2026-11-03" }), NOW)).toBe(true);
    expect(isOverdue(todo("a", { due: "2026-11-04" }), NOW)).toBe(false);
    expect(isOverdue(todo("a", { due: "2026-11-05" }), NOW)).toBe(false);
  });

  it("no due, and anything not open, is never overdue", () => {
    expect(isOverdue(todo("a"), NOW)).toBe(false);
    expect(isOverdue(todo("a", { state: "proposed", due: "2026-10-01" }), NOW)).toBe(false);
    expect(isOverdue(todo("a", { state: "done", due: "2026-10-01" }), NOW)).toBe(false);
  });
});

describe("sections", () => {
  it("orders them To do, Marlow suggested, Done this week and hides the empty ones", () => {
    const all = [todo("d", { state: "done", closed: iso(2026, 11, 3, 9) }), todo("p", { state: "proposed" }), todo("o")];
    expect(sectionsOf(all).map((s) => s.title)).toEqual(["To do", "Marlow suggested", "Done this week"]);
    expect(sectionsOf([todo("o")]).map((s) => s.id)).toEqual(["todo"]);
    expect(sectionsOf([])).toEqual([]);
  });

  it("keeps open items in the box's order: it owns that, drags included", () => {
    const open = [
      todo("undated"),
      todo("far", { due: "2026-12-31" }),
      todo("soon", { due: "2026-11-05" }),
      todo("timed", { due: iso(2026, 11, 6, 9) }),
    ];
    expect(sectionsOf(open)[0].todos.map((t) => t.id)).toEqual(["undated", "far", "soon", "timed"]);
  });

  it("a ticked item holds its place in that order", () => {
    const list = [todo("a"), todo("b", { state: "done", closed: iso(2026, 11, 4, 9) }), todo("c")];
    const held = new Map([["b", todo("b")]]);
    expect(sectionsOf(list, held)[0].todos.map((t) => t.id)).toEqual(["a", "b", "c"]);
  });

  it("proposed newest first; done newest closed first", () => {
    const all = [
      todo("p-old", { state: "proposed", created: iso(2026, 11, 1, 8) }),
      todo("p-new", { state: "proposed", created: iso(2026, 11, 3, 8) }),
      todo("d-old", { state: "done", closed: iso(2026, 11, 1, 9) }),
      todo("d-new", { state: "done", closed: iso(2026, 11, 3, 9) }),
    ];
    const s = sectionsOf(all);
    expect(s[0].todos.map((t) => t.id)).toEqual(["p-new", "p-old"]);
    expect(s[1].todos.map((t) => t.id)).toEqual(["d-new", "d-old"]);
  });

  it("never lists a dropped item", () => {
    expect(sectionsOf([todo("x", { state: "dropped" })])).toEqual([]);
  });
});

describe("badge", () => {
  it("counts overdue open items plus proposed ones, and is 0 otherwise", () => {
    const all = [
      todo("late", { due: "2026-11-02" }),
      todo("later", { due: iso(2026, 11, 4, 9) }),
      todo("future", { due: "2026-11-09" }),
      todo("undated"),
      todo("p1", { state: "proposed" }),
      todo("p2", { state: "proposed", due: "2026-10-01" }),
      todo("done", { state: "done", due: "2026-10-01", closed: iso(2026, 11, 3, 9) }),
    ];
    expect(badgeCount(all, NOW)).toBe(4);
    expect(badgeCount([todo("fine"), todo("future", { due: "2026-11-09" })], NOW)).toBe(0);
    expect(badgeCount([], NOW)).toBe(0);
  });
});

describe("the due line", () => {
  it("says nothing for no due", () => {
    expect(dueLine(todo("a"), NOW)).toBeNull();
  });

  it("a date: Today, Tomorrow, then weekday day month", () => {
    expect(dueLine(todo("a", { due: "2026-11-04" }), NOW)).toBe("Today");
    expect(dueLine(todo("a", { due: "2026-11-05" }), NOW)).toBe("Tomorrow");
    expect(dueLine(todo("a", { due: "2026-11-06" }), NOW)).toBe("Fri 6 Nov");
  });

  it("a time adds the 24h clock", () => {
    expect(dueLine(todo("a", { due: iso(2026, 11, 4, 17, 30) }), NOW)).toBe("Today 17:30");
    expect(dueLine(todo("a", { due: iso(2026, 11, 5, 9) }), NOW)).toBe("Tomorrow 09:00");
    expect(dueLine(todo("a", { due: iso(2026, 11, 6, 9) }), NOW)).toBe("Fri 6 Nov 09:00");
  });

  it("an overdue open item is prefixed", () => {
    expect(dueLine(todo("a", { due: iso(2026, 11, 2, 14) }), NOW)).toBe("Overdue · Mon 2 Nov 14:00");
    expect(dueLine(todo("a", { due: "2026-11-02" }), NOW)).toBe("Overdue · Mon 2 Nov");
    expect(dueLine(todo("a", { due: iso(2026, 11, 4, 9) }), NOW)).toBe("Overdue · Today 09:00");
  });

  it("a done item is never marked overdue", () => {
    expect(dueLine(todo("a", { state: "done", due: "2026-11-02", closed: iso(2026, 11, 3, 9) }), NOW)).toBe("Mon 2 Nov");
  });

  it("a snooze reads Snoozed until, in the same form", () => {
    expect(snoozedLine(todo("a"), NOW)).toBeNull();
    expect(snoozedLine(todo("a", { snoozedUntil: iso(2026, 11, 5, 9) }), NOW)).toBe("Snoozed until Tomorrow 09:00");
    expect(snoozedLine(todo("a", { snoozedUntil: iso(2026, 11, 9, 9) }), NOW)).toBe("Snoozed until Mon 9 Nov 09:00");
  });
});

describe("snooze presets", () => {
  it("offers in 2 hours, this evening (before 17:00 only) and tomorrow 09:00", () => {
    const presets = snoozePresets(NOW);
    expect(presets.map((p) => p.label)).toEqual(["In 2 hours", "This evening 18:00", "Tomorrow 09:00"]);
    expect(new Date(presets[0].until).getTime()).toBe(new Date(2026, 10, 4, 12, 0).getTime());
    expect(new Date(presets[1].until).getTime()).toBe(new Date(2026, 10, 4, 18, 0).getTime());
    expect(new Date(presets[2].until).getTime()).toBe(new Date(2026, 10, 5, 9, 0).getTime());
  });

  it("drops this evening from 17:00", () => {
    const labels = (d: Date) => snoozePresets(d).map((p) => p.label);
    expect(labels(new Date(2026, 10, 4, 16, 59))).toContain("This evening 18:00");
    expect(labels(new Date(2026, 10, 4, 17, 0))).toEqual(["In 2 hours", "Tomorrow 09:00"]);
  });

  it("every preset is an ISO instant with the local offset, never a bare local time", () => {
    for (const p of snoozePresets(NOW)) expect(p.until).toMatch(/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$/);
    expect(snoozePresets(NOW)[2].until).toBe(iso(2026, 11, 5, 9));
  });

  it("tomorrow 09:00 is local across a month end", () => {
    const end = new Date(2026, 10, 30, 20, 0);
    expect(snoozePresets(end).at(-1)!.until).toBe(iso(2026, 12, 1, 9));
  });

  it("snoozeIso writes the offset of that moment", () => {
    expect(snoozeIso(new Date(2026, 10, 5, 9, 0))).toBe(iso(2026, 11, 5, 9));
  });

  it("a picked datetime-local value becomes an instant, and only a future one", () => {
    expect(localInputToIso("2026-11-06T09:30", NOW)).toBe(iso(2026, 11, 6, 9, 30));
    expect(localInputToIso("2026-11-04T09:30", NOW)).toBeNull(); // past
    expect(localInputToIso("", NOW)).toBeNull();
    expect(localInputToIso("garbage", NOW)).toBeNull();
    expect(localInputToIso("2028-11-06T09:30", NOW)).toBeNull(); // over a year: the box refuses
  });
});

describe("moving", () => {
  const ids = ["a", "b", "c", "d"];

  it("the anchor is what ends up just below the moved item, null at the bottom", () => {
    expect(anchorAt(ids, "d", 0)).toBe("a");
    expect(anchorAt(ids, "a", 1)).toBe("c");
    expect(anchorAt(ids, "a", 3)).toBeNull();
    expect(anchorAt(ids, "b", 2)).toBe("d");
  });

  it("reordered applies the same move the box will", () => {
    const list = [todo("a"), todo("x", { state: "done" }), todo("b"), todo("c")];
    const order = (before: string | null, id = "c") => reordered(list, id, before).map((t) => t.id);
    expect(order("a")).toEqual(["c", "a", "x", "b"]);
    expect(order("x")).toEqual(["a", "c", "x", "b"]);
    expect(order(null, "a")).toEqual(["x", "b", "c", "a"]);
  });

  it("reordered puts a bottom move after the last open item, as the box does", () => {
    const list = [todo("a"), todo("b"), todo("p", { state: "proposed" })];
    expect(reordered(list, "a", null).map((t) => t.id)).toEqual(["b", "a", "p"]);
  });

  it("offers only the moves that do something different", () => {
    const labels = (index: number, count: number) => moveChoices(index, count).map((c) => [c.label, c.to]);
    expect(labels(0, 1)).toEqual([]);
    expect(labels(0, 2)).toEqual([["Move down", 1]]);
    expect(labels(1, 2)).toEqual([["Move up", 0]]);
    expect(labels(0, 4)).toEqual([
      ["Move down", 1],
      ["Move to bottom", 3],
    ]);
    expect(labels(2, 5)).toEqual([
      ["Move to top", 0],
      ["Move up", 1],
      ["Move down", 3],
      ["Move to bottom", 4],
    ]);
  });

  describe("dropIndex", () => {
    // Four rows of 50 with a gap of 10.
    const rows = [0, 60, 120, 180].map((top) => ({ top, height: 50 }));

    it("stays put until the row's middle passes a neighbour's", () => {
      expect(dropIndex(rows, 1, 0)).toBe(1);
      expect(dropIndex(rows, 1, 59)).toBe(1);
      expect(dropIndex(rows, 1, 61)).toBe(2);
      expect(dropIndex(rows, 1, -61)).toBe(0);
    });

    it("clamps past either end", () => {
      expect(dropIndex(rows, 1, 5000)).toBe(3);
      expect(dropIndex(rows, 2, -5000)).toBe(0);
    });

    it("reads a tall row by its middle", () => {
      const mixed = [
        { top: 0, height: 200 },
        { top: 210, height: 50 },
      ];
      expect(dropIndex(mixed, 1, -134)).toBe(1);
      expect(dropIndex(mixed, 1, -136)).toBe(0);
    });
  });
});
