// WEB-9: a freshly paired browser asks for notifications in the chat itself.
import { expect, test } from "./fixtures";

test("WEB-9: after pairing, the chat asks for notifications between the header and the transcript; Not now hides it", async ({
  page,
}) => {
  const card = page.getByTestId("notify-prompt");
  await expect(card).toBeVisible();
  await expect(card.getByRole("button", { name: "Turn on" })).toBeVisible();
  const header = (await page.locator("header").first().boundingBox())!;
  const c = (await card.boundingBox())!;
  const t = (await page.getByTestId("transcript").boundingBox())!;
  expect(c.y).toBeGreaterThanOrEqual(header.y + header.height - 1);
  expect(t.y).toBeGreaterThanOrEqual(c.y + c.height - 1);
  expect(c.x + c.width).toBeLessThanOrEqual(page.viewportSize()!.width + 1);
  await card.getByRole("button", { name: "Not now" }).click();
  await expect(card).toHaveCount(0);
});
