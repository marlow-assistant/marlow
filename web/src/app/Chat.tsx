import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { Api } from "../lib/api";
import type { ChatStore, Effort } from "../lib/chatStore";
import type { Idea } from "../lib/apiModels";
import type { LocationWatcher } from "../lib/location";
import { dividerLabels } from "../lib/dividers";
import type { Face } from "../lib/faces";
import { failedReplies } from "../lib/failedReplyPairing";
import { localToday } from "../lib/ideas";
import { presenceOf } from "../lib/presence";
import { action, endsFollowing, FollowTracker, isFarFromBottom, replyHasWords } from "../lib/replyFollow";
import { banners } from "../lib/statusStrip";
import * as UsageFormat from "../lib/usageFormat";
import type { ChatMessage } from "../lib/types";
import { BrowserSpeech } from "../lib/browserSpeech";
import { canRecord } from "../lib/segmentRecorder";
import { Composer, type ComposerVoice } from "./Composer";
import { feedbackFinished, feedbackSent, feedbackStarted, setChatFeedbackMuted } from "./feedback";
import { useChatState, useLocationState, useNow, useStreams } from "./hooks";
import { DO_IT_TEXT, IdeasPanel, quoteOf, SpotlightCard } from "./IdeasPanel";
import { MessageRow } from "./MessageRow";
import { NotificationsPrompt } from "./NotificationsPrompt";
import { PermissionCard } from "./PermissionCard";
import { FaceImage } from "./Face";
import { PresenceHeader } from "./Header";
import { Settings } from "./Settings";
import { useIdeas } from "./useIdeas";
import type { ChangeEntry } from "../lib/whatsNew";
import { useWhatsNew } from "./useWhatsNew";
import { WhatsNewCard } from "./WhatsNew";
import { MarlowSheet } from "./Views";
import { VoiceMode } from "./VoiceMode";

interface Following {
  userId: string;
  replyId: string;
  tracker: FollowTracker;
  /** Rows after the placeholder when following started; a new one ends it. */
  lastId: string;
  /** One more decision on the final layout after the placeholder settles. */
  finalPass: boolean;
}

/** Keys that scroll the page: a reader pressing one has taken over (SCR-11). */
const SCROLL_KEYS = new Set(["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "]);

export function Chat({
  store,
  location,
  api,
  token,
  face,
  onFace,
  onEffort,
  onUnpair,
  bundledChanges,
}: {
  store: ChatStore;
  location: LocationWatcher;
  api: Api;
  token: string;
  face: Face;
  onFace: (f: Face) => void;
  /** Also sent to the box, so every client shows it (lib/preferenceSync.ts). */
  onEffort: (e: Effort) => void;
  onUnpair: () => void;
  /** This build's app entries; injected for tests, which otherwise all start
   * as a first run with the update card up. */
  bundledChanges?: ChangeEntry[];
}) {
  const state = useChatState(store);
  const locationState = useLocationState(location);
  const { messages } = state;
  const scrollerRef = useRef<HTMLDivElement>(null);
  const [far, setFar] = useState(false);
  const farRef = useRef(false);
  /** scrollTop as of the last measureFar: a different value means a scroll the
   * `far` flag has not caught up with yet (its scroll event is still queued). */
  const measuredTop = useRef(0);
  const [unseen, setUnseen] = useState(false);
  /** One sheet at a time, as iOS's single `ActiveSheet`. */
  const [sheet, setSheet] = useState<"settings" | "marlow" | "ideas" | null>(null);
  const following = useRef<Following | null>(null);
  const localSends = useRef(new Set<string>());
  const pendingSendScroll = useRef(false);
  const initialScrollDone = useRef(false);
  const earlierAnchor = useRef<{ id: string; top: number } | null>(null);
  const prevCount = useRef(0);
  const prevStatuses = useRef(new Map<string, ChatMessage["status"]>());
  const now = useNow(60_000);
  // The phone's own calendar day, ticking with `now`: an idea hides when its
  // last day passes, without waiting on the server.
  const today = localToday(new Date(now));
  const ideas = useIdeas(api, today);
  const whatsNew = useWhatsNew(api, bundledChanges);
  /** The card's "Don't show again" note, here so it outlives the card being
   * held back by an approval or a sheet, or a refresh emptying `pending`. */

  // Speech to text runs on the box (docs/tasks/voice-mode-android.md), so the
  // mic and voice mode show only when the box says it can, and this browser
  // can record Opus. Asked once per session.
  const [canTranscribe, setCanTranscribe] = useState(false);
  useEffect(() => {
    if (!canRecord()) return;
    let live = true;
    api.transcribeAvailable().then(
      (ok) => live && setCanTranscribe(ok),
      () => {},
    );
    return () => {
      live = false;
    };
  }, [api]);
  const [voiceOpen, setVoiceOpen] = useState(false);
  const [dictating, setDictating] = useState(false);
  // Layout, not a plain effect: a PermissionCard mounting in the same commit
  // plays its sound from its own effect, and must find the switch already set.
  useLayoutEffect(() => setChatFeedbackMuted(voiceOpen || dictating), [voiceOpen, dictating]);
  useEffect(() => () => setChatFeedbackMuted(false), []);
  const composerVoice = useMemo<ComposerVoice | null>(
    () =>
      canTranscribe
        ? {
            api,
            dictation: true,
            voiceMode: BrowserSpeech.supported(),
            onVoiceMode: () => {
              setSheet(null);
              setVoiceOpen(true);
            },
            onDictating: setDictating,
          }
        : null,
    [api, canTranscribe],
  );

  const openIdeas = () => {
    ideas.refresh();
    ideas.markOpened();
    setSheet("ideas");
  };
  // Through the normal send path, so it shows as the user's bubble (quoting the
  // idea, from the card it came from) and polls like any other send; the panel closes so the reply is what Mike sees.
  const doIt = (idea: Idea) => {
    setSheet(null);
    ideas.take(idea.id);
    void store.send(DO_IT_TEXT, undefined, quoteOf(idea));
  };

  // --- geometry helpers ------------------------------------------------------

  const rowEl = (id: string) =>
    scrollerRef.current?.querySelector<HTMLElement>(`[data-id="${CSS.escape(id)}"]`) ?? null;

  const scrollToBottom = useCallback(() => {
    const s = scrollerRef.current;
    if (!s) return;
    // Instant, never smooth (SCR-12): an animated scroll is discarded during a
    // fling and restarted by every typed token.
    s.scrollTop = s.scrollHeight;
  }, []);

  const measureFar = useCallback(() => {
    const s = scrollerRef.current;
    if (!s) return;
    const f = isFarFromBottom(s.scrollHeight, s.scrollTop, s.clientHeight);
    farRef.current = f;
    measuredTop.current = s.scrollTop;
    setFar(f);
    // SCR-15: scrolling back inside the threshold clears the dot.
    if (!f) setUnseen(false);
  }, []);

  // --- feedback and the local-send scroll -----------------------------------

  useEffect(
    () =>
      store.onFeedback((e) => {
        if (e.kind === "sent") {
          localSends.current.add(e.userId);
          localSends.current.add(e.placeholderId);
          pendingSendScroll.current = true;
          feedbackSent();
        } else if (e.kind === "started") {
          feedbackStarted();
        } else {
          feedbackFinished();
        }
      }),
    [store],
  );

  // --- arrivals: SCR-1, SCR-2, SCR-3, SCR-5, SCR-10, WIN-4, SCR-15 ----------

  useLayoutEffect(() => {
    const s = scrollerRef.current;
    if (!s) return;
    const count = messages.length;

    // WIN-4: an earlier page was prepended. Hold the reader: the message that
    // was first on screen stays where it was.
    const anchor = earlierAnchor.current;
    if (anchor) {
      const el = rowEl(anchor.id);
      if (el && count !== prevCount.current) {
        s.scrollTop += el.getBoundingClientRect().top - anchor.top;
        earlierAnchor.current = null;
      }
    } else if (!initialScrollDone.current) {
      // SCR-1: the first painted frame with rows is at the bottom, with no
      // motion after. Layout effect, so it lands before paint.
      if (count > 0) {
        scrollToBottom();
        initialScrollDone.current = true;
      }
    } else if (pendingSendScroll.current) {
      // SCR-3: a local send always scrolls to the bottom, even from far up,
      // and starts following (SCR-5).
      pendingSendScroll.current = false;
      scrollToBottom();
      const last = messages[count - 1];
      const prev = messages[count - 2];
      if (last && prev && localSends.current.has(last.id) && localSends.current.has(prev.id)) {
        following.current = {
          userId: prev.id,
          replyId: last.id,
          tracker: new FollowTracker(),
          lastId: last.id,
          finalPass: false,
        };
      }
    } else if (count !== prevCount.current && !farRef.current && !following.current) {
      // SCR-2: an arriving row scrolls only a reader who is not far up.
      scrollToBottom();
    }

    // SCR-10: any row added after the placeholder ends following.
    const f = following.current;
    if (f) {
      const idx = messages.findIndex((m) => m.id === f.replyId);
      if (idx < 0) f.finalPass = true; // removed as merged
      else if (messages[messages.length - 1].id !== f.lastId) following.current = null;
      else if (messages[idx].status === "done" || messages[idx].status === "error") f.finalPass = true;
    }

    // SCR-15: a reply completing while the reader is far up sets the dot.
    // Not the bootstrap (awaitingFirstFetch was true), not an error, not an
    // earlier page, and not another device's user row.
    const before = prevStatuses.current;
    if (prevCount.current > 0 && farRef.current) {
      for (const m of messages) {
        if (m.role !== "assistant" || m.status !== "done") continue;
        const was = before.get(m.id);
        if (was === "done") continue;
        if (was === undefined && (anchor || state.awaitingFirstFetch)) continue;
        setUnseen(true);
        break;
      }
    }
    prevStatuses.current = new Map(messages.map((m) => [m.id, m.status]));
    prevCount.current = count;
    measureFar();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [messages]);

  // --- follow while the reply streams (SCR-6..SCR-13) -----------------------

  useEffect(() => {
    let raf = 0;
    const frame = () => {
      raf = requestAnimationFrame(frame);
      const f = following.current;
      const s = scrollerRef.current;
      if (!f || !s) return;
      const user = rowEl(f.userId);
      const reply = rowEl(f.replyId);
      if (!user || !reply) {
        following.current = null;
        return;
      }
      const msg = store.getState().messages.find((m) => m.id === f.replyId);
      const stream = store.getStream(f.replyId);
      // SCR-9: nothing until the reply has words.
      if (!msg || !replyHasWords({ streamedText: stream?.text, status: msg.status })) return;
      const sr = s.getBoundingClientRect();
      const ur = user.getBoundingClientRect();
      const rr = reply.getBoundingClientRect();
      const g = {
        // SCR-13: the row includes its divider, so this is the exchange's top.
        userTop: ur.top,
        userHeight: ur.height,
        replyTop: rr.top,
        replyBottom: rr.bottom,
        visibleTop: sr.top + s.clientTop,
        visibleBottom: sr.top + s.clientTop + s.clientHeight,
      };
      // SCR-8: only growth is news; the first measurement is a baseline.
      const news = f.tracker.isNews(g);
      if (!news && !f.finalPass) return;
      const a = action(g);
      if (a === "followReply") s.scrollTop += g.replyBottom - g.visibleBottom;
      else if (a === "pinUser") s.scrollTop += g.userTop - g.visibleTop; // SCR-7: land exactly
      else if (a === "pinReply") s.scrollTop += g.replyTop - g.visibleTop;
      if (endsFollowing(a) || f.finalPass) following.current = null;
      measureFar();
    };
    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, [store, measureFar]);

  // SCR-11: user scroll input ends following the moment it begins.
  useEffect(() => {
    const s = scrollerRef.current;
    if (!s) return;
    const stop = () => {
      following.current = null;
    };
    const onKey = (e: KeyboardEvent) => {
      if (SCROLL_KEYS.has(e.key) && e.target === s) stop();
    };
    s.addEventListener("wheel", stop, { passive: true });
    s.addEventListener("touchstart", stop, { passive: true });
    // A pointerdown on the scroller itself (its scrollbar), not on a row.
    const onPointer = (e: PointerEvent) => {
      if (e.target === s) stop();
    };
    s.addEventListener("pointerdown", onPointer);
    s.addEventListener("keydown", onKey);
    return () => {
      s.removeEventListener("wheel", stop);
      s.removeEventListener("touchstart", stop);
      s.removeEventListener("pointerdown", onPointer);
      s.removeEventListener("keydown", onKey);
    };
  }, []);

  // WEB-3: the soft keyboard (or any resize) must not move a reader at the
  // bottom. A ResizeObserver on the scroller sees the viewport shrink.
  useEffect(() => {
    const s = scrollerRef.current;
    if (!s || typeof ResizeObserver === "undefined") return;
    let lastHeight = s.clientHeight;
    const ro = new ResizeObserver(() => {
      if (s.clientHeight !== lastHeight) {
        lastHeight = s.clientHeight;
        if (!farRef.current && !following.current) scrollToBottom();
        measureFar();
      }
    });
    ro.observe(s);
    return () => ro.disconnect();
  }, [scrollToBottom, measureFar]);

  // WEB-4: an image settling changes row heights. A reader at the bottom
  // stays there.
  const onImageLoad = useCallback(() => {
    // A scroll the reader just made may not have reached measureFar yet (scroll
    // events are delivered at the next frame): `farRef` is then stale and
    // scrolling to the bottom would yank them back. Found by the SCR-15 e2e test,
    // which scrolled up while attachment images were still settling after a reload.
    const s = scrollerRef.current;
    const unprocessedScroll = !!s && Math.abs(s.scrollTop - measuredTop.current) > 1 && s.scrollTop < measuredTop.current;
    if (!farRef.current && !following.current && !unprocessedScroll) scrollToBottom();
    measureFar();
  }, [scrollToBottom, measureFar]);

  const showEarlier = () => {
    const s = scrollerRef.current;
    const first = s?.querySelector<HTMLElement>("[data-id]");
    if (first) earlierAnchor.current = { id: first.dataset.id!, top: first.getBoundingClientRect().top };
    store.showEarlier();
  };

  const jump = () => {
    following.current = null;
    scrollToBottom();
    setUnseen(false);
    measureFar();
  };

  // --- derived rendering state ----------------------------------------------

  const dividers = useMemo(() => dividerLabels(messages, new Date(now)), [messages, now]);
  const failed = useMemo(() => failedReplies(messages), [messages]);
  const pairedAway = useMemo(() => new Set(failed.values()), [failed]);
  const byId = useMemo(() => new Map(messages.map((m) => [m.id, m])), [messages]);
  const newestId = messages[messages.length - 1]?.id;

  const inFlight = messages
    .slice(-20)
    .filter((m) => m.role === "assistant" && (m.status === "sending" || m.status === "running"))
    .map((m) => m.id);
  const streams = useStreams(store, inFlight);
  const presence = presenceOf(messages, {
    activityLabel: (id) => streams.get(id)?.label,
    hasStreamedText: (id) => !!streams.get(id)?.text,
  });

  // On the minute tick as well as on state changes, so the row clears when
  // its window resets even if nothing else moves.
  const high = UsageFormat.highUsage(state.usageReport, {
    timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    now,
  });
  const strip = banners({
    reauthNeeded: state.reauthNeeded,
    queuePaused: state.queuePaused,
    reauthPrewarn: state.reauthPrewarn,
    usageHigh: high ? UsageFormat.highUsageText(high) : null,
  });

  return (
    <div className="chat">
      <PresenceHeader
        presence={presence}
        face={face}
        ideasDot={ideas.dot}
        onSettings={() => setSheet("settings")}
        onAvatar={() => setSheet("marlow")}
        onIdeas={openIdeas}
      />

      <StatusStrip items={strip} />
      <NotificationsPrompt api={api} />

      <div className="transcript-wrap">
        <div
          className="transcript"
          ref={scrollerRef}
          onScroll={measureFar}
          tabIndex={0}
          aria-label="Conversation"
          data-testid="transcript"
        >
          {state.hasOlderOnServer && (
            <button type="button" className="show-earlier" onClick={showEarlier} disabled={state.loadingEarlier}>
              {state.loadingEarlier ? "Loading…" : "Show earlier messages"}
            </button>
          )}
          {messages.map((m) => {
            const failedId = m.role === "user" ? failed.get(m.id) : undefined;
            return (
              <MessageRow
                key={m.id}
                message={m}
                store={store}
                divider={dividers.get(m.id)}
                failedReply={failedId ? byId.get(failedId) : undefined}
                pairedAway={pairedAway.has(m.id)}
                isNew={m.id === newestId && initialScrollDone.current}
                onImageLoad={onImageLoad}
              />
            );
          })}
        </div>
        {messages.length === 0 && !state.hasOlderOnServer && (
          <div className="greeting" data-testid="greeting">
            <span className="greeting-face">
              <FaceImage face={face} size={120} crop="large" />
            </span>
            <p className="greeting-title">Hi, I'm Marlow.</p>
            {state.awaitingFirstFetch ? (
              <p className="greeting-sub">
                <span className="spinner" aria-hidden="true" /> Loading your conversation…
              </p>
            ) : (
              <p className="greeting-sub">Ask me anything, or hand me something to keep track of.</p>
            )}
          </div>
        )}
        {far && (
          <button type="button" className="jump" aria-label="Jump to latest" onClick={jump} data-testid="jump">
            ↓{unseen && <span className="unseen-dot" data-testid="unseen-dot" />}
          </button>
        )}
      </div>

      <footer className="foot">
        <ConnectionIndicator state={state.connection} />
        {state.notice && (
          <div className="notice" role="status">
            {state.notice}
          </div>
        )}
        {ideas.spotlight && (
          <SpotlightCard
            idea={ideas.spotlight}
            onOpen={openIdeas}
            onDoIt={() => doIt(ideas.spotlight!)}
            onNotNow={() => ideas.dismiss(ideas.spotlight!.id, null)}
          />
        )}
        <Composer store={store} voice={composerVoice} />
      </footer>

      {voiceOpen && <VoiceMode store={store} api={api} onClose={() => setVoiceOpen(false)} />}

      {state.pendingPermission && (
        <PermissionCard
          key={state.pendingPermission.request_id}
          request={state.pendingPermission}
          onAnswer={(d, id) => store.answerPermission(d, id)}
          onExpire={(id) => store.expirePermission(id)}
        />
      )}
      {sheet === "ideas" && (
        <IdeasPanel
          ideas={ideas.ideas}
          dismissed={ideas.dismissed}
          today={today}
          onDoIt={doIt}
          onDismiss={ideas.dismiss}
          onClose={() => setSheet(null)}
        />
      )}
      {sheet === "marlow" && <MarlowSheet api={api} face={face} onFace={onFace} onClose={() => setSheet(null)} />}
      {sheet === "settings" && (
        <Settings
          api={api}
          token={token}
          reauthNeeded={state.reauthNeeded}
          queuePaused={state.queuePaused}
          effort={state.effort}
          serverModel={state.serverModel}
          serverBuild={state.serverBuild}
          onEffort={onEffort}
          locationSharing={locationState.sharing}
          locationStatus={locationState.status}
          onLocationSharing={(s) => location.setSharing(s)}
          face={face}
          onFace={onFace}
          onUnpair={onUnpair}
          onClose={() => setSheet(null)}
          whatsNew={whatsNew}
        />
      )}
      {/* "Marlow was updated" (docs/tasks/whats-new.md). Not urgent, so it
          waits behind everything: never over the approval card, a sheet, voice
          mode or a recording, and it shows once they are out of the way. */}
      {whatsNew.pending.length > 0 &&
        sheet === null &&
        !voiceOpen &&
        !dictating &&
        !state.pendingPermission && (
          <WhatsNewCard
            entries={whatsNew.pending}
            face={face}
            onDone={whatsNew.acknowledge}
          />
        )}
    </div>
  );
}

function StatusStrip({ items }: { items: { id: string; tone: string; text: string }[] }) {
  const [expanded, setExpanded] = useState(false);
  // STA-2: collapse whenever fewer than two remain. Driven by the count,
  // which outlives the content, so the last status clearing resets it.
  useEffect(() => {
    if (items.length < 2) setExpanded(false);
  }, [items.length]);
  if (items.length === 0) return null;
  const shown = expanded ? items : items.slice(0, 1);
  return (
    <div className="status-strip" data-testid="status-strip">
      {shown.map((b) => (
        <div key={b.id} className={`banner banner-${b.tone}`} data-banner={b.id}>
          {b.text}
        </div>
      ))}
      {items.length > 1 && (
        <button type="button" className="banner-more" onClick={() => setExpanded((x) => !x)}>
          {expanded ? "Less" : `+${items.length - 1}`}
        </button>
      )}
    </div>
  );
}

function ConnectionIndicator({ state }: { state: ReturnType<ChatStore["getState"]>["connection"] }) {
  if (state === "idle") return null;
  return (
    <div className={`connection connection-${state}`} data-testid="connection" role="status">
      {state === "checking" && <span className="spinner" aria-label="Checking" />}
      {state === "unreachable" && <>⚠ Can't reach Marlow</>}
      {state === "trouble" && <>⚠ Marlow's server is answering with errors</>}
    </div>
  );
}
