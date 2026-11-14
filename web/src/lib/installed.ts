/** Whether this page is running as an installed app (the home-screen icon)
 * rather than in a browser tab. Android lists only an installed app in the
 * share sheet (docs/tasks/share-and-files.md, Phase 4), so Settings says how
 * to get there when this is false. `navigator.standalone` is iOS Safari's own
 * flag for the same thing. */
export function isInstalledApp(): boolean {
  if (typeof matchMedia === "function" && matchMedia("(display-mode: standalone)").matches) return true;
  return typeof navigator !== "undefined" && (navigator as Navigator & { standalone?: boolean }).standalone === true;
}
