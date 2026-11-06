import { useEffect, useRef, useState, type RefObject } from "react";
import { useMenuDismiss } from "./CopyMenu";
import type { Todo } from "../lib/apiModels";
import type { Quote } from "../lib/types";
import {
  anchorAt,
  dueLine,
  isOverdue,
  localInputToIso,
  moveChoices,
  sectionsOf,
  snoozedLine,
  snoozePresets,
  type MoveChoice,
} from "../lib/todoFormat";
import { useRowDrag, type RowDrag } from "./useRowDrag";

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
  /** Put an open item just above `before`, or at the bottom for null. False
   * when the move was ignored. */
  onMove: (todo: Todo, before: string | null) => boolean;
  onTalk: (todo: Todo) => void;
  onClose: () => void;
}

/** What the checklist icon opens (docs/tasks/todos-and-reminders.md, part 2):
 * To do, Marlow suggested, Done this week, each hidden when empty. Every tap
 * goes through the caller, which replaces the item with the box's answer. */
export function TodosPanel(props: TodosPanelProps) {
  const { todos, notice, onClose, onDone, onReopen, onMove } = props;
  // Items ticked or unticked since the panel opened, as they were before the
  // first such tap: they keep that place so a tick can be seen and taken back
  // (Mike, 2026-11-04). The panel mounts on open, so the next open sorts them.
  const [held, setHeld] = useState<ReadonlyMap<string, Todo>>(new Map());
  const hold = (todo: Todo) => setHeld((h) => (h.has(todo.id) ? h : new Map(h).set(todo.id, todo)));
  const sections = sectionsOf(todos, held);
  // Heard, not seen: the row moving is what a sighted user reads.
  const [moved, setMoved] = useState("");
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
  const moveTo = (ordered: Todo[], todo: Todo, to: number) => {
    if (onMove(todo, anchorAt(ordered.map((t) => t.id), todo.id, to))) {
      setMoved(`${todo.title} moved to ${to + 1} of ${ordered.length}.`);
    }
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
        <p className="visually-hidden" aria-live="polite">
          {moved}
        </p>
        {sections.length === 0 && <p className="sheet-empty">Nothing on your list. Ask Marlow to add something.</p>}
        {sections.map((section) => (
          <section key={section.id} aria-label={section.title}>
            <h3 className="view-day">{section.title}</h3>
            {section.id === "todo" ? (
              <OrderedRows ordered={section.todos} rowProps={rowProps} moveTo={moveTo} />
            ) : (
              <ul className="todo-list">
                {section.todos.map((todo) => (
                  <TodoRow key={todo.id} todo={todo} {...rowProps} />
                ))}
              </ul>
            )}
            {section.id === "suggested" && (
              <p className="todo-footnote">These won't remind you until you accept.</p>
            )}
          </section>
        ))}
      </div>
    </div>
  );
}

/** The To do rows, the one section whose order is the user's. Every row has
 * a slot for the handle when the list can reorder at all, so a ticked row that
 * has lost its handle stays lined up with the rest. */
function OrderedRows({
  ordered,
  rowProps,
  moveTo,
}: {
  ordered: Todo[];
  rowProps: TodosPanelProps;
  moveTo: (ordered: Todo[], todo: Todo, to: number) => void;
}) {
  const drag = useRowDrag(
    (id, to) => {
      const todo = ordered.find((t) => t.id === id);
      if (todo) moveTo(ordered, todo, to);
    },
    ordered.map((t) => t.id).join(),
  );
  const sortable = ordered.length > 1;
  return (
    <ul ref={drag.listRef} className={drag.dragging ? "todo-list todo-sorting" : "todo-list"}>
      {ordered.map((todo, index) => (
        <TodoRow
          key={todo.id}
          todo={todo}
          {...rowProps}
          order={
            sortable
              ? { index, drag, moves: moveChoices(index, ordered.length), onMoveTo: (to) => moveTo(ordered, todo, to) }
              : undefined
          }
        />
      ))}
    </ul>
  );
}

interface RowOrder {
  index: number;
  drag: RowDrag;
  moves: MoveChoice[];
  onMoveTo: (to: number) => void;
}

type RowProps = TodosPanelProps & { todo: Todo; order?: RowOrder };

function TodoRow({ todo, now, busy, onDone, onReopen, onDrop, onAccept, onSnooze, onTalk, order }: RowProps) {
  const [notesOpen, setNotesOpen] = useState(false);
  const waiting = busy.has(todo.id);
  const done = todo.state === "done";
  const due = dueLine(todo, now);
  const snoozed = todo.state === "open" ? snoozedLine(todo, now) : null;
  const dragging = order?.drag.dragging === todo.id;
  const base = done ? "todo-row todo-done" : todo.state === "open" ? "todo-row todo-has-menu" : "todo-row";

  return (
    <li
      className={dragging ? `${base} todo-dragging` : base}
      style={order?.drag.rowStyle(order.index)}
      aria-label={todo.title}
    >
      {order &&
        (todo.state === "open" && !waiting ? (
          // Hidden from screen readers, which can't drag: the ⋯ menu's Move is
          // their way, and a keyboard's.
          <span className="todo-grip" aria-hidden="true" {...order.drag.gripProps(todo.id, order.index)}>
            <svg viewBox="0 0 10 16" width="10" height="16">
              {[2, 8, 14].map((y) => [2, 8].map((x) => <circle key={`${x}-${y}`} cx={x} cy={y} r="1.5" fill="currentColor" />))}
            </svg>
          </span>
        ) : (
          <span className="todo-grip todo-grip-off" aria-hidden="true" />
        ))}
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
          <OpenMenu
            todo={todo}
            now={now}
            waiting={waiting}
            onDrop={onDrop}
            onSnooze={onSnooze}
            onTalk={onTalk}
            moves={order?.moves ?? []}
            onMoveTo={order?.onMoveTo}
          />
        )}
      </div>
    </li>
  );
}

/** The open row's menu: Snooze (presets, or a picked time), Move, Talk about
 * it, Drop. Closed until the row's own button opens it. */
function OpenMenu({
  todo,
  now,
  waiting,
  onDrop,
  onSnooze,
  onTalk,
  moves,
  onMoveTo,
}: {
  todo: Todo;
  now: Date;
  waiting: boolean;
  onDrop: (todo: Todo) => void;
  onSnooze: (todo: Todo, until: string) => void;
  onTalk: (todo: Todo) => void;
  moves: MoveChoice[];
  onMoveTo?: (to: number) => void;
}) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  const moreRef = useRef<HTMLButtonElement>(null);
  // A move closes the menu and carries the row off, taking focus with it. It
  // goes back to the ⋯ so a screen reader or keyboard can take the next step.
  const refocus = useRef(false);
  useEffect(() => {
    if (!refocus.current) return;
    refocus.current = false;
    moreRef.current?.focus();
  });

  return (
    <div className="todo-menu" ref={wrapRef}>
      <button
        ref={moreRef}
        type="button"
        className="todo-more"
        aria-label={`More for ${todo.title}`}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        {/* The circled ellipsis in the accent, as on iOS: a control, not text. */}
        <svg viewBox="0 0 24 24" width="26" height="26" aria-hidden="true">
          <circle cx="12" cy="12" r="10" fill="none" stroke="currentColor" strokeWidth="1.6" />
          <circle cx="7.5" cy="12" r="1.4" fill="currentColor" />
          <circle cx="12" cy="12" r="1.4" fill="currentColor" />
          <circle cx="16.5" cy="12" r="1.4" fill="currentColor" />
        </svg>
      </button>
      {open && (
        <RowMenu
          todo={todo}
          now={now}
          waiting={waiting}
          wrapRef={wrapRef}
          onClose={() => setOpen(false)}
          onDrop={onDrop}
          onSnooze={onSnooze}
          onTalk={onTalk}
          moves={moves}
          onMoveTo={(to) => {
            refocus.current = true;
            setOpen(false);
            onMoveTo?.(to);
          }}
        />
      )}
    </div>
  );
}

/** The ⋯'s popover, anchored under it at the row's right edge, as iOS's
 * context menu is (Mike, 2026-11-04: inline buttons under the title didn't
 * read as the row's menu). Snooze opens its presets in place, as iOS's
 * nested menu does. Escape or a tap away closes it (`useMenuDismiss`). */
function RowMenu({
  todo,
  now,
  waiting,
  wrapRef,
  onClose,
  onDrop,
  onSnooze,
  onTalk,
  moves,
  onMoveTo,
}: {
  todo: Todo;
  now: Date;
  waiting: boolean;
  wrapRef: RefObject<HTMLDivElement | null>;
  onClose: () => void;
  onDrop: (todo: Todo) => void;
  onSnooze: (todo: Todo, until: string) => void;
  onTalk: (todo: Todo) => void;
  moves: MoveChoice[];
  onMoveTo: (to: number) => void;
}) {
  const [snoozing, setSnoozing] = useState(false);
  const [moving, setMoving] = useState(false);
  const [picking, setPicking] = useState(false);
  const [picked, setPicked] = useState("");
  const pickedIso = localInputToIso(picked, now);
  // Dismissal watches the wrapper, so a tap on the ⋯ itself toggles it shut.
  useMenuDismiss(wrapRef, onClose, { anchored: true });
  // On the last rows it opens past the bottom of the sheet: bring it in.
  const menuRef = useRef<HTMLDivElement>(null);
  // Braces, so nothing is returned: Chrome's scrollIntoView returns a promise,
  // which React would call as the cleanup and crash the page (2026-11-04).
  useEffect(() => {
    menuRef.current?.scrollIntoView?.({ block: "nearest" });
  }, [snoozing, picking, moving]);

  const snooze = (until: string) => {
    onClose();
    onSnooze(todo, until);
  };

  return (
    <div ref={menuRef} className="copy-menu todo-popover" role="menu" aria-label={`Actions for ${todo.title}`}>
      <button
        type="button"
        role="menuitem"
        aria-expanded={snoozing}
        disabled={waiting}
        onClick={() => setSnoozing((x) => !x)}
      >
        Snooze <span aria-hidden="true">{snoozing ? "▾" : "▸"}</span>
      </button>
      {snoozing && (
        <div className="todo-snooze-items">
          {snoozePresets(now).map((p) => (
            <button key={p.id} type="button" role="menuitem" onClick={() => snooze(p.until)}>
              {p.label}
            </button>
          ))}
          <button type="button" role="menuitem" aria-expanded={picking} onClick={() => setPicking((x) => !x)}>
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
      {moves.length > 0 && (
        <button
          type="button"
          role="menuitem"
          aria-expanded={moving}
          disabled={waiting}
          onClick={() => setMoving((x) => !x)}
        >
          Move <span aria-hidden="true">{moving ? "▾" : "▸"}</span>
        </button>
      )}
      {moving && (
        <div className="todo-snooze-items">
          {moves.map((m) => (
            <button key={m.label} type="button" role="menuitem" onClick={() => onMoveTo(m.to)}>
              {m.label}
            </button>
          ))}
        </div>
      )}
      <button
        type="button"
        role="menuitem"
        disabled={waiting}
        onClick={() => {
          onClose();
          onTalk(todo);
        }}
      >
        Talk about it
      </button>
      <button
        type="button"
        role="menuitem"
        className="todo-menu-destructive"
        disabled={waiting}
        onClick={() => {
          onClose();
          onDrop(todo);
        }}
      >
        Drop
      </button>
    </div>
  );
}
