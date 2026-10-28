// Pairing, unpairing, 401 handling and what the server serves.
import { expect, idbAll, pairViaUi, pairingInvite, test, composerInput, BASE } from "./fixtures";

test("a browser pairs from a link and keeps its token", async ({ newDevice }) => {
  const { page } = await newDevice({ pair: false });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Pair with Marlow" })).toBeVisible();
  await pairViaUi(page, "e2e-pairing");
  const kv = await idbAll(page, "kv");
  expect(typeof kv?.device_token).toBe("string");
  expect((kv!.device_token as string).length).toBeGreaterThan(10);

  // Reload: still paired, straight into the chat, no pairing screen.
  await page.reload();
  await expect(composerInput(page)).toBeVisible();
  await expect(page.getByRole("heading", { name: "Pair with Marlow" })).toHaveCount(0);
});

test("a pairing link's invite leaves the address bar, and works only once", async ({ newDevice }) => {
  const { page } = await newDevice({ pair: false });
  const invite = await pairingInvite("e2e-fragment");
  await page.goto(`/#pair=${invite}`);
  await expect(page.getByRole("button", { name: "Pair", exact: true })).toBeEnabled();
  expect(page.url()).not.toContain(invite);
  expect(page.url()).not.toContain("pair=");
  await page.getByRole("button", { name: "Pair", exact: true }).click();
  await expect(composerInput(page)).toBeVisible();

  // The same invite again, from another browser: refused, and says why in one line.
  const other = await newDevice({ pair: false });
  await other.page.goto("/");
  await other.page.locator("input[name=link]").fill(`${BASE}/#pair=${invite}`);
  await other.page.getByRole("button", { name: "Pair", exact: true }).click();
  await expect(other.page.getByRole("alert")).toContainText("expired or was already used");
});

test("unpairing revokes the token on the box", async ({ newDevice, request }) => {
  const dev = await newDevice();
  const { page } = dev;
  const token = (await idbAll(page, "kv"))?.device_token as string;
  expect(token).toBeTruthy();
  await page.getByRole("button", { name: "Settings" }).click();
  await page.getByRole("dialog", { name: "Settings" }).getByRole("button", { name: "Unpair" }).click();
  await page
    .getByRole("alertdialog", { name: "Unpair from Marlow?" })
    .getByRole("button", { name: "Unpair" })
    .click();
  await expect(page.getByRole("heading", { name: "Pair with Marlow" })).toBeVisible();
  const after = await request.get(`${BASE}/v1/preferences`, { headers: { Authorization: `Bearer ${token}` } });
  expect(after.status()).toBe(401);
});

test("unpairing forgets the token locally", async ({ newDevice }) => {
  const dev = await newDevice();
  const { page } = dev;
  expect((await idbAll(page, "kv"))?.device_token).toBeTruthy();
  await page.getByRole("button", { name: "Settings" }).click();
  await page.getByRole("dialog", { name: "Settings" }).getByRole("button", { name: "Unpair" }).click();
  // The confirm is in-app now (iOS's "Unpair from Marlow?"), not window.confirm.
  const confirm = page.getByRole("alertdialog", { name: "Unpair from Marlow?" });
  await expect(confirm).toContainText("needs a new pairing link");
  await confirm.getByRole("button", { name: "Unpair" }).click();
  await expect(page.getByRole("heading", { name: "Pair with Marlow" })).toBeVisible();
  expect(dev.confirms, "no native confirm").toEqual([]);
  const kv = await idbAll(page, "kv");
  expect(kv === null || !("device_token" in kv)).toBe(true);
  // And it stays unpaired across a reload.
  await page.reload();
  await expect(page.getByRole("heading", { name: "Pair with Marlow" })).toBeVisible();
});

test("a 401 returns the app to pairing with a notice (WEB-8)", async ({ page }) => {
  await page.route("**/v1/transcript*", (route) =>
    route.fulfill({ status: 401, contentType: "application/json", body: JSON.stringify({ detail: "invalid device token" }) }),
  );
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange"))); // run a sync pass now
  await expect(page.getByRole("heading", { name: "Pair with Marlow" })).toBeVisible();
  await expect(page.getByText(/no longer paired/i)).toBeVisible();
  const kv = await idbAll(page, "kv");
  expect(kv === null || !("device_token" in kv)).toBe(true);
});

test("serving: /v1/nope is a JSON 404, an unknown client route serves the app", async ({ request, newDevice }) => {
  const nope = await request.get(`${BASE}/v1/nope`);
  expect(nope.status()).toBe(404);
  expect(nope.headers()["content-type"]).toContain("application/json");
  expect(await nope.json()).toEqual({ detail: "Not Found" });

  const spa = await request.get(`${BASE}/some/page`);
  expect(spa.status()).toBe(200);
  expect(spa.headers()["content-type"]).toContain("text/html");
  expect(await spa.text()).toContain('<div id="root">');
  // The production CSP is on the app shell (and the whole run asserts it is never violated).
  expect(spa.headers()["content-security-policy"]).toContain("script-src 'self'");

  const { page } = await newDevice({ pair: false });
  await page.goto("/some/page");
  await expect(page.getByRole("heading", { name: "Pair with Marlow" })).toBeVisible();
});
