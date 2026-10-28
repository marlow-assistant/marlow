import { useCallback, useEffect, useRef, useState } from "react";
import type { Api } from "../lib/api";
import { kvGet, kvSet, KV_WHATS_NEW_MUTED, KV_WHATS_NEW_SEEN } from "../lib/storage";
import { bundledChanges, mergeChanges, refreshState, union, type ChangeEntry } from "../lib/whatsNew";

export type ChangesApi = Pick<Api, "changes">;

export interface WhatsNewModel {
  /** Everything known, newest first: Settings' list. */
  entries: ChangeEntry[];
  /** What the card shows. Stays until `acknowledge()`, so "Don't show again"
   * can swap the card to its confirmation without the list emptying. */
  pending: ChangeEntry[];
  muted: boolean;
  acknowledge: () => void;
  setMuted: (muted: boolean) => void;
}

const BUNDLED = bundledChanges();

function readSeen(v: unknown): number[] | null {
  return Array.isArray(v) && v.every((n) => typeof n === "number") ? v : null;
}

/** Port of iOS `WhatsNewStore` (docs/tasks/whats-new.md). Seen ids are this
 * device's, in IndexedDB, so a second person on the box hears of an update on
 * their own phone. Fetched on mount and whenever the page is visible again (a
 * box can be updated while the tab sits in the background). Quiet on failure:
 * the last server half stays, and the app's own entries still show. */
/** `bundled` must be stable (a module constant): it keys the fetch. */
export function useWhatsNew(api: ChangesApi, bundled: ChangeEntry[] = BUNDLED): WhatsNewModel {
  const [entries, setEntries] = useState<ChangeEntry[]>(() => mergeChanges(bundled, []));
  const [pending, setPending] = useState<ChangeEntry[]>([]);
  const [muted, setMutedState] = useState(false);
  const server = useRef<ChangeEntry[]>([]);
  const seen = useRef<number[] | null>(null);
  const onboarded = useRef(true);
  const mutedRef = useRef(false);
  const pendingRef = useRef<ChangeEntry[]>([]);
  pendingRef.current = pending;
  const loaded = useRef<Promise<void> | null>(null);
  const mutedTouched = useRef(false);
  const seq = useRef(0);

  const saveSeen = (ids: number[]) => {
    seen.current = ids;
    void kvSet(KV_WHATS_NEW_SEEN, ids).catch(() => {});
  };

  const refresh = useCallback(() => {
    const mine = ++seq.current;
    void (async () => {
      // The stored state is read once, before the first decision: deciding on
      // a guess would show the first-run card to a device that has seen it.
      loaded.current ??= Promise.all([
        kvGet<unknown>(KV_WHATS_NEW_SEEN).catch(() => undefined),
        kvGet<unknown>(KV_WHATS_NEW_MUTED).catch(() => undefined),
      ]).then(([s, m]) => {
        // Merged with, never over, what a Settings toggle wrote before this
        // read landed (the race useIdeas had with "last opened").
        const stored = readSeen(s);
        seen.current = seen.current === null ? stored : union(stored ?? [], seen.current);
        if (!mutedTouched.current) {
          mutedRef.current = m === true;
          setMutedState(mutedRef.current);
        }
      });
      await loaded.current;
      let fresh: ChangeEntry[] | null = null;
      try {
        const got = await api.changes();
        fresh = got.entries;
        onboarded.current = got.onboardingComplete;
      } catch {
        // Keep the last server half.
      }
      // Before anything is kept: a slower, older answer must not replace a
      // newer one.
      if (mine !== seq.current) return;
      if (fresh) server.current = fresh;
      const all = mergeChanges(bundled, server.current);
      setEntries(all);
      const next = refreshState(all, seen.current, mutedRef.current, onboarded.current);
      if (next.seen !== seen.current) saveSeen(next.seen);
      // A card that is up keeps its entries: they are not seen until it
      // closes, so the recompute finds them again.
      setPending(next.pending);
    })();
  }, [api, bundled]);

  useEffect(() => {
    refresh();
    const onVisible = () => {
      if (document.visibilityState === "visible") refresh();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [refresh]);

  const acknowledge = useCallback(() => {
    const shown = pendingRef.current;
    if (!shown.length) return;
    saveSeen(union(seen.current ?? [], shown.map((e) => e.id)));
    setPending([]);
  }, []);

  const setMuted = useCallback(
    (value: boolean) => {
      mutedTouched.current = true;
      mutedRef.current = value;
      setMutedState(value);
      void kvSet(KV_WHATS_NEW_MUTED, value).catch(() => {});
      // Counted as seen while muted, so turning it back on brings only what
      // is new from then on.
      if (value) saveSeen(union(seen.current ?? [], entries.map((e) => e.id)));
    },
    [entries],
  );

  return { entries, pending, muted, acknowledge, setMuted };
}
