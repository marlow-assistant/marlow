import { useState } from "react";
import type { Todo } from "../lib/apiModels";
import type { Quote } from "../lib/types";
import { dueLine, isOverdue, localInputToIso, sectionsOf, snoozedLine, snoozePresets } from "../lib/todoFormat";

/** What "Talk about it" sends as text. The bubble quotes the to-do above it,
 * so the words themselves only open the conversation. */
export const TALK_TEXT = "About this to-do";

/** The mark the box puts on a to-do quote (server `talk about it`), so the
 * optimistic bubble reads the same as the row that replaces it. */
const TODO_EMOJI = "☑️";

/** The quote the optimistic bubble carries, before the server row says so. */
export function quoteOf(todo: Todo): Quote {
  return { todo_id: todo.id, emoji: TODO_EMOJI, title: todo.title };
}

export interface TodosPanelProps {
  /** Every to-do the chat screen holds (see `useTodos`). Which section each
   * lands in, and which are hidden, is decided here. */
  todos: Todo[];
  now: Date;
  /** One calm line after a tap that did not go through; null when none. */
  notice: string | null;
  /** Ids with a tap in flight: their controls wait. */
  busy: ReadonlySet<string>;
  onDone: (todo: Todo) => void;
  /** The untick: undoes a done. */
  onReopen: (todo: Todo) => void;
  onDrop: (todo: Todo) => void;
  onAccept: (todo: Todo) => void;
  onSnooze: (todo: Todo, until: string) => void;
  onTalk: (todo: Todo) => void;
  onClose: () => void;
}

/** What the checklist icon opens (docs/tasks/todos-and-reminders.md, part 2):
 * To do, Marlow suggested, Done this week, each hidden when empty. Every tap
 * goes through the caller, which replaces the item with the box's answer. */
export function TodosPanel(props: TodosPanelProps) {
  const { todos, notice, onClose, onDone, onReopen } = props;
  // Items ticked or unticked since the panel opened, as they were before the
  // first such tap: they keep that place so a tick can be seen and taken back
  // (Mike, 2026-11-04). The panel mounts on open, so the next open sorts them.
  const [held, setHeld] = useState<ReadonlyMap<string, Todo>>(new Map());
  const hold = (todo: Todo) => setHeld((h) => (h.has(todo.id) ? h : new Map(h).set(todo.id, todo)));
  const sections = sectionsOf(todos, held);
  const rowProps = {
    ...props,
    onDone: (todo: Todo) => {
      hold(todo);
      onDone(todo);
    },
    onReopen: (todo: Todo) => {
      hold(todo);
      onReopen(todo);
    },
  };

  return (
    <div className="sheet" role="dialog" aria-label="To-dos">
      <header className="sheet-header">
        <h2>To-dos</h2>
        <button type="button" onClick={onClose}>
          Done
        </button>
      </header>
      <div className="sheet-body">
        {notice && (
          <p className="todo-notice" role="status">
            {notice}
          </p>
        )}
        {sections.length === 0 && <p className="sheet-empty">Nothing on your list. Ask Marlow to add something.</p>}
        {sections.map((section) => (
          <section key={section.id} aria-label={section.title}>
            <h3 className="view-day">{section.title}</h3>
            <ul className="todo-list">
              {section.todos.map((todo) => (
                <TodoRow key={todo.id} todo={todo} {...rowProps} />
              ))}
            </ul>
            {section.id === "suggested" && (
              <p className="todo-footnote">These won't remind you until you accept.</p>
            )}
          </section>
        ))}
      </div>
    </div>
  );
}

type RowProps = TodosPanelProps & { todo: Todo };

function TodoRow({ todo, now, busy, onDone, onReopen, onDrop, onAccept, onSnooze, onTalk }: RowProps) {
  const [notesOpen, setNotesOpen] = useState(false);
  const waiting = busy.has(todo.id);
  const done = todo.state === "done";
  const due = dueLine(todo, now);
  const snoozed = todo.state === "open" ? snoozedLine(todo, now) : null;

  return (
    <li className={done ? "todo-row todo-done" : "todo-row"} aria-label={todo.title}>
      {(todo.state === "open" || done) && (
        <input
          type="checkbox"
          className="todo-check"
          checked={done}
          disabled={waiting}
          aria-label={done ? `Mark not done: ${todo.title}` : `Mark done: ${todo.title}`}
          onChange={() => (done ? onReopen(todo) : onDone(todo))}
        />
      )}
      <div className="todo-main">
        <div className="todo-title">{todo.title}</div>
        {due && <div className={isOverdue(todo, now) ? "todo-due todo-overdue" : "todo-due"}>{due}</div>}
        {snoozed && <div className="todo-due">{snoozed}</div>}
        {todo.notes && !done && (
          <button
            type="button"
            className={notesOpen ? "todo-notes todo-notes-open" : "todo-notes"}
            aria-expanded={notesOpen}
            onClick={() => setNotesOpen((o) => !o)}
          >
            {todo.notes}
          </button>
        )}
        {todo.state === "proposed" && (
          <div className="todo-actions">
            <button type="button" className="primary" disabled={waiting} onClick={() => onAccept(todo)}>
              Accept
            </button>
            <button type="button" className="idea-secondary" disabled={waiting} onClick={() => onDrop(todo)}>
              Drop
            </button>
            <button type="button" className="idea-secondary" disabled={waiting} onClick={() => onTalk(todo)}>
              Talk about it
            </button>
          </div>
        )}
        {todo.state === "open" && (
          <OpenMenu todo={todo} now={now} waiting={waiting} onDrop={onDrop} onSnooze={onSnooze} onTalk={onTalk} />
        )}
      </div>
    </li>
  );
}

/** The open row's menu: Snooze (presets, or a picked time), Talk about it,
 * Drop. Closed until the row's own button opens it. */
function OpenMenu({
  todo,
  now,
  waiting,
  onDrop,
  onSnooze,
  onTalk,
}: {
  todo: Todo;
  now: Date;
  waiting: boolean;
  onDrop: (todo: Todo) => void;
  onSnooze: (todo: Todo, until: string) => void;
  onTalk: (todo: Todo) => void;
}) {
  const [open, setOpen] = useState(false);
  const [snoozing, setSnoozing] = useState(false);
  const [picking, setPicking] = useState(false);
  const [picked, setPicked] = useState("");
  const pickedIso = localInputToIso(picked, now);

  const snooze = (until: string) => {
    setOpen(false);
    setSnoozing(false);
    setPicking(false);
    onSnooze(todo, until);
  };

  return (
    <div className="todo-menu">
      <button
        type="button"
        className="todo-more"
        aria-label={`More for ${todo.title}`}
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span aria-hidden="true">⋯</span>
      </button>
      {open && (
        <div className="todo-actions" role="group" aria-label={`Actions for ${todo.title}`}>
          <button
            type="button"
            className="idea-secondary"
            aria-expanded={snoozing}
            disabled={waiting}
            onClick={() => setSnoozing((s) => !s)}
          >
            Snooze
          </button>
          {snoozing && (
            <div className="todo-snooze">
              {snoozePresets(now).map((p) => (
                <button key={p.id} type="button" className="idea-secondary" onClick={() => snooze(p.until)}>
                  {p.label}
                </button>
              ))}
              <button type="button" className="idea-secondary" aria-expanded={picking} onClick={() => setPicking((x) => !x)}>
                Pick a time…
              </button>
              {picking && (
                <div className="todo-pick">
                  <input
                    type="datetime-local"
                    aria-label="Snooze until"
                    value={picked}
                    onChange={(e) => setPicked(e.target.value)}
                  />
                  <button
                    type="button"
                    className="primary"
                    aria-label="Snooze until picked time"
                    disabled={!pickedIso}
                    onClick={() => pickedIso && snooze(pickedIso)}
                  >
                    Snooze
                  </button>
                </div>
              )}
            </div>
          )}
          <button
            type="button"
            className="idea-secondary"
            disabled={waiting}
            onClick={() => {
              setOpen(false);
              onTalk(todo);
            }}
          >
            Talk about it
          </button>
          <button
            type="button"
            className="idea-secondary"
            disabled={waiting}
            onClick={() => {
              setOpen(false);
              onDrop(todo);
            }}
          >
            Drop
          </button>
        </div>
      )}
    </div>
  );
}
