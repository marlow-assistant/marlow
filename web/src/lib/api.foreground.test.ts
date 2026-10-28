// X-Marlow-Foreground: how the box knows this page is on screen, so it can
// hold back a web push iOS Safari would flash over it
// (docs/tasks/web-push-on-screen.md).
import { describe, expect, it } from "vitest";
import { Api } from "./api";

function capture(onScreen: boolean) {
  const sent: Record<string, string>[] = [];
  const api = new Api({
    getToken: () => "tok",
    isOnScreen: () => onScreen,
    fetch: async (_input, init) => {
      sent.push({ ...(init?.headers as Record<string, string>) });
      return new Response(JSON.stringify({}), { status: 200, headers: { "Content-Type": "application/json" } });
    },
  });
  return { api, sent };
}

describe("Api › on-screen marker", () => {
  it("marks a request made while the page is on screen", async () => {
    const { api, sent } = capture(true);
    await api.health().catch(() => {});
    expect(sent[0]["X-Marlow-Foreground"]).toBe("1");
  });

  it("leaves it off while the page is hidden, as a background tab still polls", async () => {
    const { api, sent } = capture(false);
    await api.health().catch(() => {});
    expect(sent).toHaveLength(1);
    expect(sent[0]["X-Marlow-Foreground"]).toBeUndefined();
  });
});
