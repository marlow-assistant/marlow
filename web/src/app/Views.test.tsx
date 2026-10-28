import "fake-indexeddb/auto";
import { IDBFactory } from "fake-indexeddb";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MarlowSheet } from "./Views";
import { daySections, fileOfKind, isMarkdownId, memoryFiles } from "./views/viewModel";
import { decodeIdentityIndex, decodeUsageReport, makeActivityEntry } from "../lib/apiModels";
import { DEFAULT_FACE, FACES, type Face } from "../lib/faces";
import { kvGet, kvSet, KV_SHOW_BACKGROUND, resetDbForTests } from "../lib/storage";

afterEach(cleanup);

const NOW = Date.UTC(2026, 9, 1, 15, 0, 0); // 2026-10-01 15:00Z
const TZ = "UTC";
const sec = (ms: number) => ms / 1000;

function rawEntry(id: string, finishedMs: number, extra: Record<string, unknown> = {}) {
  return {
    turn_id: id,
    source: "cron",
    label: "morning",
    started_at: sec(finishedMs) - 14,
    finished_at: sec(finishedMs),
    status: "done",
    tools: [{ name: "mcp__gmail__search", target: null }, { name: "Read", target: "/x/travel.md" }],
    ...extra,
  };
}

function fakeApi(over: Record<string, unknown> = {}) {
  return {
    activity: vi.fn().mockResolvedValue({ entries: [], next_before: null }),
    identity: vi.fn(),
    identityFile: vi.fn(),
    usage: vi.fn().mockResolvedValue(decodeUsageReport({})),
    ...over,
  } as unknown as Parameters<typeof MarlowSheet>[0]["api"] & {
    activity: ReturnType<typeof vi.fn>;
    identity: ReturnType<typeof vi.fn>;
    identityFile: ReturnType<typeof vi.fn>;
    usage: ReturnType<typeof vi.fn>;
  };
}

type Panel = "Activity" | "Usage" | "Schedule" | "Memory" | "You" | "Soul";

/** The Marlow sheet, opened on `panel` (Activity is the default). */
function renderViews(api: ReturnType<typeof fakeApi>, panel: Panel, onClose = () => {}, onFace: (f: Face) => void = () => {}) {
  const r = render(<MarlowSheet api={api} face={DEFAULT_FACE} onFace={onFace} onClose={onClose} timeZone={TZ} now={() => NOW} />);
  if (panel !== "Activity") fireEvent.click(screen.getByRole("button", { name: panel }));
  return r;
}

describe("activity view", () => {
  it("shows first page newest first, loads more, stops at null next_before", async () => {
    const api = fakeApi();
    api.activity
      .mockResolvedValueOnce({
        entries: [rawEntry("b", NOW - 3600_000, { label: "consolidate" }), rawEntry("a", NOW - 7200_000)],
        next_before: 100,
      })
      .mockResolvedValueOnce({
        entries: [rawEntry("c", NOW - 86400_000, { label: "weekly-digest" })],
        next_before: null,
      });
    renderViews(api, "Activity");
    await screen.findByText("Nightly tidy");
    expect(screen.getByText("Today")).toBeTruthy();
    expect(screen.getAllByText("Checked Gmail · read travel.md")).toHaveLength(2);
    expect(screen.getByText("2:00 PM")).toBeTruthy();
    expect(api.activity).toHaveBeenCalledWith(undefined);

    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    await screen.findByText("Weekly digest");
    expect(screen.getByText("Yesterday")).toBeTruthy();
    expect(api.activity).toHaveBeenLastCalledWith(100);
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  });

  it("expands a row to show detail lines and duration", async () => {
    const api = fakeApi();
    api.activity.mockResolvedValue({ entries: [rawEntry("a", NOW - 60_000)], next_before: null });
    renderViews(api, "Activity");
    const row = (await screen.findByText("Morning briefing")).closest("button")!;
    expect(screen.queryByText("Took 14s")).toBeNull();
    fireEvent.click(row);
    expect(screen.getByText("Took 14s")).toBeTruthy();
    expect(screen.getByText("/x/travel.md")).toBeTruthy();
  });

  it("shows flags with warnings", async () => {
    const api = fakeApi();
    api.activity.mockResolvedValue({ entries: [rawEntry("a", NOW - 60_000, { status: "error" })], next_before: null });
    renderViews(api, "Activity");
    expect(await screen.findByText("Didn't finish")).toBeTruthy();
  });

  it("shows an error with Retry, then recovers", async () => {
    const api = fakeApi();
    api.activity
      .mockRejectedValueOnce(new Error("boom"))
      .mockResolvedValueOnce({ entries: [], next_before: null });
    renderViews(api, "Activity");
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("boom");
    fireEvent.click(within(alert).getByRole("button", { name: "Retry" }));
    await screen.findByText(/Nothing yet/);
    expect(api.activity).toHaveBeenCalledTimes(2);
  });

  it("hides chat turns that used no tools", () => {
    const quiet = makeActivityEntry({ turnId: "q", source: "chat", finishedAt: sec(NOW) });
    expect(daySections([quiet], { timeZone: TZ, now: NOW })).toEqual([]);
  });
});

describe("background jobs", () => {
  beforeEach(() => {
    globalThis.indexedDB = new IDBFactory();
    resetDbForTests();
  });

  const tidy = (status = "done", extra: Record<string, unknown> = {}) =>
    rawEntry("tidy", NOW - 60_000, { label: "consolidate", tier: "plumbing", status, tools: [], ...extra });

  it("are shown by default, hidden by the toggle, and the toggle is remembered", async () => {
    const api = fakeApi();
    api.activity.mockResolvedValue({ entries: [tidy()], next_before: null });
    renderViews(api, "Activity");
    expect(await screen.findByText("Nightly tidy")).toBeTruthy();
    const toggle = screen.getByRole("checkbox", { name: "Show background jobs" }) as HTMLInputElement;
    expect(toggle.checked).toBe(true);

    fireEvent.click(toggle);
    // Only a background job in the log: the toggle must still be reachable.
    await waitFor(() => expect(screen.queryByText("Nightly tidy")).toBeNull());
    expect(screen.getByRole("checkbox", { name: "Show background jobs" })).toBeTruthy();
    await waitFor(async () => expect(await kvGet(KV_SHOW_BACKGROUND)).toBe(false));

    cleanup();
    renderViews(api, "Activity");
    await screen.findByRole("checkbox", { name: "Show background jobs" });
    await waitFor(() => expect(screen.queryByText("Nightly tidy")).toBeNull());
  });

  it("that failed show even with the toggle off", async () => {
    await kvSet(KV_SHOW_BACKGROUND, false);
    const api = fakeApi();
    api.activity.mockResolvedValue({ entries: [tidy("error")], next_before: null });
    renderViews(api, "Activity");
    expect(await screen.findByText("Nightly tidy")).toBeTruthy();
    expect(screen.getByText("Didn't finish")).toBeTruthy();
  });

  it("fetch what the job said on expand, once", async () => {
    await kvSet(KV_SHOW_BACKGROUND, true);
    const api = fakeApi({
      activityOutput: vi.fn().mockResolvedValue({ turn_id: "tidy", text: "Saved two notes." }),
    });
    api.activity.mockResolvedValue({
      entries: [tidy("done", { has_output: true, tools: [{ name: "Read", target: "memory/a.md" }, { name: "Read", target: "memory/a.md" }] })],
      next_before: null,
    });
    renderViews(api, "Activity");
    const row = (await screen.findByText("Nightly tidy")).closest("button")!;
    fireEvent.click(row);
    expect(await screen.findByText("Saved two notes.")).toBeTruthy();
    expect(screen.getByText("×2")).toBeTruthy();
    fireEvent.click(row);
    fireEvent.click(row);
    expect(await screen.findByText("Saved two notes.")).toBeTruthy();
    expect((api as unknown as { activityOutput: ReturnType<typeof vi.fn> }).activityOutput).toHaveBeenCalledTimes(1);
  });

  it("say so when what the job said can't be loaded", async () => {
    await kvSet(KV_SHOW_BACKGROUND, true);
    const api = fakeApi({ activityOutput: vi.fn().mockRejectedValue(new Error("404")) });
    api.activity.mockResolvedValue({ entries: [tidy("done", { has_output: true })], next_before: null });
    renderViews(api, "Activity");
    fireEvent.click((await screen.findByText("Nightly tidy")).closest("button")!);
    expect(await screen.findByText("Couldn't load what it said.")).toBeTruthy();
  });
});

const INDEX = {
  files: [
    { id: "memory/old.md", kind: "memory", modified_at: sec(NOW) - 10 * 86400, size: 1, description: "Old notes" },
    { id: "memory/new-thing.md", kind: "memory", modified_at: sec(NOW) - 120, size: 1, description: "New" },
    { id: "SOUL.md", kind: "soul", modified_at: 1, size: 1 },
    { id: "USER.md", kind: "user", modified_at: 1, size: 1 },
    { id: "CLAUDE.md", kind: "instructions", modified_at: 1, size: 1 },
    { id: "jobs/morning.md", kind: "job", modified_at: 1, size: 1 },
  ],
  schedules: {
    timezone: "UTC",
    entries: [
      {
        id: "morning",
        cron: "0 8 * * *",
        recurrence: { type: "daily", times: ["08:00"] },
        job_file: "jobs/morning.md",
        prompt: "Do the morning briefing",
      },
      { id: "late", recurrence: { type: "once" }, next_fire_at: sec(NOW) - 3600, notify: "Call back" },
      { id: "soon", recurrence: { type: "once" }, next_fire_at: sec(NOW) + 3600, notify: "Later" },
    ],
  },
};

describe("marlow sheet", () => {
  it("has six panel buttons (as iOS), Activity first and selected, and Done closes", () => {
    const api = fakeApi();
    const onClose = vi.fn();
    renderViews(api, "Activity", onClose);
    const dialog = screen.getByRole("dialog", { name: "Marlow" });
    expect(screen.getByRole("heading", { name: "Marlow" })).toBeTruthy();
    const strip = within(dialog)
      .getAllByRole("button")
      .filter((b) => b.getAttribute("aria-label") !== "Appearance")
      .map((b) => b.textContent);
    expect(strip.filter((t) => t !== "Done")).toEqual(["Activity", "Usage", "Schedule", "Memory", "You", "Soul"]);
    expect(screen.getByRole("button", { name: "Activity" }).getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(onClose).toHaveBeenCalled();
  });

  it("the face at the top opens the face picker; a pick calls onFace and Back returns", () => {
    const onFace = vi.fn();
    // jsdom has no scrollIntoView; record which element asks for it.
    const scrolled = vi.fn();
    Element.prototype.scrollIntoView = scrolled;
    renderViews(fakeApi(), "Activity", () => {}, onFace);
    fireEvent.click(screen.getByRole("button", { name: "Appearance" }));
    expect(screen.getByRole("heading", { name: "Appearance" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Activity" })).toBeNull();
    const group = screen.getByRole("radiogroup", { name: "Appearance" });
    const current = within(group).getByRole("radio", { name: "Face 1, monocle" });
    expect(current.getAttribute("aria-checked")).toBe("true");
    // Opens on the current face, once: a pick doesn't scroll it again.
    expect(scrolled.mock.contexts).toEqual([current]);
    fireEvent.click(within(group).getByRole("radio", { name: "Face 5, glasses" }));
    expect(onFace).toHaveBeenCalledWith(FACES[4]);
    expect(scrolled).toHaveBeenCalledTimes(1);
    delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
    fireEvent.click(screen.getByRole("button", { name: /Back/ }));
    expect(screen.queryByRole("radiogroup")).toBeNull();
    expect(screen.getByRole("button", { name: "Activity" })).toBeTruthy();
  });

  it("the strip switches panels and fetches the identity index once", async () => {
    const api = fakeApi();
    api.identity.mockResolvedValue(INDEX);
    renderViews(api, "Activity");
    await screen.findByText(/Nothing yet/);
    fireEvent.click(screen.getByRole("button", { name: "Memory" }));
    await screen.findByRole("button", { name: /New thing/ });
    expect(screen.getByRole("button", { name: "Memory" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByText(/Nothing yet/).closest("[hidden]")).not.toBeNull(); // Activity is hidden, not unmounted
    fireEvent.click(screen.getByRole("button", { name: "Schedule" }));
    await screen.findByRole("region", { name: "Reminders" });
    fireEvent.click(screen.getByRole("button", { name: "Activity" }));
    expect(screen.getByText(/Nothing yet/).closest("[hidden]")).toBeNull();
    expect(api.identity).toHaveBeenCalledTimes(1);
    expect(api.activity).toHaveBeenCalledTimes(1);
  });

  it("Memory lists files newest first with when they changed", async () => {
    const api = fakeApi();
    api.identity.mockResolvedValue(INDEX);
    renderViews(api, "Memory");
    const names = (await screen.findAllByRole("button", { name: /New thing|Old/ })).map((b) => b.textContent);
    expect(names[0]).toContain("New thing");
    expect(names[0]).toContain("Changed 2 minutes ago");
    expect(names[1]).toContain("Old");
  });

  it("Memory says so when there are no memory files", async () => {
    const api = fakeApi();
    api.identity.mockResolvedValue({ files: [], schedules: {} });
    renderViews(api, "Memory");
    await screen.findByText(/No memory files/);
  });

  it("Schedule groups entries and marks overdue", async () => {
    const api = fakeApi();
    api.identity.mockResolvedValue(INDEX);
    renderViews(api, "Schedule");
    await screen.findByRole("region", { name: "Reminders" });
    const reminders = screen.getByRole("region", { name: "Reminders" });
    const rows = within(reminders).getAllByRole("listitem");
    expect(rows[0].textContent).toContain("Late");
    expect(rows[0].textContent).toContain("(overdue)");
    expect(rows[1].textContent).not.toContain("overdue");
    const daily = screen.getByRole("region", { name: "Daily" });
    expect(daily.textContent).toContain("8:00");
    expect(daily.textContent).toContain("Morning briefing");
    // Section order: Reminders before Daily.
    expect(reminders.compareDocumentPosition(daily) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("Schedule opens a job file and Back returns to the strip", async () => {
    const api = fakeApi();
    api.identity.mockResolvedValue(INDEX);
    api.identityFile.mockResolvedValue({ id: "jobs/morning.md", kind: "job", modified_at: 1, text: "# Job body", truncated: false });
    renderViews(api, "Schedule");
    fireEvent.click(await screen.findByRole("button", { name: /Morning briefing/ }));
    await screen.findByText("Job body");
    expect(api.identityFile).toHaveBeenCalledWith("jobs/morning.md");
    expect(screen.queryByRole("button", { name: "Memory" })).toBeNull(); // the strip is covered while a file is open
    fireEvent.click(screen.getByRole("button", { name: /Back/ }));
    expect(screen.getByRole("button", { name: "Memory" })).toBeTruthy();
    expect(screen.getByRole("region", { name: "Daily" })).toBeTruthy();
  });

  it("You shows USER.md in place; Soul shows SOUL.md safely, strips frontmatter, and links Instructions", async () => {
    const api = fakeApi();
    api.identity.mockResolvedValue(INDEX);
    api.identityFile.mockImplementation(async (id: string) =>
      id === "SOUL.md"
        ? {
            id,
            kind: "soul",
            modified_at: 1,
            text: '---\nname: x\n---\n# Heading\n\n<script>alert(1)</script>\n\n<img src=x onerror="alert(2)">\n',
            truncated: false,
          }
        : id === "USER.md"
          ? { id, kind: "user", modified_at: 1, text: "# About Mike", truncated: false }
          : { id, kind: "instructions", modified_at: 1, text: "# The rules", truncated: false },
    );
    const { container } = renderViews(api, "You");
    await screen.findByText("About Mike");
    expect(api.identityFile).toHaveBeenCalledWith("USER.md");
    expect(screen.queryByRole("button", { name: "Instructions" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Soul" }));
    await screen.findByText("Heading");
    expect(container.querySelector("img[onerror]")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain("<script>alert(1)</script>");
    expect(container.textContent).not.toContain("name: x");
    expect(screen.getAllByText("About this file").length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("button", { name: /Instructions/ }));
    await screen.findByText("The rules");
    expect(screen.getByRole("heading", { name: "Instructions" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Back/ }));
    expect(screen.getByText("Heading")).toBeTruthy();
  });

  it("shows a non-markdown file in a pre, verbatim", async () => {
    const api = fakeApi();
    api.identity.mockResolvedValue({
      files: [],
      schedules: { entries: [{ id: "raw", recurrence: { type: "daily", times: ["08:00"] }, job_file: "jobs/raw.txt" }] },
    });
    api.identityFile.mockResolvedValue({ id: "jobs/raw.txt", kind: "job", modified_at: 1, text: "# not **md**" });
    const { container } = renderViews(api, "Schedule");
    fireEvent.click(await screen.findByRole("button", { name: /Raw/ }));
    await waitFor(() => expect(container.querySelector("pre")?.textContent).toBe("# not **md**"));
  });

  it("You and Soul say so when their file is missing", async () => {
    const api = fakeApi();
    api.identity.mockResolvedValue({ files: [], schedules: {} });
    renderViews(api, "You");
    await screen.findByText("USER.md isn't there yet.");
    fireEvent.click(screen.getByRole("button", { name: "Soul" }));
    await screen.findByText("SOUL.md isn't there yet.");
  });

  it("shows an error with Retry for the index", async () => {
    const api = fakeApi();
    api.identity.mockRejectedValueOnce(new Error("nope")).mockResolvedValueOnce(INDEX);
    renderViews(api, "Memory");
    fireEvent.click(await screen.findByRole("button", { name: "Retry" }));
    await screen.findByRole("button", { name: /New thing/ });
  });
});

describe("view model", () => {
  it("memoryFiles sorts newest first; fileOfKind finds the one file", () => {
    const files = decodeIdentityIndex(INDEX).files;
    expect(memoryFiles(files).map((f) => f.id)).toEqual(["memory/new-thing.md", "memory/old.md"]);
    expect(fileOfKind(files, "soul")?.id).toBe("SOUL.md");
    expect(fileOfKind([], "user")).toBeNull();
    expect(isMarkdownId("a/B.MD")).toBe(true);
    expect(isMarkdownId("a.txt")).toBe(false);
  });
});

describe("usage panel", () => {
  const report = decodeUsageReport({
    plan: {
      observed_at: sec(NOW - 600_000),
      status: "allowed",
      windows: {
        five_hour: { utilization: 0.93, resets_at: sec(NOW + 3600_000) },
        seven_day: { utilization: 0.08, resets_at: sec(NOW + 4 * 86400_000) },
      },
    },
    conversation: { context_tokens: 36_938, compaction_threshold_tokens: 110_000, started_at: sec(NOW - 3 * 3600_000), compactions: 1 },
    marlow: { five_hour: { chat: { turns: 3 }, scheduled: { turns: 1 } }, seven_day: {} },
    subscription_type: "max",
  });

  it("shows the plan, the conversation and Marlow's turns in the iOS wording", async () => {
    const api = fakeApi();
    api.usage.mockResolvedValue(report);
    renderViews(api, "Usage");
    const plan = await screen.findByRole("region", { name: "Claude Max" });
    expect(within(plan).getByText("2:50 PM")).toBeTruthy();
    expect(within(plan).getByText("93% used · resets 4:00 PM")).toBeTruthy();
    expect(within(plan).getByText("8% used · resets Mon 3:00 PM")).toBeTruthy();
    expect(within(plan).getByText(/Marlow shares your Claude Max limits/)).toBeTruthy();
    // The high window's bar is tinted; the other is not.
    const bars = plan.querySelectorAll("progress");
    expect([...bars].map((b) => b.className)).toEqual(["usage-bar usage-bar-high", "usage-bar"]);

    const conv = screen.getByRole("region", { name: "This conversation" });
    expect(within(conv).getByText("37K tokens")).toBeTruthy();
    expect(within(conv).getByText("Marlow tidies up around 110K")).toBeTruthy();
    expect(within(conv).getByText("Started 12:00 PM · tidied up once")).toBeTruthy();

    const turns = screen.getByRole("region", { name: "Marlow's turns" });
    expect(within(turns).getByText("3 chat · 1 scheduled")).toBeTruthy();
    expect(within(turns).getByText("No turns in this window.")).toBeTruthy();
  });

  it("says there is no reading yet for each empty block", async () => {
    renderViews(fakeApi(), "Usage");
    await screen.findByRole("region", { name: "Your Claude plan" });
    expect(screen.getAllByText(/No reading yet/)).toHaveLength(3);
  });

  it("is not fetched until shown, and is read again each time it is shown", async () => {
    const api = fakeApi();
    renderViews(api, "Activity");
    await waitFor(() => expect(api.activity).toHaveBeenCalled());
    expect(api.usage).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Usage" }));
    await waitFor(() => expect(api.usage).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: "Activity" }));
    fireEvent.click(screen.getByRole("button", { name: "Usage" }));
    await waitFor(() => expect(api.usage).toHaveBeenCalledTimes(2));
  });

  it("offers a retry on a first failure", async () => {
    const api = fakeApi();
    api.usage.mockRejectedValueOnce(new Error("offline")).mockResolvedValue(report);
    renderViews(api, "Usage");
    fireEvent.click(await screen.findByRole("button", { name: "Retry" }));
    await screen.findByRole("region", { name: "Claude Max" });
  });
});
