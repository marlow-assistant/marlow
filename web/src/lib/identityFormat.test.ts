import { describe, expect, it } from "vitest";
import type { IdentityKind } from "./apiModels";
import { aboutNote, calendarZone, changed, memoryName, stripFrontmatter, title } from "./identityFormat";

describe("names", () => {
  it("memory names drop the extension and folder and humanise", () => {
    expect(memoryName("memory/marlow-features.md")).toBe("Marlow features");
    expect(memoryName("travel.md")).toBe("Travel");
    expect(memoryName("memory/some_notes.md")).toBe("Some notes");
  });

  it("titles use the strip names for the fixed files", () => {
    expect(title("SOUL.md", "soul")).toBe("Soul");
    expect(title("USER.md", "user")).toBe("You");
    expect(title("CLAUDE.md", "instructions")).toBe("Instructions");
    // A job reads the same here as it does in the activity list.
    expect(title("cron/morning.md", "job")).toBe("Morning briefing");
    expect(title("cron/physio-nudge.md", "job")).toBe("Physio nudge");
  });

  it("every kind has an about note", () => {
    const kinds: IdentityKind[] = ["soul", "user", "instructions", "memory", "job", "unknown"];
    for (const k of kinds) expect(aboutNote(k)).not.toBe("");
    expect(aboutNote("memory")).toContain("can be out of date");
  });
});

describe("frontmatter", () => {
  it("is stripped", () => {
    const text = "---\nname: travel\ndescription: Trips\n---\n\n# Travel\n\nBody";
    expect(stripFrontmatter(text)).toBe("# Travel\n\nBody");
  });

  it("text without frontmatter is untouched", () => {
    const text = "# Title\n\nBody\n---\nmore";
    expect(stripFrontmatter(text)).toBe(text);
  });

  it("an unclosed leading rule keeps its body", () => {
    // A rule at the top with no closing fence is not frontmatter; stripping it
    // would swallow the whole file.
    const text = "---\n\nSome body text\n\nMore text";
    expect(stripFrontmatter(text)).toBe(text);
  });

  it("CRLF fences still count", () => {
    expect(stripFrontmatter("---\r\na: b\r\n---\r\nBody")).toBe("Body");
  });

  it("a fence only in the middle is not frontmatter", () => {
    const text = "Intro\n---\na: b\n---\nBody";
    expect(stripFrontmatter(text)).toBe(text);
  });

  it("empty text is untouched", () => {
    expect(stripFrontmatter("")).toBe("");
  });
});

describe("dates", () => {
  it("store calendar uses the store zone, not the browser's", () => {
    const phone = "America/Edmonton";
    expect(calendarZone("America/Toronto", phone)).toBe("America/Toronto");
    expect(calendarZone(null, phone)).toBe(phone);
    expect(calendarZone(undefined, phone)).toBe(phone);
    expect(calendarZone("Not/AZone", phone)).toBe(phone);
  });

  it("changed is relative and says just now inside a minute", () => {
    const now = Date.UTC(2026, 9, 14, 13, 0);
    const threeDaysAgo = (now - 3 * 86400 * 1000) / 1000;
    expect(changed(threeDaysAgo, now, "en-US")).toBe("Changed 3 days ago");
    expect(changed(now / 1000 - 10, now, "en-US")).toBe("Changed just now");
  });

  it("changed picks the largest unit", () => {
    const now = Date.UTC(2026, 9, 14, 13, 0);
    const ago = (s: number) => changed(now / 1000 - s, now, "en-US");
    expect(ago(5 * 60)).toBe("Changed 5 minutes ago");
    expect(ago(2 * 3600)).toBe("Changed 2 hours ago");
    expect(ago(14 * 86400)).toBe("Changed 2 weeks ago");
  });
});
