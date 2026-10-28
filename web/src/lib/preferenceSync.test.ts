import { describe, expect, it } from "vitest";
import { pendingKey, picked, pull, sendPending, type PreferenceSyncDeps } from "./preferenceSync";
import { KV_EFFORT, KV_FACE } from "./storage";

function fakes(face: number | null, effort: string | null) {
  const kv = new Map<string, unknown>();
  const box = { face, effort, reachable: true, sent: [] as Record<string, unknown>[] };
  const deps: PreferenceSyncDeps = {
    read: async (k) => kv.get(k),
    write: async (k, v) => void kv.set(k, v),
    remove: async (k) => void kv.delete(k),
    api: {
      preferences: async () => {
        if (!box.reachable) throw new Error("offline");
        return { face: box.face, effort: box.effort };
      },
      setPreferences: async (update) => {
        if (!box.reachable) throw new Error("offline");
        box.sent.push(update);
        if (typeof update.face === "number") box.face = update.face;
        if (typeof update.effort === "string") box.effort = update.effort;
      },
    },
  };
  return { kv, box, deps };
}

describe("preference sync", () => {
  it("takes up choices made on another client, once", async () => {
    const { kv, deps } = fakes(7, "high");
    const changed = await pull(deps);
    expect(changed.face?.id).toBe(7);
    expect(changed.effort).toBe("high");
    expect(kv.get(KV_FACE)).toBe(7);
    expect(kv.get(KV_EFFORT)).toBe("high");
    expect(await pull(deps)).toEqual({});
  });

  it("sends a pick and then no longer holds it as pending", async () => {
    const { kv, box, deps } = fakes(2, "low");
    kv.set(KV_FACE, 9);
    await picked("face", deps);
    expect(box.sent).toEqual([{ face: 9 }]);
    expect(kv.has(pendingKey("face"))).toBe(false);
    expect(box.effort).toBe("low");
  });

  // The bug this guards: a pick made offline, then the box's older value
  // put back over it the next time the tab is shown.
  it("sends an unsent pick on the next pull rather than overwriting it", async () => {
    const { kv, box, deps } = fakes(2, "low");
    box.reachable = false;
    kv.set(KV_EFFORT, "high");
    await picked("effort", deps);
    expect(kv.get(pendingKey("effort"))).toBe(true);

    box.reachable = true;
    const changed = await pull(deps);
    expect(changed.effort).toBeUndefined();
    expect(kv.get(KV_EFFORT)).toBe("high");
    expect(box.effort).toBe("high");
    expect(kv.has(pendingKey("effort"))).toBe(false);
  });

  // Clients that chose before sync existed: the first to reach an empty box
  // sets it, and the rest take that up.
  it("an empty box takes this browser's choice", async () => {
    const { kv, box, deps } = fakes(null, null);
    kv.set(KV_FACE, 4);
    expect(await pull(deps)).toEqual({});
    expect(box.sent).toEqual([{ face: 4 }]);
    expect(box.effort).toBeNull();
  });

  it("an empty box and no choice send nothing", async () => {
    const { box, deps } = fakes(null, null);
    expect(await pull(deps)).toEqual({});
    expect(box.sent).toEqual([]);
  });

  it("keeps values this build doesn't know as the choice", async () => {
    const { kv, deps } = fakes(42, "max");
    const changed = await pull(deps);
    expect(changed.face?.id).toBe(1);
    expect(changed.effort).toBeUndefined();
    expect(kv.get(KV_FACE)).toBe(42);
    expect(kv.get(KV_EFFORT)).toBe("max");
  });

  it("an unreachable box changes nothing", async () => {
    const { kv, box, deps } = fakes(7, "high");
    box.reachable = false;
    kv.set(KV_FACE, 3);
    expect(await pull(deps)).toEqual({});
    await sendPending(deps);
    expect(kv.get(KV_FACE)).toBe(3);
  });
});
