// The activity line (STR-9, STR-10, STR-11). Port of `TurnActivity.label` /
// `describesUnseenWork` (APIModels.swift) and `ActivityPlaybackState`
// (ChatStore.swift). Pure: the clock is an argument, there are no timers.

import type { TurnActivity } from "./types";

// MARK: Labels (STR-10)

/** The two labels that describe work with nothing to show for it. Named rather
 * than written inline because the view has to recognise them: once the reply is
 * typing itself in the bubble, these say strictly less than the text above them
 * and are hidden. A tool label is the opposite: it is the only thing explaining
 * why the text has stopped. */
export const THINKING = "Thinking…";
export const WRITING = "Writing…";
export const USING_A_TOOL = "Using a tool…";

/** STR-11. Absent counts as unseen work (hidden once text has streamed). */
export function describesUnseenWork(label: string | null | undefined): boolean {
  if (label == null) return true;
  return label === THINKING || label === WRITING;
}

/** STR-11: the in-row status label for a running placeholder. Once the reply is
 * typing itself, the text IS the status, and a spinner saying "Thinking…" under
 * it says strictly less than the words moving above it (Mike, 2026-09-02). A
 * TOOL label is the exception and is kept: during an interleaved turn the text
 * stops mid-answer while the model goes back to a tool, and that label is then
 * the only thing explaining why. Returns null when nothing should be shown. */
export function runningLabel(
  activityLabel: string | null | undefined,
  hasStreamedText: boolean,
): string | null {
  if (hasStreamedText && describesUnseenWork(activityLabel)) return null;
  return activityLabel ?? THINKING;
}

/** Friendly, bounded copy for the UI. Tool names select known wording but are
 * NEVER rendered: MCP connectors control those names, so an unknown value is
 * arbitrary semi-trusted text rather than user-facing copy. First match wins. */
export function activityLabel(activity: Pick<TurnActivity, "kind" | "tool">): string {
  if (activity.kind !== "tool_start") {
    // A `text` event means the model is writing the reply, and since reply
    // streaming shipped that reply is visible in the bubble directly above this
    // line, so "Thinking…" over text that is typing itself contradicts what the
    // reader sees. Every other kind (thinking, tool_end, an unknown future one)
    // stays "Thinking…", still the honest word for them.
    return activity.kind === "text" ? WRITING : THINKING;
  }
  const tool = activity.tool;
  if (tool == null) return USING_A_TOOL;

  const n = tool.toLowerCase();
  // Our own onboarding server (`mcp__onboarding__step_done`): the assistant
  // reports a setup step finished. Prefix match, ahead of the substring
  // checks below, so a later service word cannot claim it.
  if (n.startsWith("mcp__onboarding__")) return "Setting things up…";
  if (n.includes("calendar")) return "Checking your calendar…";
  // Our own weather server (docs/tasks/weather-mcp.md): `mcp__weather__*`.
  // Its route tool routes and times a drive before any weather, so it gets
  // its own words (Mike, 2026-10-14).
  if (n.includes("route_forecast")) return "Planning the route…";
  if (n.includes("weather")) return "Checking the weather…";
  // Connector-named web search, e.g. `mcp__brave__web_search`. The exact
  // WebSearch/WebFetch cases below only catch the CLI's own tools, so a
  // connector doing the same job fell through to "Using a tool…", the least
  // informative label available on the leg that takes longest. Both halves
  // required, so `webflow` and friends do not become a web search.
  if (n.includes("web") && (n.includes("search") || n.includes("fetch") || n.includes("browse"))) {
    return "Searching the web…";
  }
  if (n.includes("gmail") || n.includes("mail")) return "Checking your mail…";

  switch (tool) {
    case "Read":
      return "Reading a file…";
    case "Write":
    case "Edit":
      return "Writing a file…";
    case "Bash":
      return "Running a command…";
    case "Grep":
    case "Glob":
      return "Searching…";
    case "WebSearch":
    case "WebFetch":
      return "Searching the web…";
    default:
      return USING_A_TOOL;
  }
}

// MARK: Playback (STR-9)

/** Show each label for at least this long; the last stays until a newer one, or
 * the terminal state, clears it. The client samples a stream every 2s: a 0.16s
 * tool leg is invisible to a latest-value poll, and three events in one poll
 * would flicker as one without the dwell. Do not shorten the poll to fix laggy
 * labels; the dwell is the knob. */
export const ACTIVITY_DWELL_MS = 700;
/** A client returning after a long gap should catch up to the recent shape of
 * the turn, not spend seconds narrating its distant past. */
export const MAX_PENDING_LABELS = 3;

export interface ActivityPlaybackState {
  /** Labels queued behind the one on screen. */
  readonly pendingLabels: readonly string[];
  /** Deliberately NOT cleared when the queue drains: dedupe spans drains. */
  readonly lastQueuedLabel: string | null;
  readonly previousWindow: readonly TurnActivity[];
  /** The label on screen, and the `now` it went up. */
  readonly shownLabel: string | null;
  readonly shownAt: number | null;
}

/** A fresh state is also "cleared": a terminal status drops the label, the queue
 * and the window cursor together; there is no keep-the-cursor variant. */
export function createActivityPlaybackState(): ActivityPlaybackState {
  return {
    pendingLabels: [],
    lastQueuedLabel: null,
    previousWindow: [],
    shownLabel: null,
    shownAt: null,
  };
}

/** Equality includes `at`. It looks unused (nothing renders it) but it is the
 * only thing making two otherwise-identical events (two `Read`s, two
 * `tool_end`s) distinguishable. Drop it and a sliding window of repeated events
 * over-matches and silently swallows the new ones. */
export function sameActivity(a: TurnActivity, b: TurnActivity): boolean {
  return a.kind === b.kind && a.tool === b.tool && a.at === b.at;
}

/** Take a poll's cumulative `activities` window (oldest first, at most 64). */
export function ingestActivities(
  state: ActivityPlaybackState,
  activities: readonly TurnActivity[],
): ActivityPlaybackState {
  // The server keeps only its latest 64 events. Find the longest overlap between
  // our previous tail and its current prefix so a full window that slides by one
  // still yields that one new event. A count cursor alone would freeze forever
  // once both arrays stayed length 64.
  const prev = state.previousWindow;
  let overlap = Math.min(prev.length, activities.length);
  while (overlap > 0) {
    let match = true;
    const offset = prev.length - overlap;
    for (let i = 0; i < overlap; i++) {
      if (!sameActivity(prev[offset + i], activities[i])) {
        match = false;
        break;
      }
    }
    if (match) break;
    overlap -= 1;
  }

  let pending = [...state.pendingLabels];
  let last = state.lastQueuedLabel;
  for (const activity of activities.slice(overlap)) {
    const label = activityLabel(activity);
    if (label !== last) {
      pending.push(label);
      last = label;
    }
  }
  if (pending.length > MAX_PENDING_LABELS) pending = pending.slice(-MAX_PENDING_LABELS);
  return { ...state, pendingLabels: pending, lastQueuedLabel: last, previousWindow: [...activities] };
}

/** Pop the next queued label, ignoring the dwell. */
export function nextLabel(state: ActivityPlaybackState): {
  state: ActivityPlaybackState;
  label: string | null;
} {
  if (state.pendingLabels.length === 0) return { state, label: null };
  const [label, ...rest] = state.pendingLabels;
  return { state: { ...state, pendingLabels: rest }, label };
}

/** Advance the player at time `now` (ms): put up the next queued label if
 * nothing is showing or the current one has had its dwell. The last label stays
 * until something newer replaces it. Returns the same object when nothing
 * changed. */
export function playActivity(
  state: ActivityPlaybackState,
  now: number,
  dwellMs: number = ACTIVITY_DWELL_MS,
): ActivityPlaybackState {
  if (state.pendingLabels.length === 0) return state;
  if (state.shownAt !== null && now - state.shownAt < dwellMs) return state;
  const { state: popped, label } = nextLabel(state);
  return { ...popped, shownLabel: label, shownAt: now };
}

/** When `playActivity` next has something to do (ms), or null if the queue is
 * empty: lets the caller arm one timer instead of polling. */
export function nextActivityAt(
  state: ActivityPlaybackState,
  dwellMs: number = ACTIVITY_DWELL_MS,
): number | null {
  if (state.pendingLabels.length === 0) return null;
  return state.shownAt === null ? 0 : state.shownAt + dwellMs;
}
