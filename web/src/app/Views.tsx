import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import type { Api } from "../lib/api";
import * as ActivityFormat from "../lib/activityFormat";
import { kvGet, kvSet, KV_SHOW_BACKGROUND } from "../lib/storage";
import {
  decodeActivityOutput,
  decodeActivityPage,
  decodeIdentityFile,
  decodeIdentityIndex,
  type ActivityEntry,
  type IdentityFile,
  type IdentityFileInfo,
  type IdentityIndex,
  type ScheduleEntry,
  type UsageReport,
} from "../lib/apiModels";
import * as IdentityFormat from "../lib/identityFormat";
import * as UsageFormat from "../lib/usageFormat";
import * as ScheduleFormat from "../lib/scheduleFormat";
import { SettledMarkdown } from "../markdown/Markdown";
import {
  daySections,
  displayText,
  fileOfKind,
  isMarkdownId,
  memoryFiles,
  mergeEntries,
  timeText,
} from "./views/viewModel";
import type { Face } from "../lib/faces";
import { FaceGrid, FaceImage } from "./Face";
import "./views.css";

type ViewApi = Pick<Api, "activity" | "activityOutput" | "identity" | "identityFile" | "usage">;

type PanelId = "activity" | "usage" | "schedule" | "memory" | "you" | "soul";

const PANELS: { id: PanelId; label: string; icon: ReactNode }[] = [
  { id: "activity", label: "Activity", icon: <path d="M12 7v5l3 2M3.5 12a8.5 8.5 0 1 0 2.6-6.1M3 4v4h4" /> },
  {
    id: "usage",
    label: "Usage",
    icon: <path d="M4.5 17a8.5 8.5 0 1 1 15 0M12 14l3.5-4.5M12 15.5a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3z" />,
  },
  { id: "schedule", label: "Schedule", icon: <path d="M12 8v5l3 2M5 4 2.5 6.5M19 4l2.5 2.5M12 21a8 8 0 1 0 0-16 8 8 0 0 0 0 16z" /> },
  {
    id: "memory",
    label: "Memory",
    icon: (
      <>
        <path d="M12 4.5v15M12 6.5a3 3 0 0 0-5.5-1.2 3 3 0 0 0-2 4.7 3 3 0 0 0 .5 5 3 3 0 0 0 7 1.5" />
        <path d="M12 6.5a3 3 0 0 1 5.5-1.2 3 3 0 0 1 2 4.7 3 3 0 0 1-.5 5 3 3 0 0 1-7 1.5" />
      </>
    ),
  },
  { id: "you", label: "You", icon: <path d="M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4.5 20a7.5 7.5 0 0 1 15 0" /> },
  { id: "soul", label: "Soul", icon: <path d="M12 3l2 6 6 2-6 2-2 6-2-6-6-2 6-2z" /> },
];

interface MarlowSheetProps {
  api: ViewApi;
  face: Face;
  onFace: (face: Face) => void;
  onClose: () => void;
  /** Injected for tests; default to the browser's zone and clock. */
  timeZone?: string;
  now?: () => number;
}

/** What the avatar opens: Marlow's standing state in one sheet (iOS
 * `MarlowView`). A strip of six labelled icons; the selected one takes the
 * accent and the panel below switches. Read-only throughout.
 *
 * Panels mount the first time they are selected and then stay (hidden), so
 * switching back does not refetch or lose a scroll position, as iOS's sheet
 * owns both stores. The identity index is fetched once per open and shared by
 * the four panels that read it. */
export function MarlowSheet({ api, face, onFace, onClose, timeZone, now }: MarlowSheetProps) {
  const zone = timeZone ?? Intl.DateTimeFormat().resolvedOptions().timeZone;
  const clock = now ?? Date.now;
  const [selected, setSelected] = useState<PanelId>("activity");
  const [visited, setVisited] = useState<ReadonlySet<PanelId>>(new Set(["activity"]));
  const [openFile, setOpenFile] = useState<OpenTarget | null>(null);
  const [choosingFace, setChoosingFace] = useState(false);
  const pushed = openFile !== null || choosingFace;
  const identity = useIdentityIndex(api);

  const select = (id: PanelId) => {
    setSelected(id);
    setVisited((v) => (v.has(id) ? v : new Set(v).add(id)));
  };

  return (
    <div className="sheet" role="dialog" aria-label="Marlow">
      <header className="sheet-header">
        {pushed ? (
          <button
            type="button"
            className="view-back"
            onClick={() => {
              setOpenFile(null);
              setChoosingFace(false);
            }}
          >
            ‹ Back
          </button>
        ) : (
          <span />
        )}
        {openFile && <h2>{openFile.title}</h2>}
        {choosingFace && <h2>Appearance</h2>}
        <button type="button" onClick={onClose}>
          Done
        </button>
      </header>
      {/* Marlow's face heads the sheet with the name under it, in place of a
          title in the bar (iOS FaceHeader). A tap opens the face picker
          (Mike, 2026-10-16). */}
      {!pushed && (
        <div className="sheet-face">
          <button type="button" className="sheet-face-button" aria-label="Appearance" onClick={() => setChoosingFace(true)}>
            <FaceImage face={face} size={64} crop="header" />
          </button>
          <h2>Marlow</h2>
        </div>
      )}
      {!pushed && (
        <div className="panel-strip">
          {PANELS.map((p) => (
            <button
              key={p.id}
              type="button"
              className="panel-button"
              aria-pressed={selected === p.id}
              onClick={() => select(p.id)}
            >
              <svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                {p.icon}
              </svg>
              <span>{p.label}</span>
            </button>
          ))}
        </div>
      )}
      <div className="sheet-body">
        {openFile && <FileView api={api} target={openFile} zone={zone} />}
        {choosingFace && (
          <div className="sheet-faces">
            <FaceGrid face={face} onFace={onFace} scrollToCurrent />
          </div>
        )}
        <div hidden={pushed}>
          <div hidden={selected !== "activity"}>
            <ActivityPanel api={api} zone={zone} now={clock} />
          </div>
          {visited.has("usage") && (
            <div hidden={selected !== "usage"}>
              <UsagePanel api={api} active={selected === "usage"} zone={zone} now={clock} />
            </div>
          )}
          {visited.has("schedule") && (
            <div hidden={selected !== "schedule"}>
              <IdentityGate identity={identity}>
                {(index) => <SchedulePanel schedules={index.schedules} zone={zone} now={clock} onOpen={setOpenFile} />}
              </IdentityGate>
            </div>
          )}
          {visited.has("memory") && (
            <div hidden={selected !== "memory"}>
              <IdentityGate identity={identity}>
                {(index) => <MemoryPanel files={index.files} now={clock} onOpen={setOpenFile} />}
              </IdentityGate>
            </div>
          )}
          {visited.has("you") && (
            <div hidden={selected !== "you"}>
              <IdentityGate identity={identity}>
                {(index) => <KindPanel api={api} files={index.files} kind="user" emptyText="USER.md isn't there yet." zone={zone} onOpen={setOpenFile} />}
              </IdentityGate>
            </div>
          )}
          {visited.has("soul") && (
            <div hidden={selected !== "soul"}>
              <IdentityGate identity={identity}>
                {(index) => <KindPanel api={api} files={index.files} kind="soul" emptyText="SOUL.md isn't there yet." zone={zone} onOpen={setOpenFile} showsInstructions />}
              </IdentityGate>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function errorText(prefix: string, e: unknown): string {
  const msg = e instanceof Error ? e.message : String(e);
  return `${prefix} ${msg}`;
}

function ErrorBlock({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="view-error" role="alert">
      <p>{message}</p>
      <button type="button" onClick={onRetry}>
        Retry
      </button>
    </div>
  );
}

// MARK: Activity

/** What a background job said, fetched when its row is first expanded. */
type Output = { state: "loading" } | { state: "failed" } | { state: "loaded"; text: string };

function ActivityPanel({ api, zone, now }: { api: ViewApi; zone: string; now: () => number }) {
  const [entries, setEntries] = useState<ActivityEntry[]>([]);
  const [nextBefore, setNextBefore] = useState<number | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  // Per device, not a synced preference: whether to look at the harness's own
  // upkeep is a viewing choice, and two devices may reasonably differ
  // (docs/tasks/default-infrastructure.md). On by default: Activity defaults
  // to transparency, and hiding the upkeep is the opt-out (Mike, 2026-10-21).
  const [showBackground, setShowBackground] = useState(true);
  const [outputs, setOutputs] = useState<ReadonlyMap<string, Output>>(new Map());
  const busy = useRef(false);

  useEffect(() => {
    let live = true;
    kvGet<unknown>(KV_SHOW_BACKGROUND)
      .then((v) => {
        if (live && v === false) setShowBackground(false);
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, []);

  const load = useCallback(
    async (before: number | undefined, replace: boolean) => {
      if (busy.current) return;
      busy.current = true;
      setLoading(true);
      try {
        const page = decodeActivityPage(await api.activity(before));
        setEntries((held) => (replace ? page.entries : mergeEntries(held, page.entries)));
        setNextBefore(page.nextBefore);
        setError(null);
        setLoaded(true);
      } catch (e) {
        setError(errorText("Couldn't load activity.", e));
      } finally {
        busy.current = false;
        setLoading(false);
      }
    },
    [api],
  );

  useEffect(() => {
    void load(undefined, true);
  }, [load]);

  // Ids loading or loaded, kept in a ref rather than read from `outputs` so two
  // taps before a re-render can't both see "not fetched yet". A failure leaves
  // it, so expanding the row again retries.
  const fetched = useRef(new Set<string>());
  const fetchOutput = useCallback(
    async (turnId: string) => {
      if (fetched.current.has(turnId)) return;
      fetched.current.add(turnId);
      setOutputs((m) => new Map(m).set(turnId, { state: "loading" }));
      let next: Output;
      try {
        const text = decodeActivityOutput(await api.activityOutput(turnId));
        next = text === null ? { state: "failed" } : { state: "loaded", text };
      } catch {
        next = { state: "failed" };
      }
      if (next.state === "failed") fetched.current.delete(turnId);
      setOutputs((m) => new Map(m).set(turnId, next));
    },
    [api],
  );

  const sections = daySections(entries, { timeZone: zone, now: now(), showBackground });
  const hasMore = nextBefore !== null;

  const changeShowBackground = (on: boolean) => {
    setShowBackground(on);
    void kvSet(KV_SHOW_BACKGROUND, on).catch(() => {});
  };
  // Above the list and the empty state alike: with only background jobs in the
  // log, the list is empty until this is on.
  const backgroundToggle = (
    <label className="view-toggle">
      <input type="checkbox" checked={showBackground} onChange={(e) => changeShowBackground(e.target.checked)} />
      Show background jobs
    </label>
  );

  if (sections.length === 0 && !hasMore) {
    if (error) return <ErrorBlock message={error} onRetry={() => void load(undefined, true)} />;
    if (!loaded) return <p className="sheet-empty">Loading…</p>;
    return (
      <div>
        {backgroundToggle}
        <p className="sheet-empty">
          Nothing yet. Scheduled jobs, and the files and services Marlow uses, show up here.
        </p>
      </div>
    );
  }

  const toggle = (entry: ActivityEntry) => {
    const id = entry.turnId;
    setExpanded((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
    if (entry.hasOutput) void fetchOutput(id);
  };

  return (
    <div>
      {backgroundToggle}
      {sections.map((sec) => (
        <section key={sec.title}>
          <h3 className="view-day">{sec.title}</h3>
          <ul className="view-list">
            {sec.entries.map((entry) => (
              <li key={entry.turnId}>
                <ActivityRow
                  entry={entry}
                  zone={zone}
                  isExpanded={expanded.has(entry.turnId)}
                  output={outputs.get(entry.turnId)}
                  onToggle={() => toggle(entry)}
                />
              </li>
            ))}
          </ul>
        </section>
      ))}
      {error && (
        <ErrorBlock
          message={error}
          onRetry={() => void (hasMore ? load(nextBefore ?? undefined, false) : load(undefined, true))}
        />
      )}
      {hasMore && !error && (
        <button type="button" className="view-more" disabled={loading} onClick={() => void load(nextBefore ?? undefined, false)}>
          {loading ? "Loading…" : "Load more"}
        </button>
      )}
      <p className="view-foot">Chat messages that used no tools aren't listed — they're in the conversation.</p>
    </div>
  );
}

function ActivityRow({
  entry,
  zone,
  isExpanded,
  output,
  onToggle,
}: {
  entry: ActivityEntry;
  zone: string;
  isExpanded: boolean;
  output: Output | undefined;
  onToggle: () => void;
}) {
  const summary = ActivityFormat.summaryLine(entry);
  const flags = ActivityFormat.flags(entry);
  const run = ActivityFormat.runLine(entry);
  const background = ActivityFormat.isBackground(entry);
  return (
    <button
      type="button"
      className={background ? "view-row view-row-background" : "view-row"}
      aria-expanded={isExpanded}
      onClick={onToggle}
    >
      <span className="view-row-head">
        <span className="view-title">{ActivityFormat.title(entry)}</span>
        <span className="view-when">{timeText(entry.finishedAt, zone)}</span>
      </span>
      {summary !== null && <div className="view-sub">{summary}</div>}
      {flags.length > 0 && (
        <div className="view-flags">
          {flags.map((f, i) => (
            <span key={i}>
              {i > 0 && " · "}
              <span className={f.isWarning ? "view-flag-warn" : undefined}>{f.text}</span>
            </span>
          ))}
        </div>
      )}
      {isExpanded && (
        <>
          <ul className="view-detail">
            {ActivityFormat.detailLines(entry).map((line, i) => {
              const count = ActivityFormat.countText(line);
              return (
                <li key={i}>
                  <span className="view-detail-label">{line.label}</span>
                  {line.target !== null && <span className="view-detail-target">{line.target}</span>}
                  {count !== null && <span className="view-detail-count">{count}</span>}
                </li>
              );
            })}
          </ul>
          {entry.hasOutput && output !== undefined && (
            <div className={output.state === "failed" ? "view-output view-output-failed" : "view-output"}>
              {output.state === "loading"
                ? "Loading…"
                : output.state === "failed"
                  ? "Couldn't load what it said."
                  : ActivityFormat.outputText(output.text)}
            </div>
          )}
          {run !== null && <div className="view-took">{run}</div>}
        </>
      )}
    </button>
  );
}

// MARK: Usage

/** The Usage panel (iOS `UsagePanel`, docs/tasks/usage-visibility.md): the
 * plan's windows, how big this conversation has got, and what Marlow itself
 * has been doing. Read-only and quiet by design: nothing here is worded as a
 * warning. All wording comes from usageFormat.ts.
 *
 * Read fresh each time the panel is shown, as iOS's `.task` does: the panel
 * stays mounted once visited, and a reading left on screen goes stale as
 * Marlow runs. A later failure keeps what is shown. */
function UsagePanel({ api, active, zone, now }: { api: ViewApi; active: boolean; zone: string; now: () => number }) {
  const [report, setReport] = useState<UsageReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const busy = useRef(false);

  const load = useCallback(async () => {
    if (busy.current) return;
    busy.current = true;
    try {
      setReport(await api.usage());
      setError(null);
    } catch (e) {
      setError(errorText("Couldn't load usage.", e));
    } finally {
      busy.current = false;
    }
  }, [api]);

  useEffect(() => {
    if (active) void load();
  }, [active, load]);

  if (!report) {
    return error ? <ErrorBlock message={error} onRetry={() => void load()} /> : <p className="sheet-empty">Loading…</p>;
  }
  const clock = { timeZone: zone, now: now() };
  const rows = report.plan ? UsageFormat.planRows(report.plan, clock) : [];
  const name = UsageFormat.planName(report.subscriptionType);
  const asOf = report.plan ? UsageFormat.asOfTime(report.plan, clock) : null;
  const conv = report.conversation ? UsageFormat.conversation(report.conversation, clock) : null;
  const history = conv ? UsageFormat.history(conv) : null;
  const parts = report.marlow ? UsageFormat.marlowParts(report.marlow) : [];

  return (
    <div>
      <section aria-label={name}>
        <h3 className="view-day usage-head">
          <span>{name}</span>
          {asOf !== null && <span className="usage-asof">{asOf}</span>}
        </h3>
        {rows.length > 0 ? (
          <>
            {rows.map((row) => (
              <UsageBar key={row.id} title={row.title} fraction={row.fraction} detail={row.detail} high={row.isHigh} />
            ))}
            <p className="usage-foot">{UsageFormat.planFootnote(name)}</p>
          </>
        ) : (
          <p className="usage-quiet">{UsageFormat.NO_READING}</p>
        )}
      </section>

      <section aria-label="This conversation">
        <h3 className="view-day">This conversation</h3>
        {conv ? (
          <div className="view-row">
            {conv.size !== null &&
              (conv.fraction !== null ? (
                <UsageBar title={conv.size} fraction={conv.fraction} detail={conv.thresholdText ?? ""} bare />
              ) : (
                <div className="view-title">{conv.size}</div>
              ))}
            {conv.notice !== null && <div className="view-sub">{conv.notice}</div>}
            {history !== null && <div className="view-sub">{history}</div>}
          </div>
        ) : (
          <p className="usage-quiet">{UsageFormat.NO_READING}</p>
        )}
      </section>

      <section aria-label="Marlow's turns">
        <h3 className="view-day">Marlow's turns</h3>
        {parts.length === 0 ? (
          <p className="usage-quiet">{UsageFormat.NO_READING}</p>
        ) : (
          parts.map((part) => (
            <div key={part.id} className="view-row">
              <div className="view-title">{part.title}</div>
              {/* A bare title would read as a missing row. */}
              <div className={part.counts !== null ? "view-sub usage-num" : "view-sub"}>{part.counts ?? UsageFormat.NO_TURNS}</div>
            </div>
          ))
        )}
      </section>
    </div>
  );
}

/** A title, a bar and a line under it. A `<progress>` because the CSP forbids
 * the inline width a drawn bar would need. */
function UsageBar({
  title,
  fraction,
  detail,
  high = false,
  bare = false,
}: {
  title: string;
  fraction: number;
  detail: string;
  high?: boolean;
  bare?: boolean;
}) {
  const body = (
    <>
      <div className="view-title">{title}</div>
      <progress className={high ? "usage-bar usage-bar-high" : "usage-bar"} value={fraction} max={1} aria-label={title} />
      {detail !== "" && <div className="view-sub usage-num">{detail}</div>}
    </>
  );
  return bare ? body : <div className="view-row">{body}</div>;
}

// MARK: Identity

type OpenTarget = { id: string; title: string; kind: IdentityFileInfo["kind"] };

interface IdentityLoad {
  index: IdentityIndex | null;
  error: string | null;
  reload: () => void;
}

function useIdentityIndex(api: ViewApi): IdentityLoad {
  const [index, setIndex] = useState<IdentityIndex | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setIndex(decodeIdentityIndex(await api.identity()));
    } catch (e) {
      setError(errorText("Couldn't load Marlow's files.", e));
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  return { index, error, reload: () => void load() };
}

/** Loading and error states around the identity-backed panels; the children
 * only render once an index is in hand. */
function IdentityGate({ identity, children }: { identity: IdentityLoad; children: (index: IdentityIndex) => ReactNode }) {
  if (identity.index) return <>{children(identity.index)}</>;
  return identity.error ? (
    <ErrorBlock message={identity.error} onRetry={identity.reload} />
  ) : (
    <p className="sheet-empty">Loading…</p>
  );
}

function SchedulePanel({
  schedules,
  zone,
  now,
  onOpen,
}: {
  schedules: IdentityIndex["schedules"];
  zone: string;
  now: () => number;
  onOpen: (t: OpenTarget) => void;
}) {
  // Times read in the store's zone: the zone its wall-clock times are in. In
  // the browser's zone "8:00" turns into "6:00" whenever Mike is away from it,
  // which is true, and reads as a bug.
  const scheduleZone = IdentityFormat.calendarZone(schedules.timezone, zone);
  const nowMs = now();
  const groups = ScheduleFormat.sections(schedules.entries);
  return (
    <div>
      {schedules.rejected !== null && (
        <div className="view-warn-box" role="alert">
          Marlow's schedule file has an error. The last good version is still running.
          <div className="view-mono">{schedules.rejected}</div>
        </div>
      )}
      {groups.length === 0 && schedules.rejected === null && (
        <p className="sheet-empty">No schedules. Reminders and recurring jobs Marlow sets up show up here.</p>
      )}
      {groups.map((g) => (
        <section key={g.section} aria-label={g.section}>
          <h3 className="view-day">{g.section}</h3>
          <ul className="view-list">
            {g.entries.map((entry) => (
              <li key={entry.id}>
                <ScheduleRow
                  entry={entry}
                  zone={scheduleZone}
                  now={nowMs}
                  onOpen={entry.jobFile ? () => onOpen({ id: entry.jobFile!, title: ActivityFormat.jobTitle(entry.id), kind: "job" }) : undefined}
                />
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

function MemoryPanel({
  files,
  now,
  onOpen,
}: {
  files: readonly IdentityFileInfo[];
  now: () => number;
  onOpen: (t: OpenTarget) => void;
}) {
  const memory = memoryFiles(files);
  if (memory.length === 0) {
    return <p className="sheet-empty">No memory files. What Marlow remembers between conversations shows up here.</p>;
  }
  const nowMs = now();
  return (
    <ul className="view-list">
      {memory.map((file) => {
        const name = IdentityFormat.title(file.id, file.kind);
        return (
          <li key={file.id}>
            <button type="button" className="view-row" onClick={() => onOpen({ id: file.id, title: name, kind: file.kind })}>
              <span className="view-title">{name}</span>
              {file.description !== null && file.description !== "" && <div className="view-sub">{file.description}</div>}
              <div className="view-flags">{IdentityFormat.changed(file.modifiedAt, nowMs)}</div>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

/** You and Soul: the file rendered in place; Soul adds the row that opens CLAUDE.md. */
function KindPanel({
  api,
  files,
  kind,
  emptyText,
  zone,
  onOpen,
  showsInstructions = false,
}: {
  api: ViewApi;
  files: readonly IdentityFileInfo[];
  kind: "user" | "soul";
  emptyText: string;
  zone: string;
  onOpen: (t: OpenTarget) => void;
  showsInstructions?: boolean;
}) {
  const file = fileOfKind(files, kind);
  if (!file) return <p className="sheet-empty">{emptyText}</p>;
  const instructions = showsInstructions ? fileOfKind(files, "instructions") : null;
  return (
    <div>
      <FileView api={api} target={{ id: file.id, title: IdentityFormat.title(file.id, file.kind), kind: file.kind }} zone={zone} />
      {instructions && (
        <button
          type="button"
          className="view-link-row"
          onClick={() => onOpen({ id: instructions.id, title: "Instructions", kind: "instructions" })}
        >
          <span>Instructions</span>
          <span aria-hidden="true">›</span>
        </button>
      )}
    </div>
  );
}

function ScheduleRow({
  entry,
  zone,
  now,
  onOpen,
}: {
  entry: ScheduleEntry;
  zone: string;
  now: number;
  onOpen?: () => void;
}) {
  const overdue = ScheduleFormat.isOverdue(entry, now);
  const text = entry.notify ?? entry.prompt;
  const body = (
    <>
      <span className="view-row-head">
        <span className="view-title">{ActivityFormat.jobTitle(entry.id)}</span>
        <span className={overdue ? "view-when overdue" : "view-when"}>
          {ScheduleFormat.whenText(entry, { timeZone: zone, now })}
          {overdue && " (overdue)"}
        </span>
      </span>
      {text !== null && text !== "" && <div className="view-sub">{text}</div>}
    </>
  );
  return onOpen ? (
    <button type="button" className="view-row" onClick={onOpen}>
      {body}
    </button>
  ) : (
    <div className="view-row">{body}</div>
  );
}

function FileView({
  api,
  target,
  zone,
}: {
  api: ViewApi;
  target: OpenTarget;
  zone: string;
}) {
  const [file, setFile] = useState<IdentityFile | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const f = decodeIdentityFile(await api.identityFile(target.id));
      if (f === null) throw new Error("The server's answer could not be read.");
      setFile(f);
    } catch (e) {
      setError(errorText("Couldn't load this file.", e));
    }
  }, [api, target.id]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!file) {
    return error ? <ErrorBlock message={error} onRetry={() => void load()} /> : <p className="sheet-empty">Loading…</p>;
  }
  const kind = file.kind === "unknown" ? target.kind : file.kind;
  const when = new Intl.DateTimeFormat("en-US", { timeZone: zone, dateStyle: "medium", timeStyle: "short" }).format(
    new Date(file.modifiedAt * 1000),
  );
  return (
    <div>
      <div className="view-note">
        <strong>About this file</strong>
        <div>{IdentityFormat.aboutNote(kind)}</div>
      </div>
      <p className="view-foot">Last changed {when}</p>
      {isMarkdownId(file.id) ? (
        <div className="view-filetext">
          <SettledMarkdown text={displayText(file.text)} />
        </div>
      ) : (
        <pre className="view-filetext">{file.text}</pre>
      )}
      {file.truncated && <p className="view-foot">Showing the first 256 KB.</p>}
    </div>
  );
}
