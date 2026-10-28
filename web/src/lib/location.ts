// Location on messages (docs/tasks/message-location.md). One setting, Off /
// Approximate / Precise; when it is not Off every message carries the latest
// fix. ABSENCE IS MEANINGFUL: the server clears what it holds for this device
// when a message arrives without `location`, so Off, a denied permission and a
// stale fix are all "send no key", never a null or an empty object.
//
// Web sends coordinates only, no `place`: the browser has no geocoder, and
// calling an outside service for it would open an egress question.

export type LocationSharing = "off" | "approximate" | "precise";

export const DEFAULT_LOCATION_SHARING: LocationSharing = "off";

/** An unknown stored value (or none) is Off: never share on a guess. */
export function parseLocationSharing(value: unknown): LocationSharing {
  return value === "approximate" || value === "precise" ? value : DEFAULT_LOCATION_SHARING;
}

/** The wire shape of POST /v1/message `location`. */
export interface OutgoingLocation {
  latitude: number;
  longitude: number;
  accuracy_m?: number;
}

/** What the browser reported: a GeolocationPosition, flattened. `timestamp` is
 * epoch ms, the position's own (not when we received it). */
export interface LocationFix {
  latitude: number;
  longitude: number;
  accuracy: number;
  timestamp: number;
}

/** The client sends only a fix newer than this; the server relies on it. */
export const FIX_MAX_AGE_MS = 15 * 60_000;
/** Approximate never reports an accuracy better than this, so the server (and
 * the model) never treats a rounded point as a street address. */
export const APPROXIMATE_FLOOR_M = 1000;

/** 2 decimal places is ~1.1 km of latitude. `+ 0` turns -0 into 0. */
const roundTo2 = (x: number) => Math.round(x * 100) / 100 + 0;

/** The payload for a message sent at `now`, or undefined for "send nothing".
 * Approximate rounding happens HERE, so the precise value never reaches the
 * request body. */
export function toOutgoing(
  fix: LocationFix | undefined,
  sharing: LocationSharing,
  now: number,
): OutgoingLocation | undefined {
  if (sharing === "off" || !fix) return undefined;
  if (!Number.isFinite(fix.latitude) || !Number.isFinite(fix.longitude)) return undefined;
  if (!Number.isFinite(fix.timestamp) || now - fix.timestamp > FIX_MAX_AGE_MS) return undefined;
  const reported = Number.isFinite(fix.accuracy) && fix.accuracy > 0 ? fix.accuracy : undefined;
  if (sharing === "approximate") {
    return {
      latitude: roundTo2(fix.latitude),
      longitude: roundTo2(fix.longitude),
      accuracy_m: Math.max(reported ?? 0, APPROXIMATE_FLOOR_M),
    };
  }
  return {
    latitude: fix.latitude,
    longitude: fix.longitude,
    ...(reported !== undefined ? { accuracy_m: reported } : {}),
  };
}

/** Why the Settings footer might say something other than the default. */
export type LocationStatus = "ok" | "blocked" | "unavailable";

export interface LocationSnapshot {
  sharing: LocationSharing;
  status: LocationStatus;
}

/** The slice of `navigator.geolocation` this uses, so tests can fake it. */
export interface GeolocationLike {
  watchPosition(
    success: (p: { coords: { latitude: number; longitude: number; accuracy: number }; timestamp: number }) => void,
    error: (e: { code: number }) => void,
    options?: PositionOptions,
  ): number;
  clearWatch(id: number): void;
}

export interface LocationWatcherOptions {
  sharing: LocationSharing;
  /** Undefined when the browser has none. */
  geolocation: GeolocationLike | undefined;
  isVisible: () => boolean;
  now?: () => number;
  saveSharing?: (s: LocationSharing) => void;
}

const PERMISSION_DENIED = 1;

/** Holds the setting and the latest fix, and runs `watchPosition` only while
 * the setting is on AND the page is visible (a hidden page has no message to
 * send, and a running watch keeps the radio busy). Call `reconcile()` when
 * visibility changes. */
export class LocationWatcher {
  private sharing: LocationSharing;
  private denied = false;
  private fix: LocationFix | undefined;
  private watch: { id: number; sharing: LocationSharing } | null = null;
  private listeners = new Set<() => void>();
  private snapshot: LocationSnapshot;
  private readonly geo: GeolocationLike | undefined;
  private readonly isVisible: () => boolean;
  private readonly now: () => number;
  private readonly saveSharing: (s: LocationSharing) => void;

  constructor(opts: LocationWatcherOptions) {
    this.sharing = opts.sharing;
    this.geo = opts.geolocation;
    this.isVisible = opts.isVisible;
    this.now = opts.now ?? Date.now;
    this.saveSharing = opts.saveSharing ?? (() => {});
    this.snapshot = this.compute();
  }

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  getSnapshot = (): LocationSnapshot => this.snapshot;

  /** Begin watching if the setting and visibility say so. */
  start(): void {
    this.reconcile();
  }

  /** Stop for good (unmount). */
  stop(): void {
    this.clearWatch();
  }

  setSharing(sharing: LocationSharing): void {
    if (sharing === this.sharing) return;
    this.sharing = sharing;
    // A fix taken at the other precision must not be reused under this one,
    // and a new choice gets a fresh try at the permission.
    this.fix = undefined;
    this.denied = false;
    this.saveSharing(sharing);
    this.reconcile();
    this.publish();
  }

  /** The location to attach to a message sent now, or undefined for none. */
  outgoing(): OutgoingLocation | undefined {
    if (this.denied) return undefined;
    return toOutgoing(this.fix, this.sharing, this.now());
  }

  /** Make the running watch match what should be running. */
  reconcile(): void {
    const want = this.sharing !== "off" && !this.denied && !!this.geo && this.isVisible();
    if (this.watch && (!want || this.watch.sharing !== this.sharing)) this.clearWatch();
    if (want && !this.watch) this.startWatch();
  }

  private startWatch(): void {
    const sharing = this.sharing;
    const precise = sharing === "precise";
    // Set before the call so a callback that fires synchronously still sees
    // itself as current; the real id replaces the placeholder below.
    const mine = { id: -1, sharing };
    this.watch = mine;
    mine.id = this.geo!.watchPosition(
      (p) => {
        // A late callback from a watch that has since been replaced.
        if (this.watch !== mine) return;
        this.fix = {
          latitude: p.coords.latitude,
          longitude: p.coords.longitude,
          accuracy: p.coords.accuracy,
          timestamp: p.timestamp,
        };
      },
      (e) => {
        if (this.watch !== mine) return;
        if (e.code !== PERMISSION_DENIED) return; // unavailable / timeout: keep watching
        this.denied = true;
        this.fix = undefined;
        this.clearWatch();
        this.publish();
      },
      { enableHighAccuracy: precise, maximumAge: 60_000 },
    );
  }

  private clearWatch(): void {
    if (!this.watch) return;
    this.geo?.clearWatch(this.watch.id);
    this.watch = null;
  }

  private compute(): LocationSnapshot {
    const status: LocationStatus = !this.geo ? "unavailable" : this.denied ? "blocked" : "ok";
    return { sharing: this.sharing, status };
  }

  private publish(): void {
    const next = this.compute();
    if (next.sharing === this.snapshot.sharing && next.status === this.snapshot.status) return;
    this.snapshot = next;
    for (const fn of [...this.listeners]) fn();
  }
}
