// Web dictation and voice mode (docs/tasks/voice-mode-android.md), in a real
// Chromium against the dev server. The microphone is Chromium's fake device
// (a tone, playwright.config.ts), so what is recorded is real WebM/Opus from
// MediaRecorder; transcription is answered here at the route, since the dev
// server has no model; speechSynthesis is replaced by a recorder of what would
// be said, since headless Chromium has no voices. Nothing here hears audio.
import { composerInput, expect, test, type Page } from "./fixtures";

interface Upload {
  seq: number;
  final: boolean;
  type: string;
  bytes: number;
  head: string;
}

/** Answers /v1/transcribe with `heard`, and records every upload. */
async function fakeTranscription(page: Page, heard: string): Promise<Upload[]> {
  const uploads: Upload[] = [];
  await page.route("**/v1/transcribe**", async (route) => {
    const req = route.request();
    if (req.method() === "GET") return route.fulfill({ json: { available: true } });
    const url = new URL(req.url());
    const body = req.postDataBuffer() ?? Buffer.alloc(0);
    uploads.push({
      seq: Number(url.searchParams.get("seq")),
      final: url.searchParams.get("final") === "true",
      type: req.headers()["content-type"] ?? "",
      bytes: body.length,
      head: body.subarray(0, 4).toString("hex"),
    });
    return route.fulfill({ json: { seq: 0, text: heard, final: true, transcript: heard } });
  });
  await page.addInitScript(() => {
    const w = window as unknown as { __spoken: string[] };
    w.__spoken = [];
    const fake = {
      speaking: false,
      pending: false,
      getVoices: () => [],
      addEventListener: () => {},
      removeEventListener: () => {},
      cancel: () => {},
      speak(u: SpeechSynthesisUtterance) {
        w.__spoken.push(u.text);
        setTimeout(() => u.onend?.(new Event("end") as SpeechSynthesisEvent), 50);
      },
    };
    Object.defineProperty(window, "speechSynthesis", { value: fake, configurable: true });
  });
  return uploads;
}

function messagePosts(page: Page): Array<Record<string, unknown>> {
  const posts: Array<Record<string, unknown>> = [];
  page.on("request", (r) => {
    if (r.method() === "POST" && new URL(r.url()).pathname === "/v1/message") posts.push(r.postDataJSON());
  });
  return posts;
}

const bigButton = (page: Page) => page.locator(".voice-button");

test("voice mode: talk, send as spoken, hear the reply, and every turn is in the chat", async ({ newDevice }) => {
  let uploads: Upload[] = [];
  const heard = `what is on today ${Date.now().toString(36)}`;
  const { page } = await newDevice({
    setup: async (p) => {
      uploads = await fakeTranscription(p, heard);
    },
  });
  const posts = messagePosts(page);

  await page.getByTestId("voice-open").click();
  await expect(page.getByTestId("voice-mode")).toBeVisible();
  await expect(bigButton(page)).toHaveAttribute("data-phase", "idle");

  await bigButton(page).click();
  await expect(bigButton(page)).toHaveAttribute("data-phase", "recording");
  await page.waitForTimeout(1_500);
  await bigButton(page).click();

  // Sent spoken, and Marlow's reply read out sentence by sentence.
  await expect.poll(() => posts.length).toBe(1);
  expect(posts[0]).toMatchObject({ text: heard, spoken: true });
  await expect
    .poll(() => page.evaluate(() => (window as unknown as { __spoken: string[] }).__spoken), { timeout: 30_000 })
    .toContain("This reply streamed in small deltas, as a real one does.");
  await expect(bigButton(page)).toHaveAttribute("data-phase", "idle", { timeout: 30_000 });

  // One upload: a real WebM file (EBML magic) from MediaRecorder, as Opus.
  expect(uploads).toHaveLength(1);
  expect(uploads[0]).toMatchObject({ seq: 0, final: true, head: "1a45dfa3" });
  expect(uploads[0].type).toMatch(/^audio\/webm;\s*codecs=opus$/);
  expect(uploads[0].bytes).toBeGreaterThan(1_000);

  // The debug report: the cut, the timings and the voice, never the words.
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.getByTestId("voice-debug").click();
  await expect(page.getByTestId("voice-debug")).toHaveText("Copied");
  const report = await page.evaluate(() => navigator.clipboard.readText());
  expect(report).toContain("Marlow voice mode debug");
  expect(report).toMatch(/seg 0: \d+\.\d s, cut: release/);
  expect(report).toMatch(/release -> text: \d+ ms \(\d+ words\)/);
  expect(report).toMatch(/release -> first speech: \d+ ms/);
  expect(report).not.toContain(heard);

  // Leaving puts the user back in the chat, where the turn is an ordinary bubble.
  await page.getByRole("button", { name: "Leave voice mode" }).click();
  await expect(page.getByTestId("voice-mode")).toHaveCount(0);
  await expect(page.locator(".row[data-role=user]").filter({ hasText: heard })).toBeVisible();
});

test("voice mode: leaving mid-recording sends nothing", async ({ newDevice }) => {
  let uploads: Upload[] = [];
  const { page } = await newDevice({
    setup: async (p) => {
      uploads = await fakeTranscription(p, "never sent");
    },
  });
  const posts = messagePosts(page);
  await page.getByTestId("voice-open").click();
  await bigButton(page).click();
  await expect(bigButton(page)).toHaveAttribute("data-phase", "recording");
  await page.waitForTimeout(800);
  await page.getByRole("button", { name: "Leave voice mode" }).click();
  await page.waitForTimeout(1_500);
  expect(uploads).toHaveLength(0);
  expect(posts).toHaveLength(0);
});

test("dictation: the mic types what was heard into the draft, and sends nothing itself", async ({ newDevice }) => {
  const heard = "pick up oat milk";
  const { page } = await newDevice({
    setup: async (p) => {
      await fakeTranscription(p, heard);
    },
  });
  const posts = messagePosts(page);
  await composerInput(page).fill("Reminder:");
  const mic = page.getByTestId("mic");
  await mic.click();
  await expect(mic).toHaveAttribute("aria-pressed", "true");
  await page.waitForTimeout(1_000);
  await mic.click();
  await expect(composerInput(page)).toHaveValue(`Reminder: ${heard}`);
  expect(posts).toHaveLength(0);
});

test("no mic and no voice mode when the box can't transcribe", async ({ page }) => {
  // The dev server has no model: GET /v1/transcribe says so.
  await expect(composerInput(page)).toBeVisible();
  await page.waitForTimeout(500);
  await expect(page.getByTestId("mic")).toHaveCount(0);
  await expect(page.getByTestId("voice-open")).toHaveCount(0);
});
