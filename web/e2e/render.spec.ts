// Section 12: rendering. RND-1 (row layout), RND-3 (markdown) on a transcript the
// test serves itself (so the content is exactly what the rule needs), and the
// security rules of portable-web-client.md against the fake's `#markdown` reply.
import { expect, rectOf, replyOf, send, test, uniq, waitDone, type Page } from "./fixtures";

const NOW = () => Math.floor(Date.now() / 1000);

interface Syn {
  role: "user" | "assistant";
  text: string;
}

/** Serve `rows` as the whole server transcript for this page. */
async function serveTranscript(page: Page, rows: Syn[]): Promise<void> {
  const full = rows.map((r, i) => ({
    seq: i + 1,
    id: `syn-${i + 1}`,
    role: r.role,
    source: "chat",
    turn_id: `syn-turn-${Math.floor(i / 2) + 1}`,
    client_msg_id: null,
    text: r.text,
    status: "done",
    created_at: NOW() - 600 + i,
    answers: r.role === "assistant" ? [`syn-turn-${Math.floor(i / 2) + 1}`] : [],
    attachments: [],
  }));
  await page.route("**/v1/transcript*", (route) => {
    const after = new URL(route.request().url()).searchParams.has("after");
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ rows: after ? [] : full.slice().reverse(), latest_seq: full.length, has_more: false }),
    });
  });
}

const LONG_WORDS = Array.from({ length: 90 }, (_, i) => `word${i}`).join(" ");

test("RND-1: user bubbles sit right with a 40px gutter, long assistant bubbles span the column, rows 12px apart", async ({
  newDevice,
}) => {
  const { page } = await newDevice({
    setup: (p) =>
      serveTranscript(p, [
        { role: "user", text: "hi" },
        { role: "assistant", text: "hello" },
        { role: "user", text: LONG_WORDS },
        { role: "assistant", text: LONG_WORDS },
      ]),
  });
  const rows = page.locator("[data-testid=transcript] > .row");
  await expect(rows).toHaveCount(4);
  // The last row arrives as new and grows from scale 0.92 at its bottom-left
  // corner: measured mid-grow, its right edge sits ~50px short. The old 24px
  // gutter check (>= 23) never noticed; an exact edge does.
  await page.evaluate(() => Promise.all(document.getAnimations().map((a) => a.finished)));
  const sc = await page.getByTestId("transcript").evaluate((s) => {
    const r = s.getBoundingClientRect();
    const cs = getComputedStyle(s);
    return {
      left: r.left + s.clientLeft + parseFloat(cs.paddingLeft),
      right: r.left + s.clientLeft + s.clientWidth - parseFloat(cs.paddingRight),
    };
  });
  const bubble = (i: number) => rectOf(rows.nth(i).locator(".bubble"));
  const [uShort, aShort, uLong, aLong] = [await bubble(0), await bubble(1), await bubble(2), await bubble(3)];

  expect(Math.abs(uShort.right - sc.right), "short user bubble hugs the right edge").toBeLessThanOrEqual(1);
  expect(uShort.width, "as narrow as its text").toBeLessThan(120);
  expect(Math.abs(aShort.left - sc.left), "short assistant bubble hugs the left edge").toBeLessThanOrEqual(1);
  expect(aShort.width).toBeLessThan(140);

  expect(uLong.left - sc.left, "user bubble keeps a 40px gutter on the left").toBeGreaterThanOrEqual(40 - 1);
  expect(uLong.left - sc.left, "...and fills the rest").toBeLessThan(40 + 20);
  expect(Math.abs(uLong.right - sc.right)).toBeLessThanOrEqual(1);
  // The 24px right gutter is gone (2026-10-19): it read as the cards sitting off centre.
  expect(Math.abs(sc.right - aLong.right), "long assistant bubble runs to the right edge").toBeLessThanOrEqual(1);
  expect(Math.abs(aLong.left - sc.left)).toBeLessThanOrEqual(1);

  // Padding, radius and the 12px between rows.
  const style = await rows.nth(0).locator(".bubble").evaluate((el) => {
    const cs = getComputedStyle(el);
    return [cs.paddingLeft, cs.paddingTop, cs.borderTopLeftRadius];
  });
  expect(style).toEqual(["14px", "9px", "18px"]);
  for (const [i, j] of [[0, 1], [1, 2], [2, 3]]) {
    const gap = (await rectOf(rows.nth(j))).top - (await rectOf(rows.nth(i))).bottom;
    expect(Math.abs(gap - 12), `gap between rows ${i} and ${j}`).toBeLessThanOrEqual(1);
  }
});

test("RND-3: a list resumed at 4 keeps its start; a long list item wraps; a code block scrolls sideways instead of wrapping", async ({
  newDevice,
}) => {
  const longItem = `This list item is long on purpose, so that it has to wrap: ${LONG_WORDS}`;
  const codeLine = `const aVeryLongLine = "${"x".repeat(400)}";`;
  const md = `Where we are:\n\n4. ${longItem}\n5. short\n\n\`\`\`js\n${codeLine}\n\`\`\`\n`;
  const { page } = await newDevice({
    setup: (p) =>
      serveTranscript(p, [
        { role: "user", text: "steps?" },
        { role: "assistant", text: md },
      ]),
  });
  const bubble = page.locator(".bubble-assistant");
  await expect(bubble).toBeVisible();

  const ol = bubble.locator("ol");
  await expect(ol).toHaveAttribute("start", "4");
  expect(await ol.evaluate((el) => (el as HTMLOListElement).start)).toBe(4);

  const item = ol.locator("li").first();
  const lineHeight = await item.evaluate((el) => {
    const cs = getComputedStyle(el);
    const lh = parseFloat(cs.lineHeight);
    return Number.isFinite(lh) ? lh : parseFloat(cs.fontSize) * 1.2;
  });
  expect((await rectOf(item)).height, "the long item wraps onto several lines").toBeGreaterThan(lineHeight * 2.5);
  const short = ol.locator("li").nth(1);
  expect((await rectOf(short)).height).toBeLessThan(lineHeight * 1.6);

  const pre = bubble.locator("pre");
  await expect(pre).toBeVisible();
  const m = await pre.evaluate((el) => {
    const cs = getComputedStyle(el);
    return { ws: cs.whiteSpace, ox: cs.overflowX, sw: el.scrollWidth, cw: el.clientWidth, h: el.getBoundingClientRect().height, lh: parseFloat(cs.lineHeight) || 18 };
  });
  expect(m.ws).toMatch(/^pre/);
  expect(m.ws).not.toBe("pre-wrap");
  expect(["auto", "scroll"]).toContain(m.ox);
  expect(m.sw, "the line is wider than the block: it scrolls horizontally").toBeGreaterThan(m.cw + 50);
  expect(m.h, "and is not wrapped: one line (plus padding)").toBeLessThan(m.lh * 2.6);
});

test("RND-3: a wide table scrolls sideways inside the bubble; cells break between words; a long cell wraps", async ({
  newDevice,
}) => {
  const longNote = "Auto-renews on the fourteenth of the month unless you say otherwise before then";
  const md = [
    "Quotes:",
    "",
    "| Insurer | Annual | Deductible | Excess | Notes |",
    "|:--|--:|:-:|--:|--|",
    `| **Current** | $1,240 | $500 | $250 | ${longNote} |`,
    "| Harbour Mutual | $1,085 | $1,000 | $250 | *Cheapest* |",
  ].join("\n");
  const { page } = await newDevice({
    setup: (p) =>
      serveTranscript(p, [
        { role: "user", text: "compare?" },
        { role: "assistant", text: md },
      ]),
  });
  const bubble = page.locator(".bubble-assistant");
  await expect(bubble.locator("table")).toBeVisible();
  const wrap = bubble.locator(".md-table-wrap");

  const g = await wrap.evaluate((el) => ({ sw: el.scrollWidth, cw: el.clientWidth, ox: getComputedStyle(el).overflowX }));
  const b = await rectOf(bubble);
  const vw = page.viewportSize()!.width;
  expect(b.right, "the bubble never grows past the viewport").toBeLessThanOrEqual(vw);
  if (vw < 600) {
    // On a phone the table is wider than the bubble, so it must scroll.
    expect(["auto", "scroll"]).toContain(g.ox);
    expect(g.sw, "the table overflows its wrapper and scrolls").toBeGreaterThan(g.cw + 20);
  }

  // No cell breaks inside a word: each short cell is one line tall.
  const lineHeight = await bubble.locator("td").first().evaluate((el) => {
    const cs = getComputedStyle(el);
    const lh = parseFloat(cs.lineHeight);
    return Number.isFinite(lh) ? lh : parseFloat(cs.fontSize) * 1.2;
  });
  for (const text of ["Deductible", "Harbour Mutual", "$1,240", "$1,000"]) {
    const cell = bubble.locator("th, td", { hasText: text }).first();
    const h = await cell.evaluate((el) => {
      const r = document.createRange();
      r.selectNodeContents(el);
      return r.getBoundingClientRect().height;
    });
    expect(h, `"${text}" stays on one line`).toBeLessThan(lineHeight * 1.6);
  }

  // The long note wraps at the cap instead of running out on one line.
  const note = bubble.locator("td", { hasText: "Auto-renews" });
  expect((await rectOf(note)).height).toBeGreaterThan(lineHeight * 1.8);

  // A distinct header: tinted, and start-aligned where the column has no alignment.
  const th = await bubble.locator("th").last().evaluate((el) => {
    const cs = getComputedStyle(el);
    return { bg: cs.backgroundColor, align: cs.textAlign };
  });
  expect(th.bg).not.toBe("rgba(0, 0, 0, 0)");
  expect(th.align).toBe("start");
  expect(await bubble.locator("td").nth(1).evaluate((el) => getComputedStyle(el).textAlign)).toBe("right");
});

test("security: #markdown reply renders raw HTML as text, drops javascript: links, links safely, and loads nothing off-origin", async ({
  newDevice,
}) => {
  const { page } = await newDevice();
  const requested: string[] = [];
  page.on("request", (r) => requested.push(r.url()));
  const token = uniq("md");
  const user = await send(page, `#markdown ${token}`, token);
  const reply = replyOf(user);
  await waitDone(reply, 30_000);
  const bubble = reply.locator(".bubble");

  // Raw HTML is text: no <img> element anywhere in the bubble, and its source is visible.
  await expect(bubble.locator("img")).toHaveCount(0);
  await expect(bubble).toContainText('<img src=x onerror="alert(\'raw html must render as text\')">');
  expect(await page.evaluate(() => document.querySelector("[onerror]") === null)).toBe(true);
  expect(await page.evaluate(() => (window as unknown as { __xss?: unknown }).__xss)).toBeUndefined();

  // javascript: is not an anchor; the words survive as text.
  await expect(bubble).toContainText("javascript link");
  expect(await bubble.locator('a[href^="javascript" i]').count()).toBe(0);
  expect(await bubble.locator("a", { hasText: "javascript link" }).count()).toBe(0);

  // The safe link is an anchor with the hardened attributes.
  const safe = bubble.locator("a", { hasText: "safe link" });
  await expect(safe).toHaveCount(1);
  await expect(safe).toHaveAttribute("href", "https://example.com/");
  await expect(safe).toHaveAttribute("rel", "noopener noreferrer");
  await expect(safe).toHaveAttribute("target", "_blank");
  expect(await bubble.locator("a").count(), "the safe link is the only anchor").toBe(1);

  // Markdown structure still renders (headings, lists, code, tables) as real elements.
  await expect(bubble.locator("h2")).toHaveText("A heading");
  await expect(bubble.locator("pre")).toContainText("def hello():");

  // Nothing was fetched from anywhere but the app's own origin, and the dead `src=x` was never requested.
  expect(requested.filter((u) => new URL(u).origin !== "http://127.0.0.1:8080" && !u.startsWith("blob:") && !u.startsWith("data:"))).toEqual([]);
  expect(requested.some((u) => new URL(u).pathname === "/x")).toBe(false);
});
