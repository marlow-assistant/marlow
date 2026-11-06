import { useCallback, useEffect, useRef, useState, type CSSProperties, type PointerEvent } from "react";
import { dropIndex, type RowBox } from "../lib/todoFormat";

/** How close to the scroller's edge a held row starts it scrolling, and how
 * fast (px a frame). */
const EDGE = 56;
const SPEED = 8;

interface Live {
  id: string;
  from: number;
  pointerId: number;
  startY: number;
  y: number;
  rows: RowBox[];
  /** How far a passed row shifts: the dragged row's height and its margin. */
  step: number;
  scroller: HTMLElement | null;
  startScroll: number;
}

interface View {
  id: string;
  from: number;
  to: number;
  offset: number;
  step: number;
}

export interface RowDrag {
  /** On the `ul` whose children are the rows. */
  listRef: React.RefObject<HTMLUListElement | null>;
  /** Spread on a row's handle. */
  gripProps: (id: string, index: number) => {
    onPointerDown: (e: PointerEvent<HTMLElement>) => void;
    onPointerMove: (e: PointerEvent<HTMLElement>) => void;
    onPointerUp: (e: PointerEvent<HTMLElement>) => void;
    onPointerCancel: (e: PointerEvent<HTMLElement>) => void;
  };
  /** The id being dragged, or null. */
  dragging: string | null;
  /** Where a row sits right now: the dragged one under the finger, the ones it
   * passed shifted to make way. */
  rowStyle: (index: number) => CSSProperties | undefined;
}

/** Drag to reorder by Pointer Events, which are one path for a finger and a
 * mouse. It starts from a handle, not the row: the handle's `touch-action:
 * none` is what stops Android Chrome claiming the gesture as a scroll, and the
 * rest of the row must still scroll the sheet. No long press to learn either.
 *
 * Nothing moves in the DOM during a drag, only transforms; the list changes
 * once, at the drop, through `onDrop`. Rows are measured when it starts and a
 * scroll is added back on, so a row growing mid-drag is the one thing it
 * would misread. */
export function useRowDrag(onDrop: (id: string, to: number) => void): RowDrag {
  const listRef = useRef<HTMLUListElement>(null);
  const live = useRef<Live | null>(null);
  const frame = useRef(0);
  const [view, setView] = useState<View | null>(null);

  const offsetOf = (d: Live) => d.y - d.startY + (d.scroller ? d.scroller.scrollTop - d.startScroll : 0);

  const update = useCallback(() => {
    const d = live.current;
    if (!d) return;
    const offset = offsetOf(d);
    setView({ id: d.id, from: d.from, to: dropIndex(d.rows, d.from, offset), offset, step: d.step });
  }, []);

  // A held finger sends no pointermove, so the edge scroll has its own clock.
  const tick = useCallback(() => {
    const d = live.current;
    if (!d?.scroller) return;
    const box = d.scroller.getBoundingClientRect();
    const speed = d.y < box.top + EDGE ? -SPEED : d.y > box.bottom - EDGE ? SPEED : 0;
    if (speed) {
      d.scroller.scrollTop += speed;
      update();
    }
    frame.current = requestAnimationFrame(tick);
  }, [update]);

  useEffect(() => () => cancelAnimationFrame(frame.current), []);

  const finish = (commit: boolean) => {
    const d = live.current;
    if (!d) return;
    live.current = null;
    cancelAnimationFrame(frame.current);
    setView(null);
    if (!commit) return;
    const to = dropIndex(d.rows, d.from, offsetOf(d));
    if (to !== d.from) onDrop(d.id, to);
  };

  const gripProps: RowDrag["gripProps"] = (id, index) => ({
    onPointerDown: (e) => {
      const list = listRef.current;
      if (e.button !== 0 || live.current || !list) return;
      // Keeps every later event on the handle, wherever the finger goes.
      e.currentTarget.setPointerCapture?.(e.pointerId);
      const rows = Array.from(list.children).map((li) => {
        const { top, height } = li.getBoundingClientRect();
        return { top, height };
      });
      const margin = parseFloat(getComputedStyle(list.children[index]).marginBottom) || 0;
      const scroller = list.closest<HTMLElement>(".sheet-body");
      live.current = {
        id,
        from: index,
        pointerId: e.pointerId,
        startY: e.clientY,
        y: e.clientY,
        rows,
        step: rows[index].height + margin,
        scroller,
        startScroll: scroller?.scrollTop ?? 0,
      };
      update();
      frame.current = requestAnimationFrame(tick);
    },
    onPointerMove: (e) => {
      const d = live.current;
      if (!d || e.pointerId !== d.pointerId) return;
      d.y = e.clientY;
      update();
    },
    onPointerUp: (e) => {
      if (e.pointerId === live.current?.pointerId) finish(true);
    },
    onPointerCancel: (e) => {
      if (e.pointerId === live.current?.pointerId) finish(false);
    },
  });

  const rowStyle = (index: number): CSSProperties | undefined => {
    if (!view) return undefined;
    if (index === view.from) return { transform: `translateY(${view.offset}px)` };
    if (view.from < index && index <= view.to) return { transform: `translateY(${-view.step}px)` };
    if (view.to <= index && index < view.from) return { transform: `translateY(${view.step}px)` };
    return undefined;
  };

  return { listRef, gripProps, dragging: view?.id ?? null, rowStyle };
}
