// The status strip's persistent server states (contract STA-2) and how a health
// response becomes them (STA-3).

import type { HealthResponse } from "./types";

export interface HealthFacts {
  reauthNeeded: boolean;
  queuePaused: boolean;
  /** Already suppressed when `reauthNeeded` (see `healthToFacts`). */
  reauthPrewarn: boolean;
  model?: string | null;
  build?: string | null;
}

export interface Banner {
  id: "reauthNeeded" | "queuePaused" | "reauthPrewarn" | "usageHigh";
  tone: "red" | "orange" | "yellow";
  text: string;
}

export const INITIAL_FACTS: HealthFacts = {
  reauthNeeded: false,
  queuePaused: false,
  reauthPrewarn: false,
};

/** Highest first. Order ranks by what the user must *do*, not abstract
 * severity. Wording is client-side (facts-on-server); a friend's install will
 * not want "SSH in". Pre-warn is suppressed once reauth is needed: the red
 * banner says the same thing more urgently, and stacking both is noise. */
export function banners(
  facts: Pick<HealthFacts, "reauthNeeded" | "queuePaused" | "reauthPrewarn"> & {
    /** The >= 90% plan-usage line (usageFormat.highUsageText), or null. */
    usageHigh?: string | null;
  },
): Banner[] {
  const out: Banner[] = [];
  if (facts.reauthNeeded) {
    out.push({
      id: "reauthNeeded",
      tone: "red",
      text: "Marlow needs reauthorization — SSH in and run `claude`.",
    });
  }
  if (facts.queuePaused) {
    out.push({ id: "queuePaused", tone: "orange", text: "Marlow is paused." });
  }
  if (facts.reauthPrewarn && !facts.reauthNeeded) {
    out.push({
      id: "reauthPrewarn",
      tone: "orange",
      text: "Marlow's Claude session expires soon — SSH in and run `claude`.",
    });
  }
  // Lowest: running low is a heads-up, and every row above is something
  // already wrong. The one usage figure allowed on the chat screen (Mike,
  // 2026-10-04): running out is a hard stop, not a nudge.
  if (facts.usageHigh) {
    out.push({ id: "usageHigh", tone: "yellow", text: facts.usageHigh });
  }
  return out;
}

/** Key reauth on `auth_ok`, not `status`. The server reports *missing or
 * unparseable* credentials as status "error", not "reauth_needed", so keying on
 * the string left both banners dark in exactly that case, while the server's
 * auth watcher was pushing "session has expired" to the lock screen. A
 * notification leading to a screen that shows nothing is worse than none.
 *
 * `reauthNeeded` is only recomputed when `auth_ok` is present (an older server
 * that omits it must not raise a banner). `model` and the build id only
 * overwrite when reported, so one poll against an older server does not blank a
 * name already on screen. */
export function healthToFacts(prev: HealthFacts, r: HealthResponse): HealthFacts {
  const reauthNeeded = typeof r.auth_ok === "boolean" ? !r.auth_ok : prev.reauthNeeded;
  const next: HealthFacts = {
    ...prev,
    reauthNeeded,
    reauthPrewarn: (r.reauth_prewarn ?? false) && !reauthNeeded,
    queuePaused: r.queue_paused ?? false,
  };
  if (typeof r.model === "string") next.model = r.model;
  if (typeof r.git_branch === "string" && typeof r.git_commit === "string") {
    next.build = `${r.git_branch}@${r.git_commit}`;
  }
  return next;
}
