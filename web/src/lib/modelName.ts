// Port of ios ThinkingEffort.swift's `String.capitalizedModelName`.

const capitalized = (s: string): string => (s ? s[0].toUpperCase() + s.slice(1).toLowerCase() : s);

/** `"claude-sonnet-5"` -> `"Sonnet"`, `"sonnet"` -> `"Sonnet"`. The server pins
 * the `sonnet` *alias* rather than a version string, so what arrives is usually
 * already short; this also handles the full name the CLI records, since
 * `/health` reports whatever `settings.model` holds.
 *
 * Falls back to the whole string untouched rather than guessing, so an
 * unfamiliar model name shows up as itself instead of being mangled into
 * something that reads authoritative and is wrong. */
export function capitalizedModelName(model: string): string {
  const parts = model.split("-").filter((p) => p !== "");
  // The tier is the segment after a leading "claude", when there is one.
  if (parts.length >= 2 && parts[0].toLowerCase() === "claude") return capitalized(parts[1]);
  if (parts.length === 1) return capitalized(parts[0]);
  return model;
}
