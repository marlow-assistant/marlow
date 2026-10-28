// The chat's chrome matches the iOS app (PresenceHeader, Settings, the Marlow
// sheet). Geometry, not screenshots; runs on desktop and Pixel 7.
import { composerInput, expect, frames, pairingInvite, rectOf, send, test, waitDone, replyOf, uniq } from "./fixtures";

test("the header: avatar centred, lightbulb left, gear right on one row, both 44x44", async ({ page }) => {
  const header = page.locator("header.presence");
  const avatar = page.getByRole("button", { name: "Recent activity" });
  const bulb = page.getByRole("button", { name: "Ideas" });
  const gear = page.getByRole("button", { name: "Settings" });
  await expect(bulb).toBeVisible();
  const [h, a, c, g] = await Promise.all([rectOf(header), rectOf(avatar), rectOf(bulb), rectOf(gear)]);

  expect(Math.abs((a.left + a.right) / 2 - (h.left + h.right) / 2), "avatar centred in the header").toBeLessThanOrEqual(2);
  expect(a.width).toBeCloseTo(52, 0);
  expect(c.right, "lightbulb is left of the avatar").toBeLessThanOrEqual(a.left);
  expect(g.left, "gear is right of the avatar").toBeGreaterThanOrEqual(a.right);
  expect(Math.abs((c.top + c.bottom) / 2 - (g.top + g.bottom) / 2), "lightbulb and gear share a row").toBeLessThanOrEqual(2);
  expect(g.width).toBeCloseTo(44, 0);
  expect(g.height).toBeCloseTo(44, 0);
  expect(c.width).toBeCloseTo(44, 0);
  expect(c.height).toBeCloseTo(44, 0);
  expect(Math.abs((c.left - h.left) - (h.right - g.right)), "bulb and gear sit the same distance from their edges").toBeLessThanOrEqual(2);

  // Not floating: the transcript starts under the header, and nothing scrolls it away.
  const t = await rectOf(page.getByTestId("transcript"));
  expect(t.top).toBeGreaterThanOrEqual(h.bottom - 1);
  // Compact: well under a third of a phone screen.
  expect(h.height).toBeLessThan(130);

  await expect(page.getByTestId("presence")).toHaveText("Marlow");
  await expect(page.getByRole("button", { name: "Menu" })).toHaveCount(0);
  await expect(page.locator("select")).toHaveCount(0);
  // The model chip was removed (2026-10-12); model and effort are in Settings.
  await expect(page.getByTestId("model-chip")).toHaveCount(0);
});

test("the avatar opens the Marlow sheet with five panels; Done closes it", async ({ page }) => {
  await page.getByRole("button", { name: "Recent activity" }).click();
  const sheet = page.getByRole("dialog", { name: "Marlow" });
  await expect(sheet).toBeVisible();
  for (const name of ["Activity", "Schedule", "Memory", "You", "Soul"]) {
    await expect(sheet.getByRole("button", { name, exact: true })).toBeVisible();
  }
  await expect(sheet.getByRole("button", { name: "Activity", exact: true })).toHaveAttribute("aria-pressed", "true");
  await sheet.getByRole("button", { name: "Memory", exact: true }).click();
  await expect(sheet.getByRole("button", { name: "Memory", exact: true })).toHaveAttribute("aria-pressed", "true");
  await sheet.getByRole("button", { name: "Done" }).click();
  await expect(sheet).toHaveCount(0);
});

test("the gear opens Settings; the effort control is bound and persists", async ({ page }) => {
  await page.getByRole("button", { name: "Settings" }).click();
  const settings = page.getByRole("dialog", { name: "Settings" });
  await expect(settings).toBeVisible();
  await expect(settings.getByRole("region", { name: "Status" })).toContainText(new URL(page.url()).host);
  await expect(settings.getByRole("region", { name: "Build" })).toContainText("App");
  await settings.getByRole("radio", { name: "High" }).click();
  await expect(settings.getByRole("radio", { name: "High" })).toHaveAttribute("aria-checked", "true");
  await settings.getByRole("button", { name: "Done" }).click();

  // Still High on reopening; then put it back so later tests run at the default.
  await page.getByRole("button", { name: "Settings" }).click();
  await expect(page.getByRole("radio", { name: "High" })).toHaveAttribute("aria-checked", "true");
  await page.getByRole("radio", { name: "Low" }).click();
  await expect(page.getByRole("radio", { name: "Low" })).toHaveAttribute("aria-checked", "true");
  await page.getByRole("button", { name: "Done" }).click();
});

test("the ring is present only while a turn is in flight", async ({ page }) => {
  const ring = page.getByTestId("avatar-ring");
  await expect(ring).toHaveCount(0);
  const token = uniq("ring");
  const user = await send(page, `#tool ${token}`, token);
  await expect(ring).toHaveCount(1);
  // The ring clears the avatar: it extends past the 52px face on every side.
  const [r, a] = await Promise.all([rectOf(ring), rectOf(page.getByRole("button", { name: "Recent activity" }))]);
  expect(r.width).toBeGreaterThan(a.width);
  await waitDone(replyOf(user));
  await expect(ring).toHaveCount(0);
  await frames(page);
  await expect(composerInput(page)).toBeVisible();
});

// FaceImage zooms its portrait with a transform and relies on its parent to
// clip it round. The What's new sheet once had no such parent, so the face drew
// over "Marlow was updated" (Android emulator, 2026-10-24).
test("the What's new sheet: the face is clipped to a 44px circle beside the title", async ({ newDevice }) => {
  const { page } = await newDevice({ pair: false });
  const invite = await pairingInvite();
  await page.goto(`/#pair=${invite}`);
  await page.getByRole("button", { name: "Pair", exact: true }).click();
  const card = page.getByRole("dialog", { name: "Marlow was updated" });
  await expect(card).toBeVisible();

  const clip = await card.locator(".whats-new-header img").evaluate((img) => {
    const p = img.parentElement!;
    const r = p.getBoundingClientRect();
    return { width: r.width, height: r.height, right: r.right, overflow: getComputedStyle(p).overflow, radius: getComputedStyle(p).borderRadius };
  });
  expect(clip.width).toBeCloseTo(44, 0);
  expect(clip.height).toBeCloseTo(44, 0);
  expect(clip.overflow).toBe("hidden");
  expect(clip.radius).toBe("50%");
  const title = await rectOf(card.getByRole("heading", { name: "Marlow was updated" }));
  expect(title.left, "the title starts after the face").toBeGreaterThanOrEqual(clip.right);
});
