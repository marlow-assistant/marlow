// The to-do calls (docs/tasks/todos-and-reminders.md, part 2): paths, bodies
// and what comes back.
import { describe, expect, it } from "vitest";
import { Api, DecodeError, HttpError } from "./api";

const WIRE = {
  id: "t1",
  title: "Book the dentist",
  notes: null,
  due: "2026-11-06",
  state: "open",
  source: "user",
  created: "2026-11-01T08:00:00-05:00",
  closed: null,
  snoozed_until: null,
};

function harness(reply: (path: string) => { status?: number; body: unknown }) {
  const calls: { path: string; method: string; body: unknown }[] = [];
  const api = new Api({
    getToken: () => "tok",
    fetch: async (input, init) => {
      const r = reply(input);
      calls.push({ path: input, method: init?.method ?? "GET", body: init?.body ? JSON.parse(init.body as string) : undefined });
      return new Response(JSON.stringify(r.body), { status: r.status ?? 200, headers: { "Content-Type": "application/json" } });
    },
  });
  return { api, calls };
}

describe("Api › to-dos", () => {
  it("lists them from GET /v1/todos, decoded", async () => {
    const { api, calls } = harness(() => ({ body: { todos: [WIRE] } }));
    const todos = await api.todos();
    expect(calls[0]).toMatchObject({ path: "/v1/todos", method: "GET" });
    expect(todos).toHaveLength(1);
    expect(todos[0]).toMatchObject({ id: "t1", title: "Book the dentist", due: "2026-11-06", snoozedUntil: null });
  });

  it.each([
    ["doneTodo", "done"],
    ["dropTodo", "drop"],
    ["acceptTodo", "accept"],
  ] as const)("%s posts to /%s and returns the item", async (method, action) => {
    const { api, calls } = harness(() => ({ body: { todo: { ...WIRE, state: "done" } } }));
    const todo = await api[method]("t1");
    expect(calls[0]).toMatchObject({ path: `/v1/todos/t1/${action}`, method: "POST" });
    expect(todo.state).toBe("done");
  });

  it("snooze posts the instant", async () => {
    const until = "2026-11-05T09:00:00-05:00";
    const { api, calls } = harness(() => ({ body: { todo: { ...WIRE, snoozed_until: until } } }));
    const todo = await api.snoozeTodo("t1", until);
    expect(calls[0]).toMatchObject({ path: "/v1/todos/t1/snooze", method: "POST", body: { until } });
    expect(todo.snoozedUntil).toBe(until);
  });

  it("move posts the anchor and answers with the whole list", async () => {
    const { api, calls } = harness(() => ({ body: { todos: [WIRE, { ...WIRE, id: "t2" }] } }));
    const list = await api.moveTodo("t2", "t1");
    expect(calls[0]).toMatchObject({ path: "/v1/todos/t2/move", method: "POST", body: { before: "t1" } });
    expect(list.map((t) => t.id)).toEqual(["t1", "t2"]);
  });

  it("move sends an explicit null for the bottom: the box refuses a missing one", async () => {
    const { api, calls } = harness(() => ({ body: { todos: [] } }));
    await api.moveTodo("t2", null);
    expect(calls[0].body).toEqual({ before: null });
  });

  it("a move answered with something that is not a list is a DecodeError", async () => {
    const { api } = harness(() => ({ body: { ok: true } }));
    await expect(api.moveTodo("t1", null)).rejects.toBeInstanceOf(DecodeError);
  });

  it("encodes the id in the path", async () => {
    const { api, calls } = harness(() => ({ body: { todo: WIRE } }));
    await api.doneTodo("a/b");
    expect(calls[0].path).toBe("/v1/todos/a%2Fb/done");
  });

  it("a 404 or 409 is an HttpError with its status", async () => {
    for (const status of [404, 409]) {
      const { api } = harness(() => ({ status, body: { detail: "nope" } }));
      await expect(api.doneTodo("t1")).rejects.toMatchObject({ status });
      await expect(api.doneTodo("t1")).rejects.toBeInstanceOf(HttpError);
    }
  });

  it("a tap answered with something that is not a to-do is a DecodeError", async () => {
    const { api } = harness(() => ({ body: { todo: { id: "t1" } } }));
    await expect(api.doneTodo("t1")).rejects.toBeInstanceOf(DecodeError);
  });
});

describe("Api › renudge", () => {
  const DAY = { day_start: "08:00", day_end: "23:00", checkin_every_hours: 3 };

  it("reads renudge with the day times, null from a box that predates it", async () => {
    const on = harness(() => ({ body: { ...DAY, renudge: false } }));
    expect(await on.api.dayTimes()).toMatchObject({ renudge: false });
    const old = harness(() => ({ body: DAY }));
    expect(await old.api.dayTimes()).toMatchObject({ renudge: null });
  });

  it("posts renudge like the other fields", async () => {
    const { api, calls } = harness(() => ({ body: { ...DAY, renudge: true } }));
    const out = await api.setDayTimes({ renudge: true });
    expect(calls[0]).toMatchObject({ path: "/v1/preferences", method: "POST", body: { renudge: true } });
    expect(out.renudge).toBe(true);
  });
});
