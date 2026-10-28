// Wording for "Your day" (Settings). The box sends facts (a code, "HH:MM");
// what to say about them is the client's job (server/preferences.py).

/** What `GET`/`POST /v1/preferences` carry for the day. 24h "HH:MM". */
export interface DayTimes {
  dayStart: string;
  dayEnd: string;
  /** Hours between check-ins, 0 = off. Null from a box that predates it. */
  checkinEveryHours: number | null;
}

/** 0 (off) through 6, the range the box accepts. */
export const CHECKIN_HOURS = [0, 1, 2, 3, 4, 5, 6] as const;

export function checkinLabel(hours: number): string {
  if (hours === 0) return "Off";
  return hours === 1 ? "Every hour" : `Every ${hours} hours`;
}

export const DAY_SAVE_FAILED = "Couldn't save that time.";
/** The check-in refusal is FastAPI's own validation, whose `detail` is not a
 * code, so there is nothing to map: any refusal reads the same. */
export const CHECKIN_SAVE_FAILED = "Couldn't save that setting.";
export const DAY_OFFLINE = "Couldn't save. Check your connection.";

/** The 422 `detail` code → one line. Anything unrecognised reads as the
 * generic failure, so a code a newer box adds never shows as a raw string. */
export function dayErrorMessage(code: string | null): string {
  switch (code) {
    case "day_start_range":
      return "Your day has to start between 4 and noon.";
    case "day_end_range":
      return "Your day has to end between 8 pm and 3 am.";
    case "night_too_short":
      return "Leave at least 4 hours between the end of your day and the next start.";
    default:
      return DAY_SAVE_FAILED;
  }
}

/** A zero-padded 24h "HH:MM", the one shape the box takes and the time input
 * produces. An input being cleared yields "", which is not a time to send. */
export function isClockTime(s: string): boolean {
  return /^([01]\d|2[0-3]):[0-5]\d$/.test(s);
}
