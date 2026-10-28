// The face and the Thinking level, the same on every client: the box holds
// them (server/preferences.py; iOS `PreferenceSync`). Until 2026-10-16 both
// were per device, and web and iOS showed different Marlows that thought
// differently (Mike).
//
// IndexedDB stays the copy the page and the service worker read, so a pick
// shows at once, nothing waits on the network, and an offline open still has
// both.
//
// A pick is marked pending until the box has it, and a pull never runs over a
// pending pick: otherwise a pick made offline would be put back to the box's
// older value the next time the tab is shown. A box with nothing yet takes
// this browser's value, so clients that chose before sync existed agree from
// then on.

import type { Effort } from "./chatStore";
import { parseFace, type Face } from "./faces";
import { KV_EFFORT, KV_FACE } from "./storage";

export type Item = "face" | "effort";
const ITEMS: Item[] = ["face", "effort"];
const STORAGE: Record<Item, string> = { face: KV_FACE, effort: KV_EFFORT };
export const pendingKey = (item: Item) => `${STORAGE[item]}Pending`;

export interface Remote {
  face: number | null;
  effort: string | null;
}

export interface PreferenceSyncDeps {
  read: (key: string) => Promise<unknown>;
  write: (key: string, value: unknown) => Promise<void>;
  remove: (key: string) => Promise<void>;
  api: {
    preferences(): Promise<Remote>;
    setPreferences(update: Partial<Record<Item, number | string>>): Promise<void>;
  };
}

/** After a picker has stored its value: hold it, then send. */
export async function picked(item: Item, deps: PreferenceSyncDeps): Promise<void> {
  await deps.write(pendingKey(item), true);
  await sendPending(deps);
}

/** Sends whatever is pending, in one call. An item stays pending if the box
 * can't be reached, or if it was picked again while this was in flight. */
export async function sendPending(deps: PreferenceSyncDeps): Promise<void> {
  const update: Partial<Record<Item, number | string>> = {};
  for (const item of ITEMS) {
    if (!(await deps.read(pendingKey(item)))) continue;
    const value = await deps.read(STORAGE[item]);
    if (typeof value === "number" || typeof value === "string") update[item] = value;
  }
  const sent = Object.keys(update) as Item[];
  if (sent.length === 0) return;
  try {
    await deps.api.setPreferences(update);
  } catch {
    return;
  }
  for (const item of sent) {
    if ((await deps.read(STORAGE[item])) === update[item]) await deps.remove(pendingKey(item));
  }
}

export interface Pulled {
  face?: Face;
  effort?: Effort;
}

/** On open and whenever the tab is shown. What changed, to show. */
export async function pull(deps: PreferenceSyncDeps): Promise<Pulled> {
  await sendPending(deps);
  let remote: Remote;
  try {
    remote = await deps.api.preferences();
  } catch {
    return {};
  }
  const changed: Pulled = {};
  let seeded = false;
  for (const item of ITEMS) {
    // A pick made during the fetch, or not yet sent, wins.
    if (await deps.read(pendingKey(item))) continue;
    const value = remote[item];
    const local = await deps.read(STORAGE[item]);
    if (value === null) {
      if (local !== undefined) {
        await deps.write(pendingKey(item), true);
        seeded = true;
      }
      continue;
    }
    if (local === value) continue;
    // The raw value: one this build doesn't know shows the default here but
    // stays the choice for the other clients.
    await deps.write(STORAGE[item], value);
    if (item === "face") changed.face = parseFace(value);
    else if (value === "low" || value === "medium" || value === "high") changed.effort = value;
  }
  if (seeded) await sendPending(deps);
  return changed;
}
