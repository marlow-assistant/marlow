// The "Your day" section in Settings (lib/dayFormat.ts). The box is the only
// source: a failed save puts back what the box last said.
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { HttpError, TransportError, type Api } from "../lib/api";
import { DEFAULT_FACE } from "../lib/faces";
import { Settings } from "./Settings";

afterEach(cleanup);

function open(api: Partial<Api>) {
  return render(
    <Settings
      api={api as Api}
      token="tok-abcdef"
      reauthNeeded={false}
      queuePaused={false}
      effort="low"
      serverModel={null}
      serverBuild={null}
      onEffort={vi.fn()}
      locationSharing="off"
      locationStatus="ok"
      onLocationSharing={vi.fn()}
      face={DEFAULT_FACE}
      onFace={vi.fn()}
      onUnpair={vi.fn()}
      onClose={vi.fn()}
      host="marlow.test"
      webBuild="deadbee"
    />,
  );
}

const DAY = { dayStart: "08:00", dayEnd: "23:00", checkinEveryHours: 3 };
const starts = () => screen.getByLabelText("Starts") as HTMLInputElement;
const ends = () => screen.getByLabelText("Ends") as HTMLInputElement;
const checkins = () => screen.getByLabelText("Check-ins") as HTMLSelectElement;

describe("Settings › Your day", () => {
  it("shows the box's times and the footer once they load", async () => {
    open({ dayTimes: vi.fn().mockResolvedValue({ dayStart: "07:30", dayEnd: "02:00", checkinEveryHours: 3 }) });
    await waitFor(() => expect(starts().value).toBe("07:30"));
    expect(ends().value).toBe("02:00");
    expect(starts().step).toBe("900");
    expect(screen.getByText("Marlow tidies up after your day ends, and checks your inbox during it.")).toBeTruthy();
  });

  it("leaves the section out when the box can't say", async () => {
    const dayTimes = vi.fn().mockRejectedValue(new TransportError("offline"));
    open({ dayTimes });
    await waitFor(() => expect(dayTimes).toHaveBeenCalled());
    expect(screen.queryByText("Your day")).toBeNull();
  });

  it("a change posts only that field and shows what the box answers", async () => {
    const setDayTimes = vi.fn().mockResolvedValue({ dayStart: "08:00", dayEnd: "22:00", checkinEveryHours: 3 });
    open({ dayTimes: vi.fn().mockResolvedValue(DAY), setDayTimes });
    await waitFor(() => expect(ends().value).toBe("23:00"));
    fireEvent.change(ends(), { target: { value: "22:00" } });
    expect(setDayTimes).toHaveBeenCalledTimes(1);
    expect(setDayTimes.mock.calls[0][0]).toEqual({ day_end: "22:00" });
    await waitFor(() => expect(ends().value).toBe("22:00"));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("a refusal puts the last good time back and says why", async () => {
    const setDayTimes = vi.fn().mockRejectedValue(new HttpError(422, "night_too_short"));
    open({ dayTimes: vi.fn().mockResolvedValue(DAY), setDayTimes });
    await waitFor(() => expect(starts().value).toBe("08:00"));
    fireEvent.change(starts(), { target: { value: "12:00" } });
    expect(setDayTimes.mock.calls[0][0]).toEqual({ day_start: "12:00" });
    await waitFor(() => expect(starts().value).toBe("08:00"));
    expect(screen.getByRole("alert").textContent).toBe(
      "Leave at least 4 hours between the end of your day and the next start.",
    );
  });

  it("an unrecognised refusal reads as the generic line", async () => {
    open({
      dayTimes: vi.fn().mockResolvedValue(DAY),
      setDayTimes: vi.fn().mockRejectedValue(new HttpError(422, "bad_time")),
    });
    await waitFor(() => expect(ends().value).toBe("23:00"));
    fireEvent.change(ends(), { target: { value: "22:00" } });
    await waitFor(() => expect(screen.getByRole("alert").textContent).toBe("Couldn't save that time."));
  });

  it("no network reverts and says to check the connection; the next change clears the line", async () => {
    const setDayTimes = vi
      .fn()
      .mockRejectedValueOnce(new TransportError("offline"))
      .mockResolvedValueOnce({ dayStart: "09:00", dayEnd: "23:00", checkinEveryHours: 3 });
    open({ dayTimes: vi.fn().mockResolvedValue(DAY), setDayTimes });
    await waitFor(() => expect(starts().value).toBe("08:00"));
    fireEvent.change(starts(), { target: { value: "09:00" } });
    await waitFor(() => expect(screen.getByRole("alert").textContent).toBe("Couldn't save. Check your connection."));
    expect(starts().value).toBe("08:00");
    fireEvent.change(starts(), { target: { value: "09:00" } });
    await waitFor(() => expect(starts().value).toBe("09:00"));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("picking a check-in frequency posts only that and shows the box's whole answer", async () => {
    const setDayTimes = vi.fn().mockResolvedValue({ dayStart: "08:00", dayEnd: "23:00", checkinEveryHours: 0 });
    open({ dayTimes: vi.fn().mockResolvedValue(DAY), setDayTimes });
    await waitFor(() => expect(checkins().value).toBe("3"));
    expect(Array.from(checkins().options).map((o) => o.textContent)).toEqual([
      "Off",
      "Every hour",
      "Every 2 hours",
      "Every 3 hours",
      "Every 4 hours",
      "Every 5 hours",
      "Every 6 hours",
    ]);
    fireEvent.change(checkins(), { target: { value: "0" } });
    expect(setDayTimes.mock.calls[0][0]).toEqual({ checkin_every_hours: 0 });
    await waitFor(() => expect(checkins().value).toBe("0"));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("a refused check-in setting reverts and says so; the box's detail isn't a code", async () => {
    const setDayTimes = vi.fn().mockRejectedValue(new HttpError(422, '[{"type":"less_than_equal"}]'));
    open({ dayTimes: vi.fn().mockResolvedValue(DAY), setDayTimes });
    await waitFor(() => expect(checkins().value).toBe("3"));
    fireEvent.change(checkins(), { target: { value: "6" } });
    await waitFor(() => expect(screen.getByRole("alert").textContent).toBe("Couldn't save that setting."));
    expect(checkins().value).toBe("3");
  });

  it("a check-in change with no network reverts with the connection line", async () => {
    open({
      dayTimes: vi.fn().mockResolvedValue(DAY),
      setDayTimes: vi.fn().mockRejectedValue(new TransportError("offline")),
    });
    await waitFor(() => expect(checkins().value).toBe("3"));
    fireEvent.change(checkins(), { target: { value: "1" } });
    await waitFor(() => expect(screen.getByRole("alert").textContent).toBe("Couldn't save. Check your connection."));
    expect(checkins().value).toBe("3");
  });

  it("a box without check-ins keeps the day rows and hides only that row", async () => {
    open({ dayTimes: vi.fn().mockResolvedValue({ dayStart: "08:00", dayEnd: "23:00", checkinEveryHours: null }) });
    await waitFor(() => expect(starts().value).toBe("08:00"));
    expect(ends().value).toBe("23:00");
    expect(screen.queryByLabelText("Check-ins")).toBeNull();
  });

  it("a cleared input is not sent", async () => {
    const setDayTimes = vi.fn();
    open({ dayTimes: vi.fn().mockResolvedValue(DAY), setDayTimes });
    await waitFor(() => expect(starts().value).toBe("08:00"));
    fireEvent.change(starts(), { target: { value: "" } });
    expect(setDayTimes).not.toHaveBeenCalled();
  });
});
