// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Pairing } from "./Pairing";

const decode = vi.hoisted(() => vi.fn());
vi.mock("jsqr", () => ({ default: decode }));

const INVITE = "k3J9xQ2mZpL0vR7tYb5nWc8dHs1fGaU4eXoI6jMqKzE";

afterEach(() => {
  cleanup();
  history.replaceState(null, "", "/");
  vi.unstubAllGlobals();
  decode.mockReset();
  Reflect.deleteProperty(navigator, "mediaDevices");
  vi.restoreAllMocks();
});

function setMediaDevices(value: unknown) {
  Object.defineProperty(navigator, "mediaDevices", { configurable: true, value });
}

/** A camera that opens, with a canvas that hands jsQR something to read. */
function stubCamera() {
  const stop = vi.fn();
  const getUserMedia = vi.fn(async () => ({ getTracks: () => [{ stop }] }) as unknown as MediaStream);
  setMediaDevices({ getUserMedia });
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
  Object.defineProperty(HTMLVideoElement.prototype, "videoWidth", { configurable: true, value: 4 });
  Object.defineProperty(HTMLVideoElement.prototype, "videoHeight", { configurable: true, value: 4 });
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
    drawImage: () => {},
    getImageData: () => ({ data: new Uint8ClampedArray(64), width: 4, height: 4 }),
  } as unknown as CanvasRenderingContext2D);
  return { getUserMedia, stop };
}

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

  it("scans the code with the camera, takes the invite from it, and lets go of the camera", async () => {
    const { getUserMedia, stop } = stubCamera();
    decode.mockReturnValue({ data: `https://box.example/#pair=${INVITE}` });
    render(<Pairing onPaired={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "Scan QR code" }));
    await vi.waitFor(() => expect((screen.getByRole("button", { name: "Pair" }) as HTMLButtonElement).disabled).toBe(false));
    expect(getUserMedia).toHaveBeenCalledWith({ video: { facingMode: "environment" } });
    expect(stop).toHaveBeenCalled();
    expect(screen.queryByLabelText("Pairing link")).toBeNull();
  });

  it("keeps looking past a code that is not a pairing link", async () => {
    stubCamera();
    decode.mockReturnValueOnce({ data: "https://example.com/menu" }).mockReturnValue({ data: INVITE });
    render(<Pairing onPaired={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "Scan QR code" }));
    await vi.waitFor(() => expect((screen.getByRole("button", { name: "Pair" }) as HTMLButtonElement).disabled).toBe(false));
  });

  it("falls back to pasting when the camera is refused, and says so", async () => {
    stubCamera();
    (navigator.mediaDevices.getUserMedia as ReturnType<typeof vi.fn>).mockRejectedValue(new DOMException("no", "NotAllowedError"));
    render(<Pairing onPaired={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "Scan QR code" }));
    expect((await screen.findByRole("alert")).textContent).toContain("camera");
    expect(screen.getByLabelText("Pairing link")).toBeTruthy();
  });

  it("offers no scan button on a device with no camera API", () => {
    setMediaDevices(undefined);
    render(<Pairing onPaired={() => {}} />);
    expect(screen.queryByRole("button", { name: "Scan QR code" })).toBeNull();
  });
});
