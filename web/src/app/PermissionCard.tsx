import { useEffect, useRef, useState } from "react";
import type { PermissionDecision, PermissionPreviewLine, PermissionRequestInfo } from "../lib/types";
import { feedbackApproval } from "./feedback";

export const ARM_DELAY_MS = 500;
/** The bar turns orange for the last this-many seconds. */
export const URGENT_SECONDS = 10;

/** The tool-approval card (iOS `PermissionCard`, redesigned 2026-10-02):
 * a dimming layer and a card in the middle of the screen. Rendered outside
 * the transcript, so opening and closing it cannot move the scroll position
 * (SCR-16).
 *
 * **A card, not a sheet.** It hugs its content and never grows past 60% of
 * the viewport, so what Mike just asked for stays visible above and below it.
 * Past that the content scrolls and the buttons stay pinned underneath: the
 * decision is never scrolled out of reach.
 *
 * **The card never states what the model says as fact.** `summary.title` and
 * `summary.preview` are lifted by the server from the tool input itself;
 * `summary.marlowSays` is the model's own description, which a prompt
 * injection can write as easily as an honest turn can, so it is drawn as a
 * labelled quotation in a style nothing factual uses. The raw input is always
 * one click away ("Show details"): the card is a reading aid, never the only
 * view of what is being approved.
 *
 * Modal, and dismissible only by answering or timing out (PRM-2): Escape and
 * a click on the backdrop do nothing, since there is no "dismiss" for a
 * request the agent is waiting on. */
export function PermissionCard({
  request,
  onAnswer,
  onExpire,
}: {
  request: PermissionRequestInfo;
  /** Async so the card can keep its buttons off while the answer is in
   * flight: a second click otherwise posts a second answer, and the server's
   * "already resolved" reply shows up as a false "too late" notice. */
  onAnswer: (d: PermissionDecision, requestId: string) => Promise<void>;
  onExpire: (id: string) => void;
}) {
  // Live only after a beat: a tap already on its way when this request
  // appeared was aimed at whatever was on screen before. The card is keyed by
  // request id, so a new request remounts it and re-arms this.
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [expired, setExpired] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const dialogRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const t = setTimeout(() => setArmed(true), ARM_DELAY_MS);
    return () => clearTimeout(t);
  }, []);

  useEffect(() => {
    dialogRef.current?.focus();
  }, [request.request_id]);

  // The approval sound, once per request: the card is keyed by request id,
  // so a new request remounts it.
  useEffect(() => {
    feedbackApproval();
  }, []);

  const summary = request.summary;
  const enabled = armed && !expired && !busy;
  const raw = Object.keys(request.tool_input)
    .sort()
    .map((k) => `${k}: ${formatValue(request.tool_input[k])}`)
    .join("\n");

  const decide = (d: PermissionDecision) => {
    setBusy(true);
    void onAnswer(d, request.request_id).finally(() => setBusy(false));
  };

  return (
    <div className="modal-backdrop perm-backdrop">
      <div
        className="perm-card"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="perm-title"
        tabIndex={-1}
        ref={dialogRef}
        data-testid="permission"
        onKeyDown={(e) => {
          if (e.key === "Escape") e.preventDefault();
        }}
      >
        <TimeBar
          request={request}
          onExpire={() => {
            setExpired(true);
            onExpire(request.request_id);
          }}
        />
        <div className="perm-content">
          <h2 id="perm-title" className="perm-header">
            <span className="perm-lead">{summary ? "Marlow wants to" : "Marlow wants to use"}</span>
            <span className="perm-title">{summary?.title ?? request.tool}</span>
          </h2>
          {summary?.marlowSays && (
            <div className="perm-says" aria-label={`Marlow says: ${summary.marlowSays}`}>
              <span className="perm-says-label">Marlow says</span>
              <q className="perm-says-text">{summary.marlowSays}</q>
            </div>
          )}
          {summary && summary.preview.length > 0 && <Preview lines={summary.preview} />}
          <div className="perm-details">
            <pre className={detailsOpen ? "perm-input open" : "perm-input"} data-testid="perm-input">
              {raw || "(no input)"}
            </pre>
            <button
              type="button"
              className="perm-details-toggle"
              aria-expanded={detailsOpen}
              onClick={() => setDetailsOpen((o) => !o)}
            >
              {detailsOpen ? "Hide details" : "Show details"}
            </button>
          </div>
        </div>
        <div className="perm-actions">
          <div className="perm-row">
            <button type="button" onClick={() => decide("deny")} disabled={!enabled}>
              Deny
            </button>
            <button type="button" className="primary" onClick={() => decide("allow")} disabled={!enabled}>
              Allow
            </button>
          </div>
          {summary?.similar && (
            <div className="perm-similar">
              <button type="button" onClick={() => decide("allow_similar")} disabled={!enabled}>
                Allow similar for this reply
              </button>
              <span className="perm-similar-note">Covers {summary.similar.label}</span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/** Drains from full to empty between the request's creation and the server's
 * own deadline. Deliberately a bar with no number: a ticking "12s left" made
 * the old sheet feel like a bomb timer (Mike, 2026-10-02), and the decision is
 * not meant to be rushed. It turns orange only for the last ten seconds. At
 * zero the client clears the card itself, as a backstop to the next poll. */
function TimeBar({ request, onExpire }: { request: PermissionRequestInfo; onExpire: () => void }) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now() / 1000), 100);
    return () => clearInterval(t);
  }, [request.request_id]);

  const remaining = Math.max(0, request.expires_at - now);
  const total = Math.max(1, request.expires_at - request.created_at);
  const fraction = Math.min(1, remaining / total);
  const done = remaining <= 0;

  const expire = useRef(onExpire);
  expire.current = onExpire;
  useEffect(() => {
    if (done) expire.current();
  }, [done]);

  return (
    <progress
      className={remaining <= URGENT_SECONDS ? "perm-time urgent" : "perm-time"}
      value={fraction}
      max={1}
      data-testid="time-bar"
      aria-label={`Time left to decide: ${Math.ceil(remaining)} seconds`}
    />
  );
}

/** The facts lifted from the request, as a diff: removed lines on red, added
 * on green, anything else plain. One line each, cut with an ellipsis; the
 * full text is in the details. */
function Preview({ lines }: { lines: PermissionPreviewLine[] }) {
  return (
    <ul className="perm-preview">
      {lines.map((line, i) => (
        <li key={i} className={`perm-line perm-line-${line.op}`}>
          <span className="perm-sign" aria-hidden="true">
            {line.op === "remove" ? "−" : line.op === "add" ? "+" : ""}
          </span>
          <span className="perm-line-text">
            {line.op === "remove" && <span className="visually-hidden">Removed: </span>}
            {line.op === "add" && <span className="visually-hidden">Added: </span>}
            {line.text}
          </span>
        </li>
      ))}
    </ul>
  );
}

function formatValue(v: unknown): string {
  if (typeof v === "string") return v;
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}
