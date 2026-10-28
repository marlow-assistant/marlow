// Section 3 (streaming text and the activity line) and the two-device flow.
import {
  expect,
  nudgeSync,
  readSamples,
  replyOf,
  send,
  startSampler,
  stopSampler,
  test,
  uniq,
  waitDone,
} from "./fixtures";

test("STR-8 / STR-11: no bubble until there is text; tool labels show before it; the generic labels hide once it streams", async ({
  page,
}) => {
  const token = uniq("tool");
  await startSampler(page, token);
  const user = await send(page, `#tool ${token}`, token);
  const reply = replyOf(user);
  await waitDone(reply, 30_000);
  await stopSampler(page);
  const samples = await readSamples(page);
  const running = samples.filter((s) => s.status === "sending" || s.status === "running");
  expect(running.length).toBeGreaterThan(20);

  // STR-8: while there is no text, the row is just a status line (a label), never a bubble.
  const noText = running.filter((s) => s.bubbles === 0);
  expect(noText.length, "frames before the first text").toBeGreaterThan(10);
  expect(noText.every((s) => !!s.label), "a status line on every frame without a bubble").toBe(true);

  // STR-11: the tool labels were visible before any text, in order.
  const labels = noText.map((s) => s.label);
  const read = labels.indexOf("Reading a file…");
  const search = labels.indexOf("Searching…");
  expect(read, "Reading a file… shown before text").toBeGreaterThanOrEqual(0);
  expect(search, "Searching… shown before text").toBeGreaterThan(read);

  // STR-11: once text is on screen, Thinking… / Writing… are gone.
  const typing = running.filter((s) => s.bubbles > 0);
  expect(typing.length).toBeGreaterThan(0);
  expect(typing.filter((s) => s.label === "Thinking…" || s.label === "Writing…")).toEqual([]);

  // Settled: a bubble with the reply, no indicator.
  await expect(reply.locator(".bubble")).toHaveCount(1);
  await expect(reply.locator(".working")).toHaveCount(0);
});

test("STR-8: a failed turn's reason sits beneath the user bubble, not in a bubble", async ({ page }) => {
  const token = uniq("fail");
  const user = await send(page, `#fail ${token}`, token);
  const reply = replyOf(user);
  await expect(reply).toHaveAttribute("data-status", "error", { timeout: 30_000 });
  const caption = user.locator(".failed-caption");
  await expect(caption).toBeVisible();
  expect((await caption.locator(".error-text").textContent())?.trim().length).toBeGreaterThan(5);
  expect(await caption.evaluate((el) => el.closest(".bubble") === null), "the reason is not inside a bubble").toBe(true);
  await expect(reply.locator(".bubble")).toHaveCount(0);
  await expect(user.locator(".bubble")).toHaveCount(1); // a user message always has its bubble
});

test("two devices: a reply started on device B appears on device A and streams there", async ({
  page: a,
  newDevice,
  isMobile,
}) => {
  test.skip(isMobile, "desktop only (runtime budget): not viewport-dependent");
  const b = (await newDevice()).page;
  const token = uniq("stream");
  const bUser = await send(b, `#long ${token}`, token);

  const aUser = a.locator(".row[data-role=user]").filter({ hasText: token });
  await expect
    .poll(async () => {
      await nudgeSync(a);
      return aUser.count();
    }, { timeout: 20_000 })
    .toBe(1);
  const aReply = replyOf(aUser);
  const len = () => aReply.locator(".bubble").evaluate((el) => el.textContent?.length ?? 0).catch(() => 0);

  // A shows the placeholder running, with text that is still growing.
  await expect(aReply).toHaveAttribute("data-status", "running");
  await expect.poll(len, { timeout: 15_000 }).toBeGreaterThan(20);
  const first = await len();
  await expect.poll(len, { timeout: 15_000 }).toBeGreaterThan(first + 100);
  await expect(aReply, "still streaming while it grew").toHaveAttribute("data-status", "running");

  await waitDone(replyOf(bUser), 40_000);
  await expect(aReply).toHaveAttribute("data-status", "done", { timeout: 25_000 });
  expect(await len()).toBeGreaterThan(2_000);
});
