// The chat's chrome (iOS parity): the presence header, Settings, and how Chat
// wires the gear and the avatar to their sheets.
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import type { Api } from "../lib/api";
import type { ChatState, ChatStore } from "../lib/chatStore";
import { DEFAULT_FACE, FACES } from "../lib/faces";
import { LocationWatcher } from "../lib/location";
import type { ChangeEntry } from "../lib/whatsNew";
import { Chat } from "./Chat";
import { PresenceHeader } from "./Header";
import { Settings, type SettingsProps } from "./Settings";

afterEach(cleanup);

const IDLE = { kind: "idle" } as const;

function header(over: Partial<Parameters<typeof PresenceHeader>[0]> = {}) {
  const props = {
    presence: IDLE,
    face: DEFAULT_FACE,
    ideasDot: false,
    onSettings: vi.fn(),
    onAvatar: vi.fn(),
    onIdeas: vi.fn(),
    ...over,
  };
  return { ...render(<PresenceHeader {...props} />), props };
}

describe("presence header", () => {
  it("shows Marlow in the pill when idle, with no ring", () => {
    header();
    expect(screen.getByTestId("presence").textContent).toBe("Marlow");
    expect(screen.queryByTestId("avatar-ring")).toBeNull();
  });

  it("shows the chosen face in its header crop, zoomed toward that face's own anchor", () => {
    const { container } = header({ face: FACES[6] });
    const img = container.querySelector(".avatar-face img") as HTMLImageElement;
    expect(img.getAttribute("src")).toBe("/faces/marlow-7.jpg");
    expect(img.style.transformOrigin).toBe("65% 5%");
    expect(img.style.transform).toBe(`scale(${FACES[6].header.zoom})`);
  });

  it("shows the presence label and the ring when not idle", () => {
    header({ presence: { kind: "active", label: "Searching the web…" } });
    expect(screen.getByTestId("presence").textContent).toBe("Searching the web…");
    expect(screen.getByTestId("avatar-ring")).toBeTruthy();
  });

  it("has no model chip and no menu or select: model and effort live in Settings", () => {
    header();
    expect(screen.queryByTestId("model-chip")).toBeNull();
    expect(document.querySelector("select")).toBeNull();
    expect(screen.queryByRole("button", { name: "Menu" })).toBeNull();
  });

  it("the avatar and the gear are labelled buttons that call back", () => {
    const { props } = header();
    fireEvent.click(screen.getByRole("button", { name: "Recent activity" }));
    expect(props.onAvatar).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    expect(props.onSettings).toHaveBeenCalledTimes(1);
  });

  it("the lightbulb sits before the avatar and the gear, calls back, and shows its dot only when told to", () => {
    const { props, rerender } = header();
    expect(screen.queryByTestId("ideas-dot")).toBeNull();
    const bulb = screen.getByRole("button", { name: "Ideas" });
    const order = [bulb, screen.getByRole("button", { name: "Recent activity" }), screen.getByRole("button", { name: "Settings" })];
    // Document order is the visual order here: bulb, avatar column, gear.
    expect(order[0].compareDocumentPosition(order[2]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    fireEvent.click(bulb);
    expect(props.onIdeas).toHaveBeenCalledTimes(1);
    rerender(<PresenceHeader {...props} ideasDot />);
    expect(screen.getByTestId("ideas-dot")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Ideas" })).toBeTruthy();
  });
});

function stubStore(over: Partial<ChatState> = {}) {
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
    ...over,
  };
  return {
    subscribe: () => () => {},
    getState: () => state,
    getStream: () => undefined,
    subscribeStream: () => () => {},
    onFeedback: () => () => {},
    setEffort: vi.fn(),
    send: vi.fn(),
  } as unknown as ChatStore & { setEffort: ReturnType<typeof vi.fn> };
}

/** No update card in these tests (WhatsNew.test.tsx covers it). */
const NO_CHANGES: ChangeEntry[] = [];

const api = {
  activity: vi.fn().mockResolvedValue({ entries: [], next_before: null }),
  identity: vi.fn().mockResolvedValue({ files: [], schedules: {} }),
  identityFile: vi.fn(),
  webPushKey: vi.fn(),
  registerWebPush: vi.fn(),
} as unknown as Api;

describe("Chat chrome wiring", () => {
  it("the gear opens Settings, the avatar opens the Marlow sheet, one at a time", async () => {
    const location = new LocationWatcher({ sharing: "off", geolocation: undefined, isVisible: () => true });
    render(<Chat store={stubStore({ serverModel: "sonnet" })} location={location} api={api} token="abcdef123456" face={DEFAULT_FACE} onFace={() => {}} onEffort={() => {}} onUnpair={() => {}} bundledChanges={NO_CHANGES} />);
    expect(screen.queryByTestId("model-chip")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    const settings = screen.getByRole("dialog", { name: "Settings" });
    expect(within(settings).getByText("123456")).toBeTruthy();
    fireEvent.click(within(settings).getByRole("button", { name: "Done" }));
    expect(screen.queryByRole("dialog")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Recent activity" }));
    const sheet = screen.getByRole("dialog", { name: "Marlow" });
    expect(within(sheet).getAllByRole("button").length).toBe(8); // the face, six panels and Done
    await screen.findByText(/Nothing yet/);
    fireEvent.click(within(sheet).getByRole("button", { name: "Done" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

function settings(over: Partial<SettingsProps> = {}) {
  const props: SettingsProps = {
    api,
    token: "tok-abcdef",
    reauthNeeded: false,
    queuePaused: false,
    effort: "low",
    serverModel: "sonnet",
    serverBuild: "main@abc1234",
    onEffort: vi.fn(),
    locationSharing: "off",
    locationStatus: "ok",
    onLocationSharing: vi.fn(),
    face: DEFAULT_FACE,
    onFace: vi.fn(),
    onUnpair: vi.fn(),
    onClose: vi.fn(),
    host: "marlow.tail1234.ts.net",
    webBuild: "deadbee",
    ...over,
  };
  return { ...render(<Settings {...props} />), props };
}

const rowValue = (section: string, label: string) =>
  within(screen.getByRole("region", { name: section })).getByText(label).nextElementSibling!.textContent;

describe("settings", () => {
  it("Status: server, connection, and the last six characters of the token", () => {
    settings();
    expect(rowValue("Status", "Server")).toBe("marlow.tail1234.ts.net");
    expect(rowValue("Status", "Connection")).toBe("Connected");
    expect(rowValue("Status", "Device")).toBe("abcdef");
    expect(screen.queryByText("tok-abcdef")).toBeNull();
  });

  it("Connection reads Paused, and Needs reauthorization above it", () => {
    const { rerender, props } = settings({ queuePaused: true });
    expect(rowValue("Status", "Connection")).toBe("Paused");
    rerender(<Settings {...props} queuePaused reauthNeeded />);
    expect(rowValue("Status", "Connection")).toBe("Needs reauthorization");
  });

  it("Intelligence: the control reflects the effort and calls onEffort", () => {
    const { props } = settings({ effort: "medium" });
    const group = screen.getByRole("radiogroup", { name: "Thinking" });
    expect(within(group).getAllByRole("radio").map((r) => r.textContent)).toEqual(["Low", "Medium", "High"]);
    expect(within(group).getByRole("radio", { name: "Medium" }).getAttribute("aria-checked")).toBe("true");
    fireEvent.click(within(group).getByRole("radio", { name: "High" }));
    expect(props.onEffort).toHaveBeenCalledWith("high");
    expect(rowValue("Intelligence", "Model")).toBe("Sonnet");
    expect(screen.getByText(/Higher effort makes replies slower, not more expensive/)).toBeTruthy();
  });

  it("Location: the control reflects the setting and calls onLocationSharing", () => {
    const { props } = settings({ locationSharing: "approximate" });
    const group = screen.getByRole("radiogroup", { name: "Location" });
    expect(within(group).getAllByRole("radio").map((r) => r.textContent)).toEqual(["Off", "Approximate", "Precise"]);
    expect(within(group).getByRole("radio", { name: "Approximate" }).getAttribute("aria-checked")).toBe("true");
    expect(within(group).getByRole("radio", { name: "Off" }).getAttribute("aria-checked")).toBe("false");
    fireEvent.click(within(group).getByRole("radio", { name: "Precise" }));
    expect(props.onLocationSharing).toHaveBeenCalledWith("precise");
    expect(screen.getByText(/Approximate shares your area to about a kilometre; Precise shares your exact position/)).toBeTruthy();
  });

  it("Face: every face is offered, the current one is checked, a tap calls onFace", () => {
    const { props } = settings({ face: FACES[3] });
    const group = screen.getByRole("radiogroup", { name: "Appearance" });
    expect(within(group).getAllByRole("radio")).toHaveLength(FACES.length);
    expect(within(group).getByRole("radio", { name: "Face 4, glasses" }).getAttribute("aria-checked")).toBe("true");
    expect(within(group).getByRole("radio", { name: "Face 1, monocle" }).getAttribute("aria-checked")).toBe("false");
    const tile = within(group).getByRole("radio", { name: "Face 4, glasses" }).querySelector("img") as HTMLImageElement;
    expect(tile.style.transform).toBe(`scale(${FACES[3].large.zoom})`);
    fireEvent.click(within(group).getByRole("radio", { name: "Face 9, monocle" }));
    expect(props.onFace).toHaveBeenCalledWith(FACES[8]);
  });

  it("Location footer says so when the browser has blocked it, or has no location at all", () => {
    const { rerender, props } = settings({ locationSharing: "precise", locationStatus: "blocked" });
    expect(screen.getByText("Location is blocked for this site in the browser's settings.")).toBeTruthy();
    expect(screen.queryByText(/Sent with each message/)).toBeNull();
    rerender(<Settings {...props} locationStatus="unavailable" />);
    expect(screen.getByText("Location isn't available in this browser.")).toBeTruthy();
  });

  it("Model and the server build read as a dash before the first /health", () => {
    settings({ serverModel: null, serverBuild: null });
    expect(rowValue("Intelligence", "Model")).toBe("—");
    expect(rowValue("Build", "Server")).toBe("—");
    expect(rowValue("Build", "App")).toBe("deadbee");
  });

  it("Unpair needs a confirmation, and Cancel does nothing", () => {
    const { props } = settings();
    fireEvent.click(screen.getByRole("button", { name: "Unpair" }));
    expect(props.onUnpair).not.toHaveBeenCalled();
    const dialog = screen.getByRole("alertdialog", { name: "Unpair from Marlow?" });
    expect(dialog.textContent).toContain("Reconnecting needs a new pairing link");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(props.onUnpair).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Unpair" }));
    fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Unpair" }));
    expect(props.onUnpair).toHaveBeenCalledTimes(1);
  });
});
