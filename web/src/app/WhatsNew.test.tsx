// What's new (docs/tasks/whats-new.md): how Chat raises the update card, what
// its two buttons do, and the Settings list and switch behind it.
import "fake-indexeddb/auto";
import { IDBFactory } from "fake-indexeddb";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import type { Api } from "../lib/api";
import type { ChatState, ChatStore } from "../lib/chatStore";
import { DEFAULT_FACE } from "../lib/faces";
import { LocationWatcher } from "../lib/location";
import { resetDbForTests } from "../lib/storage";
import type { ChangeEntry } from "../lib/whatsNew";
import { Chat } from "./Chat";

beforeEach(() => {
  indexedDB = new IDBFactory();
  resetDbForTests();
});
afterEach(() => {
  cleanup();
  resetDbForTests();
});

const APP: ChangeEntry[] = [
  { id: 2, date: "2026-10-23", part: "app", category: "feature", text: "Voice mode works on Android." },
];
const SERVER: ChangeEntry = {
  id: 1,
  date: "2026-10-22",
  part: "server",
  category: "access",
  text: "Marlow can read your Drive when you ask.",
};

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
    send: vi.fn(),
  } as unknown as ChatStore;
}

function fakeApi(server: ChangeEntry[], onboardingComplete = true) {
  return {
    changes: vi.fn().mockResolvedValue({ entries: server, onboardingComplete }),
    ideas: vi.fn().mockResolvedValue([]),
    dismissIdea: vi.fn(),
    dayTimes: vi.fn(() => Promise.reject(new Error("not in this test"))),
    webPushKey: vi.fn(),
    registerWebPush: vi.fn(),
  } as unknown as Api;
}

function chat(api: Api) {
  const location = new LocationWatcher({ sharing: "off", geolocation: undefined, isVisible: () => true });
  return render(
    <Chat
      store={stubStore()}
      location={location}
      api={api}
      token="abcdef123456"
      face={DEFAULT_FACE}
      onFace={() => {}}
      onEffort={() => {}}
      onUnpair={() => {}}
      bundledChanges={APP}
    />,
  );
}

describe("the update card", () => {
  it("shows both halves on a first run, and OK means it does not come back", async () => {
    const api = fakeApi([SERVER]);
    const first = chat(api);
    const card = await screen.findByRole("dialog", { name: "Marlow was updated" });
    expect(within(card).getByText("Voice mode works on Android.")).toBeTruthy();
    expect(within(card).getByText("Marlow can read your Drive when you ask.")).toBeTruthy();
    // A must-tell category is labelled as such, not as "New".
    expect(within(card).getByText("What Marlow can reach")).toBeTruthy();

    fireEvent.click(within(card).getByRole("button", { name: "OK" }));
    expect(screen.queryByRole("dialog", { name: "Marlow was updated" })).toBeNull();

    first.unmount();
    chat(api);
    // Give the relaunch's fetch and storage read time to land.
    await vi.waitFor(() => expect(api.changes).toHaveBeenCalledTimes(2));
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByRole("dialog", { name: "Marlow was updated" })).toBeNull();
  });

  it("has no mute button, only a hint at the Settings switch, which exists and works", async () => {
    chat(fakeApi([SERVER]));
    const card = await screen.findByRole("dialog", { name: "Marlow was updated" });
    expect(within(card).queryByRole("button", { name: /Don't show/ })).toBeNull();
    expect(within(card).getByText("You can turn off What's new in Settings.")).toBeTruthy();
    fireEvent.click(within(card).getByRole("button", { name: "OK" }));

    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    const settings = screen.getByRole("dialog", { name: "Settings" });
    const updates = within(settings).getByRole("region", { name: "Updates" });
    const toggle = within(updates).getByRole("checkbox", { name: "Show after updates" }) as HTMLInputElement;
    expect(toggle.checked).toBe(true);

    fireEvent.click(within(updates).getByRole("button", { name: "What's new" }));
    expect(within(updates).getByText("Voice mode works on Android.")).toBeTruthy();
    expect(within(updates).getByText("Marlow can read your Drive when you ask.")).toBeTruthy();

    fireEvent.click(toggle);
    expect(toggle.checked).toBe(false);
  });

  it("holds the card back until onboarding ends, then shows only what is new after", async () => {
    const api = fakeApi([SERVER], false);
    const first = chat(api);
    await vi.waitFor(() => expect(api.changes).toHaveBeenCalledTimes(1));
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByRole("dialog", { name: "Marlow was updated" })).toBeNull();
    first.unmount();

    // Onboarding ends: what was held back is history, not a queued backlog.
    (api.changes as ReturnType<typeof vi.fn>).mockResolvedValue({ entries: [SERVER], onboardingComplete: true });
    chat(api);
    await vi.waitFor(() => expect(api.changes).toHaveBeenCalledTimes(2));
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByRole("dialog", { name: "Marlow was updated" })).toBeNull();
  });

  it("turned off in Settings, a card waiting behind the sheet does not appear when it closes", async () => {
    const api = fakeApi([SERVER]);
    chat(api);
    // Settings opens before the first refresh lands, so the card waits.
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    await vi.waitFor(() => expect(api.changes).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 20));
    const settings = screen.getByRole("dialog", { name: "Settings" });
    fireEvent.click(within(settings).getByRole("checkbox", { name: "Show after updates" }));
    fireEvent.click(within(settings).getByRole("button", { name: "Done" }));
    expect(screen.queryByRole("dialog", { name: "Marlow was updated" })).toBeNull();
  });
});
