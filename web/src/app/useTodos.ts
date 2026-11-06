import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { HttpError, type Api } from "../lib/api";
import type { Todo } from "../lib/apiModels";
import { badgeCount, inOrderOf, reordered } from "../lib/todoFormat";

export type TodosApi = Pick<Api, "todos" | "doneTodo" | "reopenTodo" | "dropTodo" | "acceptTodo" | "snoozeTodo" | "moveTodo">;

export type TodoTap = { kind: "done" } | { kind: "reopen" } | { kind: "drop" } | { kind: "accept" } | { kind: "snooze"; until: string };

/** The box says why in a sentence meant for logs; the screen says this. */
export const TODO_CHANGED_NOTICE = "That to-do changed; here's the latest.";
export const TODO_NOT_SAVED_NOTICE = "Couldn't save that. Check your connection.";

export interface TodosModel {
  /** The last good list, with each tap's answer put in place of its item. A
   * dropped item stays in it as `dropped`; the panel lists none. */
  todos: Todo[];
  /** Overdue open items plus proposed ones: the icon's count. */
  badge: number;
  /** Ids with a tap in flight. */
  busy: ReadonlySet<string>;
  /** After a tap that did not go through; null otherwise. */
  notice: string | null;
  /** Fetch now. Quiet on failure: the last good list stays. */
  refresh: () => void;
  clearNotice: () => void;
  /** One tap on one to-do: the item is replaced by the box's answer. A 404 or
   * 409 means it changed under us, so the list is fetched again and the notice
   * says so. */
  tap: (todo: Todo, tap: TodoTap) => void;
  /** Put an open item just above `before`, or at the bottom for null. Shown
   * at once, unlike a tap: a dragged row can't wait in mid-air for the answer.
   * The box's list then replaces it, and a failure puts the old order back.
   * False when it was ignored: another move is on the wire or the row is busy. */
  move: (todo: Todo, before: string | null) => boolean;
}

/** The to-dos the chat screen shows from: the icon's badge and the panel's
 * list. Fetched on mount (the badge must be right before anyone opens the
 * panel), whenever the page becomes visible again, and by `refresh()` when the
 * panel opens. A failed fetch says nothing anywhere: no error in the chat, and
 * the last good list stays. */
export function useTodos(api: TodosApi, now: Date): TodosModel {
  const [todos, setTodos] = useState<Todo[]>([]);
  const [busy, setBusy] = useState<ReadonlySet<string>>(new Set());
  const [notice, setNotice] = useState<string | null>(null);
  const seq = useRef(0);

  const refresh = useCallback(() => {
    const mine = ++seq.current;
    // Inside the try, so an api that cannot answer at all is as quiet as one
    // that fails.
    void (async () => {
      try {
        const fresh = await api.todos();
        // A slower, older answer must not overwrite a newer one.
        if (mine === seq.current) setTodos(fresh);
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

  const busyRef = useRef(busy);
  busyRef.current = busy;

  const tap = useCallback(
    (todo: Todo, t: TodoTap) => {
      if (busyRef.current.has(todo.id)) return;
      const mark = (on: boolean) =>
        setBusy((s) => {
          const next = new Set(s);
          if (on) next.add(todo.id);
          else next.delete(todo.id);
          return next;
        });
      busyRef.current = new Set(busyRef.current).add(todo.id);
      mark(true);
      void (async () => {
        try {
          const answer =
            t.kind === "done"
              ? await api.doneTodo(todo.id)
              : t.kind === "reopen"
                ? await api.reopenTodo(todo.id)
                : t.kind === "drop"
                  ? await api.dropTodo(todo.id)
                  : t.kind === "accept"
                    ? await api.acceptTodo(todo.id)
                    : await api.snoozeTodo(todo.id, t.until);
          // Newer than any fetch still in flight.
          seq.current += 1;
          setTodos((prev) => prev.map((x) => (x.id === answer.id ? answer : x)));
          setNotice(null);
        } catch (e) {
          if (e instanceof HttpError && (e.status === 404 || e.status === 409)) {
            setNotice(TODO_CHANGED_NOTICE);
            refresh();
          } else {
            setNotice(TODO_NOT_SAVED_NOTICE);
          }
        } finally {
          mark(false);
        }
      })();
    },
    [api, refresh],
  );

  const todosRef = useRef(todos);
  todosRef.current = todos;
  // One at a time: each answer is the whole list, and a later move restoring
  // an earlier snapshot could lose the first. A second drag in that moment
  // snaps back, which beats a list that lands in an order nobody chose.
  const moving = useRef(false);

  const move = useCallback(
    (todo: Todo, before: string | null) => {
      if (moving.current || busyRef.current.has(todo.id)) return false;
      moving.current = true;
      const shown = todosRef.current;
      setTodos(reordered(shown, todo.id, before));
      seq.current += 1; // a fetch in the air carries the old order
      void (async () => {
        try {
          const fresh = await api.moveTodo(todo.id, before);
          seq.current += 1;
          setTodos(fresh);
          setNotice(null);
        } catch (e) {
          // Just this move undone: a tap may have landed while it was out.
          setTodos((list) => inOrderOf(list, shown));
          if (e instanceof HttpError && (e.status === 404 || e.status === 409)) {
            setNotice(TODO_CHANGED_NOTICE);
            refresh();
          } else {
            setNotice(TODO_NOT_SAVED_NOTICE);
          }
        } finally {
          moving.current = false;
        }
      })();
      return true;
    },
    [api, refresh],
  );

  const clearNotice = useCallback(() => setNotice(null), []);
  const badge = useMemo(() => badgeCount(todos, now), [todos, now]);

  return { todos, badge, busy, notice, refresh, clearNotice, tap, move };
}
