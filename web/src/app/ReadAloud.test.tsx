// "Read aloud" in a reply's long-press menu, and the pill that stops it
// (docs/tasks/read-aloud.md).
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { Copyable } from "./CopyMenu";
import { ReadingPill } from "./ReadAloud";

afterEach(cleanup);

describe("Read aloud in the long-press menu", () => {
  it("offers Read aloud under Copy and starts reading", () => {
    const onRead = vi.fn();
    render(
      <Copyable text="Hello." className="bubble" readAloud={{ reading: false, onRead, onStop: vi.fn() }}>
        <span>Hello.</span>
      </Copyable>,
    );
    fireEvent.contextMenu(screen.getByText("Hello."), { clientX: 100, clientY: 300 });
    const items = screen.getAllByRole("menuitem").map((b) => b.textContent);
    expect(items).toEqual(["Copy", "Read aloud"]);
    fireEvent.click(screen.getByRole("menuitem", { name: "Read aloud" }));
    expect(onRead).toHaveBeenCalledOnce();
    expect(screen.queryByTestId("copy-menu")).toBeNull();
  });

  it("says Stop reading on the message being read", () => {
    const onStop = vi.fn();
    render(
      <Copyable text="Hello." className="bubble" readAloud={{ reading: true, onRead: vi.fn(), onStop }}>
        <span>Hello.</span>
      </Copyable>,
    );
    fireEvent.contextMenu(screen.getByText("Hello."), { clientX: 100, clientY: 300 });
    fireEvent.click(screen.getByRole("menuitem", { name: "Stop reading" }));
    expect(onStop).toHaveBeenCalledOnce();
  });

  it("an error caption has Copy alone", () => {
    render(
      <Copyable text="Failed." className="caption">
        <span>Failed.</span>
      </Copyable>,
    );
    fireEvent.contextMenu(screen.getByText("Failed."), { clientX: 100, clientY: 300 });
    expect(screen.getAllByRole("menuitem").map((b) => b.textContent)).toEqual(["Copy"]);
  });
});

describe("The stop pill", () => {
  it("stops the read on a tap", () => {
    const onStop = vi.fn();
    render(<ReadingPill onStop={onStop} />);
    fireEvent.click(screen.getByRole("button", { name: "Stop reading" }));
    expect(onStop).toHaveBeenCalledOnce();
  });
});
