// Shared plumbing for the e2e suite: pairing, instrumented contexts ("devices"),
// geometry readers and a few waits. See playwright.config.ts for how it runs.
//
// Every device (a browser context) is instrumented the same way, and at the end
// of the test the fixture fails it if any of these happened (portable-web-client
// "Security"): a CSP violation event, a console message about the Content
// Security Policy, or a JS dialog other than the unpair confirm.
import {
  test as base,
  expect,
  type Browser,
  type BrowserContext,
  type Locator,
  type Page,
  type Route,
} from "@playwright/test";
import { request } from "node:http";
import { fileURLToPath } from "node:url";
import { deflateSync } from "node:zlib";

export { expect };
export type { Locator, Page, Route };

// The dev server's admin API is an owner-only Unix socket, not a TCP port;
// the suite went red at setup for every test when that moved and this still
// dialled 127.0.0.1:8464. Its own socket since the funnel-ingress
// review, not the internal one. fetch() cannot dial a socket, so plain
// node:http with socketPath.
const ADMIN_SOCKET = fileURLToPath(new URL("../../.dev/admin/admin.sock", import.meta.url));
export const BASE = "http://127.0.0.1:8080";

/** A fresh single-use pairing invite from the dev server's admin socket. */
export function pairingInvite(name = "e2e"): Promise<string> {
  const body = JSON.stringify({ device_name: name });
  return new Promise((resolve, reject) => {
    const req = request(
      {
        socketPath: ADMIN_SOCKET,
        path: "/internal/admin/invite",
        method: "POST",
        headers: { "content-type": "application/json", "content-length": Buffer.byteLength(body) },
      },
      (res) => {
        let text = "";
        res.setEncoding("utf8");
        res.on("data", (c: string) => (text += c));
        res.on("end", () => {
          if (res.statusCode !== 200) return reject(new Error(`invite ${res.statusCode}`));
          resolve((JSON.parse(text) as { invite: string }).invite);
        });
      },
    );
    req.on("error", reject);
    req.end(body);
  });
}

export interface Device {
  ctx: BrowserContext;
  page: Page;
  /** Console messages that mention the Content Security Policy. */
  cspConsole: string[];
  /** Any alert/prompt/beforeunload dialog (a confirm is accepted and logged separately). */
  dialogs: string[];
  confirms: string[];
}

export interface NewDeviceOpts {
  /** Pair through the UI (default true). */
  pair?: boolean;
  /** Runs before the page loads: install `page.route` handlers here. */
  setup?: (page: Page) => Promise<void>;
}

/** Pair a page through the UI the way a scanned QR does: open the link with the
 * invite in its fragment, press Pair. */
export async function pairViaUi(page: Page, name = "e2e"): Promise<void> {
  const invite = await pairingInvite(name);
  await page.goto(`/#pair=${invite}`);
  await page.getByRole("button", { name: "Pair", exact: true }).click();
  await expect(composerInput(page)).toBeVisible();
  await closeWhatsNew(page);
}

/** A freshly paired browser is a first run, so the update card
 * (docs/tasks/whats-new.md) is up over the chat. Always: the build bundles its
 * own entries. Closed with OK, as a person would. */
export async function closeWhatsNew(page: Page): Promise<void> {
  const card = page.getByRole("dialog", { name: "Marlow was updated" });
  await card.getByRole("button", { name: "OK" }).click();
  await expect(card).toBeHidden();
}

export const composerInput = (page: Page) => page.locator("textarea");

type Fixtures = {
  newDevice: (opts?: NewDeviceOpts) => Promise<Device>;
  /** A paired page (convenience for tests that need exactly one device). */
  page: Page;
};

export const test = base.extend<Fixtures>({
  newDevice: async (
    { browser, viewport, userAgent, isMobile, hasTouch, deviceScaleFactor, baseURL },
    use,
  ) => {
    const devices: Device[] = [];
    await use(async (opts = {}) => {
      const ctx = await (browser as Browser).newContext({
        viewport,
        userAgent,
        isMobile,
        hasTouch,
        deviceScaleFactor,
        baseURL,
      });
      await ctx.addInitScript(() => {
        const w = window as unknown as Record<string, unknown>;
        w.__csp = [] as string[];
        addEventListener("securitypolicyviolation", (e: SecurityPolicyViolationEvent) => {
          (w.__csp as string[]).push(`${e.violatedDirective} ${e.blockedURI}`);
        });
        // A log of every state the connection indicator (STA-5) passes through,
        // so "never appeared" and "never downgraded" can be asserted.
        const conn: Array<{ cls: string | null; text: string }> = [];
        w.__conn = conn;
        const rec = () => {
          const el = document.querySelector("[data-testid=connection]");
          const cls = el ? el.className : null;
          if (conn.length === 0 ? cls !== null : conn[conn.length - 1].cls !== cls) {
            conn.push({ cls, text: el?.textContent ?? "" });
          }
        };
        new MutationObserver(rec).observe(document, { subtree: true, childList: true, attributes: true });
      });
      const page = await ctx.newPage();
      const dev: Device = { ctx, page, cspConsole: [], dialogs: [], confirms: [] };
      page.on("console", (m) => {
        if (/content security policy/i.test(m.text())) dev.cspConsole.push(m.text());
      });
      page.on("dialog", (d) => {
        if (d.type() === "confirm") {
          dev.confirms.push(d.message());
          void d.accept();
        } else {
          dev.dialogs.push(`${d.type()}: ${d.message()}`);
          void d.dismiss();
        }
      });
      devices.push(dev);
      if (opts.setup) await opts.setup(page);
      if (opts.pair !== false) await pairViaUi(page);
      return dev;
    });
    // Zero CSP violations and zero dialogs across the whole run, on every device.
    for (const d of devices) {
      let violations: string[] = [];
      try {
        violations = await d.page.evaluate(() => (window as unknown as { __csp: string[] }).__csp ?? []);
      } catch {
        // the page navigated away or closed; the console listener still counts
      }
      expect.soft(violations, "securitypolicyviolation events").toEqual([]);
      expect.soft(d.cspConsole, "console errors about the CSP").toEqual([]);
      expect.soft(d.dialogs, "unexpected JS dialogs").toEqual([]);
      await d.ctx.close();
    }
  },

  page: async ({ newDevice }, use) => {
    const d = await newDevice();
    await use(d.page);
  },
});

// --- messages ---------------------------------------------------------------

let counter = 0;
/** A token no other row contains; never starts with `#` (the fake's tag syntax). */
export function uniq(prefix = "msg"): string {
  counter += 1;
  return `${prefix}-${Date.now().toString(36)}${counter}${Math.random().toString(36).slice(2, 5)}`;
}

/** Type into the composer and press send. Returns the new user row (found by `text`). */
export async function send(page: Page, text: string, find = text): Promise<Locator> {
  await composerInput(page).fill(text);
  await page.locator("button.send").click();
  return page.locator(".row[data-role=user]").filter({ hasText: find }).last();
}

/** The assistant row right after a user row. */
export const replyOf = (userRow: Locator): Locator => userRow.locator("xpath=following-sibling::*[1]");

export async function waitDone(reply: Locator, timeout = 30_000): Promise<void> {
  await expect(reply).toHaveAttribute("data-status", "done", { timeout });
}

/** Send a message tagged with `tags` and wait for its reply to finish. */
export async function exchange(page: Page, tags = "", body = "hello", timeout = 30_000) {
  const token = uniq(body);
  const user = await send(page, `${tags} ${token}`.trim(), token);
  const reply = replyOf(user);
  await waitDone(reply, timeout);
  return { user, reply, token };
}

/** Ask the app to run its sync pass now (WEB-1: visible again restarts the loop). */
export async function nudgeSync(page: Page): Promise<void> {
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
}

// --- geometry -----------------------------------------------------------------

export interface Geo {
  st: number;
  sh: number;
  ch: number;
  /** The scroller's visible region (no floating header on the web). */
  vTop: number;
  vBottom: number;
  padBottom: number;
  lastBottom: number | null;
  rows: number;
}

export function geo(page: Page): Promise<Geo> {
  return page.evaluate(() => {
    const s = document.querySelector<HTMLElement>("[data-testid=transcript]")!;
    const r = s.getBoundingClientRect();
    const rows = s.querySelectorAll(":scope > .row");
    const last = rows[rows.length - 1];
    return {
      st: s.scrollTop,
      sh: s.scrollHeight,
      ch: s.clientHeight,
      vTop: r.top + s.clientTop,
      vBottom: r.top + s.clientTop + s.clientHeight,
      padBottom: parseFloat(getComputedStyle(s).paddingBottom),
      lastBottom: last ? last.getBoundingClientRect().bottom : null,
      rows: rows.length,
    };
  });
}

/** Rect of the first element matching `sel` inside `scope` (document by default). */
export async function rectOf(loc: Locator) {
  return loc.evaluate((el) => {
    const r = el.getBoundingClientRect();
    return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height };
  });
}

/** Wait two animation frames: layout, effects and scroll events have settled. */
export async function frames(page: Page, n = 2): Promise<void> {
  await page.evaluate(
    (count) =>
      new Promise<void>((resolve) => {
        const step = (k: number) => (k <= 0 ? resolve() : requestAnimationFrame(() => step(k - 1)));
        step(count);
      }),
    n,
  );
}

/** Set the scroller's scrollTop (a script, not user input: it must not end following). */
export async function setScrollTop(page: Page, y: number | "bottom" | "top"): Promise<void> {
  await page.evaluate((v) => {
    const s = document.querySelector<HTMLElement>("[data-testid=transcript]")!;
    s.scrollTop = v === "bottom" ? s.scrollHeight : v === "top" ? 0 : v;
  }, y);
  await frames(page, 3);
}

/** Tell the app a user is scrolling: ends any following (SCR-11) without moving anything. */
export async function userTouchesScroller(page: Page): Promise<void> {
  await page.locator("[data-testid=transcript]").dispatchEvent("wheel", { deltaY: 0 });
}

/** The last row's bottom is at the end of the content: scrolled to the maximum,
 * and the row sits flush above the scroller's bottom padding (±1px). */
export function expectAtBottom(g: Geo): void {
  expect(g.sh - g.ch - g.st, "scrollTop is at its maximum").toBeLessThanOrEqual(1);
  expect(Math.abs(g.lastBottom! - (g.vBottom - g.padBottom)), "last row bottom vs visible bottom").toBeLessThanOrEqual(1);
}

/** Make the transcript at least `factor` viewports tall by sending tall messages
 * (a 40-line message is ~900px) and waiting for each reply. */
/** Wait for the page's own startup traffic to finish (first sync pass, attachment
 * images for rows another device sent), so nothing late reflows the transcript
 * under a test that has just scrolled it. */
export async function settle(page: Page): Promise<void> {
  await expect(page.locator(".placeholder-image")).toHaveCount(0, { timeout: 10_000 });
  await expect(page.getByTestId("connection")).toHaveCount(0, { timeout: 10_000 });
  await frames(page, 3);
}

export async function ensureOverflow(page: Page, factor = 3): Promise<void> {
  await settle(page);
  for (let i = 0; i < 6; i++) {
    const g = await geo(page);
    if (g.sh >= g.ch * factor) return;
    const token = uniq("filler");
    const body = Array.from({ length: 40 }, (_, n) => `line ${n} ${token}`).join("\n");
    const user = await send(page, body, token);
    await waitDone(replyOf(user));
  }
  const g = await geo(page);
  expect(g.sh, "could not make the transcript overflow").toBeGreaterThanOrEqual(g.ch * factor);
}

// --- IndexedDB (the app's own store, read from the outside) -------------------

/** All values of one object store of the `marlow` database, or null if the
 * database or store does not exist (never creates one: opening a missing DB
 * would create an empty v1 and break the app's own upgrade). */
export function idbAll(page: Page, store: "kv" | "messages" | "images"): Promise<Record<string, unknown> | null> {
  return page.evaluate(async (storeName) => {
    const dbs = await indexedDB.databases();
    if (!dbs.some((d) => d.name === "marlow")) return null;
    const db = await new Promise<IDBDatabase>((res, rej) => {
      const req = indexedDB.open("marlow");
      req.onsuccess = () => res(req.result);
      req.onerror = () => rej(req.error);
    });
    try {
      if (!db.objectStoreNames.contains(storeName)) return null;
      const tx = db.transaction(storeName);
      const os = tx.objectStore(storeName);
      const keys = await new Promise<IDBValidKey[]>((res, rej) => {
        const r = os.getAllKeys();
        r.onsuccess = () => res(r.result);
        r.onerror = () => rej(r.error);
      });
      const vals = await new Promise<unknown[]>((res, rej) => {
        const r = os.getAll();
        r.onsuccess = () => res(r.result);
        r.onerror = () => rej(r.error);
      });
      const out: Record<string, unknown> = {};
      keys.forEach((k, i) => (out[String(k)] = vals[i]));
      return out;
    } finally {
      db.close();
    }
  }, store);
}

// --- a tiny PNG, generated -----------------------------------------------------

function crc32(buf: Buffer): number {
  let c = ~0;
  for (const b of buf) {
    c ^= b;
    for (let k = 0; k < 8; k++) c = (c >>> 1) ^ (0xedb88320 & -(c & 1));
  }
  return ~c >>> 0;
}

/** A solid-colour PNG of the given size (RGB), built by hand. */
export function pngBuffer(width = 64, height = 48, rgb: [number, number, number] = [200, 60, 40]): Buffer {
  const chunk = (type: string, data: Buffer) => {
    const len = Buffer.alloc(4);
    len.writeUInt32BE(data.length);
    const td = Buffer.concat([Buffer.from(type, "ascii"), data]);
    const crc = Buffer.alloc(4);
    crc.writeUInt32BE(crc32(td));
    return Buffer.concat([len, td, crc]);
  };
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 2; // RGB
  const row = Buffer.concat([Buffer.from([0]), Buffer.from(Array.from({ length: width }, () => rgb).flat())]);
  const raw = Buffer.concat(Array.from({ length: height }, () => row));
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", ihdr),
    chunk("IDAT", deflateSync(raw)),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

/** Record every POST /v1/message body a page sends. */
export function recordMessages(page: Page): Array<Record<string, unknown>> {
  const bodies: Array<Record<string, unknown>> = [];
  page.on("request", (req) => {
    if (req.method() === "POST" && new URL(req.url()).pathname === "/v1/message") {
      bodies.push(JSON.parse(req.postData() ?? "{}"));
    }
  });
  return bodies;
}

// --- a per-frame sampler for the follow/pin rules ----------------------------

export interface Sample {
  t: number;
  st: number;
  sh: number;
  vTop: number;
  vBottom: number;
  /** The user row (its divider, when it has one, is inside it: SCR-13). */
  userTop: number;
  userBottom: number;
  /** The divider's top, or null. */
  divTop: number | null;
  replyTop: number;
  replyBottom: number;
  bubbles: number;
  status: string;
  /** The reply row's in-row status text ("Thinking…", "Reading a file…"), or null. */
  label: string | null;
}

/** Record one sample per animation frame for the exchange whose user message
 * contains `token`, from now until `readSamples`. Registered after the app's
 * own frame loop, so each sample is taken AFTER the app has acted on that frame. */
export async function startSampler(page: Page, token: string): Promise<void> {
  await page.evaluate((tok) => {
    const w = window as unknown as { __samples: Sample[]; __sampling: boolean };
    w.__samples = [];
    w.__sampling = true;
    const s = document.querySelector<HTMLElement>("[data-testid=transcript]")!;
    const frame = () => {
      if (!w.__sampling) return;
      requestAnimationFrame(frame);
      const user = Array.from(s.querySelectorAll<HTMLElement>(":scope > .row-user")).find((r) =>
        r.textContent?.includes(tok),
      );
      const reply = user?.nextElementSibling as HTMLElement | null | undefined;
      if (!user || !reply) return;
      const sr = s.getBoundingClientRect();
      const ub = user.getBoundingClientRect();
      const rb = reply.getBoundingClientRect();
      const div = user.querySelector(".divider");
      w.__samples.push({
        t: performance.now(),
        st: s.scrollTop,
        sh: s.scrollHeight,
        vTop: sr.top + s.clientTop,
        vBottom: sr.top + s.clientTop + s.clientHeight,
        userTop: ub.top,
        userBottom: ub.bottom,
        divTop: div ? div.getBoundingClientRect().top : null,
        replyTop: rb.top,
        replyBottom: rb.bottom,
        bubbles: reply.querySelectorAll(".bubble").length,
        status: reply.dataset.status ?? "",
        label: reply.querySelector(".working-label")?.textContent ?? null,
      });
    };
    requestAnimationFrame(frame);
  }, token);
}

export const readSamples = (page: Page): Promise<Sample[]> =>
  page.evaluate(() => (window as unknown as { __samples: Sample[] }).__samples);

export const stopSampler = (page: Page): Promise<void> =>
  page.evaluate(() => {
    (window as unknown as { __sampling: boolean }).__sampling = false;
  });

/** Every state the connection indicator has been in on this page, oldest first
 * (`null` = not rendered), as recorded by the init script. */
export const connectionLog = (page: Page): Promise<Array<{ cls: string | null; text: string }>> =>
  page.evaluate(() => (window as unknown as { __conn: Array<{ cls: string | null; text: string }> }).__conn);

export const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));
