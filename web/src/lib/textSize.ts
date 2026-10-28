// The web client's text-size setting (the Android user, 2026-10-19: "mostly the small
// font"). iOS needs none: its text styles follow the system's Dynamic Type
// setting. A browser gives a page no such signal, so this is the dial instead.
//
// One scale on the root font size, which every text size in the CSS is in rem
// against. A percentage, not px, so a browser's own default-size setting still
// counts and this multiplies it. Kept on this device only: the right size is a
// property of the screen, not of the person, so it is not a synced preference.
//
// No "smaller" step: nobody has asked for one, and iOS Safari zooms the page
// when a focused input's text is under 16px.

export type TextSizeId = "default" | "large" | "larger" | "largest";

export interface TextSize {
  id: TextSizeId;
  label: string;
  scale: number;
}

export const TEXT_SIZES: readonly TextSize[] = [
  { id: "default", label: "Default", scale: 1 },
  { id: "large", label: "Large", scale: 1.15 },
  { id: "larger", label: "Larger", scale: 1.3 },
  { id: "largest", label: "Largest", scale: 1.5 },
];

export const DEFAULT_TEXT_SIZE = TEXT_SIZES[0];

/** A stored id back to its size; anything unknown is the default. */
export function parseTextSize(v: unknown): TextSize {
  return TEXT_SIZES.find((s) => s.id === v) ?? DEFAULT_TEXT_SIZE;
}

let current: TextSize = DEFAULT_TEXT_SIZE;

export function currentTextSize(): TextSize {
  return current;
}

export function applyTextSize(size: TextSize, root: HTMLElement = document.documentElement): void {
  current = size;
  root.style.fontSize = size.scale === 1 ? "" : `${size.scale * 100}%`;
}
