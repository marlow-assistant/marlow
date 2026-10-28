// RND-4: whole-message Copy through press-and-hold (a `contextmenu` event,
// which is how Android Chrome and Firefox surface a long press), not a link
// beside every reply.
import { exchange, expect, send, test, uniq } from "./fixtures";

test("RND-4: holding a reply opens a Copy menu that copies the whole message; nothing else on the row offers it", async ({
  page,
  isMobile,
}) => {
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  const { reply } = await exchange(page);
  const bubble = reply.locator(".bubble");
  await expect(reply.getByRole("button", { name: "Copy" })).toHaveCount(0);

  await bubble.click({ button: "right" });
  const menu = page.getByTestId("copy-menu");
  await expect(menu).toBeVisible();
  const box = (await menu.boundingBox())!;
  const vp = page.viewportSize()!;
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width).toBeLessThanOrEqual(vp.width);
  expect(box.y).toBeGreaterThanOrEqual(0);

  await menu.getByRole("menuitem", { name: "Copy" }).click();
  await expect(menu.getByRole("menuitem")).toHaveText("Copied");
  await expect(menu).toHaveCount(0, { timeout: 3_000 });
  const copied = await page.evaluate(() => navigator.clipboard.readText());
  expect(copied).toBe((await bubble.innerText()).trim());

  // A tap anywhere else dismisses it without copying.
  await bubble.click({ button: "right" });
  await expect(menu).toBeVisible();
  await page.locator("textarea").click();
  await expect(menu).toHaveCount(0);

  // On a touch screen the hold must not also start a text selection.
  const select = await bubble.evaluate((el) => getComputedStyle(el).userSelect);
  expect(select).toBe(isMobile ? "none" : "auto");
});

test("RND-4: a failed turn's reason is copyable the same way", async ({ page }) => {
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  const token = uniq("fail");
  const user = await send(page, `#fail ${token}`, token);
  const caption = user.locator(".failed-caption");
  await expect(caption).toBeVisible({ timeout: 30_000 });
  await caption.click({ button: "right" });
  await page.getByTestId("copy-menu").getByRole("menuitem", { name: "Copy" }).click();
  const copied = await page.evaluate(() => navigator.clipboard.readText());
  expect(copied).toBe((await caption.locator(".error-text").innerText()).trim());
});
