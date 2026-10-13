/**
 * The timeline coordinate engine.
 *
 * These are the tests that let the timeline scale. Virtualization is only
 * correct if the window functions never hide something that is on screen, so
 * most of what follows is about edges: a clip that starts before the window
 * and ends inside it, a row exactly on the fold, a zoom that lands on a
 * fraction of a pixel.
 */
import { describe, expect, it } from "vitest";

import {
  MAX_ZOOM,
  MIN_ZOOM,
  atTimeInWindow,
  buildClipIndex,
  clampZoom,
  clipsInWindow,
  contentWidth,
  firstClipIndexAfter,
  layoutTracks,
  rowsInViewport,
  ticksInWindow,
  timeToX,
  visibleTimeWindow,
  xToTime,
  zoomAbout,
  zoomToFit,
} from "@/lib/timeline";
import type { Clip, Track } from "@/lib/types";

const LAYOUT = { videoHeight: 58, audioHeight: 46, dividerHeight: 9 };

function clip(id: string, start: number, duration: number): Clip {
  return {
    id,
    kind: "video",
    name: id,
    start,
    duration,
    in_point: 0,
    speed: 1,
    volume: 1,
    enabled: true,
    locked: false,
    effects: [],
    keyframes: {},
    transform: {},
  } as unknown as Clip;
}

function track(id: string, kind: "video" | "audio", index: number,
               clips: Clip[] = []): Track {
  return {
    id,
    name: id,
    kind,
    index,
    clips,
    muted: false,
    hidden: false,
    locked: false,
    is_adjustment: false,
  } as unknown as Track;
}

// ---------------------------------------------------------------------------

describe("scalar conversions", () => {
  it("round-trips a time through pixels", () => {
    for (const zoom of [2, 17.5, 60, 400]) {
      expect(xToTime(timeToX(12.345, zoom), zoom)).toBeCloseTo(12.345, 9);
    }
  });

  it("treats a zero zoom as zero rather than infinity", () => {
    expect(xToTime(100, 0)).toBe(0);
    expect(Number.isFinite(xToTime(100, 0))).toBe(true);
  });

  it("clamps a zoom to the usable range", () => {
    expect(clampZoom(0)).toBe(MIN_ZOOM);
    expect(clampZoom(1e9)).toBe(MAX_ZOOM);
    expect(clampZoom(Number.NaN)).toBe(MIN_ZOOM);
    expect(clampZoom(60)).toBe(60);
  });
});

describe("zooming about a point", () => {
  it("keeps the anchored time under the anchored pixel", () => {
    // The frame the pointer is over must not move when the zoom changes.
    const anchorTime = 42;
    const anchorX = 300;
    const scrollLeft = zoomAbout(anchorTime, anchorX, 120);
    expect(xToTime(scrollLeft + anchorX, 120)).toBeCloseTo(anchorTime, 9);
  });

  it("never scrolls before the start of the timeline", () => {
    expect(zoomAbout(0.1, 500, 10)).toBe(0);
  });
});

// ---------------------------------------------------------------------------

describe("the visible time window", () => {
  const viewport = { scrollLeft: 6000, scrollTop: 0, width: 1000, height: 600 };

  it("covers the viewport plus overscan on both sides", () => {
    const w = visibleTimeWindow(viewport, 100, 400);
    expect(w.start).toBeCloseTo((6000 - 400) / 100);
    expect(w.end).toBeCloseTo((6000 + 1000 + 400) / 100);
  });

  it("never starts before zero", () => {
    const w = visibleTimeWindow({ ...viewport, scrollLeft: 0 }, 100, 400);
    expect(w.start).toBe(0);
  });

  it("is empty before the viewport has been measured", () => {
    // Rendering the whole timeline for one frame is the stall this avoids.
    const w = visibleTimeWindow({ ...viewport, width: 0 }, 100);
    expect(w.end).toBe(0);
  });
});

// ---------------------------------------------------------------------------

describe("finding clips in a window", () => {
  const index = buildClipIndex([
    clip("a", 0, 5),
    clip("b", 5, 5),
    clip("c", 10, 5),
    clip("d", 20, 5),
    clip("e", 30, 5),
  ]);

  it("returns only what overlaps, in timeline order", () => {
    const found = clipsInWindow(index, { start: 11, end: 22 });
    expect(found.map((c) => c.id)).toEqual(["c", "d"]);
  });

  it("includes a clip that starts before the window and reaches into it", () => {
    // The case a naive "start >= window.start" filter gets wrong, and the
    // reason a long clip does not vanish when you scroll into its middle.
    const long = buildClipIndex([clip("long", 0, 600), clip("late", 500, 5)]);
    const found = clipsInWindow(long, { start: 300, end: 310 });
    expect(found.map((c) => c.id)).toEqual(["long"]);
  });

  it("does not stop the backward walk at a short clip in the way", () => {
    // A clip that ends before the window sits between two that reach into
    // it. A walk that halts at the first miss loses the one behind it.
    const awkward = buildClipIndex([
      clip("reaches", 0, 100),
      clip("short", 10, 1),
      clip("inside", 90, 5),
    ]);
    const found = clipsInWindow(awkward, { start: 92, end: 95 });
    expect(found.map((c) => c.id)).toEqual(["reaches", "inside"]);
  });

  it("includes a clip touching the window edge exactly", () => {
    expect(clipsInWindow(index, { start: 5, end: 6 }).map((c) => c.id))
      .toEqual(["a", "b"]);
    expect(clipsInWindow(index, { start: 4, end: 5 }).map((c) => c.id))
      .toEqual(["a", "b"]);
  });

  it("returns nothing for an empty window", () => {
    expect(clipsInWindow(index, { start: 10, end: 10 })).toEqual([]);
  });

  it("returns nothing for an empty track", () => {
    expect(clipsInWindow(buildClipIndex([]), { start: 0, end: 100 })).toEqual([]);
  });

  it("agrees with a brute-force filter on random data", () => {
    // Virtualization must never hide something a linear scan would show.
    const random: Clip[] = [];
    let seed = 7;
    const next = () => {
      seed = (seed * 1103515245 + 12345) % 2147483648;
      return seed / 2147483648;
    };
    for (let i = 0; i < 500; i += 1) {
      random.push(clip(`c${i}`, next() * 1800, 0.1 + next() * 20));
    }
    const randomIndex = buildClipIndex(random);

    for (let i = 0; i < 60; i += 1) {
      const start = next() * 1800;
      const window = { start, end: start + 5 + next() * 40 };
      const expected = randomIndex.clips
        .filter((c) => c.start <= window.end && c.start + c.duration >= window.start)
        .map((c) => c.id);
      expect(clipsInWindow(randomIndex, window).map((c) => c.id)).toEqual(expected);
    }
  });

  it("locates the first clip at or after a time", () => {
    expect(firstClipIndexAfter(index.clips, 0)).toBe(0);
    expect(firstClipIndexAfter(index.clips, 10)).toBe(2);
    expect(firstClipIndexAfter(index.clips, 999)).toBe(index.clips.length);
  });
});

// ---------------------------------------------------------------------------

describe("ruler ticks", () => {
  it("draws a bounded number of ticks however long the sequence is", () => {
    // 30 minutes at maximum zoom is 720,000 pixels. The tick count must
    // follow the viewport, not the timeline.
    const viewport = { scrollLeft: 0, scrollTop: 0, width: 1400, height: 600 };
    const window = visibleTimeWindow(viewport, MAX_ZOOM);
    expect(ticksInWindow(window, 1 / 30).length).toBeLessThan(200);
  });

  it("starts at or before the window so the first label is not clipped", () => {
    const ticks = ticksInWindow({ start: 10.4, end: 14 }, 1);
    expect(ticks[0]).toBeLessThanOrEqual(10.4);
  });

  it("covers the whole window", () => {
    const ticks = ticksInWindow({ start: 10.4, end: 14 }, 1);
    expect(ticks[ticks.length - 1]).toBeGreaterThanOrEqual(14 - 1);
  });

  it("refuses to lock the browser on a nonsense step", () => {
    expect(ticksInWindow({ start: 0, end: 1800 }, 0)).toEqual([]);
    expect(ticksInWindow({ start: 0, end: 1e9 }, 0.001).length).toBeLessThanOrEqual(5000);
  });

  it("filters markers to the window", () => {
    const markers = [{ time: 1 }, { time: 50 }, { time: 100 }];
    expect(atTimeInWindow(markers, { start: 40, end: 60 })).toEqual([{ time: 50 }]);
  });
});

// ---------------------------------------------------------------------------

describe("track layout", () => {
  const tracks = [
    track("v1", "video", 0),
    track("v2", "video", 1),
    track("a1", "audio", 0),
    track("a2", "audio", 1),
  ];

  it("stacks the highest video track at the top", () => {
    const { rows } = layoutTracks(tracks, LAYOUT);
    expect(rows.map((r) => r.track.id)).toEqual(["v2", "v1", "a1", "a2"]);
  });

  it("leaves no gaps between rows of the same kind", () => {
    const { rows } = layoutTracks(tracks, LAYOUT);
    expect(rows[1].top).toBe(rows[0].top + rows[0].height);
  });

  it("puts a divider between video and audio", () => {
    const { rows, dividerTop } = layoutTracks(tracks, LAYOUT);
    expect(dividerTop).toBe(2 * LAYOUT.videoHeight);
    expect(rows[2].top).toBe(dividerTop + LAYOUT.dividerHeight);
  });

  it("omits the divider when there is nothing on one side", () => {
    const videoOnly = layoutTracks(
      [track("v1", "video", 0), track("v2", "video", 1)], LAYOUT);
    expect(videoOnly.totalHeight).toBe(2 * LAYOUT.videoHeight);
  });

  it("handles an empty sequence", () => {
    const empty = layoutTracks([], LAYOUT);
    expect(empty.rows).toEqual([]);
    expect(empty.totalHeight).toBe(0);
  });
});

describe("rows in the viewport", () => {
  const many = Array.from({ length: 40 }, (_, i) => track(`v${i}`, "video", i));
  const layout = layoutTracks(many, LAYOUT);

  it("renders a band, not the whole stack", () => {
    const rows = rowsInViewport(
      layout, { scrollLeft: 0, scrollTop: 0, width: 1000, height: 300 }, 2);
    expect(rows.length).toBeLessThan(12);
    expect(rows.length).toBeGreaterThan(4);
  });

  it("includes a row that is only partly visible at the top", () => {
    // Scrolled one pixel into the second row: both must still be drawn.
    const rows = rowsInViewport(
      layout,
      { scrollLeft: 0, scrollTop: LAYOUT.videoHeight + 1, width: 1000, height: 300 },
      0,
    );
    expect(rows[0].top).toBeLessThanOrEqual(LAYOUT.videoHeight + 1);
    expect(rows[0].top + rows[0].height).toBeGreaterThan(LAYOUT.videoHeight + 1);
  });

  it("covers every row a brute-force check says is visible", () => {
    for (const scrollTop of [0, 37, 300, 1200, layout.totalHeight - 50]) {
      const viewport = { scrollLeft: 0, scrollTop, width: 1000, height: 400 };
      const expected = layout.rows.filter(
        (r) => r.top < scrollTop + 400 && r.top + r.height > scrollTop);
      const got = new Set(rowsInViewport(layout, viewport).map((r) => r.track.id));
      for (const row of expected) expect(got.has(row.track.id)).toBe(true);
    }
  });

  it("renders nothing before the viewport is measured", () => {
    expect(rowsInViewport(
      layout, { scrollLeft: 0, scrollTop: 0, width: 0, height: 0 })).toEqual([]);
  });
});

// ---------------------------------------------------------------------------

describe("content extent", () => {
  it("leaves runway past the last clip so a clip can be dragged out", () => {
    expect(contentWidth(100, 10)).toBeGreaterThan(1000);
  });

  it("does not collapse for a nearly empty sequence", () => {
    expect(contentWidth(0, 10)).toBeGreaterThanOrEqual(1200);
  });

  it("fits a sequence into the available width", () => {
    const zoom = zoomToFit(120, 1224, 24);
    expect(120 * zoom).toBeLessThanOrEqual(1200);
    expect(120 * zoom).toBeGreaterThan(1100);
  });

  it("returns a usable zoom for an empty sequence", () => {
    const zoom = zoomToFit(0, 1200);
    expect(zoom).toBeGreaterThanOrEqual(MIN_ZOOM);
    expect(zoom).toBeLessThanOrEqual(MAX_ZOOM);
  });
});
