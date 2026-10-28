import { describe, expect, test } from "vitest";
import { SEARCH_WINDOW, THINKING, WRITING, presenceOf } from "./presence";
import type { ChatMessage, MessageStatus, Role } from "./types";

// The header is the one place a working turn is described once it has scrolled
// off screen, so the failure worth guarding is it describing the WRONG turn, or
// claiming work nobody is doing.

const labels = new Map<string, string>();
const streamed = new Set<string>();
const lookups = {
  activityLabel: (id: string) => labels.get(id),
  hasStreamedText: (id: string) => streamed.has(id),
};

let n = 0;
function message(role: Role, status: MessageStatus, label?: string, text?: string): ChatMessage {
  const id = `m${++n}`;
  if (label !== undefined) labels.set(id, label);
  if (text) streamed.add(id);
  return { id, role, text: "", createdAt: 0, status };
}

const active = (label: string) => ({ kind: "active", label });
const idle = { kind: "idle" };

describe("presence", () => {
  test("an empty transcript is idle", () => {
    expect(presenceOf([], lookups)).toEqual(idle);
  });

  test("a finished transcript is idle", () => {
    const messages = [message("user", "done"), message("assistant", "done"), message("assistant", "error")];
    expect(presenceOf(messages, lookups)).toEqual(idle);
  });

  test("sending says sending", () => {
    expect(presenceOf([message("user", "done"), message("assistant", "sending")], lookups)).toEqual(
      active("Sending…"),
    );
  });

  test("running with no label says thinking", () => {
    expect(presenceOf([message("user", "done"), message("assistant", "running")], lookups)).toEqual(
      active(THINKING),
    );
  });

  test("running shows the tool label", () => {
    expect(presenceOf([message("assistant", "running", "Searching the web…")], lookups)).toEqual(
      active("Searching the web…"),
    );
  });

  test("streaming text never says thinking", () => {
    expect(presenceOf([message("assistant", "running", THINKING, "Sure —")], lookups)).toEqual(active(WRITING));
  });

  // Text, then back to a tool mid-answer: the label is the only thing
  // explaining why the text stopped.
  test("a tool label survives streamed text", () => {
    const m = message("assistant", "running", "Checking your calendar…", "Let me look.");
    expect(presenceOf([m], lookups)).toEqual(active("Checking your calendar…"));
  });

  test("stalled is not claimed as work", () => {
    expect(presenceOf([message("assistant", "stalled", "Searching the web…")], lookups)).toEqual(idle);
  });

  // The server runs one turn at a time: with two queued, the older is the one
  // actually working.
  test("the oldest in-flight reply wins", () => {
    const messages = [
      message("user", "done"),
      message("assistant", "running", "Searching the web…"),
      message("user", "done"),
      message("assistant", "running"),
    ];
    expect(presenceOf(messages, lookups)).toEqual(active("Searching the web…"));
  });

  test("only the tail is searched", () => {
    const stranded = message("assistant", "running", "Reading a file…");
    const done = Array.from({ length: SEARCH_WINDOW }, () => message("assistant", "done"));
    expect(presenceOf([stranded, ...done], lookups)).toEqual(idle);
  });

  test("works with no lookups at all", () => {
    expect(presenceOf([message("assistant", "running")])).toEqual(active(THINKING));
  });
});
