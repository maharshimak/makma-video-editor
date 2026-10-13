"use client";

/**
 * Track what part of a scroll container is on screen.
 *
 * Scroll events fire far faster than frames, so updates are coalesced into
 * one per animation frame. State is only replaced when a value actually
 * changed, because setting identical numbers still re-renders every consumer,
 * and this hook feeds the timeline.
 *
 * The container is attached through the returned callback ref rather than a
 * plain ref object. That matters: a component that renders a placeholder
 * before its data arrives has a null ref on the first pass, and an effect
 * with stable dependencies never runs again to notice the element appearing.
 * A callback ref fires when the node is actually there.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { EMPTY_VIEWPORT, type Viewport } from "./timeline";

function same(a: Viewport, b: Viewport): boolean {
  return (
    a.scrollLeft === b.scrollLeft &&
    a.scrollTop === b.scrollTop &&
    a.width === b.width &&
    a.height === b.height
  );
}

export interface ViewportHandle {
  viewport: Viewport;
  /** Put this on the scroll container: `<div ref={attach}>`. */
  attach: (node: HTMLDivElement | null) => void;
}

export function useViewport(
  ref?: React.MutableRefObject<HTMLDivElement | null>,
): ViewportHandle {
  const [element, setElement] = useState<HTMLDivElement | null>(null);
  const [viewport, setViewport] = useState<Viewport>(EMPTY_VIEWPORT);
  const frame = useRef<number | null>(null);

  const attach = useCallback(
    (node: HTMLDivElement | null) => {
      if (ref) ref.current = node;
      setElement(node);
    },
    [ref],
  );

  useEffect(() => {
    if (!element) {
      setViewport((current) => (same(current, EMPTY_VIEWPORT) ? current : EMPTY_VIEWPORT));
      return;
    }

    const measure = () => {
      const next: Viewport = {
        scrollLeft: element.scrollLeft,
        scrollTop: element.scrollTop,
        width: element.clientWidth,
        height: element.clientHeight,
      };
      setViewport((current) => (same(current, next) ? current : next));
    };

    const schedule = () => {
      if (frame.current !== null) return;
      frame.current = window.requestAnimationFrame(() => {
        frame.current = null;
        measure();
      });
    };

    measure();
    element.addEventListener("scroll", schedule, { passive: true });
    const observer = new ResizeObserver(schedule);
    observer.observe(element);

    return () => {
      element.removeEventListener("scroll", schedule);
      observer.disconnect();
      if (frame.current !== null) window.cancelAnimationFrame(frame.current);
      frame.current = null;
    };
  }, [element]);

  return { viewport, attach };
}
