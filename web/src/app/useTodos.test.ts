// What a move does to the list while it is on the wire and after it fails
// (docs/tasks/todos-and-reminders.md, "Manual order").
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { TransportError } from "../lib/api";
import type { Todo } from "../lib/apiModels";
import { useTodos, type TodosApi } from "./useTodos";

const NOW = new Date("2026-11-06T09:00:00-05:00");

const todo = (id: string, over: Partial<Todo> = {}): Todo => ({
  id,
  title: `Title ${id}`,
  notes: null,
  due: null,
  state: "open",
  source: "user",
  created: "2026-11-01T08:00:00-05:00",
  closed: null,
  snoozedUntil: null,
  ...over,
});

afterEach(cleanup);

async function setUp(over: Partial<TodosApi>) {
  const api = { todos: vi.fn(async () => [todo("a"), todo("b"), todo("c")]), ...over } as unknown as TodosApi;
  const view = renderHook(() => useTodos(api, NOW));
  act(() => view.result.current.refresh());
  await waitFor(() => expect(view.result.current.todos).toHaveLength(3));
  return view;
}

describe("moving", () => {
  it("a failed move puts the order back and keeps what else happened meanwhile", async () => {
    let fail = (_: Error) => {};
    const { result } = await setUp({
      moveTodo: vi.fn(() => new Promise<Todo[]>((_, reject) => (fail = reject))),
      doneTodo: vi.fn(async () => todo("b", { state: "done", closed: "2026-11-06T08:59:00-05:00" })),
    });
    const [, b, c] = result.current.todos;
    act(() => void result.current.move(c, "a"));
    expect(result.current.todos.map((t) => t.id)).toEqual(["c", "a", "b"]);
    act(() => result.current.tap(b, { kind: "done" }));
    await waitFor(() => expect(result.current.todos.find((t) => t.id === "b")?.state).toBe("done"));

    await act(async () => fail(new TransportError("offline")));

    expect(result.current.todos.map((t) => t.id)).toEqual(["a", "b", "c"]);
    expect(result.current.todos.find((t) => t.id === "b")?.state).toBe("done");
  });

  it("says whether it took the move: one at a time", async () => {
    const { result } = await setUp({ moveTodo: vi.fn(() => new Promise<Todo[]>(() => {})) });
    const [a, , c] = result.current.todos;
    let first = false;
    let second = true;
    act(() => {
      first = result.current.move(c, "a");
      second = result.current.move(a, null);
    });
    expect(first).toBe(true);
    expect(second).toBe(false);
  });
});
