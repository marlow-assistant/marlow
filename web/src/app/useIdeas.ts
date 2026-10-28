import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Api } from "../lib/api";
import type { Idea } from "../lib/apiModels";
import { keepDismissed, showDot, spotlightOf } from "../lib/ideas";
import { kvGet, kvSet, KV_IDEAS_OPENED } from "../lib/storage";

export type IdeasApi = Pick<Api, "ideas" | "dismissIdea">;

export interface IdeasModel {
  /** The last good list, minus what "Do it" has taken. It still holds ideas
   * dismissed this session (the panel shows them as collapsed rows), so a
   * caller that wants only the open ones filters by `dismissed`. */
  ideas: Idea[];
  dismissed: ReadonlySet<string>;
  spotlight: Idea | null;
  dot: boolean;
  /** Fetch now. Quiet on failure: the last good list stays. */
  refresh: () => void;
  /** The panel was opened: clears the dot's "added since" half. */
  markOpened: () => void;
  /** "Do it": the card goes at once. The chat send is the caller's. */
  take: (id: string) => void;
  /** "Not now", and later the reason. Optimistic and quiet: the card is gone
   * from the spotlight and the dot whether or not the POST lands. */
  dismiss: (id: string, reason: string | null) => void;
}

/** The ideas the chat screen shows from: the lightbulb's dot, the spotlight
 * card and the panel's list. Fetched on mount (the dot and the spotlight must
 * be right before anyone opens the panel), whenever the page becomes visible
 * again, and by `refresh()` when the panel opens. A failed fetch says nothing
 * anywhere: no error in the chat, and the last good list stays. */
export function useIdeas(api: IdeasApi, today: string): IdeasModel {
  const [fetched, setFetched] = useState<Idea[]>([]);
  const [taken, setTaken] = useState<ReadonlySet<string>>(new Set());
  const [dismissed, setDismissed] = useState<ReadonlySet<string>>(new Set());
  /** undefined until the stored value is read (no dot from "added since" on a
   * guess); null once read and never opened. */
  const [lastOpened, setLastOpened] = useState<string | null | undefined>(undefined);
  const dismissedRef = useRef(dismissed);
  dismissedRef.current = dismissed;
  const seq = useRef(0);

  useEffect(() => {
    // Only if nothing newer is there: opening the panel before this read lands
    // would otherwise have its date put back to the stored one, and the dot
    // relit (seen as a flaky test once a second IndexedDB reader joined).
    const stored = (v: string | null) => setLastOpened((prev) => (prev === undefined ? v : prev));
    kvGet<unknown>(KV_IDEAS_OPENED)
      .then((v) => stored(typeof v === "string" ? v : null))
      .catch(() => stored(null));
  }, []);

  const refresh = useCallback(() => {
    const mine = ++seq.current;
    // Inside the try, so an api that cannot answer at all is as quiet as one
    // that fails.
    void (async () => {
      try {
        const fresh = await api.ideas();
        // A slower, older answer must not overwrite a newer one.
        if (mine === seq.current) setFetched((prev) => keepDismissed(prev, fresh, dismissedRef.current));
      } catch {
        // Keep the last good list.
      }
    })();
  }, [api]);

  useEffect(() => {
    refresh();
    const onVisible = () => {
      if (document.visibilityState === "visible") refresh();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [refresh]);

  const markOpened = useCallback(() => {
    setLastOpened(today);
    void kvSet(KV_IDEAS_OPENED, today).catch(() => {});
  }, [today]);

  const take = useCallback((id: string) => setTaken((s) => new Set(s).add(id)), []);

  const dismiss = useCallback(
    (id: string, reason: string | null) => {
      setDismissed((s) => (s.has(id) ? s : new Set(s).add(id)));
      void (async () => {
        try {
          await api.dismissIdea(id, reason);
        } catch {
          // Quiet. A 404 means it is already gone; anything else, the card is
          // hidden here and the nightly run will see it still open.
        }
      })();
    },
    [api],
  );

  const ideas = useMemo(() => fetched.filter((i) => !taken.has(i.id)), [fetched, taken]);
  const open = useMemo(() => ideas.filter((i) => !dismissed.has(i.id)), [ideas, dismissed]);
  const spotlight = useMemo(() => spotlightOf(open, today), [open, today]);
  const dot = useMemo(
    // No dot until the stored date is read: "added since" is not a guess.
    () => (lastOpened === undefined ? false : showDot(open, today, lastOpened)),
    [open, today, lastOpened],
  );

  return { ideas, dismissed, spotlight, dot, refresh, markOpened, take, dismiss };
}
