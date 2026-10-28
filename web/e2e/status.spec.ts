// Section 4 of the contract: status surfaces. STA-2 (strip order), STA-4/5
// (connection indicator), STA-7 (greeting while the first fetch is out).
// STA-6 (the transient notice) needs two devices and lives in permission.spec.ts.
import {
  composerInput,
  connectionLog,
  expect,
  nudgeSync,
  rectOf,
  sleep,
  test,
  type Page,
} from "./fixtures";

const healthBody = (over: Record<string, unknown>) =>
  JSON.stringify({
    status: "ok",
    auth_ok: true,
    reauth_prewarn: false,
    queue_depth: 0,
    queue_paused: false,
    oldest_pending_permission_age: null,
    model: "sonnet",
    effort: "low",
    ...over,
  });

test("STA-2: the strip shows the highest state, +N expands the rest below, prewarn is suppressed by reauth, nothing dismisses", async ({
  page,
}) => {
  let health = healthBody({ auth_ok: false, queue_paused: true, reauth_prewarn: true });
  await page.route("**/health", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: health }),
  );
  await nudgeSync(page);

  const strip = page.getByTestId("status-strip");
  await expect(strip).toBeVisible();
  const banners = strip.locator("[data-banner]");
  await expect(banners).toHaveCount(1);
  await expect(banners.first()).toHaveClass(/banner-red/);
  await expect(banners.first()).toContainText("Marlow needs reauthorization");
  const more = strip.getByRole("button", { name: "+1" });
  await expect(more, "prewarn is suppressed once reauth is needed, so only one more").toBeVisible();
  expect((await rectOf(more)).height).toBeGreaterThanOrEqual(44);

  // The strip sits above the transcript.
  expect((await rectOf(strip)).bottom).toBeLessThanOrEqual((await rectOf(page.getByTestId("transcript"))).top + 1);

  await more.click();
  await expect(banners).toHaveCount(2);
  await expect(banners.nth(1)).toHaveClass(/banner-orange/);
  await expect(banners.nth(1)).toContainText("Marlow is paused.");
  expect((await rectOf(banners.nth(1))).top, "the second banner is below the first").toBeGreaterThanOrEqual(
    (await rectOf(banners.first())).bottom - 1,
  );
  await expect(strip.getByText(/expires soon/)).toHaveCount(0);
  // Nothing is dismissible: the only control is the collapse toggle.
  await expect(strip.getByRole("button")).toHaveCount(1);
  await strip.getByRole("button", { name: "Less" }).click();
  await expect(banners).toHaveCount(1);

  // Auth recovers: paused outranks prewarn.
  health = healthBody({ auth_ok: true, queue_paused: true, reauth_prewarn: true });
  await nudgeSync(page);
  await expect(banners.first()).toContainText("Marlow is paused.");
  await expect(banners.first()).toHaveClass(/banner-orange/);
  await strip.getByRole("button", { name: "+1" }).click();
  await expect(banners).toHaveCount(2);
  await expect(banners.nth(1)).toContainText("expires soon");

  // All clear: the strip is gone (and was never dismissed by hand).
  health = healthBody({});
  await nudgeSync(page);
  await expect(strip).toHaveCount(0);
});

/** Pair on a page whose first /v1/transcript is handled by `first`. */
async function holdFirstTranscript(page: Page, first: (route: import("@playwright/test").Route) => Promise<void>) {
  let n = 0;
  await page.route("**/v1/transcript*", async (route) => {
    n += 1;
    if (n === 1) return first(route);
    return route.continue();
  });
}

test.describe("connection indicator (STA-4, STA-5)", () => {
  test("STA-5: a first pass still running at 600ms shows the spinner, then clears", async ({ newDevice }) => {
    const { page } = await newDevice({
      setup: async (p) => {
        await holdFirstTranscript(p, async (route) => {
          await sleep(1_200);
          await route.continue();
        });
      },
    });
    const conn = page.getByTestId("connection");
    await expect(conn).toBeVisible({ timeout: 3_000 });
    await expect(conn.locator(".spinner")).toBeVisible();
    await expect(conn).toHaveClass(/connection-checking/);
    // At the foot of the conversation: outside the scroller, above the composer.
    const c = await rectOf(conn);
    expect(c.top).toBeGreaterThanOrEqual((await rectOf(page.getByTestId("transcript"))).bottom - 1);
    expect(c.bottom).toBeLessThanOrEqual((await rectOf(composerInput(page))).top + 1);
    await expect(conn).toHaveCount(0, { timeout: 5_000 });
  });

  test("STA-5: a first pass that finishes within 300ms never shows it", async ({ newDevice }) => {
    let hit = 0;
    const { page } = await newDevice({
      setup: async (p) => {
        await holdFirstTranscript(p, async (route) => {
          hit += 1;
          await sleep(300);
          await route.continue();
        });
      },
    });
    await expect.poll(() => hit).toBe(1);
    await sleep(1_500); // well past the 600ms threshold plus the delayed pass
    const log = await connectionLog(page);
    expect(log.filter((e) => e.cls !== null), "the indicator never rendered").toEqual([]);
  });

  test("STA-4 / STA-5: a transport failure says Can't reach Marlow, stays through later passes without downgrading, clears when one succeeds", async ({
    page,
  }) => {
    let block = true;
    await page.route("**/v1/transcript*", (route) => (block ? route.abort("connectionrefused") : route.continue()));
    await nudgeSync(page);
    const conn = page.getByTestId("connection");
    await expect(conn).toContainText("Can't reach Marlow");
    await expect(conn).toHaveClass(/connection-unreachable/);

    // Sticky: further passes (each restarts the loop) keep it, and none goes back to "checking".
    for (let i = 0; i < 3; i++) {
      await nudgeSync(page);
      await sleep(700);
      await expect(conn).toContainText("Can't reach Marlow");
    }
    const log = await connectionLog(page);
    const firstUnreachable = log.findIndex((e) => e.cls?.includes("unreachable"));
    expect(firstUnreachable).toBeGreaterThanOrEqual(0);
    expect(
      log.slice(firstUnreachable).every((e) => e.cls?.includes("unreachable")),
      "never downgraded to checking while unreachable",
    ).toBe(true);

    // A later pass that gets through clears it.
    block = false;
    await nudgeSync(page);
    await expect(conn).toHaveCount(0);
  });

  test("STA-4: a 500 is the box answering badly (trouble), never Can't reach Marlow", async ({ page }) => {
    let fail = true;
    await page.route("**/v1/transcript*", (route) =>
      fail
        ? route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "boom" }) })
        : route.continue(),
    );
    await nudgeSync(page);
    const conn = page.getByTestId("connection");
    await expect(conn).toHaveClass(/connection-trouble/);
    await expect(conn).toContainText("answering with errors");
    await expect(conn).not.toContainText("Can't reach");
    const log = await connectionLog(page);
    expect(log.some((e) => e.text.includes("Can't reach"))).toBe(false);
    fail = false;
    await nudgeSync(page);
    await expect(conn).toHaveCount(0);
  });
});

test("STA-7: an empty transcript says Loading your conversation… until the first fetch lands, then the ask-me-anything line", async ({
  newDevice,
}) => {
  let release!: () => void;
  const gate = new Promise<void>((r) => (release = r));
  const { page } = await newDevice({
    setup: async (p) => {
      await p.route("**/v1/transcript*", async (route) => {
        await gate;
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ rows: [], latest_seq: 0, has_more: false }),
        });
      });
    },
  });
  const greeting = page.getByTestId("greeting");
  await expect(greeting).toBeVisible();
  await expect(greeting).toContainText("Hi, I'm Marlow.");
  await expect(greeting).toContainText("Loading your conversation…");
  await expect(greeting.locator(".spinner")).toBeVisible();
  await expect(greeting).not.toContainText("Ask me anything");
  // An overlay, not a row.
  expect(await page.locator("[data-testid=transcript] .row").count()).toBe(0);
  expect(await greeting.evaluate((el) => !el.closest("[data-testid=transcript]"))).toBe(true);

  release();
  await expect(greeting).toContainText("Ask me anything, or hand me something to keep track of.");
  await expect(greeting).not.toContainText("Loading your conversation");
  await expect(greeting.locator(".spinner")).toHaveCount(0);
});
