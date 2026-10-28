// Section 11: permission prompts. PRM-2 (the modal card, raw input, drain bar, answer),
// SCR-16 (a prompt does not move the scroll position), STA-6 (the transient
// "already moved on" notice, which needs two devices).
import {
  composerInput,
  ensureOverflow,
  expect,
  frames,
  nudgeSync,
  rectOf,
  replyOf,
  send,
  setScrollTop,
  sleep,
  test,
  uniq,
  userTouchesScroller,
  waitDone,
  type Page,
} from "./fixtures";

const modalOf = (p: Page) => p.getByTestId("permission");

/** Send `#approve` and wait for the prompt. */
async function askApprove(page: Page) {
  const token = uniq("approve");
  const user = await send(page, `#approve ${token}`, token);
  await expect(modalOf(page)).toBeVisible({ timeout: 40_000 });
  return { user, token };
}

test("PRM-2: the card is modal, leads with the server's title, keeps the raw input a click away, drains, and Allow closes it at once", async ({
  page,
}) => {
  const { user } = await askApprove(page);
  const modal = modalOf(page);
  await expect(modal).toHaveAttribute("aria-modal", "true");
  // The server's reading of the request leads; the raw input is still there.
  await expect(modal.getByRole("heading")).toContainText("Marlow wants to");
  const raw = modal.getByTestId("perm-input");
  await expect(raw).toHaveText("command: rm -rf ~/scratch/old-drafts");
  expect(await raw.evaluate((el) => getComputedStyle(el).fontFamily)).toMatch(/mono|Menlo|Consolas/i);
  await modal.getByRole("button", { name: "Show details" }).click();
  await expect(raw).toHaveClass(/open/);

  // The card hugs its content and stays within 60% of the viewport, centred.
  const card = await rectOf(modal);
  const vh = page.viewportSize()!.height;
  expect(card.height).toBeLessThanOrEqual(vh * 0.6 + 1);
  expect(Math.abs(card.top + card.height / 2 - vh / 2)).toBeLessThanOrEqual(2);

  // A bar that drains, with no number on screen (Mike, 2026-10-02).
  const bar = modal.getByTestId("time-bar");
  const v1 = Number(await bar.evaluate((el) => (el as HTMLProgressElement).value));
  expect(v1).toBeGreaterThan(0);
  await sleep(1_600);
  expect(Number(await bar.evaluate((el) => (el as HTMLProgressElement).value))).toBeLessThan(v1);
  await expect(modal).not.toContainText(/\d+s left/);

  // Not dismissible: Escape and a click on the backdrop do nothing.
  await page.keyboard.press("Escape");
  await page.mouse.click(4, 4);
  await expect(modal).toBeVisible();
  // It covers the page: the composer is not reachable underneath.
  const c = await rectOf(composerInput(page));
  const hit = await page.evaluate(
    ([x, y]) => !!document.elementFromPoint(x, y)?.closest(".modal-backdrop"),
    [c.left + c.width / 2, c.top + c.height / 2],
  );
  expect(hit).toBe(true);

  await modal.getByRole("button", { name: "Allow" }).click();
  await expect(modal, "closed at once, not on the next poll").toBeHidden({ timeout: 700 });
  await waitDone(replyOf(user), 40_000);
  await expect(replyOf(user)).toContainText(/answered:\s*allow/);
});

test("PRM-2: Deny closes it and the reply says deny", async ({ page, isMobile }) => {
  test.skip(isMobile, "desktop only: the layout was checked by the Allow run; this one is a 12s server round trip");
  const { user } = await askApprove(page);
  await modalOf(page).getByRole("button", { name: "Deny" }).click();
  await expect(modalOf(page)).toBeHidden({ timeout: 700 });
  await waitDone(replyOf(user), 40_000);
  await expect(replyOf(user)).toContainText(/answered:\s*deny/);
});

test("SCR-16: opening and answering a prompt does not change scrollTop", async ({ page, isMobile }) => {
  test.skip(isMobile, "desktop only (runtime budget): the modal sits outside the list on both layouts");
  await ensureOverflow(page);
  const token = uniq("scr16");
  const user = await send(page, `#approve ${token}`, token);
  await userTouchesScroller(page); // a reader has taken over: following ends
  await setScrollTop(page, 300);
  const start = await page.evaluate(() => document.querySelector<HTMLElement>("[data-testid=transcript]")!.scrollTop);
  await page.evaluate(() => {
    const w = window as unknown as { __st: number[]; __run: boolean };
    w.__st = [];
    w.__run = true;
    const s = document.querySelector<HTMLElement>("[data-testid=transcript]")!;
    const f = () => {
      if (!w.__run) return;
      requestAnimationFrame(f);
      w.__st.push(s.scrollTop, document.scrollingElement!.scrollTop);
    };
    requestAnimationFrame(f);
  });
  await expect(modalOf(page)).toBeVisible({ timeout: 40_000 });
  await sleep(500);
  await modalOf(page).getByRole("button", { name: "Allow" }).click();
  await expect(modalOf(page)).toBeHidden({ timeout: 700 });
  await waitDone(replyOf(user), 40_000);
  await frames(page, 5);
  await sleep(500);
  const samples = await page.evaluate(() => {
    const w = window as unknown as { __st: number[]; __run: boolean };
    w.__run = false;
    return w.__st;
  });
  expect(samples.length).toBeGreaterThan(60);
  const scroller = samples.filter((_, i) => i % 2 === 0);
  const doc = samples.filter((_, i) => i % 2 === 1);
  expect(Math.max(...scroller.map((v) => Math.abs(v - start))), "transcript scrollTop through open and answer").toBeLessThanOrEqual(1);
  expect(Math.max(...doc), "the page itself never scrolls").toBe(0);
});

test("STA-6: answering a prompt another device already answered shows the 'moved on' notice beside the composer for ~3s", async ({
  page: a,
  newDevice,
  isMobile,
}) => {
  test.skip(isMobile, "desktop only (runtime budget): two-device behaviour is not viewport-dependent");
  const b = (await newDevice()).page;
  const { user } = await askApprove(a);
  // Device B learns of the turn through its sync and shows the same prompt (PRM-4: requests are global).
  await expect
    .poll(async () => {
      await nudgeSync(b);
      return modalOf(b).isVisible();
    }, { timeout: 30_000 })
    .toBe(true);

  // Hold B's next permission poll so its prompt cannot clear on its own.
  let held!: (r: import("@playwright/test").Route) => void;
  const heldRoute = new Promise<import("@playwright/test").Route>((r) => (held = r));
  await b.route("**/v1/permissions", (route) => held(route));
  const route = await heldRoute;

  await modalOf(a).getByRole("button", { name: "Allow" }).click();
  await expect(modalOf(a)).toBeHidden({ timeout: 700 });

  await expect(modalOf(b)).toBeVisible();
  await modalOf(b).getByRole("button", { name: "Allow" }).click();
  const notice = b.locator(".foot .notice");
  await expect(notice).toHaveText("Marlow already moved on — that request was no longer open.");
  await expect(modalOf(b), "the stale prompt is cleared").toBeHidden();
  // Beside the composer: in the foot, between the connection indicator and the composer.
  const nr = await rectOf(notice);
  const cr = await rectOf(b.locator(".composer"));
  expect(nr.bottom).toBeLessThanOrEqual(cr.top + 1);
  expect(nr.top).toBeGreaterThanOrEqual((await rectOf(b.getByTestId("transcript"))).bottom - 1);
  const shownAt = Date.now();
  await expect(notice).toBeHidden({ timeout: 6_000 });
  const shownFor = Date.now() - shownAt;
  expect(shownFor, "cleared after about three seconds").toBeGreaterThan(1_500);
  expect(shownFor).toBeLessThan(4_500);

  await route.continue();
  await b.unroute("**/v1/permissions");
  await waitDone(replyOf(user), 40_000);
});
