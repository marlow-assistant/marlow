// The web-only Text size section in Settings (lib/textSize.ts).
import "fake-indexeddb/auto";
import { IDBFactory } from "fake-indexeddb";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { Api } from "../lib/api";
import { DEFAULT_FACE } from "../lib/faces";
import { kvGet, KV_TEXT_SIZE, resetDbForTests } from "../lib/storage";
import { applyTextSize, DEFAULT_TEXT_SIZE } from "../lib/textSize";
import { Settings } from "./Settings";

beforeEach(() => {
  globalThis.indexedDB = new IDBFactory();
  resetDbForTests();
});

afterEach(() => {
  cleanup();
  applyTextSize(DEFAULT_TEXT_SIZE);
});

function open() {
  return render(
    <Settings
      api={{} as Api}
      token="tok-abcdef"
      reauthNeeded={false}
      queuePaused={false}
      effort="low"
      serverModel={null}
      serverBuild={null}
      onEffort={vi.fn()}
      locationSharing="off"
      locationStatus="ok"
      onLocationSharing={vi.fn()}
      face={DEFAULT_FACE}
      onFace={vi.fn()}
      onUnpair={vi.fn()}
      onClose={vi.fn()}
      host="marlow.test"
      webBuild="deadbee"
    />,
  );
}

const group = () => screen.getByRole("radiogroup", { name: "Text size" });

describe("Settings › Text size", () => {
  it("starts on Default", () => {
    open();
    expect(within(group()).getByRole("radio", { name: "Default" }).getAttribute("aria-checked")).toBe("true");
    expect(document.documentElement.style.fontSize).toBe("");
  });

  it("a choice scales the page at once and is kept on the device", async () => {
    open();
    fireEvent.click(within(group()).getByRole("radio", { name: "Larger" }));
    expect(document.documentElement.style.fontSize).toBe("130%");
    expect(within(group()).getByRole("radio", { name: "Larger" }).getAttribute("aria-checked")).toBe("true");
    await waitFor(async () => expect(await kvGet(KV_TEXT_SIZE)).toBe("larger"));
  });

  it("reopening Settings shows the size in force", () => {
    open();
    fireEvent.click(within(group()).getByRole("radio", { name: "Large" }));
    cleanup();
    open();
    expect(within(group()).getByRole("radio", { name: "Large" }).getAttribute("aria-checked")).toBe("true");
  });
});
