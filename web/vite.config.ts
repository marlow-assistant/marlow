/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { execSync } from "node:child_process";

// Shown in Settings next to the server's build, so "do these two agree?" is
// answerable. Never fails the build: no git (a tarball, a container) is "dev".
function webBuild(): string {
  try {
    const out = execSync("git rev-parse --short HEAD", { stdio: ["ignore", "pipe", "ignore"] }).toString().trim();
    return out || "dev";
  } catch {
    return "dev";
  }
}

// Same-origin in development as in production: the dev server proxies the API
// to scripts/dev-server.sh rather than the client knowing a server URL.
const api = process.env.MARLOW_DEV_API ?? "http://127.0.0.1:8080";

export default defineConfig({
  plugins: [react()],
  define: { __WEB_BUILD__: JSON.stringify(webBuild()) },
  build: {
    outDir: process.env.MARLOW_WEB_OUT_DIR ?? "dist",
    emptyOutDir: true,
    // No inline <script> or data: module preload polyfill: the CSP is
    // script-src 'self' with nothing else.
    modulePreload: { polyfill: false },
    assetsInlineLimit: 0,
    rollupOptions: {
      input: { main: "index.html" },
    },
  },
  server: {
    proxy: {
      "/v1": api,
      "/health": api,
    },
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
  },
});
