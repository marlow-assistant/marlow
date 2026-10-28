// The ideas panel (docs/tasks/ideas-panel.md, "v1"): the panel's own rendering,
// then how Chat wires the lightbulb, the panel and the spotlight card to the
// api and to the normal send path.
import "fake-indexeddb/auto";
import { IDBFactory } from "fake-indexeddb";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { Api } from "../lib/api";
import { HttpError } from "../lib/api";
import type { Idea } from "../lib/apiModels";
import { DEFAULT_FACE } from "../lib/faces";
import type { ChatState, ChatStore } from "../lib/chatStore";
import { LocationWatcher } from "../lib/location";
import { resetDbForTests } from "../lib/storage";
import type { ChangeEntry } from "../lib/whatsNew";
import { Chat } from "./Chat";
import { MessageRow } from "./MessageRow";
import { IdeasPanel, type IdeasPanelProps } from "./IdeasPanel";

beforeEach(() => {
  // A fresh "panel last opened" for each test.
  indexedDB = new IDBFactory();
  resetDbForTests();
});
afterEach(() => {
  cleanup();
  resetDbForTests();
});

const TODAY = "2026-10-12";

function idea(id: string, over: Partial<Idea> = {}): Idea {
  return {
    id,
    section: "now",
    emoji: "💡",
    title: `Title ${id}`,
    plan: `Plan for ${id}.`,
    category: "Travel",
    source: null,
    until: null,
    added: "2026-10-01",
    spotlight: false,
    ...over,
  };
}

function panel(over: Partial<IdeasPanelProps> = {}) {
  const props: IdeasPanelProps = {
    ideas: [],
    dismissed: new Set(),
    today: TODAY,
    onDoIt: vi.fn(),
    onDismiss: vi.fn(),
    onClose: vi.fn(),
    ...over,
  };
  return { ...render(<IdeasPanel {...props} />), props };
}

describe("ideas panel", () => {
  it("shows the two sections in order, each card with emoji, title, plan, source and category", () => {
    panel({
      ideas: [
        idea("s1", { section: "standing", emoji: "🧰", category: "Van" }),
        idea("n1", { emoji: "🍽", source: "Calendar: Sunday dinner" }),
      ],
    });
    const headers = screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent);
    expect(headers).toEqual(["For you now", "Things I can do"]);
    const card = screen.getByRole("article", { name: "Title n1" });
    expect(card.textContent).toContain("🍽");
    expect(within(card).getByText("Plan for n1.")).toBeTruthy();
    expect(within(card).getByText("Calendar: Sunday dinner")).toBeTruthy();
    expect(within(card).getByText("Travel")).toBeTruthy();
    expect(within(screen.getByRole("article", { name: "Title s1" })).getByText("Van")).toBeTruthy();
  });

  it("has no source line when the idea has none", () => {
    panel({ ideas: [idea("s1", { section: "standing" })] });
    expect(document.querySelector(".idea-source")).toBeNull();
  });

  it("marks an idea whose last day is in the next two days, and nothing further out", () => {
    panel({
      ideas: [idea("a", { until: "2026-10-14" }), idea("b", { until: "2026-10-15" })],
    });
    expect(within(screen.getByRole("article", { name: "Title a" })).getByText("until Oct 14")).toBeTruthy();
    expect(within(screen.getByRole("article", { name: "Title b" })).queryByText(/until/)).toBeNull();
  });

  it("hides an idea past its until without being told", () => {
    panel({ ideas: [idea("gone", { until: "2026-10-11" }), idea("here")] });
    expect(screen.queryByRole("article", { name: "Title gone" })).toBeNull();
    expect(screen.getByRole("article", { name: "Title here" })).toBeTruthy();
  });

  it("says so calmly when there is nothing", () => {
    panel();
    expect(screen.getByText("No ideas right now.")).toBeTruthy();
    expect(screen.queryAllByRole("heading", { level: 3 })).toHaveLength(0);
  });

  it("Do it and Not now call back with the idea", () => {
    const a = idea("a");
    const { props } = panel({ ideas: [a] });
    fireEvent.click(screen.getByRole("button", { name: "Do it" }));
    expect(props.onDoIt).toHaveBeenCalledWith(a);
    fireEvent.click(screen.getByRole("button", { name: "Not now" }));
    expect(props.onDismiss).toHaveBeenCalledWith("a", null);
  });

  it("an idea dismissed during this opening collapses to a row; one dismissed earlier is not listed", () => {
    const ideas = [idea("a"), idea("b")];
    const { rerender, props } = panel({ ideas, dismissed: new Set(["a"]) });
    // "a" was already dismissed when the panel opened.
    expect(screen.queryByRole("group", { name: /Title a/ })).toBeNull();
    expect(screen.queryByRole("article", { name: "Title a" })).toBeNull();
    rerender(<IdeasPanel {...props} ideas={ideas} dismissed={new Set(["a", "b"])} />);
    const row = screen.getByRole("group", { name: "Title b, dismissed" });
    expect(row.textContent).toContain("Dismissed — add a reason");
    expect(screen.queryByRole("article", { name: "Title b" })).toBeNull();
  });
});

// --- Chat wiring ---------------------------------------------------------------

function stubStore() {
  const state: ChatState = {
    messages: [],
    hasOlderOnServer: false,
    awaitingFirstFetch: false,
    loadingEarlier: false,
    reauthNeeded: false,
    reauthPrewarn: false,
    queuePaused: false,
    serverModel: null,
    serverBuild: null,
    connection: "idle",
    pendingPermission: null,
    usageReport: null,
    notice: null,
    effort: "low",
  };
  return {
    subscribe: () => () => {},
    getState: () => state,
    getStream: () => undefined,
    subscribeStream: () => () => {},
    onFeedback: () => () => {},
    setEffort: vi.fn(),
    send: vi.fn().mockResolvedValue(true),
  } as unknown as ChatStore & { send: ReturnType<typeof vi.fn> };
}

function fakeApi(ideas: Idea[] | Error) {
  return {
    ideas: vi.fn(() => (ideas instanceof Error ? Promise.reject(ideas) : Promise.resolve(ideas))),
    dismissIdea: vi.fn().mockResolvedValue(undefined),
    activity: vi.fn().mockResolvedValue({ entries: [], next_before: null }),
    identity: vi.fn(),
    identityFile: vi.fn(),
    webPushKey: vi.fn(),
    registerWebPush: vi.fn(),
  } as unknown as Api & { ideas: ReturnType<typeof vi.fn>; dismissIdea: ReturnType<typeof vi.fn> };
}

function chat(api: ReturnType<typeof fakeApi>, store = stubStore()) {
  const location = new LocationWatcher({ sharing: "off", geolocation: undefined, isVisible: () => true });
  render(<Chat store={store} location={location} api={api} token="abcdef123456" face={DEFAULT_FACE} onFace={() => {}} onEffort={() => {}} onUnpair={() => {}} bundledChanges={NO_CHANGES} />);
  return store;
}

/** No update card in these tests (WhatsNew.test.tsx covers it). */
const NO_CHANGES: ChangeEntry[] = [];

const SPOT = idea("spot", { spotlight: true, emoji: "✈️", title: "Rebook the Montreal flight", added: "2026-10-12" });
const OTHER = idea("other", { section: "standing", title: "Check the tire pressure", category: "Van" });

async function openPanel() {
  fireEvent.click(screen.getByRole("button", { name: "Ideas" }));
  return screen.findByRole("dialog", { name: "Ideas" });
}

describe("Chat and the ideas panel", () => {
  it("the lightbulb opens the panel with the fetched ideas, and Done closes it", async () => {
    chat(fakeApi([OTHER]));
    const dialog = await openPanel();
    await within(dialog).findByRole("article", { name: "Check the tire pressure" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Done" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("Do it closes the panel, drops the card, and sends 'Do it' with the idea as its quote", async () => {
    const api = fakeApi([OTHER]);
    const store = chat(api);
    const dialog = await openPanel();
    const card = await within(dialog).findByRole("article", { name: "Check the tire pressure" });
    fireEvent.click(within(card).getByRole("button", { name: "Do it" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(store.send).toHaveBeenCalledTimes(1);
    expect(store.send).toHaveBeenCalledWith("Do it", undefined, { idea_id: "other", emoji: "💡", title: "Check the tire pressure" });
    expect(api.dismissIdea).not.toHaveBeenCalled();
    // Reopened, the card is gone without waiting on a refetch.
    api.ideas.mockClear();
    api.ideas.mockImplementation(() => new Promise(() => {})); // refetch pending
    const again = await openPanel();
    expect(within(again).queryByRole("article", { name: "Check the tire pressure" })).toBeNull();
    expect(within(again).getByText("No ideas right now.")).toBeTruthy();
  });

  it("Not now dismisses at once with no reason, collapses the card, then a reason re-posts", async () => {
    const api = fakeApi([OTHER]);
    chat(api);
    const dialog = await openPanel();
    const card = await within(dialog).findByRole("article", { name: "Check the tire pressure" });
    fireEvent.click(within(card).getByRole("button", { name: "Not now" }));
    expect(api.dismissIdea).toHaveBeenCalledTimes(1);
    expect(api.dismissIdea).toHaveBeenLastCalledWith("other", null);

    const row = within(dialog).getByRole("group", { name: "Check the tire pressure, dismissed" });
    expect(row.textContent).toContain("Dismissed — add a reason");
    expect(within(dialog).queryByRole("article")).toBeNull();

    const send = within(row).getByRole("button", { name: "Send" }) as HTMLButtonElement;
    expect(send.disabled).toBe(true); // nothing typed yet
    fireEvent.change(within(row).getByLabelText("Reason"), { target: { value: "  already did it  " } });
    fireEvent.click(send);
    expect(api.dismissIdea).toHaveBeenCalledTimes(2);
    expect(api.dismissIdea).toHaveBeenLastCalledWith("other", "already did it");
    expect(within(dialog).getByText("Dismissed — thanks")).toBeTruthy();
    expect(within(dialog).queryByLabelText("Reason")).toBeNull();
  });

  it("a collapsed row stays through a refresh that no longer lists the idea, and goes when the panel closes", async () => {
    const api = fakeApi([OTHER]);
    chat(api);
    const dialog = await openPanel();
    fireEvent.click(within(await within(dialog).findByRole("article")).getByRole("button", { name: "Not now" }));
    // The server has it dismissed now: the next fetch does not list it.
    api.ideas.mockImplementation(() => Promise.resolve([]));
    await act(async () => {
      Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(api.ideas).toHaveBeenCalledTimes(3); // on mount, on open, on coming back
    expect(within(dialog).getByRole("group", { name: "Check the tire pressure, dismissed" })).toBeTruthy();

    fireEvent.click(within(dialog).getByRole("button", { name: "Done" }));
    const again = await openPanel();
    expect(within(again).queryByRole("group")).toBeNull();
    expect(within(again).getByText("No ideas right now.")).toBeTruthy();
  });

  it("a failed fetch is quiet: no notice, no panel error, and the last good list stays", async () => {
    const api = fakeApi([OTHER]);
    chat(api);
    await waitFor(() => expect(api.ideas).toHaveBeenCalledTimes(1));
    api.ideas.mockImplementation(() => Promise.reject(new HttpError(500, "boom")));
    const dialog = await openPanel();
    await waitFor(() => expect(api.ideas).toHaveBeenCalledTimes(2));
    expect(within(dialog).getByRole("article", { name: "Check the tire pressure" })).toBeTruthy();
    expect(document.querySelector(".notice")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("an api that cannot even fetch ideas leaves the chat as it was", async () => {
    chat(fakeApi(new Error("offline")));
    const dialog = await openPanel();
    expect(within(dialog).getByText("No ideas right now.")).toBeTruthy();
    expect(screen.queryByTestId("spotlight")).toBeNull();
  });
});

describe("the dot", () => {
  it("shows for an idea new since the panel was last opened, and opening the panel clears it", async () => {
    chat(fakeApi([OTHER]));
    await screen.findByTestId("ideas-dot");
    fireEvent.click(screen.getByRole("button", { name: "Ideas" }));
    const dialog = await screen.findByRole("dialog", { name: "Ideas" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Done" }));
    expect(screen.queryByTestId("ideas-dot")).toBeNull();
  });

  it("clears on opening the panel even while a spotlight is open: the card is its signal", async () => {
    chat(fakeApi([SPOT]));
    await screen.findByTestId("spotlight");
    const dialog = await openPanel();
    fireEvent.click(within(dialog).getByRole("button", { name: "Done" }));
    expect(screen.queryByTestId("ideas-dot")).toBeNull();
    expect(screen.getByTestId("spotlight")).toBeTruthy();
  });
});

describe("the spotlight card", () => {
  it("shows the open spotlight with emoji and title, between the transcript and the composer", async () => {
    chat(fakeApi([SPOT, OTHER]));
    const card = await screen.findByTestId("spotlight");
    expect(card.textContent).toContain("✈️");
    expect(card.textContent).toContain("Rebook the Montreal flight");
    expect(card.closest("footer")).toBe(screen.getByPlaceholderText(/./).closest("footer"));
    // Nothing of it in the transcript.
    expect(within(screen.getByTestId("transcript")).queryByText(/Montreal/)).toBeNull();
  });

  it("is absent when no idea is the spotlight", async () => {
    const api = fakeApi([OTHER]);
    chat(api);
    await waitFor(() => expect(api.ideas).toHaveBeenCalled());
    expect(screen.queryByTestId("spotlight")).toBeNull();
  });

  it("is absent once the spotlight idea has passed its until", async () => {
    const api = fakeApi([{ ...SPOT, until: "2020-01-01" }]);
    chat(api);
    await waitFor(() => expect(api.ideas).toHaveBeenCalled());
    expect(screen.queryByTestId("spotlight")).toBeNull();
  });

  it("clicking the body opens the panel", async () => {
    chat(fakeApi([SPOT]));
    const card = await screen.findByTestId("spotlight");
    fireEvent.click(within(card).getByText("Rebook the Montreal flight"));
    const dialog = await screen.findByRole("dialog", { name: "Ideas" });
    await within(dialog).findByRole("article", { name: "Rebook the Montreal flight" });
  });

  it("Do it sends through the normal path with the idea id, without opening the panel, and the card goes", async () => {
    const store = chat(fakeApi([SPOT]));
    const card = await screen.findByTestId("spotlight");
    fireEvent.click(within(card).getByRole("button", { name: "Do it" }));
    expect(store.send).toHaveBeenCalledWith("Do it", undefined, { idea_id: "spot", emoji: "✈️", title: "Rebook the Montreal flight" });
    expect(screen.queryByTestId("spotlight")).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("Not now dismisses with no reason and the card goes", async () => {
    const api = fakeApi([SPOT]);
    const store = chat(api);
    const card = await screen.findByTestId("spotlight");
    fireEvent.click(within(card).getByRole("button", { name: "Not now" }));
    expect(api.dismissIdea).toHaveBeenCalledWith("spot", null);
    expect(store.send).not.toHaveBeenCalled();
    expect(screen.queryByTestId("spotlight")).toBeNull();
  });
});

describe("the Do it bubble", () => {
  const row = (quote?: { idea_id: string; emoji: string; title: string }) =>
    render(
      <MessageRow
        message={{ id: "u1", role: "user", text: "Do it", createdAt: 0, status: "done", ...(quote ? { quote } : {}) }}
        store={{} as ChatStore}
        isNew={false}
        onImageLoad={() => {}}
      />,
    );

  it("quotes the idea above the words: emoji and title, then Do it", () => {
    row({ idea_id: "spot", emoji: "✈️", title: "Rebook the Montreal flight" });
    const quote = screen.getByTestId("user-quote");
    expect(quote.textContent).toBe("✈️Rebook the Montreal flight");
    const text = document.querySelector(".user-text")!;
    expect(text.textContent).toBe("Do it");
    // Quote first in the bubble, words under it.
    expect(quote.compareDocumentPosition(text) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("a plain message has no quote block", () => {
    row();
    expect(screen.queryByTestId("user-quote")).toBeNull();
    expect(document.querySelector(".user-text")!.textContent).toBe("Do it");
  });
});
