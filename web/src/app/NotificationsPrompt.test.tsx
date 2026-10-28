// WEB-9: the chat asks for notifications itself until they are on.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { HttpError, TransportError, type Api } from "../lib/api";
import { DEFAULT_FACE } from "../lib/faces";
import { NotificationsPrompt } from "./NotificationsPrompt";
import { resetPushStateForTests, syncPushSubscription } from "./push";
import { Settings } from "./Settings";

type Perm = NotificationPermission;
let permission: Perm;
let askAnswer: Perm;
const requestPermission = vi.fn(async () => {
  permission = askAnswer;
  return askAnswer;
});

const subscribe = vi.fn(async () => ({ toJSON: () => ({ endpoint: "https://fcm.googleapis.com/x", keys: {} }) }));

function installPush() {
  vi.stubGlobal("PushManager", function PushManager() {});
  vi.stubGlobal("Notification", {
    get permission() {
      return permission;
    },
    requestPermission,
  });
  Object.defineProperty(navigator, "serviceWorker", {
    configurable: true,
    value: { ready: Promise.resolve({ pushManager: { getSubscription: async () => null, subscribe } }) },
  });
}

function api(over: Partial<Api> = {}): Api {
  return {
    webPushKey: vi.fn(async () => "BAAA"),
    registerWebPush: vi.fn(async () => undefined),
    ...over,
  } as unknown as Api;
}

beforeEach(() => {
  permission = "default";
  askAnswer = "granted";
  requestPermission.mockClear();
  resetPushStateForTests();
  installPush();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  delete (navigator as unknown as Record<string, unknown>).serviceWorker;
});

const prompt = () => screen.queryByTestId("notify-prompt");

describe("WEB-9 notifications prompt", () => {
  it("asks while notifications are off, and goes away once Turn on succeeds", async () => {
    const a = api();
    render(<NotificationsPrompt api={a} />);
    expect(prompt()?.textContent).toContain("Turn on notifications");
    expect(requestPermission).not.toHaveBeenCalled(); // never on load: only from a tap
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Turn on" })));
    expect(requestPermission).toHaveBeenCalledTimes(1);
    expect(a.registerWebPush).toHaveBeenCalledTimes(1);
    expect(prompt()).toBeNull();
  });

  it("says so when the browser grants permission but the box cannot take the subscription", async () => {
    render(<NotificationsPrompt api={api({ webPushKey: vi.fn(async () => Promise.reject(new Error("x"))) })} />);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Turn on" })));
    expect(prompt()?.textContent).toContain("can't be turned on");
    expect(screen.queryByRole("button", { name: "Turn on" })).toBeNull();
  });

  it("a refusal at the browser's prompt turns it into the blocked notice, with no button to ask again", async () => {
    askAnswer = "denied";
    render(<NotificationsPrompt api={api()} />);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Turn on" })));
    expect(prompt()?.textContent).toContain("blocked");
    expect(screen.queryByRole("button", { name: "Turn on" })).toBeNull();
  });

  it("Not now hides it for this launch only", () => {
    const { unmount } = render(<NotificationsPrompt api={api()} />);
    fireEvent.click(screen.getByRole("button", { name: "Not now" }));
    expect(prompt()).toBeNull();
    unmount();
    render(<NotificationsPrompt api={api()} />);
    expect(prompt()).not.toBeNull();
  });

  it("shows nothing when notifications are already on, or the browser has no push", () => {
    permission = "granted";
    const { unmount } = render(<NotificationsPrompt api={api()} />);
    expect(prompt()).toBeNull();
    unmount();
    vi.unstubAllGlobals();
    render(<NotificationsPrompt api={api()} />);
    expect(prompt()).toBeNull();
  });

  it("turning them on in Settings clears the prompt too", async () => {
    const a = api();
    render(<NotificationsPrompt api={a} />);
    render(
      <Settings
        api={a}
        token="tok"
        reauthNeeded={false}
        queuePaused={false}
        effort="low"
        serverModel={null}
        serverBuild={null}
        onEffort={() => {}}
        locationSharing="off"
        locationStatus="ok"
        onLocationSharing={() => {}}
        face={DEFAULT_FACE}
        onFace={() => {}}
        onUnpair={() => {}}
        onClose={() => {}}
      />,
    );
    expect(prompt()).not.toBeNull();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Turn on notifications" })));
    expect(prompt()).toBeNull();
    expect(screen.getByRole("button", { name: "Notifications are on" })).toBeTruthy();
  });

  it("a permission granted elsewhere is picked up when the app comes back to the foreground", async () => {
    render(<NotificationsPrompt api={api()} />);
    expect(prompt()).not.toBeNull();
    permission = "granted";
    await act(async () => document.dispatchEvent(new Event("visibilitychange")));
    expect(prompt()).toBeNull();
  });

  it("a launch whose subscription the box refuses is reported, and Settings opened later says so too", async () => {
    permission = "granted";
    const a = api({ registerWebPush: vi.fn(async () => Promise.reject(new HttpError(400, "invalid web push subscription"))) });
    render(<NotificationsPrompt api={a} />);
    expect(prompt()).toBeNull(); // granted: nothing to ask, until the launch sync answers
    await act(async () => void (await syncPushSubscription(a)));
    expect(prompt()?.textContent).toContain("can't be turned on");
    render(
      <Settings
        api={a}
        token="tok"
        reauthNeeded={false}
        queuePaused={false}
        effort="low"
        serverModel={null}
        serverBuild={null}
        onEffort={() => {}}
        locationSharing="off"
        locationStatus="ok"
        onLocationSharing={() => {}}
        face={DEFAULT_FACE}
        onFace={() => {}}
        onUnpair={() => {}}
        onClose={() => {}}
      />,
    );
    expect(screen.getByRole("button", { name: "This browser's notifications can't be turned on" })).toBeTruthy();
  });

  it("a launch that cannot reach the box says nothing: unreachable is not broken", async () => {
    permission = "granted";
    const a = api({ webPushKey: vi.fn(async () => Promise.reject(new TransportError("Failed to fetch"))) });
    render(<NotificationsPrompt api={a} />);
    await act(async () => void (await syncPushSubscription(a)));
    expect(prompt()).toBeNull();
  });

  it("a later launch that registers clears an earlier failure", async () => {
    permission = "granted";
    render(<NotificationsPrompt api={api()} />);
    await act(async () => void (await syncPushSubscription(api({ registerWebPush: vi.fn(async () => Promise.reject(new HttpError(400, null))) }))));
    expect(prompt()).not.toBeNull();
    await act(async () => void (await syncPushSubscription(api())));
    expect(prompt()).toBeNull();
  });
});
