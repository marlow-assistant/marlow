// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Pairing } from "./Pairing";

const INVITE = "k3J9xQ2mZpL0vR7tYb5nWc8dHs1fGaU4eXoI6jMqKzE";

afterEach(() => {
  cleanup();
  history.replaceState(null, "", "/");
  vi.unstubAllGlobals();
});

describe("Pairing", () => {
  it("takes the invite from the link, drops it from the address bar, and offers one tap", () => {
    history.replaceState(null, "", `/#pair=${INVITE}`);
    render(<Pairing onPaired={() => {}} />);
    expect(location.hash).toBe("");
    expect(location.href).not.toContain(INVITE);
    expect(screen.queryByLabelText("Pairing link")).toBeNull();
    expect((screen.getByRole("button", { name: "Pair" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("sends the invite and device name, and hands back the token", async () => {
    history.replaceState(null, "", `/#pair=${INVITE}`);
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ device_token: "tok" }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const onPaired = vi.fn();
    render(<Pairing onPaired={onPaired} />);
    fireEvent.click(screen.getByRole("button", { name: "Pair" }));
    await vi.waitFor(() => expect(onPaired).toHaveBeenCalledWith("tok"));
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toContain("/v1/pair");
    expect(JSON.parse(init.body as string)).toMatchObject({ invite: INVITE });
  });

  it("without a link, asks for one to be pasted and stays disabled until it parses", () => {
    render(<Pairing onPaired={() => {}} />);
    const button = screen.getByRole("button", { name: "Pair" }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Pairing link"), { target: { value: `https://box.example/#pair=${INVITE}` } });
    expect(button.disabled).toBe(false);
  });

  it("takes an invite that arrives by a fragment change, with no reload", () => {
    render(<Pairing onPaired={() => {}} />);
    const button = () => screen.getByRole("button", { name: "Pair" }) as HTMLButtonElement;
    expect(button().disabled).toBe(true);
    act(() => {
      history.replaceState(null, "", `/#pair=${INVITE}`);
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    expect(button().disabled).toBe(false);
    expect(location.href).not.toContain(INVITE);
  });

  it("says the same thing for every refusal", async () => {
    history.replaceState(null, "", `/#pair=${INVITE}`);
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "pairing failed" }), { status: 400 })));
    render(<Pairing onPaired={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "Pair" }));
    expect((await screen.findByRole("alert")).textContent).toContain("expired or was already used");
  });
});
