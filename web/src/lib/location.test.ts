import { afterEach, describe, expect, it, vi } from "vitest";
import "fake-indexeddb/auto";
import {
  APPROXIMATE_FLOOR_M,
  LocationWatcher,
  parseLocationSharing,
  toOutgoing,
  type GeolocationLike,
  type LocationFix,
} from "./location";
import { kvGet, kvSet, KV_LOCATION, resetDbForTests } from "./storage";

const NOW = 1_800_000_000_000;
const MIN = 60_000;
const fix = (over: Partial<LocationFix> = {}): LocationFix => ({
  latitude: 46.81,
  longitude: -71.21,
  accuracy: 65,
  timestamp: NOW,
  ...over,
});

describe("toOutgoing", () => {
  it("Off sends nothing, however fresh the fix", () => {
    expect(toOutgoing(fix(), "off", NOW)).toBeUndefined();
  });

  it("no fix sends nothing", () => {
    expect(toOutgoing(undefined, "precise", NOW)).toBeUndefined();
    expect(toOutgoing(undefined, "approximate", NOW)).toBeUndefined();
  });

  it("Precise passes the values and the reported accuracy through untouched", () => {
    expect(toOutgoing(fix(), "precise", NOW)).toEqual({
      latitude: 46.81,
      longitude: -71.21,
      accuracy_m: 65,
    });
  });

  it("Precise leaves accuracy out when the browser gave none usable, rather than sending 0", () => {
    const out = toOutgoing(fix({ accuracy: 0 }), "precise", NOW)!;
    expect(out).not.toHaveProperty("accuracy_m");
    expect(toOutgoing(fix({ accuracy: NaN }), "precise", NOW)).not.toHaveProperty("accuracy_m");
  });

  it("Approximate rounds to 2 decimal places, so the precise value is not in the payload", () => {
    const out = toOutgoing(fix({ latitude: 46.814949, longitude: -71.213838 }), "approximate", NOW)!;
    expect(out.latitude).toBe(46.81);
    expect(out.longitude).toBe(-71.21);
    expect(JSON.stringify(out)).not.toContain("46.8149");
    expect(JSON.stringify(out)).not.toContain("71.2138");
  });

  it("Approximate rounds half away correctly on both signs and never yields -0", () => {
    expect(toOutgoing(fix({ latitude: 46.816, longitude: -71.216 }), "approximate", NOW)).toMatchObject({
      latitude: 46.82,
      longitude: -71.22,
    });
    const tiny = toOutgoing(fix({ latitude: -0.004, longitude: 0.001 }), "approximate", NOW)!;
    expect(Object.is(tiny.latitude, 0)).toBe(true);
    expect(Object.is(tiny.longitude, 0)).toBe(true);
  });

  it("Approximate floors accuracy at 1000 m, and keeps a coarser one", () => {
    expect(toOutgoing(fix({ accuracy: 12 }), "approximate", NOW)!.accuracy_m).toBe(APPROXIMATE_FLOOR_M);
    expect(toOutgoing(fix({ accuracy: 999 }), "approximate", NOW)!.accuracy_m).toBe(1000);
    expect(toOutgoing(fix({ accuracy: 4200 }), "approximate", NOW)!.accuracy_m).toBe(4200);
    expect(toOutgoing(fix({ accuracy: NaN }), "approximate", NOW)!.accuracy_m).toBe(1000);
  });

  it("sends a 14-minute-old fix and nothing for a 16-minute-old one; 15 is the edge and still fresh", () => {
    expect(toOutgoing(fix({ timestamp: NOW - 14 * MIN }), "precise", NOW)).toBeDefined();
    expect(toOutgoing(fix({ timestamp: NOW - 15 * MIN }), "precise", NOW)).toBeDefined();
    expect(toOutgoing(fix({ timestamp: NOW - 16 * MIN }), "precise", NOW)).toBeUndefined();
    expect(toOutgoing(fix({ timestamp: NOW - 16 * MIN }), "approximate", NOW)).toBeUndefined();
  });

  it("drops a fix with a non-finite coordinate rather than send the server something it will reject", () => {
    expect(toOutgoing(fix({ latitude: NaN }), "precise", NOW)).toBeUndefined();
    expect(toOutgoing(fix({ longitude: Infinity }), "approximate", NOW)).toBeUndefined();
  });
});

describe("stored setting", () => {
  afterEach(() => resetDbForTests());

  it("an unknown or missing stored value is Off", () => {
    expect(parseLocationSharing(undefined)).toBe("off");
    expect(parseLocationSharing("everywhere")).toBe("off");
    expect(parseLocationSharing(3)).toBe("off");
    expect(parseLocationSharing("approximate")).toBe("approximate");
    expect(parseLocationSharing("precise")).toBe("precise");
    expect(parseLocationSharing("off")).toBe("off");
  });

  it("round-trips through the kv store under its own key", async () => {
    expect(await kvGet(KV_LOCATION)).toBeUndefined();
    await kvSet(KV_LOCATION, "precise");
    expect(parseLocationSharing(await kvGet(KV_LOCATION))).toBe("precise");
    expect(KV_LOCATION).toBe("locationSharing");
  });
});

class FakeGeo implements GeolocationLike {
  watches = new Map<number, { ok: (p: never) => void; err: (e: { code: number }) => void; options: PositionOptions }>();
  cleared: number[] = [];
  private n = 0;
  watchPosition = vi.fn(
    (ok: (p: never) => void, err: (e: { code: number }) => void, options?: PositionOptions) => {
      const id = ++this.n;
      this.watches.set(id, { ok, err, options: options ?? {} });
      return id;
    },
  );
  clearWatch = vi.fn((id: number) => {
    this.cleared.push(id);
    this.watches.delete(id);
  });
  get active() {
    return [...this.watches.entries()];
  }
  emit(id: number, lat: number, lon: number, accuracy: number, timestamp: number) {
    this.watches.get(id)!.ok({ coords: { latitude: lat, longitude: lon, accuracy }, timestamp } as never);
  }
  fail(id: number, code: number) {
    this.watches.get(id)!.err({ code });
  }
}

function watcher(over: { sharing?: "off" | "approximate" | "precise"; visible?: boolean; geo?: FakeGeo | null } = {}) {
  const geo = over.geo === null ? undefined : (over.geo ?? new FakeGeo());
  const state = { visible: over.visible ?? true, now: NOW };
  const saved: string[] = [];
  const w = new LocationWatcher({
    sharing: over.sharing ?? "off",
    geolocation: geo,
    isVisible: () => state.visible,
    now: () => state.now,
    saveSharing: (s) => saved.push(s),
  });
  w.start();
  return { w, geo: geo as FakeGeo, state, saved };
}

describe("LocationWatcher", () => {
  it("never calls watchPosition while Off", () => {
    const { geo, w } = watcher({ sharing: "off" });
    expect(geo.watchPosition).not.toHaveBeenCalled();
    expect(w.outgoing()).toBeUndefined();
  });

  it("watches at low accuracy for Approximate and high for Precise, with a 60 s maximumAge", () => {
    const a = watcher({ sharing: "approximate" });
    expect(a.geo.active[0][1].options).toEqual({ enableHighAccuracy: false, maximumAge: 60_000 });
    const p = watcher({ sharing: "precise" });
    expect(p.geo.active[0][1].options).toEqual({ enableHighAccuracy: true, maximumAge: 60_000 });
  });

  it("does not watch while hidden, starts when visible, and clears the watch when hidden again", () => {
    const { w, geo, state } = watcher({ sharing: "precise", visible: false });
    expect(geo.watchPosition).not.toHaveBeenCalled();
    state.visible = true;
    w.reconcile();
    expect(geo.active).toHaveLength(1);
    w.reconcile(); // idempotent
    expect(geo.watchPosition).toHaveBeenCalledTimes(1);
    state.visible = false;
    w.reconcile();
    expect(geo.active).toHaveLength(0);
    expect(geo.clearWatch).toHaveBeenCalledTimes(1);
  });

  it("keeps the last fix across a hide/show, and it still expires by age", () => {
    const { w, geo, state } = watcher({ sharing: "precise" });
    geo.emit(1, 46.8, -71.2, 30, NOW);
    state.visible = false;
    w.reconcile();
    state.now = NOW + 10 * MIN;
    expect(w.outgoing()).toEqual({ latitude: 46.8, longitude: -71.2, accuracy_m: 30 });
    state.now = NOW + 16 * MIN;
    expect(w.outgoing()).toBeUndefined();
  });

  it("outgoing applies the approximate rounding to what the watch reported", () => {
    const { w, geo } = watcher({ sharing: "approximate" });
    geo.emit(1, 46.814949, -71.213838, 20, NOW);
    expect(w.outgoing()).toEqual({ latitude: 46.81, longitude: -71.21, accuracy_m: 1000 });
  });

  it("switching to Off clears the watch AND forgets the fix", () => {
    const { w, geo, saved } = watcher({ sharing: "precise" });
    geo.emit(1, 46.8, -71.2, 30, NOW);
    w.setSharing("off");
    expect(geo.active).toHaveLength(0);
    expect(w.outgoing()).toBeUndefined();
    w.setSharing("precise");
    expect(w.outgoing()).toBeUndefined(); // no stale fix resurrected
    expect(saved).toEqual(["off", "precise"]);
  });

  it("restarts the watch with the new options when precision changes, and drops the old fix", () => {
    const { w, geo } = watcher({ sharing: "approximate" });
    geo.emit(1, 46.8, -71.2, 5000, NOW);
    w.setSharing("precise");
    expect(geo.cleared).toEqual([1]);
    expect(geo.active).toHaveLength(1);
    expect(geo.active[0][1].options.enableHighAccuracy).toBe(true);
    expect(w.outgoing()).toBeUndefined();
  });

  it("ignores a late callback from a watch that was replaced", () => {
    const { w, geo } = watcher({ sharing: "approximate" });
    const stale = geo.watches.get(1)!;
    w.setSharing("precise");
    stale.ok({ coords: { latitude: 1, longitude: 2, accuracy: 3 }, timestamp: NOW } as never);
    expect(w.outgoing()).toBeUndefined();
    stale.err({ code: 1 });
    expect(w.getSnapshot().status).toBe("ok");
  });

  it("permission denied: no location, watch cleared, status blocked, subscribers told", () => {
    const { w, geo } = watcher({ sharing: "precise" });
    const seen = vi.fn();
    w.subscribe(seen);
    geo.emit(1, 46.8, -71.2, 30, NOW);
    geo.fail(1, 1);
    expect(w.outgoing()).toBeUndefined();
    expect(geo.active).toHaveLength(0);
    expect(w.getSnapshot()).toEqual({ sharing: "precise", status: "blocked" });
    expect(seen).toHaveBeenCalledTimes(1);
    w.reconcile(); // a visibility flip must not re-ask while blocked
    expect(geo.watchPosition).toHaveBeenCalledTimes(1);
  });

  it("choosing a setting again after a denial tries the permission again", () => {
    const { w, geo } = watcher({ sharing: "precise" });
    geo.fail(1, 1);
    w.setSharing("approximate");
    expect(w.getSnapshot().status).toBe("ok");
    expect(geo.watchPosition).toHaveBeenCalledTimes(2);
  });

  it("position-unavailable and timeout errors keep the watch and the fix", () => {
    const { w, geo } = watcher({ sharing: "precise" });
    geo.emit(1, 46.8, -71.2, 30, NOW);
    geo.fail(1, 2);
    geo.fail(1, 3);
    expect(geo.active).toHaveLength(1);
    expect(w.getSnapshot().status).toBe("ok");
    expect(w.outgoing()).toBeDefined();
  });

  it("no geolocation API: status unavailable, nothing sent, nothing thrown", () => {
    const { w } = watcher({ sharing: "precise", geo: null });
    expect(w.getSnapshot()).toEqual({ sharing: "precise", status: "unavailable" });
    expect(w.outgoing()).toBeUndefined();
    w.setSharing("approximate");
    expect(w.outgoing()).toBeUndefined();
  });

  it("stop() clears a running watch", () => {
    const { w, geo } = watcher({ sharing: "precise" });
    w.stop();
    expect(geo.active).toHaveLength(0);
  });
});
