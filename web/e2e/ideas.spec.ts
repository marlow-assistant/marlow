// The ideas panel (docs/tasks/ideas-panel.md, "v1") on the real client in a real
// browser; runs on desktop and Pixel 7. /v1/ideas is stubbed with page.route, so
// this tests the client against the contract, not the server half.
import { expect, rectOf, test } from "./fixtures";
import type { Page } from "./fixtures";

const IDEAS = [
  {
    id: "montreal-flight",
    section: "now",
    emoji: "✈️",
    title: "Rebook the Montreal flight",
    plan: "I would compare the two fares your email mentions and draft the change for your approval.",
    category: "Travel",
    source: "Email from the airline",
    until: null,
    added: "2026-10-12",
    spotlight: true,
  },
  {
    id: "tire-pressure",
    section: "standing",
    emoji: "🛞",
    title: "Remind me about tire pressure",
    plan: "I would check the van's tire pressure notes and set a monthly reminder.",
    category: "Van",
    source: null,
    until: null,
    added: "2026-10-01",
    spotlight: false,
  },
];

async function stubIdeas(page: Page, dismissals: unknown[] = []) {
  await page.route("**/v1/ideas", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ideas: IDEAS }) }),
  );
  await page.route("**/v1/ideas/*/dismiss", async (route) => {
    dismissals.push({ url: route.request().url(), body: route.request().postDataJSON() });
    await route.fulfill({ status: 200, contentType: "application/json", body: '{"ok":true}' });
  });
}

test("the lightbulb is left of the avatar and the gear right of it, on one row, with a dot for the spotlight", async ({ newDevice }) => {
  const { page } = await newDevice({ setup: (p) => stubIdeas(p) });
  const bulb = page.getByRole("button", { name: "Ideas" });
  await expect(page.getByTestId("ideas-dot")).toBeVisible();
  const [b, a, g] = await Promise.all([
    rectOf(bulb),
    rectOf(page.getByRole("button", { name: "Recent activity" })),
    rectOf(page.getByRole("button", { name: "Settings" })),
  ]);
  expect(b.right, "bulb clears the avatar").toBeLessThanOrEqual(a.left);
  expect(g.left, "gear is right of the avatar").toBeGreaterThanOrEqual(a.right);
  expect(Math.abs((b.top + b.bottom) / 2 - (g.top + g.bottom) / 2), "bulb and gear share a row").toBeLessThanOrEqual(2);
  expect(b.width).toBeCloseTo(44, 0);
  expect(b.height).toBeCloseTo(44, 0);
});

test("the spotlight sits above the composer, outside the transcript; the panel lists both sections", async ({ newDevice }) => {
  const { page } = await newDevice({ setup: (p) => stubIdeas(p) });
  const spotlight = page.getByTestId("spotlight");
  await expect(spotlight).toContainText("Rebook the Montreal flight");
  const [s, ta] = await Promise.all([rectOf(spotlight), rectOf(page.locator("textarea"))]);
  expect(s.bottom, "above the composer").toBeLessThanOrEqual(ta.top);
  await expect(page.getByTestId("transcript").getByText("Rebook the Montreal flight", { exact: true })).toHaveCount(0);

  await spotlight.getByText("Rebook the Montreal flight").click();
  const panel = page.getByRole("dialog", { name: "Ideas" });
  await expect(panel.getByRole("heading", { name: "For you now" })).toBeVisible();
  await expect(panel.getByRole("heading", { name: "Things I can do" })).toBeVisible();
  const card = panel.getByRole("article", { name: "Remind me about tire pressure" });
  await expect(card).toContainText("Van");
  // A phone-width card does not scroll sideways.
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await panel.getByRole("button", { name: "Done" }).click();
  await expect(panel).toHaveCount(0);
});

test("Not now on a panel card dismisses, then takes a reason", async ({ newDevice }) => {
  const dismissals: unknown[] = [];
  const { page } = await newDevice({ setup: (p) => stubIdeas(p, dismissals) });
  await page.getByRole("button", { name: "Ideas" }).click();
  const panel = page.getByRole("dialog", { name: "Ideas" });
  await panel.getByRole("article", { name: "Remind me about tire pressure" }).getByRole("button", { name: "Not now" }).click();
  await expect.poll(() => dismissals.length).toBe(1);
  await expect(panel.getByText("Dismissed — add a reason")).toBeVisible();
  await panel.getByLabel("Reason").fill("done already");
  await panel.getByRole("button", { name: "Send" }).click();
  await expect.poll(() => dismissals.length).toBe(2);
  expect(dismissals[0]).toMatchObject({ body: { reason: null } });
  expect(dismissals[1]).toMatchObject({ body: { reason: "done already" } });
});

test("Do it from the spotlight sends 'Do it' with idea_id and shows a bubble quoting the idea", async ({ newDevice }) => {
  const { page } = await newDevice({ setup: (p) => stubIdeas(p) });
  const sent = page.waitForRequest((r) => r.url().endsWith("/v1/message") && r.method() === "POST");
  await page.getByTestId("spotlight").getByRole("button", { name: "Do it" }).click();
  const body = (await sent).postDataJSON();
  expect(body).toMatchObject({ text: "Do it", idea_id: "montreal-flight" });
  // The bubble quotes the idea at once (from the card), with "Do it" under it.
  const bubble = page.locator(".row[data-role=user]").filter({ hasText: "Rebook the Montreal flight" }).last();
  await expect(bubble.getByTestId("user-quote")).toContainText("Rebook the Montreal flight");
  await expect(bubble.locator(".user-text")).toHaveText("Do it");
  await expect(page.getByTestId("spotlight")).toHaveCount(0);
});
