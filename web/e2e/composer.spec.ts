// Section 10 (composer and images) and section 5 (sending): CMP-1, CMP-2,
// CMP-5, CMP-8, SND-3, SND-4, SND-8.
import {
  composerInput,
  expect,
  pngBuffer,
  rectOf,
  recordMessages,
  replyOf,
  send,
  test,
  uniq,
  waitDone,
} from "./fixtures";

const png = () => ({ name: "photo.png", mimeType: "image/png", buffer: pngBuffer(64, 48) });

test("CMP-2: the send button shows only for non-blank text or an attached image", async ({ page }) => {
  const sendBtn = page.locator("button.send");
  await expect(sendBtn).toHaveCount(0);
  await composerInput(page).fill("   \n  ");
  await expect(sendBtn).toHaveCount(0);
  await composerInput(page).fill("hello");
  await expect(sendBtn).toBeVisible();
  await composerInput(page).fill("");
  await expect(sendBtn).toHaveCount(0);

  // An image alone is sendable.
  await page.getByTestId("file-input").setInputFiles(png());
  await expect(page.getByTestId("preview")).toBeVisible();
  await expect(sendBtn).toBeVisible();
});

test("CMP-5: the preview is 64x64 with a remove control", async ({ page }) => {
  await page.getByTestId("file-input").setInputFiles(png());
  const preview = page.getByTestId("preview");
  await expect(preview).toBeVisible();
  const r = await rectOf(preview);
  expect(r.width).toBeCloseTo(64, 0);
  expect(r.height).toBeCloseTo(64, 0);
  const img = await rectOf(preview.locator("img"));
  expect(img.width).toBeCloseTo(64, 0);
  expect(img.height).toBeCloseTo(64, 0);
  const remove = preview.getByRole("button", { name: "Remove photo" });
  await expect(remove).toBeVisible();
  const rr = await rectOf(remove);
  // It straddles the thumbnail's top-right corner and is big enough to hit.
  expect(Math.abs(rr.right - r.right)).toBeLessThanOrEqual(14);
  expect(Math.abs(rr.top - r.top)).toBeLessThanOrEqual(14);
  expect(rr.width).toBeGreaterThanOrEqual(26);
  await remove.click();
  await expect(preview).toHaveCount(0);
  await expect(page.locator("button.send")).toHaveCount(0);
});

test("CMP-5: several photos stage as 64x64 thumbnails three across, each removable, capped at 6", async ({
  page,
}) => {
  const files = (n: number) => Array.from({ length: n }, (_, i) => ({ ...png(), name: `p${i}.png` }));
  await page.getByTestId("file-input").setInputFiles(files(3));
  const previews = page.getByTestId("preview");
  await expect(previews).toHaveCount(3);
  const rects = await Promise.all([0, 1, 2].map((i) => rectOf(previews.nth(i))));
  for (const r of rects) {
    expect(r.width).toBeCloseTo(64, 0);
    expect(r.height).toBeCloseTo(64, 0);
  }
  // Three across: same top, left to right.
  expect(rects[1].top).toBeCloseTo(rects[0].top, 0);
  expect(rects[1].left).toBeGreaterThan(rects[0].right - 1);
  expect(rects[2].top).toBeCloseTo(rects[0].top, 0);

  // Removing the middle one leaves the other two.
  await previews.nth(1).getByRole("button", { name: "Remove photo" }).click();
  await expect(previews).toHaveCount(2);

  // Picking more than fit keeps the first that fit and says so.
  await page.getByTestId("file-input").setInputFiles(files(7));
  await expect(previews).toHaveCount(6);
  await expect(page.locator(".notice")).toContainText("6");
  // The fourth starts a second row, under the first.
  const [first, fourth] = await Promise.all([rectOf(previews.nth(0)), rectOf(previews.nth(3))]);
  expect(fourth.left).toBeCloseTo(first.left, 0);
  expect(fourth.top).toBeGreaterThan(first.bottom - 1);
});

test("CMP-3: several photos go in one request as images[], and the bubble shows them as a grid", async ({
  page,
}) => {
  const bodies = recordMessages(page);
  const token = uniq("multi");
  await page.getByTestId("file-input").setInputFiles([png(), png(), png(), png()]);
  await expect(page.getByTestId("preview")).toHaveCount(4);
  await composerInput(page).fill(token);
  await page.locator("button.send").click();
  await expect(page.getByTestId("preview")).toHaveCount(0);

  const user = page.locator(".row[data-role=user]").filter({ hasText: token }).last();
  const grid = user.getByTestId("bubble-grid");
  await expect(grid.locator("img")).toHaveCount(4);
  const first = await rectOf(grid.locator("img").nth(0));
  expect(first.width).toBeCloseTo(first.height, 0); // square-cropped tiles
  const mine = bodies.filter((b) => b.text === token);
  expect(mine).toHaveLength(1);
  expect(mine[0].images).toHaveLength(4);
  expect(mine[0]).not.toHaveProperty("image_b64");
  await waitDone(replyOf(user));
});

test("CMP-1: send captures text and image together; both are cleared; one request carries both", async ({
  page,
}) => {
  const bodies = recordMessages(page);
  const token = uniq("cap");
  await page.getByTestId("file-input").setInputFiles(png());
  await expect(page.getByTestId("preview")).toBeVisible();
  await composerInput(page).fill(`  ${token}  `);
  await page.locator("button.send").click();
  // Cleared in one step: field and preview are both empty at once.
  await expect(composerInput(page)).toHaveValue("");
  await expect(page.getByTestId("preview")).toHaveCount(0);
  await expect(page.locator("button.send")).toHaveCount(0);

  const user = page.locator(".row[data-role=user]").filter({ hasText: token }).last();
  await waitDone(replyOf(user));
  await expect(user.locator("img.bubble-image")).toBeVisible();
  const mine = bodies.filter((b) => typeof b.text === "string" && (b.text as string).includes(token));
  expect(mine, "exactly one POST /v1/message").toHaveLength(1);
  expect(mine[0].text, "text is trimmed before sending").toBe(token);
  const images = mine[0].images as { b64: string; mime: string }[];
  expect(images).toHaveLength(1);
  expect(images[0].mime).toBe("image/jpeg");
  expect(images[0].b64.startsWith("/9j/"), "a JPEG").toBe(true); // JPEG magic, base64
  // The next message does not inherit the old draft or photo.
  const next = uniq("next");
  await send(page, next);
  await expect.poll(() => bodies.find((b) => b.text === next) !== undefined).toBe(true);
  expect(bodies.find((b) => b.text === next)).not.toHaveProperty("images");
  await waitDone(replyOf(page.locator(".row[data-role=user]").filter({ hasText: next }).last()));
});

test("CMP-8: the input grows to five lines, then scrolls", async ({ page }) => {
  const ta = composerInput(page);
  const measure = async (lines: number) => {
    await ta.fill(Array.from({ length: lines }, (_, i) => `line ${i + 1}`).join("\n"));
    return ta.evaluate((el) => ({
      h: el.getBoundingClientRect().height,
      scrolls: el.scrollHeight > el.clientHeight + 1,
      overflowY: getComputedStyle(el).overflowY,
    }));
  };
  const m1 = await measure(1);
  const m2 = await measure(2);
  const m5 = await measure(5);
  const m6 = await measure(6);
  const m9 = await measure(9);
  expect(m2.h).toBeGreaterThan(m1.h + 15);
  expect(m5.h).toBeGreaterThan(m2.h + 40);
  expect(m5.scrolls, "five lines fit without scrolling").toBe(false);
  expect(Math.abs(m6.h - m5.h), "height stops growing at the sixth line").toBeLessThanOrEqual(1);
  expect(Math.abs(m9.h - m5.h)).toBeLessThanOrEqual(1);
  expect(m6.scrolls && m9.scrolls, "beyond five lines it scrolls").toBe(true);
  expect(m9.overflowY).toBe("auto");
  // Clearing shrinks it back.
  const m0 = await measure(1);
  expect(Math.abs(m0.h - m1.h)).toBeLessThanOrEqual(1);
});

test.describe("sending under fault injection", () => {
  test("SND-3: two aborted attempts then success is one bubble and one reply, same client_msg_id", async ({
    newDevice,
  }) => {
    // A controllable clock compresses the 4s and 8s backoff sleeps; time still passes naturally.
    const { page } = await newDevice({ setup: (p) => p.clock.install() });
    const bodies = recordMessages(page);
    let aborted = 0;
    await page.route("**/v1/message", (route) => {
      if (aborted < 2) {
        aborted += 1;
        return route.abort("connectionreset");
      }
      return route.continue();
    });
    const token = uniq("retry");
    const user = await send(page, token);
    const reply = replyOf(user);
    await expect.poll(() => aborted).toBe(1);
    await expect(reply).toHaveAttribute("data-status", "sending"); // never looks failed
    await page.clock.fastForward(4_500); // sleep 4s
    await expect.poll(() => aborted).toBe(2);
    await expect(reply).toHaveAttribute("data-status", "sending");
    await page.clock.fastForward(8_500); // sleep 8s
    await waitDone(reply);

    const mine = bodies.filter((b) => b.text === token);
    expect(mine).toHaveLength(3);
    expect(new Set(mine.map((b) => b.client_msg_id)).size, "the same client_msg_id on every attempt").toBe(1);
    expect(await page.locator(".row[data-role=user]").filter({ hasText: token }).count(), "one bubble").toBe(1);
    // One reply: the row after the user row is its reply, and the one after that is not another reply to it.
    const afterReply = reply.locator("xpath=following-sibling::*[1]");
    expect(await afterReply.count()).toBe(0);
    await expect(user.locator(".failed-caption")).toHaveCount(0);
    await expect(reply.locator(".inline-error")).toHaveCount(0);
  });

  test("SND-4: a 413 fails at once with the server's detail and a retry arrow beside the bubble", async ({
    page,
  }) => {
    const bodies = recordMessages(page);
    const detail = "image exceeds the 6MB decoded limit";
    await page.route("**/v1/message", (route) =>
      route.fulfill({ status: 413, contentType: "application/json", body: JSON.stringify({ detail }) }),
    );
    await page.getByTestId("file-input").setInputFiles(png());
    await expect(page.getByTestId("preview")).toBeVisible();
    const token = uniq("big");
    const user = await send(page, token);
    // The caption and arrow belong to the user's row.
    await expect(user.locator(".failed-caption")).toContainText(detail);
    await expect(user.locator(".retry-arrow")).toBeVisible();
    await page.waitForTimeout(800);
    expect(bodies.filter((b) => b.text === token), "a permanent failure is not retried").toHaveLength(1);
  });

  test("SND-8: a failed send gets a red retry arrow beside the bubble and a right-aligned caption below; retry resends the same client_msg_id", async ({
    page,
  }) => {
    const bodies = recordMessages(page);
    let fail = true;
    await page.route("**/v1/message", (route) =>
      fail
        ? route.fulfill({ status: 400, contentType: "application/json", body: JSON.stringify({ detail: "bad request for test" }) })
        : route.continue(),
    );
    const token = uniq("fail400");
    const user = await send(page, token);
    const arrow = user.locator(".retry-arrow");
    const caption = user.locator(".failed-caption");
    await expect(arrow).toBeVisible();
    await expect(caption).toContainText("bad request for test");

    const bubble = await rectOf(user.locator(".bubble"));
    const a = await rectOf(arrow);
    const c = await rectOf(caption);
    expect(a.right, "arrow is beside the bubble, not over it").toBeLessThanOrEqual(bubble.left);
    expect(Math.abs((a.top + a.bottom) / 2 - (bubble.top + bubble.bottom) / 2), "vertically centred on the bubble").toBeLessThanOrEqual(2);
    expect(Math.abs(c.right - bubble.right), "caption right-aligned with the bubble").toBeLessThanOrEqual(2);
    expect(c.top, "caption below the bubble (pulled up into the gap)").toBeGreaterThanOrEqual(bubble.bottom - 8);
    expect(c.top).toBeLessThan(bubble.bottom + 24);
    expect(await arrow.evaluate((el) => getComputedStyle(el).borderRadius)).toBe("50%");
    const bg = await arrow.evaluate((el) => getComputedStyle(el).backgroundColor);
    const [r, g, b] = bg.match(/\d+/g)!.map(Number);
    expect(r, "red").toBeGreaterThan(g + 80);
    expect(r).toBeGreaterThan(b + 80);

    // Retry re-sends under the SAME client_msg_id and lands as one bubble, one reply.
    fail = false;
    await arrow.click();
    await waitDone(replyOf(user));
    const mine = bodies.filter((b2) => b2.text === token);
    expect(mine).toHaveLength(2);
    expect(mine[1].client_msg_id).toBe(mine[0].client_msg_id);
    await expect(arrow).toHaveCount(0);
    await expect(caption).toHaveCount(0);
    expect(await page.locator(".row[data-role=user]").filter({ hasText: token }).count()).toBe(1);
  });
});

// CMP-9: on a touch device "+" offers the camera through a `capture` input,
// because a plain image input opens Android's photo picker, which has no
// camera. A desktop keeps the one-click file dialog. Whether the phone really
// opens its camera is [device]; this checks the wiring.
test("CMP-9: touch gets Take photo / Choose photo; desktop goes straight to the file dialog", async ({
  page,
  isMobile,
}) => {
  const plus = page.getByRole("button", { name: "Add a photo" });
  if (!isMobile) {
    const chooser = page.waitForEvent("filechooser");
    await plus.click();
    const fc = await chooser;
    expect(await fc.element().getAttribute("capture")).toBeNull();
    await expect(page.getByTestId("attach-menu")).toHaveCount(0);
    return;
  }
  const menu = page.getByTestId("attach-menu");
  await plus.click();
  await expect(menu).toBeVisible();
  await expect(menu.getByRole("menuitem", { name: "Take photo" })).toBeFocused();
  // Each item is an icon beside one line of text. In the narrow anchored menu
  // the labels once wrapped to two lines (Android, 2026-10-24).
  for (const name of ["Take photo", "Choose photo"]) {
    const item = menu.getByRole("menuitem", { name });
    await expect(item.locator("svg")).toHaveCount(1);
    const box = await item.boundingBox();
    expect(box!.height).toBeLessThan(48);
  }
  // Opening it with the soft keyboard up closes the keyboard, which resizes
  // and scrolls the page (resizes-content); the menu must survive that.
  // Waits two frames before returning: the close is a React state update, so
  // asserting straight away passed against a menu that was about to vanish.
  await page.evaluate(async () => {
    window.dispatchEvent(new Event("resize"));
    document.dispatchEvent(new Event("scroll"));
    await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  });
  await expect(menu).toHaveCount(1);
  // Tapping "+" again closes it rather than closing and reopening.
  await plus.click();
  await expect(menu).toHaveCount(0);

  await plus.click();
  const chooser = page.waitForEvent("filechooser");
  await menu.getByRole("menuitem", { name: "Take photo" }).click();
  const fc = await chooser;
  expect(await fc.element().getAttribute("capture")).toBe("environment");
  await expect(menu).toHaveCount(0);
  await fc.setFiles(png());
  await expect(page.getByTestId("preview")).toBeVisible();
  await page.getByTestId("preview").getByRole("button", { name: "Remove photo" }).click();

  await plus.click();
  const library = page.waitForEvent("filechooser");
  await menu.getByRole("menuitem", { name: "Choose photo" }).click();
  expect(await (await library).element().getAttribute("capture")).toBeNull();
});
