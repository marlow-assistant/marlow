// "Read aloud" on a reply's long-press menu (docs/tasks/read-aloud.md), in a
// real Chromium against the dev server. speechSynthesis is a recorder that
// stays "speaking" until stopped, so the pill can be seen and tapped;
// Phonon's model is unreachable, as in voice.spec.ts, so the browser's voice
// reads. Nothing here hears audio.
import { exchange, expect, test, type Page } from "./fixtures";

async function fakeSpeech(page: Page): Promise<void> {
  await page.context().route("https://huggingface.co/**", (route) => route.abort());
  await page.addInitScript(() => {
    const w = window as unknown as { __spoken: string[]; __cancels: number };
    w.__spoken = [];
    w.__cancels = 0;
    const fake = {
      speaking: true,
      pending: false,
      getVoices: () => [],
      addEventListener: () => {},
      removeEventListener: () => {},
      cancel: () => {
        w.__cancels += 1;
      },
      speak(u: SpeechSynthesisUtterance) {
        if (u.text) w.__spoken.push(u.text);
      },
    };
    Object.defineProperty(window, "speechSynthesis", { value: fake, configurable: true });
  });
}

test("holding a reply offers Read aloud; the pill stops it", async ({ newDevice }) => {
  const { page } = await newDevice({ setup: fakeSpeech });
  const { reply } = await exchange(page);
  const bubble = reply.locator(".bubble");
  const menu = page.getByTestId("copy-menu");
  const spoken = () => page.evaluate(() => (window as unknown as { __spoken: string[] }).__spoken);
  const cancels = () => page.evaluate(() => (window as unknown as { __cancels: number }).__cancels);

  await bubble.click({ button: "right" });
  await expect(menu.getByRole("menuitem")).toHaveText(["Copy", "Read aloud"]);
  const box = (await menu.boundingBox())!;
  expect(box.y).toBeGreaterThanOrEqual(0);
  expect(box.y + box.height).toBeLessThanOrEqual(page.viewportSize()!.height);
  await menu.getByRole("menuitem", { name: "Read aloud" }).click();
  await expect(menu).toHaveCount(0);

  const pill = page.getByTestId("reading-pill");
  await expect(pill).toBeVisible();
  await expect.poll(spoken).not.toEqual([]);
  const said = (await spoken()).join(" ");
  expect((await bubble.innerText()).replace(/\s+/g, " ")).toContain(said.split(" ").slice(0, 3).join(" "));

  // The menu on the message being read stops it. Held at its top corner:
  // the pill floats over the bottom of the transcript, as the jump button
  // does, and can sit on a short last reply.
  await bubble.click({ button: "right", position: { x: 8, y: 8 } });
  await expect(menu.getByRole("menuitem").nth(1)).toHaveText("Stop reading");
  await page.keyboard.press("Escape");

  const before = await cancels();
  await pill.click();
  await expect(pill).toHaveCount(0);
  expect(await cancels()).toBeGreaterThan(before);
});
