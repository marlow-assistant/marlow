import { useEffect, useState, useSyncExternalStore } from "react";
import type { ChatState, ChatStore, StreamState } from "../lib/chatStore";
import type { LocationSnapshot, LocationWatcher } from "../lib/location";

export function useChatState(store: ChatStore): ChatState {
  return useSyncExternalStore(store.subscribe, store.getState);
}

export function useLocationState(watcher: LocationWatcher): LocationSnapshot {
  return useSyncExternalStore(watcher.subscribe, watcher.getSnapshot);
}

/** One placeholder's streaming state. Only the component that calls this
 * re-renders on a typewriter tick (STR-12). */
export function useStream(store: ChatStore, id: string): StreamState | undefined {
  return useSyncExternalStore(
    (fn) => store.subscribeStream(id, fn),
    () => store.getStream(id),
  );
}

/** Several placeholders' streams, for the presence header. Re-renders the
 * header (not the list) on their ticks. */
export function useStreams(store: ChatStore, ids: string[]): Map<string, StreamState | undefined> {
  const key = ids.join(",");
  const [, bump] = useState(0);
  useEffect(() => {
    const offs = ids.map((id) => store.subscribeStream(id, () => bump((n) => n + 1)));
    return () => offs.forEach((off) => off());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [store, key]);
  return new Map(ids.map((id) => [id, store.getStream(id)]));
}

/** An object URL for a stored image, or null while loading / missing. */
export function useImageUrl(store: ChatStore, key: string | undefined): string | null {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!key) {
      setUrl(null);
      return;
    }
    let live = true;
    void store.imageUrl(key).then((u) => live && setUrl(u));
    return () => {
      live = false;
    };
  }, [store, key]);
  return url;
}

export function useNow(intervalMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(t);
  }, [intervalMs]);
  return now;
}
