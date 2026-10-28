import { describe, expect, it } from "vitest";
import { CHECKIN_HOURS, checkinLabel, DAY_OFFLINE, DAY_SAVE_FAILED, dayErrorMessage, isClockTime } from "./dayFormat";

describe("dayErrorMessage", () => {
  it("words each code the box can answer with", () => {
    expect(dayErrorMessage("day_start_range")).toBe("Your day has to start between 4 and noon.");
    expect(dayErrorMessage("day_end_range")).toBe("Your day has to end between 8 pm and 3 am.");
    expect(dayErrorMessage("night_too_short")).toBe(
      "Leave at least 4 hours between the end of your day and the next start.",
    );
  });

  it("bad_time, an unknown code and no code are all the generic line", () => {
    expect(dayErrorMessage("bad_time")).toBe(DAY_SAVE_FAILED);
    expect(dayErrorMessage("something_new")).toBe(DAY_SAVE_FAILED);
    expect(dayErrorMessage(null)).toBe(DAY_SAVE_FAILED);
    expect(DAY_SAVE_FAILED).toBe("Couldn't save that time.");
    expect(DAY_OFFLINE).toBe("Couldn't save. Check your connection.");
  });
});

describe("check-in options", () => {
  it("run from Off to every 6 hours, each worded for its number", () => {
    expect(CHECKIN_HOURS.map(checkinLabel)).toEqual([
      "Off",
      "Every hour",
      "Every 2 hours",
      "Every 3 hours",
      "Every 4 hours",
      "Every 5 hours",
      "Every 6 hours",
    ]);
  });
});

describe("isClockTime", () => {
  it("accepts zero-padded 24h times", () => {
    for (const t of ["00:00", "08:00", "12:15", "23:59"]) expect(isClockTime(t)).toBe(true);
  });

  it("rejects a cleared input and anything not HH:MM", () => {
    for (const t of ["", "8:00", "24:00", "08:60", "08:00:00", "8 am"]) expect(isClockTime(t)).toBe(false);
  });
});
