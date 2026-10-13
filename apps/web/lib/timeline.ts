/**
 * The timeline coordinate engine.
 *
 * Every conversion between seconds and pixels lives here, and nothing in it
 * touches React or the DOM. That is deliberate: coordinate maths is where
 * timeline bugs hide, and a pure module can be tested exhaustively without
 * rendering anything.
 *
 * It also does the virtualization arithmetic. A 30-minute sequence at maximum
 * zoom is 720,000 pixels wide; drawing a ruler tick every 78 pixels means
 * 9,000 DOM nodes for the ruler alone, before a single clip. The window
 * functions below answer "what is actually on screen" so the component can
 * render that and nothing else.
 */

import type { Clip, Track } from "./types";

/** Pixels rendered beyond each edge of the viewport, so scrolling is not bare. */
export const OVERSCAN_PX = 400;

/** Rows rendered above and below the visible band, for the same reason. */
export const OVERSCAN_ROWS = 2;

export const MIN_ZOOM = 2;
export const MAX_ZOOM = 400;

// ---------------------------------------------------------------------------
// Scalar conversions
// ---------------------------------------------------------------------------

export function timeToX(time: number, zoom: number): number {
  return time * zoom;
}

export function xToTime(x: number, zoom: number): number {
  if (zoom <= 0) return 0;
  return x / zoom;
}

export function clampZoom(zoom: number): number {
  if (!Number.isFinite(zoom)) return MIN_ZOOM;
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, zoom));
}

/**
 * Zoom about a fixed point, so the frame under the cursor stays under it.
 *
 * Returns the scroll offset that holds `anchorTime` at `anchorX` pixels from
 * the left edge of the viewport. Without this, zooming walks the content out
 * from under the pointer and the user loses their place.
 */
export function zoomAbout(
  anchorTime: number,
  anchorX: number,
  nextZoom: number,
): number {
  return Math.max(0, timeToX(anchorTime, clampZoom(nextZoom)) - anchorX);
}

// ---------------------------------------------------------------------------
// The visible window
// ---------------------------------------------------------------------------

export interface Viewport {
  scrollLeft: number;
  scrollTop: number;
  width: number;
  height: number;
}

export interface TimeWindow {
  start: number;
  end: number;
}

export const EMPTY_VIEWPORT: Viewport = {
  scrollLeft: 0,
  scrollTop: 0,
  width: 0,
  height: 0,
};

/**
 * The span of time on screen, in seconds, widened by the overscan.
 *
 * A viewport with no measured width yet returns an empty window rather than
 * the whole timeline: rendering everything for one frame before the first
 * measurement is exactly the stall virtualization exists to prevent.
 */
export function visibleTimeWindow(
  viewport: Viewport,
  zoom: number,
  overscanPx = OVERSCAN_PX,
): TimeWindow {
  if (viewport.width <= 0) return { start: 0, end: 0 };
  const start = xToTime(viewport.scrollLeft - overscanPx, zoom);
  const end = xToTime(viewport.scrollLeft + viewport.width + overscanPx, zoom);
  return { start: Math.max(0, start), end: Math.max(0, end) };
}

export function windowIsEmpty(window: TimeWindow): boolean {
  return window.end <= window.start;
}

// ---------------------------------------------------------------------------
// Clips
// ---------------------------------------------------------------------------

/**
 * A track's clips, prepared for windowed lookup.
 *
 * `maxEnd[i]` is the furthest any of the first i+1 clips reaches. Sorting by
 * start alone is not enough to search backwards safely: a clip that ends
 * before the window can sit between two that reach into it, so a backward
 * walk that stops at the first non-overlapping clip drops the ones behind it.
 * The running maximum is what makes "no earlier clip can reach here" a fact
 * rather than a guess about how tidy the timeline is.
 */
export interface ClipIndex {
  clips: Clip[];
  maxEnd: number[];
}

export const EMPTY_CLIP_INDEX: ClipIndex = { clips: [], maxEnd: [] };

/** Sort by start and precompute the reach of every prefix. */
export function buildClipIndex(clips: Clip[]): ClipIndex {
  const sorted = [...clips].sort(
    (a, b) => a.start - b.start || a.id.localeCompare(b.id),
  );
  const maxEnd: number[] = new Array(sorted.length);
  let running = -Infinity;
  for (let i = 0; i < sorted.length; i += 1) {
    running = Math.max(running, sorted[i].start + sorted[i].duration);
    maxEnd[i] = running;
  }
  return { clips: sorted, maxEnd };
}

/**
 * Index of the first clip starting at or after `time`.
 *
 * A binary search, because a linear scan is fine at 100 clips and not at
 * 10,000, and the point of this module is that the second case behaves like
 * the first.
 */
export function firstClipIndexAfter(clips: Clip[], time: number): number {
  let low = 0;
  let high = clips.length;
  while (low < high) {
    const mid = (low + high) >> 1;
    if (clips[mid].start < time) low = mid + 1;
    else high = mid;
  }
  return low;
}

/**
 * The clips that intersect a window, in timeline order.
 *
 * Finds the last clip that starts inside the window, then walks backwards
 * while the prefix reach says something earlier could still overlap. On a
 * normal timeline that walk stops after one step; on a messy one it stays
 * correct.
 */
export function clipsInWindow(index: ClipIndex, window: TimeWindow): Clip[] {
  const { clips, maxEnd } = index;
  if (clips.length === 0 || windowIsEmpty(window)) return [];

  // One past the last clip that starts at or before the window's end.
  let last = firstClipIndexAfter(clips, window.end);
  while (last < clips.length && clips[last].start <= window.end) last += 1;

  const visible: Clip[] = [];
  for (let i = last - 1; i >= 0; i -= 1) {
    if (maxEnd[i] < window.start) break;
    const clip = clips[i];
    if (clip.start + clip.duration >= window.start) visible.push(clip);
  }
  visible.reverse();
  return visible;
}

// ---------------------------------------------------------------------------
// Ruler ticks
// ---------------------------------------------------------------------------

/**
 * Ticks inside a window, at a given step.
 *
 * The count is bounded by the window, not by the sequence, so a 30-minute
 * project and a 30-second one both draw the same handful of ticks.
 */
export function ticksInWindow(window: TimeWindow, step: number): number[] {
  if (step <= 0 || windowIsEmpty(window)) return [];

  const first = Math.floor(window.start / step) * step;
  const count = Math.floor((window.end - first) / step) + 1;
  // A guard, not an expectation: a bad step must not lock the browser.
  const safeCount = Math.min(count, 5000);

  const ticks: number[] = [];
  for (let i = 0; i < safeCount; i += 1) {
    ticks.push(Math.round((first + i * step) * 1000) / 1000);
  }
  return ticks;
}

/** Items positioned by a single time, such as markers. */
export function atTimeInWindow<T extends { time: number }>(
  items: T[],
  window: TimeWindow,
): T[] {
  if (windowIsEmpty(window)) return [];
  return items.filter((item) => item.time >= window.start && item.time <= window.end);
}

// ---------------------------------------------------------------------------
// Vertical layout
// ---------------------------------------------------------------------------

export interface Row {
  track: Track;
  top: number;
  height: number;
}

export interface Layout {
  rows: Row[];
  totalHeight: number;
  /** Where the video block ends and the audio block begins, in pixels. */
  dividerTop: number;
}

export interface LayoutOptions {
  videoHeight: number;
  audioHeight: number;
  dividerHeight: number;
}

/**
 * Stack tracks into absolute pixel rows.
 *
 * Video runs top to bottom in descending index, the way an editor expects the
 * upper layers to sit above the lower ones; audio runs the other way below a
 * divider. Absolute positions are what make vertical virtualization possible
 * at all -- rows in normal document flow cannot be skipped.
 */
export function layoutTracks(tracks: Track[], options: LayoutOptions): Layout {
  const video = tracks
    .filter((t) => t.kind === "video")
    .sort((a, b) => b.index - a.index);
  const audio = tracks
    .filter((t) => t.kind === "audio")
    .sort((a, b) => a.index - b.index);

  const rows: Row[] = [];
  let top = 0;
  for (const track of video) {
    rows.push({ track, top, height: options.videoHeight });
    top += options.videoHeight;
  }

  const dividerTop = top;
  if (video.length && audio.length) top += options.dividerHeight;

  for (const track of audio) {
    rows.push({ track, top, height: options.audioHeight });
    top += options.audioHeight;
  }

  return { rows, totalHeight: top, dividerTop };
}

/**
 * The rows on screen.
 *
 * Rows are in ascending `top` order, so this is two binary searches rather
 * than a filter over every track. Eight tracks would not need it; the ceiling
 * this is built for is not eight.
 */
export function rowsInViewport(
  layout: Layout,
  viewport: Viewport,
  overscanRows = OVERSCAN_ROWS,
): Row[] {
  const { rows } = layout;
  if (rows.length === 0) return [];
  if (viewport.height <= 0) return [];

  const top = viewport.scrollTop;
  const bottom = viewport.scrollTop + viewport.height;

  let first = 0;
  let high = rows.length;
  while (first < high) {
    const mid = (first + high) >> 1;
    if (rows[mid].top + rows[mid].height <= top) first = mid + 1;
    else high = mid;
  }

  let last = first;
  while (last < rows.length && rows[last].top < bottom) last += 1;

  const from = Math.max(0, first - overscanRows);
  const to = Math.min(rows.length, last + overscanRows);
  return rows.slice(from, to);
}

// ---------------------------------------------------------------------------
// Content extent
// ---------------------------------------------------------------------------

/**
 * How wide the scrollable content is.
 *
 * `tailSeconds` is empty runway past the last clip so a clip can be dragged
 * beyond the current end, and `minWidth` keeps a nearly empty timeline from
 * collapsing to nothing.
 */
export function contentWidth(
  duration: number,
  zoom: number,
  { tailSeconds = 6, minWidth = 1200 }: { tailSeconds?: number; minWidth?: number } = {},
): number {
  const width = (Math.max(0, duration) + tailSeconds) * clampZoom(zoom);
  return Math.max(minWidth, Math.round(width));
}

/**
 * A zoom level that fits `duration` into `width`.
 *
 * Used by "zoom to fit"; clamped, so fitting a very short or very long
 * sequence still lands somewhere usable.
 */
export function zoomToFit(duration: number, width: number, padding = 24): number {
  const usable = Math.max(1, width - padding);
  if (duration <= 0) return clampZoom(60);
  return clampZoom(usable / duration);
}
