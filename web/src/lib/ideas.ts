// The ideas panel's rules (docs/tasks/ideas-panel.md, "v1"), pure so they are
// tested without a screen. The server sends facts; what is hidden, what is
// "nearing", the section order and the dot are decided here, on the phone's
// own calendar day, so none of it waits on the nightly run.
//
// Dates are the store's `YYYY-MM-DD` strings throughout. They are calendar
// days, not instants, and fixed-width ISO strings order correctly as text, so
// no Date arithmetic touches them except to step a day forward.

import type { Idea, IdeaSection } from "./apiModels";

/** "Today" on the phone: local calendar fields, never `toISOString()`. Late in
 * the evening west of UTC the UTC date is already tomorrow, which would hide an
 * idea whose last day is today. */
export function localToday(now: Date): string {
  return isoDay(now.getFullYear(), now.getMonth(), now.getDate());
}

function isoDay(year: number, monthIndex: number, day: number): string {
  const p = (n: number, w: number) => String(n).padStart(w, "0");
  return `${p(year, 4)}-${p(monthIndex + 1, 2)}-${p(day, 2)}`;
}

/** `today` plus `days`, as a day string. Built from local noon so a DST change
 * cannot move it across midnight. */
export function addDays(today: string, days: number): string {
  const [y, m, d] = today.split("-").map(Number);
  const t = new Date(y, m - 1, d + days, 12);
  return isoDay(t.getFullYear(), t.getMonth(), t.getDate());
}

/** Past its last useful day: hidden without waiting on the nightly run. */
export function isExpired(idea: Idea, today: string): boolean {
  return idea.until !== null && idea.until < today;
}

/** The last day is today, tomorrow or the day after. (An idea past `until` is
 * hidden, not nearing.) */
export function isNearing(idea: Idea, today: string): boolean {
  return idea.until !== null && idea.until >= today && idea.until <= addDays(today, 2);
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "until Oct 13". Fixed English wording rather than `Intl`, so the marker
 * reads the same in every locale and in tests. */
export function untilText(until: string): string {
  const [, m, d] = until.split("-").map(Number);
  return `until ${MONTHS[m - 1] ?? "?"} ${d}`;
}

export function visibleIdeas(ideas: Idea[], today: string): Idea[] {
  return ideas.filter((i) => !isExpired(i, today));
}

export const SECTION_TITLES: Record<IdeaSection, string> = {
  now: "For you now",
  standing: "Things I can do",
};

export interface IdeaSectionView {
  id: IdeaSection;
  title: string;
  ideas: Idea[];
}

/** "For you now", then "Things I can do", each in the store's order. A section
 * with nothing in it is left out: a header over no cards is only noise. */
export function sectionsOf(ideas: Idea[], today: string): IdeaSectionView[] {
  const shown = visibleIdeas(ideas, today);
  return (["now", "standing"] as const)
    .map((id) => ({ id, title: SECTION_TITLES[id], ideas: shown.filter((i) => i.section === id) }))
    .filter((s) => s.ideas.length > 0);
}

/** The first open spotlight. The server allows at most one; if a stale list
 * holds an expired one ahead of another, the expired one is skipped. */
export function spotlightOf(ideas: Idea[], today: string): Idea | null {
  return visibleIdeas(ideas, today).find((i) => i.spotlight) ?? null;
}

/** The lightbulb's dot: has Mike read the list since something was added? An
 * idea added after the panel was last opened; never opened (`null`) counts
 * everything as new. Compared by day, so an idea added the same day the panel
 * was opened is not new. A spotlight does not raise it: its card is already the
 * louder signal, and a dot only a decision could clear was noise (Mike,
 * 2026-10-12). */
export function showDot(ideas: Idea[], today: string, lastOpened: string | null): boolean {
  const open = visibleIdeas(ideas, today);
  return open.some((i) => lastOpened === null || i.added > lastOpened);
}

/** After a refresh, an idea dismissed in this session that the server no longer
 * lists is put back where it was. The panel is showing it as a collapsed row
 * with a reason box, and a refresh (the app coming back to the foreground)
 * must not take that out from under a half-typed reason. */
export function keepDismissed(prev: Idea[], fresh: Idea[], dismissed: ReadonlySet<string>): Idea[] {
  const merged = fresh.slice();
  const have = new Set(fresh.map((i) => i.id));
  prev.forEach((idea, index) => {
    if (dismissed.has(idea.id) && !have.has(idea.id)) merged.splice(Math.min(index, merged.length), 0, idea);
  });
  return merged;
}
