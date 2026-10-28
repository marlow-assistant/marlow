// End-to-end suite for the Marlow web client (docs/chat-ux-contract.md,
// section 15: every [test] rule, geometry not screenshots).
//
//   cd web
//   npx playwright test                                  # Mike's Mac: Playwright's own Chromium
//   PW_CHROMIUM_PATH=/opt/pw-browsers/chromium npx playwright test   # the dev container
//
// `webServer` builds the client and starts scripts/dev-server.sh --reset: the
// real marlow server serving web/dist with the production CSP on
// http://127.0.0.1:8080 (admin on the Unix socket .dev/admin/admin.sock), with scripts/dev/fake_claude.py
// standing in for the CLI. No model is called. --reset wipes .dev/ (the dev
// sandbox's pairings and transcript), so do not run this against a sandbox you
// care about. The helpers assume the default port and socket path.
//
// One worker, no parallelism: the server has ONE work queue and the whole
// suite shares one conversation, so tests wait for their own replies and
// target the rows they created (by text) rather than counting.
import { defineConfig, devices } from "@playwright/test";

const executablePath = process.env.PW_CHROMIUM_PATH || undefined;
// A fake microphone (a tone) that is granted without a prompt, for the voice
// tests (e2e/voice.spec.ts). Harmless to every other test.
const args = ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"];
// Without a path, `channel: "chromium"` runs Playwright's full Chromium in new
// headless mode, not its default headless shell: the shell reports
// Notification.permission as "denied", so WEB-9 rendered its blocked card and
// failed on Mike's Mac while passing in the container (2026-10-02).
const launchOptions = executablePath ? { executablePath, args } : { channel: "chromium", args };

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  // `#long` replies take ~15s, and a permission prompt waits on a poll loop.
  timeout: 90_000,
  expect: { timeout: 10_000 },
  reporter: [["list"]],
  use: {
    baseURL: "http://127.0.0.1:8080",
    trace: "retain-on-failure",
    launchOptions,
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"], launchOptions } },
    // Pixel 7 emulation on Chromium: viewport, touch and DPR, NOT an Android
    // browser with a keyboard up. [device] rules are not verified by it.
    { name: "pixel7", use: { ...devices["Pixel 7"], launchOptions } },
  ],
  webServer: {
    // Build first so the suite always tests the current source, then serve it.
    command: "npm run build && ../scripts/dev-server.sh --reset",
    url: "http://127.0.0.1:8080/health",
    reuseExistingServer: false,
    timeout: 180_000,
    stdout: "ignore",
    stderr: "ignore",
  },
});
