import { useRef, useState } from "react";
import type { Idea } from "../lib/apiModels";
import type { IdeaQuote } from "../lib/types";
import { isNearing, sectionsOf, untilText } from "../lib/ideas";

/** What a "Do it" sends as text. The bubble quotes the idea above it, so the
 * words themselves are only the go-ahead. */
export const DO_IT_TEXT = "Do it";

/** The quote the optimistic bubble carries, before the server row says so. */
export function quoteOf(idea: Idea): IdeaQuote {
  return { idea_id: idea.id, emoji: idea.emoji, title: idea.title };
}

export interface IdeasPanelProps {
  /** Every idea the chat screen holds, dismissed ones included (see
   * `useIdeas`). Which of those show is decided here. */
  ideas: Idea[];
  dismissed: ReadonlySet<string>;
  /** Phone-local `YYYY-MM-DD`. */
  today: string;
  onDoIt: (idea: Idea) => void;
  onDismiss: (id: string, reason: string | null) => void;
  onClose: () => void;
}

/** What the lightbulb opens (docs/tasks/ideas-panel.md): offers Marlow would
 * act on, in two sections, each a card with the plan and a one-tap Do it.
 *
 * Not now dismisses at once and the card collapses to a one-line row that
 * offers a reason; the reason re-posts the dismissal. Those rows belong to
 * this opening of the panel: an idea dismissed before it opened is simply not
 * listed, and the rows go with the panel when it closes. */
export function IdeasPanel({ ideas, dismissed, today, onDoIt, onDismiss, onClose }: IdeasPanelProps) {
  // What was already dismissed when the panel opened gets no row.
  const before = useRef(dismissed);
  const [reasonSent, setReasonSent] = useState<ReadonlySet<string>>(new Set());
  const listed = ideas.filter((i) => !dismissed.has(i.id) || !before.current.has(i.id));
  const sections = sectionsOf(listed, today);

  return (
    <div className="sheet" role="dialog" aria-label="Ideas">
      <header className="sheet-header">
        <h2>Ideas</h2>
        <button type="button" onClick={onClose}>
          Done
        </button>
      </header>
      <div className="sheet-body">
        {sections.length === 0 && <p className="sheet-empty">No ideas right now.</p>}
        {sections.map((section) => (
          <section key={section.id} aria-label={section.title}>
            <h3 className="view-day">{section.title}</h3>
            {section.ideas.map((idea) =>
              dismissed.has(idea.id) ? (
                <DismissedRow
                  key={idea.id}
                  idea={idea}
                  sent={reasonSent.has(idea.id)}
                  onReason={(reason) => {
                    onDismiss(idea.id, reason);
                    setReasonSent((s) => new Set(s).add(idea.id));
                  }}
                />
              ) : (
                <IdeaCard
                  key={idea.id}
                  idea={idea}
                  nearing={isNearing(idea, today)}
                  onDoIt={() => onDoIt(idea)}
                  onNotNow={() => onDismiss(idea.id, null)}
                />
              ),
            )}
          </section>
        ))}
      </div>
    </div>
  );
}

function IdeaCard({
  idea,
  nearing,
  onDoIt,
  onNotNow,
}: {
  idea: Idea;
  nearing: boolean;
  onDoIt: () => void;
  onNotNow: () => void;
}) {
  return (
    <article className="idea-card" aria-label={idea.title}>
      <h4 className="idea-title">
        <span className="idea-emoji" aria-hidden="true">
          {idea.emoji}
        </span>
        {idea.title}
      </h4>
      <p className="idea-plan">{idea.plan}</p>
      {idea.source && <p className="idea-source">{idea.source}</p>}
      <div className="idea-meta">
        {idea.category && <span className="idea-category">{idea.category}</span>}
        {nearing && idea.until && <span className="idea-nearing">{untilText(idea.until)}</span>}
      </div>
      <div className="idea-actions">
        <button type="button" className="primary" onClick={onDoIt}>
          Do it
        </button>
        <button type="button" className="idea-secondary" onClick={onNotNow}>
          Not now
        </button>
      </div>
    </article>
  );
}

function DismissedRow({ idea, sent, onReason }: { idea: Idea; sent: boolean; onReason: (reason: string) => void }) {
  const [reason, setReason] = useState("");
  const trimmed = reason.trim();
  const submit = () => {
    if (trimmed) onReason(trimmed);
  };
  return (
    <div className="idea-dismissed" role="group" aria-label={`${idea.title}, dismissed`}>
      <p className="idea-dismissed-text">{sent ? "Dismissed — thanks" : "Dismissed — add a reason"}</p>
      {!sent && (
        <form
          className="idea-reason"
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
        >
          <input
            type="text"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            aria-label="Reason"
            placeholder="Why not?"
            maxLength={500}
            enterKeyHint="send"
          />
          <button type="submit" className="idea-secondary" disabled={!trimmed}>
            Send
          </button>
        </form>
      )}
    </div>
  );
}

/** The one idea Marlow chose to surface, above the composer. Compact, and not
 * a chat message: it is never in the transcript or the context. Tapping the
 * body opens the panel; the buttons act on it without opening anything. */
export function SpotlightCard({
  idea,
  onOpen,
  onDoIt,
  onNotNow,
}: {
  idea: Idea;
  onOpen: () => void;
  onDoIt: () => void;
  onNotNow: () => void;
}) {
  return (
    <div className="spotlight" role="group" aria-label="Idea for you" data-testid="spotlight">
      <button type="button" className="spotlight-body" onClick={onOpen}>
        <span className="idea-emoji" aria-hidden="true">
          {idea.emoji}
        </span>
        <span className="spotlight-title">{idea.title}</span>
      </button>
      <div className="spotlight-actions">
        <button type="button" className="primary" onClick={onDoIt}>
          Do it
        </button>
        <button type="button" className="idea-secondary" onClick={onNotNow}>
          Not now
        </button>
      </div>
    </div>
  );
}
