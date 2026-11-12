import { readFileSync } from "node:fs";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Api } from "../lib/api";
import { DEFAULT_FACE } from "../lib/faces";
import { NOTICES, noticeText } from "../lib/notices";
import { Settings } from "./Settings";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function open(serverBuild: string | null = "worktree-settings-panes@abc1234") {
  render(
    <Settings
      api={{} as Api}
      token="tok-abcdef"
      reauthNeeded={false}
      queuePaused={false}
      effort="low"
      serverModel="sonnet"
      serverBuild={serverBuild}
      onEffort={() => {}}
      locationSharing="off"
      locationStatus="ok"
      onLocationSharing={() => {}}
      face={DEFAULT_FACE}
      onFace={() => {}}
      onUnpair={() => {}}
      onClose={() => {}}
      host="marlow.test"
      webBuild="deadbee"
      voiceEngine={null}
    />,
  );
}

const row = (name: string | RegExp) => screen.getByRole("button", { name });
const headings = () => screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent);

describe("Settings › layout", () => {
  it("keeps server and build detail off the main pane, behind two rows at the bottom", () => {
    open();
    expect(headings()).toEqual(["Appearance and voice", "Model", "Think time", "Location"]);
    expect(screen.queryByText("marlow.test")).toBeNull();
    expect(screen.queryByRole("button", { name: "Unpair" })).toBeNull();
    const rows = screen.getAllByRole("button").map((b) => b.textContent);
    expect(rows.slice(-2)).toEqual(["Server connection", "About"]);
  });

  it("shows each appearance row's current choice", () => {
    open();
    const section = within(screen.getByRole("region", { name: "Appearance and voice" }));
    expect(section.getByRole("button", { name: /^Avatar/ }).textContent).toContain("Monocle");
    expect(section.getByRole("button", { name: /^Text size/ }).textContent).toContain("Default");
  });
});

describe("Settings › panes", () => {
  it("a row opens its pane, with Back in place of Done, and focus on the heading", () => {
    open();
    fireEvent.click(row("About"));
    const heading = screen.getByRole("heading", { level: 2, name: "About" });
    expect(document.activeElement).toBe(heading);
    expect(screen.queryByRole("button", { name: "Done" })).toBeNull();
    expect(screen.queryByRole("region", { name: "Model" })).toBeNull();
    expect(screen.getByRole("dialog", { name: "Settings" })).toBeTruthy();
  });

  it("Back returns to the main pane, with focus on the row that opened it", () => {
    open();
    fireEvent.click(row("Server connection"));
    fireEvent.click(row("Back"));
    expect(screen.getByRole("heading", { level: 2, name: "Settings" })).toBeTruthy();
    expect(row("Done")).toBeTruthy();
    expect(document.activeElement).toBe(row("Server connection"));
  });

  it("returns to where the main pane was scrolled, the rows being at the bottom of it", () => {
    open();
    const body = screen.getByRole("region", { name: "Model" }).closest(".sheet-body") as HTMLElement;
    body.scrollTop = 640;
    fireEvent.click(row("About"));
    body.scrollTop = 0; // Hiding the pane drops it in a real browser.
    fireEvent.click(row("Back"));
    expect(body.scrollTop).toBe(640);
  });

  /** Twelve faces are taller than a phone: a later one opened below the fold,
   * showing no selection (Mike, 2026-10-16, on the Marlow sheet). */
  it("the Avatar pane opens on the current face", () => {
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    open();
    fireEvent.click(row(/^Avatar/));
    expect(scrollIntoView).toHaveBeenCalledTimes(1);
    expect((scrollIntoView.mock.contexts[0] as Element).getAttribute("aria-checked")).toBe("true");
    delete (Element.prototype as Partial<Element>).scrollIntoView;
  });

  it("goes more than one level: About, the list, a notice, and back up", async () => {
    open();
    fireEvent.click(row("About"));
    fireEvent.click(row("Open-source notices"));
    fireEvent.click(row(/^marked/));
    expect(screen.getByRole("heading", { level: 2, name: "marked" })).toBeTruthy();
    await waitFor(() => expect(screen.getByTestId("notice-text").textContent).toContain("Permission is hereby granted"));
    fireEvent.click(row("Back"));
    expect(document.activeElement).toBe(row(/^marked/));
    fireEvent.click(row("Back"));
    expect(document.activeElement).toBe(row("Open-source notices"));
  });
});

describe("Settings › copyable rows", () => {
  it("keeps the commit whole, so only the branch can be clipped", () => {
    open();
    fireEvent.click(row("About"));
    const value = row("worktree-settings-panes@abc1234");
    expect(value.lastElementChild?.textContent).toBe("@abc1234");
  });

  it("has nothing to copy while the server hasn't said its build", () => {
    open(null);
    fireEvent.click(row("About"));
    expect(screen.getByText("—").closest("button")).toBeNull();
  });

  it("a tap copies the full value and says so", async () => {
    const writeText = vi.fn(async () => {});
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    open();
    fireEvent.click(row("About"));
    await act(async () => fireEvent.click(row("worktree-settings-panes@abc1234")));
    expect(writeText).toHaveBeenCalledWith("worktree-settings-panes@abc1234");
    expect(row("Copied")).toBeTruthy();
  });

  it("says nothing where the browser refuses the copy", async () => {
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText: async () => Promise.reject(new Error("no")) } });
    open();
    fireEvent.click(row("Server connection"));
    await act(async () => fireEvent.click(row("marlow.test")));
    expect(screen.queryByRole("button", { name: "Copied" })).toBeNull();
  });
});

describe("Open-source notices", () => {
  /** Apache-2.0 §4 wants the licence text with what's distributed, and MIT its
   * copyright line, so a notice with no text is the failure that matters. */
  it("every notice has its text", async () => {
    expect(NOTICES.length).toBeGreaterThan(0);
    for (const notice of NOTICES) {
      expect((await noticeText(notice)).length, notice.name).toBeGreaterThan(500);
    }
  });

  /** The next production dependency added without a notice fails here. */
  it("every production package in the lockfile has a notice", () => {
    const lock = JSON.parse(readFileSync("package-lock.json", "utf8")) as {
      packages: Record<string, { dev?: boolean; devOptional?: boolean; optional?: boolean }>;
    };
    const production = Object.entries(lock.packages)
      .filter(([path, p]) => path !== "" && !p.dev && !p.devOptional)
      .map(([path]) => path.replace(/^.*node_modules\//, ""));
    expect(production.length).toBeGreaterThan(0);
    const covered = new Set(NOTICES.flatMap((n) => n.packages));
    expect(production.filter((name) => !covered.has(name))).toEqual([]);
  });

  it("every Phonon crate's licences come with phonon-tts", async () => {
    const phonon = NOTICES.find((n) => n.packages.includes("phonon-tts"))!;
    expect(await noticeText(phonon)).toContain("Third-party licences");
  });
});
