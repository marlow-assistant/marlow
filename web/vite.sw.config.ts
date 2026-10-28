import { defineConfig } from "vite";

// The service worker, built on its own as one classic script. A worker
// registered as a module would share chunks with the app, and Firefox cannot
// load a module service worker; a self-contained IIFE works everywhere. It
// lives at a stable root path so its scope is the whole app and the server
// serves it no-cache. Runs after the main build, into the same directory.
export default defineConfig({
  build: {
    outDir: process.env.MARLOW_WEB_OUT_DIR ?? "dist",
    emptyOutDir: false,
    copyPublicDir: false,
    lib: {
      entry: "src/sw.ts",
      formats: ["iife"],
      name: "marlowServiceWorker",
      fileName: () => "sw.js",
    },
  },
});
