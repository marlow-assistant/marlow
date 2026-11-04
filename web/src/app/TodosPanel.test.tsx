// The to-dos pane (docs/tasks/todos-and-reminders.md, part 2): the panel's own
// rendering, then how Chat wires the icon, its badge and the taps to the api
// and to the normal send path.
import "fake-indexeddb/auto";
import { IDBFactory } from "fake-indexeddb";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { Api } from "../lib/api";
import { HttpError, TransportError } from "../lib/api";
import type { Todo } from "../lib/apiModels";
import { DEFAULT_FACE } from "../lib/faces";
import type { ChatState, ChatStore } from "../lib/chatStore";
import { LocationWatcher } from "../lib/location";
import { resetDbForTests } from "../lib/storage";
import type { ChatMessage } from "../lib/types";
import type { ChangeEntry } from "../lib/whatsNew";
import { Chat } from "./Chat";
import { MessageRow } from "./MessageRow";
import { TALK_TEXT, TodosPanel, type TodosPanelProps } from "./TodosPanel";

beforeEach(() => {
  indexedDB = new IDBFactory();
  resetDbForTests();
});
afterEach(() => {
  cleanup();
  resetDbForTests();
});

// Wed 4 Nov 2026, 10:00 local.
const NOW = new Date(2026, 10, 4, 10, 0);

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

function panel(over: Partial<TodosPanelProps> = {}) {
  const props: TodosPanelProps = {
    todos: [],
    now: NOW,
    notice: null,
    busy: new Set(),
    onDone: vi.fn(),
    onReopen: vi.fn(),
    onDrop: vi.fn(),
    onAccept: vi.fn(),
    onSnooze: vi.fn(),
    onTalk: vi.fn(),
    onClose: vi.fn(),
    ...over,
  };
  return { ...render(<TodosPanel {...props} />), props };
}

const row = (title: string) => screen.getByRole("listitem", { name: title });

describe("to-dos panel", () => {
  it("shows the sections in order, each hidden when empty", () => {
    panel({
      todos: [
        todo("d", { state: "done", closed: iso(2026, 11, 3, 9) }),
        todo("p", { state: "proposed" }),
        todo("o"),
      ],
    });
    expect(screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent)).toEqual([
      "To do",
      "Marlow suggested",
      "Done this week",
    ]);
    cleanup();
    panel({ todos: [todo("o")] });
    expect(screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent)).toEqual(["To do"]);
  });

  it("says so calmly when there is nothing", () => {
    panel();
    expect(screen.getByText("Nothing on your list. Ask Marlow to add something.")).toBeTruthy();
    expect(screen.queryAllByRole("heading", { level: 3 })).toHaveLength(0);
  });

  it("a row has its title, a friendly due line, and marks an overdue one", () => {
    panel({
      todos: [todo("late", { due: iso(2026, 11, 2, 14) }), todo("soon", { due: "2026-11-05" })],
    });
    const late = within(row("Title late")).getByText("Overdue · Mon 2 Nov 14:00");
    expect(late.className).toContain("todo-overdue");
    const soon = within(row("Title soon")).getByText("Tomorrow");
    expect(soon.className).not.toContain("todo-overdue");
  });

  it("shows Snoozed until when set", () => {
    panel({ todos: [todo("s", { snoozedUntil: iso(2026, 11, 5, 9) })] });
    expect(within(row("Title s")).getByText("Snoozed until Tomorrow 09:00")).toBeTruthy();
  });

  it("notes are one greyed line that opens on tap", () => {
    panel({ todos: [todo("n", { notes: "ask about\nthe cleaning" })] });
    const r = row("Title n");
    const toggle = within(r).getByRole("button", { name: /ask about/ });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(toggle.className).not.toContain("todo-notes-open");
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(toggle.className).toContain("todo-notes-open");
    // The notes keep their line break once open.
    expect(toggle.textContent).toBe("ask about\nthe cleaning");
  });

  it("no notes line for an item without notes", () => {
    panel({ todos: [todo("n")] });
    expect(row("Title n").querySelector(".todo-notes")).toBeNull();
  });

  it("done rows are struck through, ticked, and the tick takes it back", () => {
    const d = todo("d", { state: "done", closed: iso(2026, 11, 3, 9) });
    const { props } = panel({ todos: [d] });
    const r = row("Title d");
    expect(r.className).toContain("todo-done");
    expect(within(r).queryAllByRole("button")).toHaveLength(0);
    const box = within(r).getByRole("checkbox", { name: "Mark not done: Title d" }) as HTMLInputElement;
    expect(box.checked).toBe(true);
    fireEvent.click(box);
    expect(props.onReopen).toHaveBeenCalledWith(d);
  });

  it("the checkbox on an open row is Done", () => {
    const a = todo("a");
    const { props } = panel({ todos: [a] });
    fireEvent.click(within(row("Title a")).getByRole("checkbox", { name: "Mark done: Title a" }));
    expect(props.onDone).toHaveBeenCalledWith(a);
  });

  it("the open row's menu holds Snooze, Talk about it and Drop", () => {
    const a = todo("a");
    const { props } = panel({ todos: [a] });
    const r = row("Title a");
    expect(within(r).queryByRole("button", { name: "Drop" })).toBeNull();
    fireEvent.click(within(r).getByRole("button", { name: "More for Title a" }));
    fireEvent.click(within(r).getByRole("menuitem", { name: "Talk about it" }));
    expect(props.onTalk).toHaveBeenCalledWith(a);
    // Choosing closes the menu.
    expect(within(r).queryByRole("menu")).toBeNull();
    fireEvent.click(within(r).getByRole("button", { name: "More for Title a" }));
    fireEvent.click(within(r).getByRole("menuitem", { name: "Drop" }));
    expect(props.onDrop).toHaveBeenCalledWith(a);
  });

  // Mike, 2026-11-04: inline buttons under the title didn't read as the
  // row's menu. A popover by the ⋯, as on iOS, that Escape or a tap away closes.
  it("the ⋯ opens a menu beside it, and Escape or a tap elsewhere closes it", () => {
    panel({ todos: [todo("a")] });
    const r = row("Title a");
    const more = within(r).getByRole("button", { name: "More for Title a" });
    fireEvent.click(more);
    const menu = within(r).getByRole("menu", { name: "Actions for Title a" });
    expect(more.getAttribute("aria-expanded")).toBe("true");
    expect(menu.closest(".todo-menu")).toBe(more.closest(".todo-menu"));
    fireEvent.keyDown(document, { key: "Escape" });
    expect(within(r).queryByRole("menu")).toBeNull();
    fireEvent.click(more);
    fireEvent.pointerDown(document.body);
    expect(within(r).queryByRole("menu")).toBeNull();
  });

  // Mike, 2026-11-04, Chrome on a Mac: Snooze blanked the page. Chrome's
  // scrollIntoView returns a promise, and an effect that returned it handed
  // React a "cleanup" that isn't a function. jsdom has no scrollIntoView, so
  // stand in Chrome's.
  it("opening Snooze survives a scrollIntoView that returns a promise, as Chrome's does", () => {
    const proto = Element.prototype as unknown as { scrollIntoView?: unknown };
    const had = proto.scrollIntoView;
    proto.scrollIntoView = () => Promise.resolve();
    try {
      panel({ todos: [todo("a")] });
      const r = row("Title a");
      fireEvent.click(within(r).getByRole("button", { name: "More for Title a" }));
      fireEvent.click(within(r).getByRole("menuitem", { name: "Snooze" }));
      expect(within(r).getByRole("menuitem", { name: "Tomorrow 09:00" })).toBeTruthy();
      fireEvent.click(within(r).getByRole("menuitem", { name: "Snooze" }));
      expect(within(r).queryByRole("menuitem", { name: "Tomorrow 09:00" })).toBeNull();
    } finally {
      proto.scrollIntoView = had;
    }
  });

  it("Snooze offers the presets, and a tap hands over the ISO instant", () => {
    const a = todo("a");
    const { props } = panel({ todos: [a] });
    const r = row("Title a");
    fireEvent.click(within(r).getByRole("button", { name: "More for Title a" }));
    fireEvent.click(within(r).getByRole("menuitem", { name: "Snooze" }));
    expect(within(r).getAllByRole("menuitem", { name: /^(In 2 hours|This evening 18:00|Tomorrow 09:00|Pick a time…)$/ }).map((b) => b.textContent)).toEqual([
      "In 2 hours",
      "This evening 18:00",
      "Tomorrow 09:00",
      "Pick a time…",
    ]);
    fireEvent.click(within(r).getByRole("menuitem", { name: "Tomorrow 09:00" }));
    expect(props.onSnooze).toHaveBeenCalledWith(a, iso(2026, 11, 5, 9));
  });

  it("This evening is not offered from 17:00", () => {
    panel({ todos: [todo("a")], now: new Date(2026, 10, 4, 17, 30) });
    const r = row("Title a");
    fireEvent.click(within(r).getByRole("button", { name: "More for Title a" }));
    fireEvent.click(within(r).getByRole("menuitem", { name: "Snooze" }));
    expect(within(r).queryByRole("menuitem", { name: /This evening/ })).toBeNull();
  });

  it("Pick a time takes a date and time, and only a future one can be confirmed", () => {
    const a = todo("a");
    const { props } = panel({ todos: [a] });
    const r = row("Title a");
    fireEvent.click(within(r).getByRole("button", { name: "More for Title a" }));
    fireEvent.click(within(r).getByRole("menuitem", { name: "Snooze" }));
    fireEvent.click(within(r).getByRole("menuitem", { name: "Pick a time…" }));
    const input = within(r).getByLabelText("Snooze until") as HTMLInputElement;
    expect(input.type).toBe("datetime-local");
    const confirm = within(r).getByRole("button", { name: "Snooze until picked time" }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    fireEvent.change(input, { target: { value: "2026-11-04T08:00" } });
    expect(confirm.disabled).toBe(true);
    fireEvent.change(input, { target: { value: "2026-11-06T09:30" } });
    expect(confirm.disabled).toBe(false);
    fireEvent.click(confirm);
    expect(props.onSnooze).toHaveBeenCalledWith(a, iso(2026, 11, 6, 9, 30));
  });

  it("a proposed row offers Accept, Drop and Talk about it, and says it won't remind until accepted", () => {
    const p = todo("p", { state: "proposed" });
    const { props } = panel({ todos: [p] });
    const r = row("Title p");
    expect(within(r).queryByRole("checkbox")).toBeNull();
    fireEvent.click(within(r).getByRole("button", { name: "Accept" }));
    expect(props.onAccept).toHaveBeenCalledWith(p);
    fireEvent.click(within(r).getByRole("button", { name: "Drop" }));
    expect(props.onDrop).toHaveBeenCalledWith(p);
    fireEvent.click(within(r).getByRole("button", { name: "Talk about it" }));
    expect(props.onTalk).toHaveBeenCalledWith(p);
    expect(screen.getByText(/won't remind you until you accept/)).toBeTruthy();
  });

  it("a row with a tap in flight has its controls disabled", () => {
    panel({ todos: [todo("a"), todo("p", { state: "proposed" })], busy: new Set(["a", "p"]) });
    expect((within(row("Title a")).getByRole("checkbox") as HTMLInputElement).disabled).toBe(true);
    expect((within(row("Title p")).getByRole("button", { name: "Accept" }) as HTMLButtonElement).disabled).toBe(true);
    expect((within(row("Title p")).getByRole("button", { name: "Drop" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("shows the notice, if any, as a status", () => {
    panel({ todos: [todo("a")], notice: "That to-do changed; here's the latest." });
    expect(screen.getByRole("status").textContent).toBe("That to-do changed; here's the latest.");
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

type Fn = ReturnType<typeof vi.fn>;

/** A todos list the fake serves; the taps answer with the item changed. */
function fakeApi(initial: Todo[] | Error) {
  let list = initial instanceof Error ? [] : initial;
  const answer = (state: Todo["state"], extra: Partial<Todo> = {}) =>
    vi.fn(async (id: string) => {
      const found = list.find((t) => t.id === id);
      if (!found) throw new HttpError(404, "no such to-do");
      const next = { ...found, state, ...extra };
      list = list.map((t) => (t.id === id ? next : t));
      return next;
    });
  const api = {
    todos: vi.fn(() => (initial instanceof Error ? Promise.reject(initial) : Promise.resolve(list))),
    doneTodo: answer("done", { closed: iso(2026, 11, 4, 10) }),
    dropTodo: answer("dropped"),
    acceptTodo: answer("open"),
    reopenTodo: answer("open", { closed: null }),
    snoozeTodo: vi.fn(async (id: string, until: string) => {
      const next = { ...list.find((t) => t.id === id)!, snoozedUntil: until };
      list = list.map((t) => (t.id === id ? next : t));
      return next;
    }),
    ideas: vi.fn().mockResolvedValue([]),
    dismissIdea: vi.fn(),
    activity: vi.fn().mockResolvedValue({ entries: [], next_before: null }),
    identity: vi.fn(),
    identityFile: vi.fn(),
    webPushKey: vi.fn(),
    registerWebPush: vi.fn(),
  };
  return Object.assign(api, {
    serve: (next: Todo[]) => {
      list = next;
    },
  }) as unknown as Api & {
    todos: Fn;
    doneTodo: Fn;
    dropTodo: Fn;
    acceptTodo: Fn;
    reopenTodo: Fn;
    snoozeTodo: Fn;
    serve: (t: Todo[]) => void;
  };
}

const NO_CHANGES: ChangeEntry[] = [];

function chat(api: ReturnType<typeof fakeApi>, store = stubStore()) {
  const location = new LocationWatcher({ sharing: "off", geolocation: undefined, isVisible: () => true });
  render(
    <Chat store={store} location={location} api={api} token="abcdef123456" face={DEFAULT_FACE} onFace={() => {}} onEffort={() => {}} onUnpair={() => {}} bundledChanges={NO_CHANGES} />,
  );
  return store;
}

// Real clock in Chat, so these dates are far from now in either direction.
const LATE = "2020-01-01";
const FAR = "2099-12-31";

async function openPanel() {
  fireEvent.click(screen.getByRole("button", { name: /^To-dos/ }));
  return screen.findByRole("dialog", { name: "To-dos" });
}

describe("Chat and the to-dos panel", () => {
  it("the icon opens the panel with the fetched list, and Done closes it", async () => {
    chat(fakeApi([todo("a", { due: FAR })]));
    const dialog = await openPanel();
    await within(dialog).findByRole("listitem", { name: "Title a" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Done" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("the badge counts overdue open items plus proposed ones; nothing at 0", async () => {
    chat(fakeApi([todo("late", { due: LATE }), todo("p", { state: "proposed" }), todo("fine", { due: FAR })]));
    const badge = await screen.findByTestId("todos-badge");
    expect(badge.textContent).toBe("2");
    expect(screen.getByRole("button", { name: "To-dos, 2 need attention" })).toBeTruthy();
  });

  it("no badge when nothing needs attention", async () => {
    const api = fakeApi([todo("fine", { due: FAR })]);
    chat(api);
    await waitFor(() => expect(api.todos).toHaveBeenCalled());
    expect(screen.queryByTestId("todos-badge")).toBeNull();
    expect(screen.getByRole("button", { name: "To-dos" })).toBeTruthy();
  });

  it("fetches on open and when the app comes back to the foreground", async () => {
    const api = fakeApi([todo("a")]);
    chat(api);
    await waitFor(() => expect(api.todos).toHaveBeenCalledTimes(1));
    await openPanel();
    await waitFor(() => expect(api.todos).toHaveBeenCalledTimes(2));
    await act(async () => {
      Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(api.todos).toHaveBeenCalledTimes(3);
  });

  it("a failed fetch is quiet: no notice, no alert, and the last good list stays", async () => {
    const api = fakeApi([todo("a")]);
    chat(api);
    await waitFor(() => expect(api.todos).toHaveBeenCalledTimes(1));
    api.todos.mockImplementation(() => Promise.reject(new HttpError(500, "boom")));
    const dialog = await openPanel();
    await waitFor(() => expect(api.todos).toHaveBeenCalledTimes(2));
    expect(within(dialog).getByRole("listitem", { name: "Title a" })).toBeTruthy();
    expect(within(dialog).queryByRole("status")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("an api that cannot even fetch leaves the chat as it was", async () => {
    chat(fakeApi(new Error("offline")));
    const dialog = await openPanel();
    expect(within(dialog).getByText("Nothing on your list. Ask Marlow to add something.")).toBeTruthy();
  });

  // Mike, 2026-11-04: a ticked item jumped to the bottom and couldn't be
  // taken back. It stays put, struck through, until the pane is reopened.
  it("the checkbox posts done and the item stays where it was, struck through", async () => {
    const api = fakeApi([todo("a", { due: FAR }), todo("b")]);
    chat(api);
    const dialog = await openPanel();
    const r = await within(dialog).findByRole("listitem", { name: "Title a" });
    fireEvent.click(within(r).getByRole("checkbox"));
    expect(api.doneTodo).toHaveBeenCalledWith("a");
    await waitFor(() => expect(within(dialog).getByRole("listitem", { name: "Title a" }).className).toContain("todo-done"));
    expect(within(dialog).queryByRole("region", { name: "Done this week" })).toBeNull();
    const toDo = within(dialog).getByRole("region", { name: "To do" });
    expect(within(toDo).getAllByRole("listitem").map((li) => li.getAttribute("aria-label"))).toEqual(["Title a", "Title b"]);
  });

  it("untick reopens it in place, and the next open sorts a done item to the bottom", async () => {
    const api = fakeApi([todo("a", { due: FAR }), todo("b")]);
    chat(api);
    let dialog = await openPanel();
    fireEvent.click(within(await within(dialog).findByRole("listitem", { name: "Title a" })).getByRole("checkbox"));
    const box = await within(dialog).findByRole("checkbox", { name: "Mark not done: Title a" });
    fireEvent.click(box);
    expect(api.reopenTodo).toHaveBeenCalledWith("a");
    await within(dialog).findByRole("checkbox", { name: "Mark done: Title a" });
    expect(within(dialog).getByRole("listitem", { name: "Title a" }).className).not.toContain("todo-done");

    fireEvent.click(within(dialog).getByRole("checkbox", { name: "Mark done: Title a" }));
    await within(dialog).findByRole("checkbox", { name: "Mark not done: Title a" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Done" }));
    dialog = await openPanel();
    const done = await within(dialog).findByRole("region", { name: "Done this week" });
    expect(within(done).getByRole("listitem", { name: "Title a" })).toBeTruthy();
  });

  it("a second tap while the first is in flight posts nothing more", async () => {
    const api = fakeApi([todo("a")]);
    chat(api);
    const dialog = await openPanel();
    const r = await within(dialog).findByRole("listitem", { name: "Title a" });
    const box = within(r).getByRole("checkbox");
    fireEvent.click(box);
    fireEvent.click(box);
    await waitFor(() => expect(within(dialog).getByRole("listitem", { name: "Title a" }).className).toContain("todo-done"));
    expect(api.doneTodo).toHaveBeenCalledTimes(1);
    expect(api.reopenTodo).not.toHaveBeenCalled();
  });

  it("Drop removes the item", async () => {
    const api = fakeApi([todo("a"), todo("b")]);
    chat(api);
    const dialog = await openPanel();
    const r = await within(dialog).findByRole("listitem", { name: "Title a" });
    fireEvent.click(within(r).getByRole("button", { name: "More for Title a" }));
    fireEvent.click(within(r).getByRole("menuitem", { name: "Drop" }));
    expect(api.dropTodo).toHaveBeenCalledWith("a");
    await waitFor(() => expect(within(dialog).queryByRole("listitem", { name: "Title a" })).toBeNull());
    expect(within(dialog).getByRole("listitem", { name: "Title b" })).toBeTruthy();
  });

  it("Accept moves a suggestion into To do", async () => {
    const api = fakeApi([todo("p", { state: "proposed" })]);
    chat(api);
    const dialog = await openPanel();
    const r = await within(dialog).findByRole("listitem", { name: "Title p" });
    fireEvent.click(within(r).getByRole("button", { name: "Accept" }));
    expect(api.acceptTodo).toHaveBeenCalledWith("p");
    await waitFor(() => expect(within(dialog).getByRole("region", { name: "To do" })).toBeTruthy());
    expect(within(dialog).queryByRole("region", { name: "Marlow suggested" })).toBeNull();
  });

  it("a snooze posts the preset's instant and the row then reads Snoozed until", async () => {
    const api = fakeApi([todo("a")]);
    chat(api);
    const dialog = await openPanel();
    const r = await within(dialog).findByRole("listitem", { name: "Title a" });
    fireEvent.click(within(r).getByRole("button", { name: "More for Title a" }));
    fireEvent.click(within(r).getByRole("menuitem", { name: "Snooze" }));
    fireEvent.click(within(r).getByRole("menuitem", { name: "Tomorrow 09:00" }));
    expect(api.snoozeTodo).toHaveBeenCalledTimes(1);
    const [id, until] = api.snoozeTodo.mock.calls[0];
    expect(id).toBe("a");
    expect(until).toMatch(/^\d{4}-\d\d-\d\dT09:00:00[+-]\d\d:\d\d$/);
    await waitFor(() => expect(within(dialog).getByText(/^Snoozed until/)).toBeTruthy());
  });

  it.each([404, 409])("a %i refetches the list and says so gently", async (status) => {
    const api = fakeApi([todo("a"), todo("b")]);
    chat(api);
    const dialog = await openPanel();
    const r = await within(dialog).findByRole("listitem", { name: "Title a" });
    // Somewhere else the item changed: the list the box now serves lacks it.
    api.serve([todo("b")]);
    api.doneTodo.mockRejectedValueOnce(new HttpError(status, "That to-do is not open"));
    const before = api.todos.mock.calls.length;
    fireEvent.click(within(r).getByRole("checkbox"));
    await waitFor(() => expect(api.todos.mock.calls.length).toBeGreaterThan(before));
    const note = await within(dialog).findByRole("status");
    expect(note.textContent).toBe("That to-do changed; here's the latest.");
    expect(within(dialog).queryByRole("listitem", { name: "Title a" })).toBeNull();
    expect(within(dialog).getByRole("listitem", { name: "Title b" })).toBeTruthy();
    // The server's sentence is for logs, not for the screen.
    expect(dialog.textContent).not.toContain("not open");
  });

  it("a failure that is not 404/409 says it could not save, and leaves the item", async () => {
    const api = fakeApi([todo("a")]);
    chat(api);
    const dialog = await openPanel();
    const r = await within(dialog).findByRole("listitem", { name: "Title a" });
    api.doneTodo.mockRejectedValueOnce(new TransportError("offline"));
    fireEvent.click(within(r).getByRole("checkbox"));
    const note = await within(dialog).findByRole("status");
    expect(note.textContent).toBe("Couldn't save that. Check your connection.");
    expect(within(dialog).getByRole("listitem", { name: "Title a" })).toBeTruthy();
    expect((within(dialog).getByRole("checkbox") as HTMLInputElement).disabled).toBe(false);
  });

  async function stageTalk(store: ReturnType<typeof chat>, title: string) {
    const dialog = await openPanel();
    const r = await within(dialog).findByRole("listitem", { name: title });
    const more = within(r).queryByRole("button", { name: `More for ${title}` });
    if (more) fireEvent.click(more);
    fireEvent.click(within(r).getByRole(more ? "menuitem" : "button", { name: "Talk about it" }));
    return store;
  }

  // Mike, 2026-11-04: sending "About this to-do" on the tap spoke for him.
  it("Talk about it closes the pane and stages the to-do above the composer, sending nothing", async () => {
    const api = fakeApi([todo("t3", { title: "Book the dentist" })]);
    const store = await stageTalk(chat(api), "Book the dentist");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(store.send).not.toHaveBeenCalled();
    expect(screen.getByTestId("staged-todo").textContent).toContain("Book the dentist");
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("textbox", { name: "Message" })));
    // Nothing was tapped on the list itself.
    expect(api.doneTodo).not.toHaveBeenCalled();
  });

  it("what they type goes out with the to-do as its quote, and the chip clears", async () => {
    const store = await stageTalk(chat(fakeApi([todo("t3", { title: "Book the dentist" })])), "Book the dentist");
    fireEvent.change(screen.getByRole("textbox", { name: "Message" }), { target: { value: "can this wait a week?" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(store.send).toHaveBeenCalledTimes(1));
    expect(store.send).toHaveBeenCalledWith("can this wait a week?", [], { todo_id: "t3", emoji: "☑️", title: "Book the dentist" });
    await waitFor(() => expect(screen.queryByTestId("staged-todo")).toBeNull());
  });

  it("the chip alone sends the default words", async () => {
    const store = await stageTalk(chat(fakeApi([todo("t4", { state: "proposed", title: "Pay the water bill" })])), "Pay the water bill");
    expect(TALK_TEXT).toBe("About this to-do");
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(store.send).toHaveBeenCalledWith("About this to-do", [], { todo_id: "t4", emoji: "☑️", title: "Pay the water bill" }));
  });

  it("removing the chip unstages it, and the next message is an ordinary one", async () => {
    const store = await stageTalk(chat(fakeApi([todo("t3", { title: "Book the dentist" })])), "Book the dentist");
    fireEvent.click(screen.getByRole("button", { name: "Don't talk about this to-do" }));
    expect(screen.queryByTestId("staged-todo")).toBeNull();
    expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
    fireEvent.change(screen.getByRole("textbox", { name: "Message" }), { target: { value: "hi" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(store.send).toHaveBeenCalledWith("hi", []));
  });
});

describe("a to-do quote in the transcript", () => {
  function userMessage(quote: ChatMessage["quote"]): ChatMessage {
    return { id: "u1", role: "user", text: "About this to-do", createdAt: 1, status: "done", quote };
  }
  function renderRow(m: ChatMessage) {
    return render(<MessageRow message={m} store={{} as ChatStore} isNew={false} onImageLoad={() => {}} />);
  }

  it("renders like an idea's: emoji and title above the text", () => {
    renderRow(userMessage({ todo_id: "t3", emoji: "☑️", title: "Book the dentist" }));
    const q = screen.getByTestId("user-quote");
    expect(q.textContent).toContain("☑️");
    expect(q.textContent).toContain("Book the dentist");
    expect(screen.getByText("About this to-do")).toBeTruthy();
  });

  it("an idea's old shape still renders", () => {
    renderRow(userMessage({ idea_id: "dinner", emoji: "🍽", title: "Draft the dinner invites" }));
    expect(screen.getByTestId("user-quote").textContent).toContain("Draft the dinner invites");
  });
});
