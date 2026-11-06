// Reordering the to-do list (docs/tasks/todos-and-reminders.md, "Manual order")
// in a real browser, which is the point: what decides whether a drag works on
// Android is `touch-action` and pointer capture, and jsdom has neither. On the
// pixel7 project the finger is real touch input, through CDP; on desktop it's
// the mouse. /v1/todos is stubbed with page.route, so this tests the client
// against the contract, not the server half.
import { expect, test } from "./fixtures";
import type { Page } from "./fixtures";

interface Wire {
  id: string;
  title: string;
  notes: null;
  due: null;
  state: "open";
  source: "user";
  created: string;
  closed: null;
  snoozed_until: null;
}

const wire = (n: number): Wire => ({
  id: `t${n}`,
  title: `Item ${n}`,
  notes: null,
  due: null,
  state: "open",
  source: "user",
  created: "2026-11-01T08:00:00-05:00",
  closed: null,
  snoozed_until: null,
});

/** A box with `count` to-dos that applies moves, and the anchors it was sent. */
async function stubTodos(page: Page, count: number, moves: unknown[]) {
  let list = Array.from({ length: count }, (_, i) => wire(i + 1));
  const json = (body: unknown) => ({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  await page.route("**/v1/todos", (route) => route.fulfill(json({ todos: list })));
  await page.route("**/v1/todos/*/move", async (route) => {
    const id = route.request().url().split("/").at(-2);
    const { before } = route.request().postDataJSON() as { before: string | null };
    moves.push({ id, before });
    const moved = list.find((t) => t.id === id)!;
    const rest = list.filter((t) => t.id !== id);
    const at = before === null ? rest.length : rest.findIndex((t) => t.id === before);
    list = [...rest.slice(0, at), moved, ...rest.slice(at)];
    await route.fulfill(json({ todos: list }));
  });
}

async function openTodos(page: Page) {
  await page.getByRole("button", { name: /^To-dos/ }).click();
  const panel = page.getByRole("dialog", { name: "To-dos" });
  await expect(panel.getByRole("listitem", { name: "Item 1", exact: true })).toBeVisible();
  return panel;
}

const shown = (page: Page) =>
  page.locator(".todo-list > .todo-row").evaluateAll((rows) => rows.map((r) => r.getAttribute("aria-label")));

/** A press, a drag in steps, then `hold` ms still before letting go. On a touch
 * device this is a finger; Playwright's touchscreen can only tap. */
async function press(page: Page, from: { x: number; y: number }, toY: number, hold = 0) {
  const steps = 12;
  const at = (i: number) => from.y + ((toY - from.y) * i) / steps;
  if (await page.evaluate(() => navigator.maxTouchPoints > 0)) {
    const cdp = await page.context().newCDPSession(page);
    const touch = (type: string, y?: number) =>
      cdp.send("Input.dispatchTouchEvent", { type, touchPoints: y === undefined ? [] : [{ x: from.x, y }] } as never);
    await touch("touchStart", from.y);
    for (let i = 1; i <= steps; i++) await touch("touchMove", at(i));
    if (hold) await page.waitForTimeout(hold);
    await touch("touchEnd");
    await cdp.detach();
  } else {
    await page.mouse.move(from.x, from.y);
    await page.mouse.down();
    for (let i = 1; i <= steps; i++) await page.mouse.move(from.x, at(i));
    if (hold) await page.waitForTimeout(hold);
    await page.mouse.up();
  }
}

async function centre(page: Page, title: string, selector: string) {
  const box = await page.getByRole("listitem", { name: title, exact: true }).locator(selector).first().boundingBox();
  if (!box) throw new Error(`no ${selector} on ${title}`);
  return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
}

test("dragging a row by its handle moves it, and the order holds after reopening", async ({ newDevice }) => {
  const moves: unknown[] = [];
  const { page } = await newDevice({ setup: (p) => stubTodos(p, 4, moves) });
  const panel = await openTodos(page);
  const grip = await centre(page, "Item 4", ".todo-grip");
  const target = await centre(page, "Item 2", ".todo-grip");
  await press(page, grip, target.y - 10);
  await expect.poll(() => moves).toEqual([{ id: "t4", before: "t2" }]);
  expect(await shown(page)).toEqual(["Item 1", "Item 4", "Item 2", "Item 3"]);

  await panel.getByRole("button", { name: "Done", exact: true }).click();
  await openTodos(page);
  expect(await shown(page)).toEqual(["Item 1", "Item 4", "Item 2", "Item 3"]);
});

test("a finger on the row, not the handle, scrolls the sheet and moves nothing", async ({ newDevice }) => {
  const moves: unknown[] = [];
  const { page } = await newDevice({ setup: (p) => stubTodos(p, 30, moves) });
  await openTodos(page);
  test.skip(!(await page.evaluate(() => navigator.maxTouchPoints > 0)), "touch only");
  const title = await centre(page, "Item 8", ".todo-title");
  await press(page, title, title.y - 300);
  await expect.poll(() => page.locator(".sheet-body").evaluate((el) => el.scrollTop)).toBeGreaterThan(100);
  expect(moves).toEqual([]);
});

test("a row held at the edge scrolls the sheet, and lands where it was let go", async ({ newDevice }) => {
  const moves: unknown[] = [];
  const { page } = await newDevice({ setup: (p) => stubTodos(p, 40, moves) });
  await openTodos(page);
  const body = (await page.locator(".sheet-body").boundingBox())!;
  const grip = await centre(page, "Item 1", ".todo-grip");
  await press(page, grip, body.y + body.height - 10, 800);
  await expect.poll(() => moves.length).toBe(1);
  const order = await shown(page);
  expect(await page.locator(".sheet-body").evaluate((el) => el.scrollTop)).toBeGreaterThan(100);
  // Well past the rows that were on screen when the drag began.
  const visibleAtStart = Math.floor(body.height / 60);
  expect(order.indexOf("Item 1")).toBeGreaterThan(visibleAtStart);
});

test("the ⋯ menu moves a row without any drag", async ({ newDevice }) => {
  const moves: unknown[] = [];
  const { page } = await newDevice({ setup: (p) => stubTodos(p, 3, moves) });
  const panel = await openTodos(page);
  await panel.getByRole("button", { name: "More for Item 3" }).click();
  await panel.getByRole("menuitem", { name: /^Move/ }).click();
  await panel.getByRole("menuitem", { name: "Move to top" }).click();
  await expect.poll(() => moves).toEqual([{ id: "t3", before: "t1" }]);
  expect(await shown(page)).toEqual(["Item 3", "Item 1", "Item 2"]);
  await expect(panel.getByRole("button", { name: "More for Item 3" })).toBeFocused();
});
