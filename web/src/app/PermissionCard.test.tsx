import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PermissionRequestInfo, PermissionSummary } from "../lib/types";
import { feedbackApproval } from "./feedback";
import { ARM_DELAY_MS, PermissionCard } from "./PermissionCard";

vi.mock("./feedback", () => ({ feedbackApproval: vi.fn() }));

beforeEach(() => vi.useFakeTimers({ now: Date.UTC(2026, 9, 5, 12, 0, 0) }));
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const nowS = () => Date.now() / 1000;

function request(summary: PermissionSummary | null = null, over: Partial<PermissionRequestInfo> = {}): PermissionRequestInfo {
  return {
    request_id: "r1",
    tool: "Bash",
    tool_input: { description: "Find the notes", command: "grep -rn Percé notes/" },
    created_at: nowS(),
    expires_at: nowS() + 55,
    summary,
    ...over,
  };
}

const summary: PermissionSummary = {
  title: "Search your notes",
  preview: [
    { op: "remove", text: "old line" },
    { op: "add", text: "new line" },
  ],
  marlowSays: "Looking for the trip notes",
  similar: { label: "other grep searches" },
};

function renderCard(r: PermissionRequestInfo) {
  const onAnswer = vi.fn((_d: string, _id: string) => Promise.resolve());
  const onExpire = vi.fn();
  render(<PermissionCard request={r} onAnswer={onAnswer} onExpire={onExpire} />);
  return { onAnswer, onExpire };
}

const button = (name: string) => screen.getByRole("button", { name }) as HTMLButtonElement;

describe("the approval card", () => {
  it("leads with the server's title, quotes Marlow, and shows the preview as a diff", () => {
    renderCard(request(summary));
    expect(screen.getByRole("heading").textContent).toBe("Marlow wants toSearch your notes");
    expect(screen.getByLabelText("Marlow says: Looking for the trip notes")).toBeTruthy();
    const lines = document.querySelectorAll(".perm-line");
    expect([...lines].map((l) => l.className)).toEqual(["perm-line perm-line-remove", "perm-line perm-line-add"]);
    expect(lines[0].textContent).toBe("−Removed: old line");
  });

  it("falls back to the tool name without a summary, and offers no Allow similar", () => {
    renderCard(request(null));
    expect(screen.getByRole("heading").textContent).toBe("Marlow wants to useBash");
    expect(screen.queryByRole("button", { name: "Allow similar for this reply" })).toBeNull();
    expect(document.querySelector(".perm-says")).toBeNull();
  });

  it("always carries the raw input, sorted, behind Show details", () => {
    renderCard(request(summary));
    const raw = screen.getByTestId("perm-input");
    expect(raw.textContent).toBe("command: grep -rn Percé notes/\ndescription: Find the notes");
    expect(raw.className).toBe("perm-input");
    fireEvent.click(button("Show details"));
    expect(raw.className).toBe("perm-input open");
    expect(button("Hide details").getAttribute("aria-expanded")).toBe("true");
  });

  it("arms after a beat, then answers with the request id", async () => {
    const { onAnswer } = renderCard(request(summary));
    expect(button("Allow").disabled).toBe(true);
    act(() => vi.advanceTimersByTime(ARM_DELAY_MS));
    expect(button("Allow").disabled).toBe(false);
    fireEvent.click(button("Allow similar for this reply"));
    expect(onAnswer).toHaveBeenCalledWith("allow_similar", "r1");
    expect(screen.getByText("Covers other grep searches")).toBeTruthy();
  });

  it("keeps every button off while an answer is in flight", async () => {
    let finish!: () => void;
    const onAnswer = vi.fn(() => new Promise<void>((r) => (finish = r)));
    render(<PermissionCard request={request(summary)} onAnswer={onAnswer} onExpire={() => {}} />);
    act(() => vi.advanceTimersByTime(ARM_DELAY_MS));
    fireEvent.click(button("Deny"));
    expect(onAnswer).toHaveBeenCalledWith("deny", "r1");
    expect(button("Allow").disabled).toBe(true);
    fireEvent.click(button("Allow"));
    expect(onAnswer).toHaveBeenCalledTimes(1);
    await act(async () => finish());
    expect(button("Allow").disabled).toBe(false);
  });

  it("drains a bar with no number, orange for the last ten seconds, and expires at zero", () => {
    const { onExpire } = renderCard(request(summary));
    const bar = screen.getByTestId("time-bar") as HTMLProgressElement;
    expect(bar.value).toBeCloseTo(1, 2);
    expect(bar.getAttribute("aria-label")).toBe("Time left to decide: 55 seconds");
    expect(screen.queryByText(/s left/)).toBeNull();
    act(() => vi.advanceTimersByTime(45_500));
    expect(bar.className).toBe("perm-time urgent");
    expect(onExpire).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(10_000));
    expect(onExpire).toHaveBeenCalledWith("r1");
    expect(button("Allow").disabled).toBe(true);
  });

  it("is not dismissed by Escape", () => {
    renderCard(request(summary));
    const dialog = screen.getByRole("alertdialog");
    const e = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
    dialog.dispatchEvent(e);
    expect(e.defaultPrevented).toBe(true);
  });
});

describe("the approval sound", () => {
  it("plays once when a card appears, and again only for a new request", () => {
    vi.mocked(feedbackApproval).mockClear();
    const props = { onAnswer: () => Promise.resolve(), onExpire: () => {} };
    const { rerender } = render(<PermissionCard key="r1" request={request()} {...props} />);
    expect(feedbackApproval).toHaveBeenCalledTimes(1);
    rerender(<PermissionCard key="r1" request={request()} {...props} />);
    expect(feedbackApproval).toHaveBeenCalledTimes(1);
    rerender(<PermissionCard key="r2" request={request(null, { request_id: "r2" })} {...props} />);
    expect(feedbackApproval).toHaveBeenCalledTimes(2);
  });
});
