/**
 * Coordinate-engine throughput.
 *
 * This measures the arithmetic the timeline does on every scroll frame, in
 * isolation from React and the DOM. It answers one question: is the windowing
 * itself ever the bottleneck? If a window lookup costs microseconds, then any
 * dropped frame is a rendering problem, not a maths problem, and that is worth
 * knowing before optimising the wrong half.
 *
 *     npx vitest bench --config vitest.bench.config.ts
 */
import { bench, describe } from "vitest";

import {
  buildClipIndex,
  clipsInWindow,
  layoutTracks,
  rowsInViewport,
  ticksInWindow,
  visibleTimeWindow,
} from "@/lib/timeline";
import type { Clip, Track } from "@/lib/types";

const LAYOUT = { videoHeight: 58, audioHeight: 46, dividerHeight: 9 };
const VIEWPORT = { scrollLeft: 40000, scrollTop: 200, width: 1400, height: 600 };

function makeClips(count: number, span: number): Clip[] {
  let seed = 99;
  const next = () => {
    seed = (seed * 1103515245 + 12345) % 2147483648;
    return seed / 2147483648;
  };
  return Array.from({ length: count }, (_, i) => ({
    id: `clip_${i}`,
    kind: "video",
    name: `clip ${i}`,
    start: next() * span,
    duration: 2 + next() * 12,
    in_point: 0,
    speed: 1,
    volume: 1,
    enabled: true,
    locked: false,
    effects: [],
    transform: {},
  })) as unknown as Clip[];
}

function makeTracks(count: number): Track[] {
  return Array.from({ length: count }, (_, i) => ({
    id: `track_${i}`,
    name: `T${i}`,
    kind: i < count / 2 ? "video" : "audio",
    index: i,
    clips: [],
    muted: false,
    hidden: false,
    locked: false,
    is_adjustment: false,
  })) as unknown as Track[];
}

// The benchmark project: 145 clips over 30 minutes. Then two orders of
// magnitude past it, because the number that matters is where it stops
// scaling, not whether it handles the case it was built for.
const SIZES = [145, 1_000, 10_000];

describe("clip lookup", () => {
  for (const size of SIZES) {
    const index = buildClipIndex(makeClips(size, 1800));
    const window = visibleTimeWindow(VIEWPORT, 60);
    bench(`clipsInWindow, ${size} clips`, () => {
      clipsInWindow(index, window);
    });
  }
});

describe("index construction", () => {
  for (const size of SIZES) {
    const clips = makeClips(size, 1800);
    bench(`buildClipIndex, ${size} clips`, () => {
      buildClipIndex(clips);
    });
  }
});

describe("ruler", () => {
  const window = visibleTimeWindow(VIEWPORT, 400);
  bench("ticksInWindow at maximum zoom", () => {
    ticksInWindow(window, 1 / 30);
  });
});

describe("track rows", () => {
  for (const size of [9, 64, 512]) {
    const layout = layoutTracks(makeTracks(size), LAYOUT);
    bench(`rowsInViewport, ${size} tracks`, () => {
      rowsInViewport(layout, VIEWPORT);
    });
  }
});
