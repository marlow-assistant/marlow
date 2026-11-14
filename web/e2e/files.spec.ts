// Files beyond photos and the Android share target (docs/tasks/share-and-files.md,
// Phases 2 and 4; the wire is share-and-files-wire.md). The server half is
// built separately, so these tests serve the server's side themselves: the send
// is answered by a mock and the transcript, attachments and share entries are
// seeded, so what is checked is the client: the request body's shape, the chips,
// and that opening a file never runs it.
import {
  composerInput,
  expect,
  pngBuffer,
  rectOf,
  recordMessages,
  send,
  test,
  uniq,
  type Page,
} from "./fixtures";

const DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
const MB = 1024 * 1024;

const pdf = () => ({ name: "Plan.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4\n".padEnd(1234, " ")) });
const docx = () => ({ name: "Trip notes.docx", mimeType: DOCX, buffer: Buffer.from("PK\u0003\u0004".padEnd(2048, "x")) });
// A type nobody has heard of: staged like any other, with no notice.
const numbers = () => ({ name: "Budget.numbers", mimeType: "", buffer: Buffer.from("not really numbers") });
const png = () => ({ name: "photo.png", mimeType: "image/png", buffer: pngBuffer(64, 48) });

/** Answers the send and the poll of its turn, so a file send ends as an
 * ordinary exchange without the server half. */
async function mockSend(page: Page): Promise<void> {
  await page.route("**/v1/message", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ turn_id: "turn-files" }) }),
  );
  await page.route("**/v1/turn/turn-files*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        turn_id: "turn-files",
        status: "done",
        result_text: "Got them.",
        activities: [],
        partial_text: "",
        partial_text_marks: [],
      }),
    }),
  );
}

test("CMP-5: a PDF, a docx and a .numbers file stage as chips beside the photos, three across, each removable", async ({
  page,
}) => {
  const input = page.getByTestId("attach-input");
  expect(await input.getAttribute("accept"), "no type filter on the file input").toBeNull();
  await input.setInputFiles([pdf(), docx(), numbers()]);

  const chips = page.getByTestId("file-chip");
  await expect(chips).toHaveCount(3);
  await expect(page.locator(".notice"), "an unfamiliar type is not refused").toHaveCount(0);
  const rects = await Promise.all([0, 1, 2].map((i) => rectOf(chips.nth(i))));
  for (const r of rects) {
    expect(r.width).toBe(64);
    expect(r.height).toBe(64);
  }
  expect(Math.abs(rects[0].top - rects[1].top)).toBeLessThan(1); // one row of three
  expect(rects[1].left).toBeGreaterThan(rects[0].right);
  await expect(chips.nth(0)).toContainText("Plan.pdf");
  // The name is cut in the middle, so the start and the extension both show.
  const long = await chips.nth(1).innerText();
  expect(long).toContain("…");
  expect(long.startsWith("Trip")).toBe(true);
  expect(long).toContain("docx");
  await expect(chips.nth(1)).toHaveAttribute("title", "Trip notes.docx");
  await expect(chips.nth(0).locator("svg"), "a type icon").toHaveCount(1);

  await chips.nth(2).getByRole("button", { name: "Remove Budget.numbers" }).click();
  await expect(chips).toHaveCount(2);
  await expect(page.locator("button.send")).toBeVisible(); // a file alone is sendable
});

test("CMP-5: photos and files share one six-item cap and the size limits are the client's to check", async ({
  page,
}) => {
  const input = page.getByTestId("attach-input");
  await input.setInputFiles([png(), pdf(), docx(), numbers()]);
  await expect(page.getByTestId("preview")).toHaveCount(1); // the PNG decoded: a photo, whichever input picked it
  await expect(page.getByTestId("file-chip")).toHaveCount(3);

  await input.setInputFiles([pdf(), docx(), numbers()]);
  await expect(page.getByTestId("preview-row").locator(".preview")).toHaveCount(6);
  await expect(page.locator(".notice")).toHaveText("Only the first 2 added: 6 items at most.");

  await input.setInputFiles([pdf()]);
  await expect(page.locator(".notice")).toHaveText("6 items at most.");
  await expect(page.getByTestId("preview-row").locator(".preview")).toHaveCount(6);

  // Remove one to make room, then a file over 20MB is refused with its own notice.
  await page.getByTestId("file-chip").first().getByRole("button").click();
  await input.setInputFiles({ name: "huge.bin", mimeType: "application/octet-stream", buffer: Buffer.alloc(20 * MB + 1) });
  await expect(page.locator(".notice")).toHaveText("That file is over 20MB.");
  await expect(page.getByTestId("preview-row").locator(".preview")).toHaveCount(5);
});

test("CMP-3: photos go as images[] and files as files[{b64, mime, name}] in one request; the bubble shows a chip per file", async ({
  page,
}) => {
  await mockSend(page);
  const bodies = recordMessages(page);
  const token = uniq("files");
  await page.getByTestId("attach-input").setInputFiles([png(), pdf(), docx(), numbers()]);
  await expect(page.getByTestId("file-chip")).toHaveCount(3);
  const user = await send(page, token);
  await expect(page.getByTestId("preview-row")).toHaveCount(0); // cleared in one step with the text

  await expect.poll(() => bodies.filter((b) => b.text === token).length).toBe(1);
  const [body] = bodies.filter((b) => b.text === token);
  expect(body.images, "the PNG went through the downscaler").toHaveLength(1);
  expect((body.images as { mime: string }[])[0].mime).toBe("image/jpeg");
  const files = body.files as { b64: string; mime: string; name: string }[];
  expect(files.map((f) => f.name)).toEqual(["Plan.pdf", "Trip notes.docx", "Budget.numbers"]);
  for (const f of files) expect(Object.keys(f).sort()).toEqual(["b64", "mime", "name"]);
  expect(files[0].mime).toBe("application/pdf");
  expect(files[1].mime).toBe(DOCX);
  expect(typeof files[2].mime, "the guess may be empty, but is a string").toBe("string");
  expect(Buffer.from(files[0].b64, "base64").toString()).toBe(pdf().buffer.toString());
  expect(Buffer.from(files[2].b64, "base64").toString()).toBe("not really numbers");

  // The bubble: the photo, then one chip row per file with its name and size.
  await expect(user.locator("img.bubble-image")).toBeVisible();
  const chips = user.getByTestId("bubble-file");
  await expect(chips).toHaveCount(3);
  await expect(chips.nth(0)).toContainText("Plan.pdf");
  await expect(chips.nth(0)).toContainText("1.2 KB");
  await expect(chips.nth(1)).toContainText("2 KB");
  await expect(chips.nth(2)).toContainText("Budget.numbers");
  const photo = await rectOf(user.locator("img.bubble-image"));
  const first = await rectOf(chips.nth(0));
  expect(first.top, "chips come after the photos").toBeGreaterThanOrEqual(photo.bottom - 1);
});

test("CMP-6: a retry after a refused send carries the same photos and files, from their saved copies", async ({
  page,
}) => {
  await page.route("**/v1/message", (route) =>
    route.fulfill({ status: 400, contentType: "application/json", body: JSON.stringify({ detail: "no" }) }),
  );
  await page.getByTestId("attach-input").setInputFiles([png(), pdf()]);
  await composerInput(page).fill("nope");
  await page.locator("button.send").click();
  // A 400 is not retried: the bubble shows its error (SND-8: the arrow beside
  // it), and Retry resends from what was saved when the message was sent.
  const arrow = page.locator(".row[data-role=user]").filter({ hasText: "nope" }).getByRole("button", { name: "Retry" });
  await expect(arrow).toBeVisible();
  const bodies = recordMessages(page);
  await arrow.click();
  await expect.poll(() => bodies.length).toBe(1);
  expect(bodies[0].images).toHaveLength(1);
  expect((bodies[0].files as { name: string }[]).map((f) => f.name)).toEqual(["Plan.pdf"]);
});

// --- opening a file from the transcript ---------------------------------------

const NOW = () => Math.floor(Date.now() / 1000);
const SCRIPT = "<script>window.__pwned=1</script>";

interface Att {
  id: string;
  mime: string;
  name: string | null;
  size: number | null;
  /** What the (mock) endpoint answers with. */
  serve: { contentType: string; body: string };
}

/** A transcript of one user row from "another device" with these attachments,
 * and its reply, and the attachment endpoint, recording the headers it saw. */
async function serveFiles(page: Page, atts: Att[]): Promise<{ auth: string[]; fetched: string[] }> {
  const seen = { auth: [] as string[], fetched: [] as string[] };
  const rows = [
    {
      seq: 1,
      id: "row-1",
      role: "user",
      source: "ios",
      turn_id: "tf",
      client_msg_id: null,
      text: "files from the phone",
      status: "done",
      created_at: NOW() - 60,
      answers: [],
      attachments: atts.map(({ id, mime, name, size }) => ({ id, mime, name, size })),
    },
    {
      seq: 2,
      id: "row-2",
      role: "assistant",
      source: "chat",
      turn_id: "tf-reply",
      client_msg_id: null,
      text: "Read them.",
      status: "done",
      created_at: NOW() - 50,
      answers: ["tf"],
      attachments: [],
    },
  ];
  await page.route("**/v1/transcript*", (route) => {
    const after = new URL(route.request().url()).searchParams.has("after");
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ rows: after ? [] : rows.slice().reverse(), latest_seq: 2, has_more: false }),
    });
  });
  await page.route("**/v1/transcript/attachment/*", (route) => {
    const id = decodeURIComponent(new URL(route.request().url()).pathname.split("/").pop()!);
    seen.auth.push(route.request().headers()["authorization"] ?? "");
    seen.fetched.push(id);
    const att = atts.find((a) => a.id === id);
    return att
      ? route.fulfill({ status: 200, contentType: att.serve.contentType, body: att.serve.body })
      : route.fulfill({ status: 404, body: "no" });
  });
  return seen;
}

const chip = (page: Page, name: string) => page.getByTestId("bubble-file").filter({ hasText: name });

/** No page of the context ran the script. */
async function expectNothingRan(page: Page): Promise<void> {
  for (const p of page.context().pages()) {
    if (p.isClosed()) continue;
    expect(await p.evaluate(() => (window as unknown as { __pwned?: number }).__pwned), p.url()).toBeUndefined();
  }
}

test("a file from another device draws as a chip and is not fetched until it is tapped", async ({ newDevice }) => {
  const atts: Att[] = [
    { id: "f-pdf", mime: "application/pdf", name: "Plan.pdf", size: 5 * MB, serve: { contentType: "application/pdf", body: "%PDF-1.4" } },
    { id: "f-old", mime: "application/pdf", name: null, size: null, serve: { contentType: "application/pdf", body: "%PDF-1.4" } },
  ];
  let seen!: Awaited<ReturnType<typeof serveFiles>>;
  const { page } = await newDevice({ setup: async (p) => void (seen = await serveFiles(p, atts)) });
  const user = page.locator(".row[data-role=user]").filter({ hasText: "files from the phone" });
  const chips = user.getByTestId("bubble-file");
  await expect(chips).toHaveCount(2);
  await expect(chips.nth(0)).toContainText("Plan.pdf");
  await expect(chips.nth(0)).toContainText("5 MB");
  await expect(chips.nth(1), "an old row has no name or size").toContainText("File");
  await expect(chips.nth(0)).toBeEnabled();
  expect(seen.fetched, "files are fetched on tap, not to draw the bubble").toEqual([]);
});

test("opening a text file shows it as text from a blob, fetched with the bearer header, and never runs it", async ({
  newDevice,
}) => {
  // The endpoint serves it as text/plain, as the server does, and the row's mime says text/plain.
  const atts: Att[] = [
    { id: "f-html", mime: "text/plain", name: "page.html", size: SCRIPT.length, serve: { contentType: "text/plain", body: SCRIPT } },
  ];
  let seen!: Awaited<ReturnType<typeof serveFiles>>;
  const { page, ctx } = await newDevice({ setup: async (p) => void (seen = await serveFiles(p, atts)) });
  const opened = ctx.waitForEvent("page");
  await chip(page, "page.html").click();
  const tab = await opened;
  await tab.waitForURL(/^blob:/);
  expect(seen.auth[0], "the token goes in the header, never in a URL").toMatch(/^Bearer .+/);
  expect(await tab.evaluate(() => document.contentType)).toBe("text/plain");
  expect(await tab.evaluate(() => document.body.innerText)).toContain(SCRIPT);
  await expectNothingRan(page);
  // And it is not the app's page that opened it for itself.
  expect(tab.url()).not.toContain("Bearer");
});

test("a row mis-recorded as text/html is downloaded, not opened, and nothing runs", async ({ newDevice }) => {
  const atts: Att[] = [
    // Even if the endpoint says text/plain, the row's mime decides, and html is never opened.
    { id: "f-evil", mime: "text/html", name: "evil.html", size: SCRIPT.length, serve: { contentType: "text/plain", body: SCRIPT } },
    // And a hostile Content-Type does not change a safe row: this opens as the row says.
    { id: "f-svg", mime: "image/svg+xml", name: "pic.svg", size: 10, serve: { contentType: "image/svg+xml", body: `<svg xmlns="http://www.w3.org/2000/svg"><script>window.__pwned=1</script></svg>` } },
  ];
  const { page, ctx } = await newDevice({ setup: (p) => serveFiles(p, atts).then(() => {}) });
  const pagesBefore = ctx.pages().length;

  const dl = page.waitForEvent("download");
  await chip(page, "evil.html").click();
  const saved = await dl;
  expect(saved.suggestedFilename()).toBe("evil.html");
  expect(ctx.pages().length, "no tab was opened for it").toBe(pagesBefore);

  const dl2 = page.waitForEvent("download");
  await chip(page, "pic.svg").click();
  expect((await dl2).suggestedFilename()).toBe("pic.svg");
  expect(ctx.pages().length).toBe(pagesBefore);
  await expectNothingRan(page);
});

test("a docx and a PDF download under their own names; a text file opens in a tab", async ({ newDevice }) => {
  const atts: Att[] = [
    { id: "f-docx", mime: DOCX, name: "Trip notes.docx", size: 20, serve: { contentType: DOCX, body: "PK" } },
    { id: "f-pdf", mime: "application/pdf", name: "Plan.pdf", size: 20, serve: { contentType: "application/pdf", body: "%PDF-1.4" } },
    { id: "f-txt", mime: "text/plain", name: "notes.txt", size: 20, serve: { contentType: "text/html", body: "plain words" } },
  ];
  const { page, ctx } = await newDevice({ setup: (p) => serveFiles(p, atts).then(() => {}) });
  const dl = page.waitForEvent("download");
  await chip(page, "Trip notes.docx").click();
  expect((await dl).suggestedFilename()).toBe("Trip notes.docx");
  const pdfDl = page.waitForEvent("download");
  await chip(page, "Plan.pdf").click();
  expect((await pdfDl).suggestedFilename()).toBe("Plan.pdf");

  const opened = ctx.waitForEvent("page");
  await chip(page, "notes.txt").click();
  const tab = await opened;
  // Whatever the endpoint's Content-Type said (text/html here), it is a blob,
  // not a page: the tab's address is the blob and the app's own page is not it.
  await tab.waitForURL(/^blob:/);
  await expectNothingRan(page);
});

// --- Android's share target ----------------------------------------------------

/** Writes a share entry the way the service worker does (lib/shareTarget.ts). */
function seedShare(
  page: Page,
  entry: { id: string; title: string; text: string; url: string; files: { name: string; type: string; body: string }[] },
): Promise<void> {
  return page.evaluate(async (e) => {
    const db = await new Promise<IDBDatabase>((resolve, reject) => {
      const req = indexedDB.open("marlow-share", 1);
      req.onupgradeneeded = () => req.result.createObjectStore("shares", { keyPath: "id" });
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction("shares", "readwrite");
      tx.objectStore("shares").put({
        id: e.id,
        createdAt: Date.now(),
        title: e.title,
        text: e.text,
        url: e.url,
        files: e.files.map((f) => new File([f.body], f.name, { type: f.type })),
      });
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
    db.close();
  }, entry);
}

const shareLeft = (page: Page): Promise<number> =>
  page.evaluate(
    () =>
      new Promise<number>((resolve, reject) => {
        const req = indexedDB.open("marlow-share", 1);
        req.onsuccess = () => {
          const db = req.result;
          const count = db.transaction("shares").objectStore("shares").count();
          count.onsuccess = () => {
            db.close();
            resolve(count.result);
          };
        };
        req.onerror = () => reject(req.error);
      }),
  );

test("Phase 4: /?share=<id> stages the shared files and fills the caption, never sends, and cleans the URL", async ({
  page,
}) => {
  const bodies = recordMessages(page);
  await seedShare(page, {
    id: "share-1",
    title: "A page",
    text: "Worth a look",
    url: "https://example.com/a",
    files: [
      { name: "Plan.pdf", type: "application/pdf", body: "%PDF-1.4" },
      { name: "Budget.numbers", type: "", body: "x" },
    ],
  });
  await page.goto("/?share=share-1");
  await expect(page.getByTestId("file-chip")).toHaveCount(2);
  await expect(page.getByTestId("file-chip").nth(0)).toContainText("Plan.pdf");
  await expect(composerInput(page)).toHaveValue("A page\nWorth a look\nhttps://example.com/a");
  await expect.poll(() => new URL(page.url()).searchParams.has("share")).toBe(false);
  expect(new URL(page.url()).pathname).toBe("/");
  expect(await shareLeft(page), "the entry is deleted once taken").toBe(0);
  expect(bodies, "never auto-sent").toEqual([]);

  // A reload does not stage it again.
  await page.reload();
  await expect(composerInput(page)).toBeVisible();
  await expect(page.getByTestId("file-chip")).toHaveCount(0);
});

test("Phase 4: a share goes through the same limits and notices as a pick", async ({ page }) => {
  await seedShare(page, {
    id: "share-many",
    title: "",
    text: "",
    url: "",
    files: Array.from({ length: 7 }, (_, i) => ({ name: `f${i}.txt`, type: "text/plain", body: "x" })),
  });
  await page.goto("/?share=share-many");
  await expect(page.getByTestId("file-chip")).toHaveCount(6);
  await expect(page.locator(".notice")).toHaveText("Only the first 6 added: 6 items at most.");
});

test("Phase 4: an unknown share id says so and still cleans the URL", async ({ page }) => {
  await page.goto("/?share=gone");
  await expect(page.locator(".notice")).toHaveText("Couldn't find what you shared.");
  await expect.poll(() => new URL(page.url()).searchParams.has("share")).toBe(false);
});

test("Phase 4: the service worker answers the share-target POST with a 303 to the app with the files staged", async ({
  page,
}) => {
  // The real handler, in Chromium: wait for the worker, then submit a form the
  // way the share sheet's POST arrives (multipart, a files field and text).
  await page.evaluate(async () => {
    await navigator.serviceWorker.ready;
  });
  await page.evaluate(() => {
    const form = document.createElement("form");
    form.method = "POST";
    form.action = "/share-target";
    form.enctype = "multipart/form-data";
    form.hidden = true;
    for (const [name, value] of [
      ["title", "Shared title"],
      ["text", "Shared text"],
      ["url", ""],
    ]) {
      const i = document.createElement("input");
      i.name = name;
      i.value = value;
      form.appendChild(i);
    }
    const files = document.createElement("input");
    files.type = "file";
    files.name = "files";
    files.multiple = true;
    const dt = new DataTransfer();
    dt.items.add(new File(["%PDF-1.4"], "Shared.pdf", { type: "application/pdf" }));
    dt.items.add(new File(["x"], "Thing.numbers", { type: "" }));
    files.files = dt.files;
    form.appendChild(files);
    document.body.appendChild(form);
    form.submit();
  });
  await expect(page.getByTestId("file-chip")).toHaveCount(2);
  await expect(composerInput(page)).toHaveValue("Shared title\nShared text");
  expect(new URL(page.url()).searchParams.has("share")).toBe(false);
});
