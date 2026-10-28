import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Api, HttpError } from "../lib/api";
import { ChatStore, DEFAULT_EFFORT, type Effort } from "../lib/chatStore";
import { LocationWatcher, parseLocationSharing, type LocationSharing } from "../lib/location";
import { DEFAULT_FACE, parseFace, type Face } from "../lib/faces";
import { picked, pull, type PreferenceSyncDeps } from "../lib/preferenceSync";
import { getToken, idbPersistence, kvDelete, kvGet, kvSet, KV_EFFORT, KV_FACE, KV_LOCATION, setToken, wipeAll } from "../lib/storage";
import { Chat } from "./Chat";
import { Pairing } from "./Pairing";
import { closePermissionNotifications, registerServiceWorker, syncPushSubscription, unsubscribePush } from "./push";

type Boot =
  | { kind: "loading" }
  | { kind: "unpaired"; reason?: string }
  | { kind: "paired"; token: string }
  | { kind: "elsewhere" };

const LOCK_NAME = "marlow-chat";

/** WEB-5: two tabs share IndexedDB, so the outbox and the cursor can race.
 * Resends are idempotent, so the cost would be duplicate polling rather than
 * duplicate messages, but the cache writes would also interleave. One tab
 * owns the chat (a Web Lock); another shows a notice and can take over, which
 * hands the lock across and stops the first. Where Web Locks are missing
 * every tab simply runs. */
function useTabOwnership(enabled: boolean): { owner: boolean; takeOver: () => void } {
  const [owner, setOwner] = useState(!("locks" in navigator));
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!enabled || !("locks" in navigator)) return;
    let release: (() => void) | null = null;
    let cancelled = false;
    const held = new Promise<void>((r) => (release = r));
    navigator.locks
      .request(LOCK_NAME, attempt === 0 ? { ifAvailable: true } : { steal: true }, async (lock) => {
        if (!lock || cancelled) {
          if (!cancelled) setOwner(false);
          return;
        }
        setOwner(true);
        await held;
      })
      .catch(() => {
        // Stolen by another tab (AbortError): this one stops.
        if (!cancelled) setOwner(false);
      });
    return () => {
      cancelled = true;
      release?.();
    };
  }, [enabled, attempt]);
  return { owner, takeOver: () => setAttempt((n) => n + 1) };
}

export function App() {
  const [boot, setBoot] = useState<Boot>({ kind: "loading" });

  useEffect(() => {
    registerServiceWorker();
    getToken()
      .then((token) => {
        // A pairing link opened in a browser that is already paired is not
        // used, so <Pairing> never mounts to take its invite out of the URL.
        if (token && /(^|[#&])pair=/.test(location.hash)) {
          history.replaceState(null, "", location.pathname + location.search);
        }
        setBoot(token ? { kind: "paired", token } : { kind: "unpaired" });
      })
      .catch(() => setBoot({ kind: "unpaired" }));
  }, []);

  const onPaired = useCallback(async (token: string) => {
    await setToken(token);
    setBoot({ kind: "paired", token });
  }, []);

  const forget = useCallback(async (reason?: string) => {
    await unsubscribePush();
    await wipeAll().catch(() => {});
    setBoot({ kind: "unpaired", reason });
  }, []);

  const { owner, takeOver } = useTabOwnership(boot.kind === "paired");

  if (boot.kind === "loading") return <div className="boot" aria-busy="true" />;
  if (boot.kind === "unpaired") return <Pairing onPaired={onPaired} notice={boot.reason} />;
  if (boot.kind === "paired" && !owner) {
    return (
      <div className="elsewhere">
        <p>Marlow is open in another tab.</p>
        <button type="button" className="primary" onClick={takeOver}>
          Use it here
        </button>
      </div>
    );
  }
  return <Session token={boot.kind === "paired" ? boot.token : ""} onForget={forget} />;
}

function Session({ token, onForget }: { token: string; onForget: (reason?: string) => void }) {
  const forgetRef = useRef(onForget);
  forgetRef.current = onForget;
  const [effort, setEffort] = useState<Effort | null>(null);

  useEffect(() => {
    kvGet<Effort>(KV_EFFORT)
      .then((e) => setEffort(e === "low" || e === "medium" || e === "high" ? e : DEFAULT_EFFORT))
      .catch(() => setEffort(DEFAULT_EFFORT));
  }, []);

  // Off until the stored choice is read: nothing may start watching first.
  const [sharing, setSharing] = useState<LocationSharing | null>(null);
  useEffect(() => {
    kvGet<unknown>(KV_LOCATION)
      .then((v) => setSharing(parseLocationSharing(v)))
      .catch(() => setSharing("off"));
  }, []);

  // Read before the chat shows, so a chosen face never flashes the default.
  const [face, setFace] = useState<Face | null>(null);
  useEffect(() => {
    kvGet<unknown>(KV_FACE)
      .then((v) => setFace(parseFace(v)))
      .catch(() => setFace(DEFAULT_FACE));
  }, []);

  const api = useMemo(
    () =>
      new Api({
        getToken: () => token,
        // WEB-8: iOS has no 401 handling (a revoked token shows "Server error
        // 401" in a bubble while polls fail quietly). The web treats any 401
        // as "this browser is no longer paired": forget it and say so.
        onUnauthorized: () =>
          forgetRef.current("This browser is no longer paired with Marlow. Pair it again with a new link."),
      }),
    [token],
  );

  // Revoke on the box first, so a token that was copied out of this browser
  // stops working too (docs/tasks/funnel-ingress.md, step 2). Local state is
  // cleared either way: an unreachable box must not trap a device in a session
  // it asked to leave. A 401 means the box already forgot it.
  const unpair = useCallback(async () => {
    try {
      await api.unpair();
    } catch (err) {
      // A 401 already ran the unauthorized handler, which forgets this device;
      // a second forget would race it over which notice shows.
      if (err instanceof HttpError && err.status === 401) return;
      forgetRef.current(
        "Unpaired here, but Marlow couldn't be reached to revoke this device. Ask whoever runs the box to revoke it (deploy/devices.sh).",
      );
      return;
    }
    forgetRef.current(undefined);
  }, [api]);

  const location = useMemo(
    () =>
      sharing === null
        ? null
        : new LocationWatcher({
            sharing,
            geolocation: typeof navigator !== "undefined" ? navigator.geolocation : undefined,
            isVisible: () => document.visibilityState === "visible",
            saveSharing: (s) => void kvSet(KV_LOCATION, s).catch(() => {}),
          }),
    [sharing],
  );

  const store = useMemo(
    () =>
      effort === null || location === null
        ? null
        : new ChatStore({
            api,
            persistence: idbPersistence,
            initialEffort: effort,
            saveEffort: (e) => void kvSet(KV_EFFORT, e).catch(() => {}),
            location: () => location.outgoing(),
            isForeground: () => document.visibilityState === "visible",
          }),
    [api, effort, location],
  );

  // One face and one Thinking level on every client (lib/preferenceSync.ts):
  // the box's are taken up on open and whenever the tab is shown, and a pick
  // here is sent to it. Never awaited by the UI: what IndexedDB holds shows
  // at once, and the box's answer lands when it lands.
  const sync = useMemo<PreferenceSyncDeps>(
    () => ({ read: (k) => kvGet(k), write: kvSet, remove: kvDelete, api }),
    [api],
  );
  useEffect(() => {
    if (!store) return;
    const pullNow = () => {
      if (document.visibilityState !== "visible") return;
      void pull(sync)
        .then((changed) => {
          if (changed.face) setFace(changed.face);
          if (changed.effort) store.setEffort(changed.effort);
        })
        .catch(() => {});
    };
    pullNow();
    document.addEventListener("visibilitychange", pullNow);
    return () => document.removeEventListener("visibilitychange", pullNow);
  }, [store, sync]);
  const chooseFace = useCallback(
    (f: Face) => {
      setFace(f);
      // Stored before it is marked, so the send reads the new value.
      void kvSet(KV_FACE, f.id)
        .then(() => picked("face", sync))
        .catch(() => {});
    },
    [sync],
  );
  const chooseEffort = useCallback(
    (e: Effort) => {
      store?.setEffort(e);
      void kvSet(KV_EFFORT, e)
        .then(() => picked("effort", sync))
        .catch(() => {});
    },
    [store, sync],
  );

  // Watches only while the setting is on and the page is visible.
  useEffect(() => {
    if (!location) return;
    const onVisibility = () => location.reconcile();
    location.start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      location.stop();
    };
  }, [location]);

  useEffect(() => {
    if (!store) return;
    void store.start();
    void syncPushSubscription(api);
    // The badge means "arrived while you were away"; looking clears it
    // (iOS clears its icon badge when the transcript opens).
    const clearBadge = () => void (navigator as Navigator & { clearAppBadge?: () => Promise<void> }).clearAppBadge?.()?.catch(() => {});
    if (document.visibilityState === "visible") {
      clearBadge();
      void closePermissionNotifications();
    }
    const onVisible = () => {
      if (document.visibilityState === "visible") {
        clearBadge();
        void closePermissionNotifications();
        store.restartLoop();
      }
    };
    const onOnline = () => store.restartLoop();
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("online", onOnline);
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("online", onOnline);
      store.stop();
    };
  }, [store, api]);

  if (!store || !location || !face) return <div className="boot" aria-busy="true" />;
  return (
    <Chat
      store={store}
      location={location}
      api={api}
      token={token}
      face={face}
      onFace={chooseFace}
      onEffort={chooseEffort}
      onUnpair={() => void unpair()}
    />
  );
}
