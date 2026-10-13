"use client";

/**
 * The timeline.
 *
 * Interactions are optimistic: dragging or trimming mutates the local document
 * immediately so the UI tracks the pointer at 60fps, and the gesture is
 * committed as one version on release. That keeps undo granular (one entry per
 * gesture, not per pixel) without ever making the user wait on the network.
 *
 * The playhead is deliberately not React state -- it is moved by writing a
 * transform on a single DOM node, so scrubbing does not re-render the tracks.
 *
 * Rendering is virtualized in both directions. Only the clips, ruler ticks
 * and track rows inside the viewport exist in the DOM, so a 30-minute
 * sequence with a hundred clips costs the same as a short one. All of the
 * coordinate and windowing arithmetic lives in `lib/timeline.ts`, which is
 * pure and tested on its own.
 */
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { formatTimecode, rulerStep, snapToFrame } from "@/lib/time";
import {
  atTimeInWindow,
  buildClipIndex,
  clampZoom,
  clipsInWindow,
  contentWidth as computeContentWidth,
  EMPTY_CLIP_INDEX,
  layoutTracks,
  rowsInViewport,
  ticksInWindow,
  visibleTimeWindow,
  xToTime,
  zoomAbout,
  type ClipIndex,
  type Row,
  type TimeWindow,
} from "@/lib/timeline";
import type { Asset, Clip, Sequence, Track } from "@/lib/types";
import { useViewport } from "@/lib/useViewport";
import {
  findClip,
  getPlayhead,
  sequenceDuration,
  setPlayhead,
  setPlaying,
  snap,
  snapTargets,
  subscribePlayhead,
  useEditor,
  withSequence,
} from "@/store/editor";

const TRACK_HEIGHT = 58;
const AUDIO_TRACK_HEIGHT = 46;
const HEADER_WIDTH = 132;
const RULER_HEIGHT = 26;
const DIVIDER_HEIGHT = 9;
const LAYOUT = {
  videoHeight: TRACK_HEIGHT,
  audioHeight: AUDIO_TRACK_HEIGHT,
  dividerHeight: DIVIDER_HEIGHT,
};

type DragMode = "move" | "trim-start" | "trim-end";

interface DragState {
  mode: DragMode;
  clipId: string;
  originX: number;
  originStart: number;
  originDuration: number;
  originIn: number;
  trackId: string;
  moved: boolean;
}

export function Timeline() {
  const document_ = useEditor((s) => s.document);
  const zoom = useEditor((s) => s.zoom);
  const setZoom = useEditor((s) => s.setZoom);
  const snapping = useEditor((s) => s.snapping);
  const toggleSnapping = useEditor((s) => s.toggleSnapping);
  const selection = useEditor((s) => s.selection);
  const toggleSelect = useEditor((s) => s.toggleSelect);
  const clearSelection = useEditor((s) => s.clearSelection);
  const patchDocument = useEditor((s) => s.patchDocument);
  const commitDocument = useEditor((s) => s.commitDocument);
  const applyOps = useEditor((s) => s.applyOps);
  const assets = useEditor((s) => s.assets);

  const scrollRef = useRef<HTMLDivElement>(null);
  const laneRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<DragState | null>(null);
  const [dragging, setDragging] = useState(false);

  const seq = useMemo<Sequence | null>(() => {
    if (!document_) return null;
    return (
      document_.sequences.find((s) => s.id === document_.active_sequence_id) ??
      document_.sequences[0] ??
      null
    );
  }, [document_]);

  const { viewport, attach: attachScroller } = useViewport(scrollRef);

  const duration = sequenceDuration(seq);
  const contentWidth = computeContentWidth(duration, zoom);
  const assetById = useMemo(
    () => Object.fromEntries(assets.map((a) => [a.id, a])),
    [assets],
  );

  // The lane area is narrower than the scroll container by the sticky track
  // headers, so the visible time window is measured against that width.
  const laneViewport = useMemo(
    () => ({ ...viewport, width: Math.max(0, viewport.width - HEADER_WIDTH) }),
    [viewport],
  );
  const window_ = useMemo(
    () => visibleTimeWindow(laneViewport, zoom),
    [laneViewport, zoom],
  );

  const layout = useMemo(
    () => layoutTracks(seq?.tracks ?? [], LAYOUT),
    [seq?.tracks],
  );
  // Rows scroll under the sticky ruler, so the visible band starts below it.
  const rowViewport = useMemo(
    () => ({ ...viewport, height: Math.max(0, viewport.height - RULER_HEIGHT) }),
    [viewport],
  );
  const rows = useMemo(
    () => rowsInViewport(layout, rowViewport),
    [layout, rowViewport],
  );

  // Indexing once per track keeps the per-window binary search valid. Clip
  // starts change during a drag, so this cannot be hoisted out of the render;
  // it is a sort per track per document version, not per frame.
  const indexByTrack = useMemo(() => {
    const map = new Map<string, ClipIndex>();
    for (const track of seq?.tracks ?? []) {
      map.set(track.id, buildClipIndex(track.clips));
    }
    return map;
  }, [seq?.tracks]);

  const timeFromEvent = useCallback(
    (clientX: number) => {
      const lane = laneRef.current;
      if (!lane) return 0;
      const rect = lane.getBoundingClientRect();
      return Math.max(0, (clientX - rect.left) / zoom);
    },
    [zoom],
  );

  // --- scrubbing -----------------------------------------------------------
  const scrub = useCallback(
    (clientX: number) => {
      const fps = seq?.fps ?? 30;
      setPlayhead(snapToFrame(timeFromEvent(clientX), fps));
    },
    [seq?.fps, timeFromEvent],
  );

  const onRulerPointerDown = (e: React.PointerEvent) => {
    e.currentTarget.setPointerCapture(e.pointerId);
    setPlaying(false);
    scrub(e.clientX);
    const move = (ev: PointerEvent) => scrub(ev.clientX);
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };

  // --- clip drag / trim ----------------------------------------------------
  const beginDrag = (e: React.PointerEvent, clip: Clip, track: Track, mode: DragMode) => {
    if (clip.locked || track.locked) return;
    e.stopPropagation();
    e.preventDefault();
    if (!selection.includes(clip.id)) toggleSelect(clip.id, e.shiftKey);

    dragRef.current = {
      mode,
      clipId: clip.id,
      originX: e.clientX,
      originStart: clip.start,
      originDuration: clip.duration,
      originIn: clip.in_point,
      trackId: track.id,
      moved: false,
    };
    setDragging(true);
  };

  useEffect(() => {
    if (!dragging) return;

    const onMove = (e: PointerEvent) => {
      const drag = dragRef.current;
      const doc = useEditor.getState().document;
      if (!drag || !doc) return;
      const deltaSeconds = (e.clientX - drag.originX) / zoom;
      if (Math.abs(e.clientX - drag.originX) > 2) drag.moved = true;

      const next = withSequence(doc, (sequence) => {
        const hit = findClip(sequence, drag.clipId);
        if (!hit) return;
        const { clip } = hit;
        const fps = sequence.fps || 30;
        const targets = snapping
          ? snapTargets(sequence, new Set([drag.clipId]))
          : [];
        const tolerance = 8 / zoom;

        if (drag.mode === "move") {
          let start = Math.max(0, drag.originStart + deltaSeconds);
          if (snapping) {
            const snappedStart = snap(start, targets, tolerance);
            const snappedEnd = snap(start + drag.originDuration, targets, tolerance);
            start = Math.abs(snappedStart - start) <= Math.abs(snappedEnd - (start + drag.originDuration))
              ? snappedStart
              : snappedEnd - drag.originDuration;
          }
          clip.start = Math.max(0, snapToFrame(start, fps));
        } else if (drag.mode === "trim-start") {
          const originEnd = drag.originStart + drag.originDuration;
          // Trimming the head must not run past the head of the source.
          const room = drag.originIn / Math.max(clip.speed, 0.001);
          let start = drag.originStart + deltaSeconds;
          start = Math.max(drag.originStart - room, Math.max(0, start));
          start = Math.min(start, originEnd - 1 / fps);
          if (snapping) start = snap(start, targets, tolerance);
          start = snapToFrame(start, fps);
          const head = start - drag.originStart;
          clip.start = start;
          clip.duration = originEnd - start;
          clip.in_point = Math.max(0, drag.originIn + head * clip.speed);
        } else {
          let end = drag.originStart + drag.originDuration + deltaSeconds;
          end = Math.max(drag.originStart + 1 / fps, end);
          if (snapping) end = snap(end, targets, tolerance);
          clip.duration = snapToFrame(end - drag.originStart, fps);
        }
      });
      patchDocument(next);
    };

    const onUp = () => {
      const drag = dragRef.current;
      dragRef.current = null;
      setDragging(false);
      if (drag?.moved) {
        const label =
          drag.mode === "move" ? "Move clip"
            : drag.mode === "trim-start" ? "Trim clip head" : "Trim clip tail";
        void commitDocument(label);
      }
    };

    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
  }, [dragging, zoom, snapping, patchDocument, commitDocument]);

  // --- wheel zoom ----------------------------------------------------------
  const onWheel = (e: React.WheelEvent) => {
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault();
    const el = scrollRef.current;
    if (!el) return;

    // Zoom about the pointer, so the frame under the cursor stays put.
    const rect = el.getBoundingClientRect();
    const anchorX = e.clientX - rect.left - HEADER_WIDTH;
    const anchorTime = xToTime(el.scrollLeft + anchorX, zoom);
    const nextZoom = clampZoom(zoom * (e.deltaY < 0 ? 1.16 : 0.86));

    setZoom(nextZoom);
    requestAnimationFrame(() => {
      if (!scrollRef.current) return;
      scrollRef.current.scrollLeft = zoomAbout(anchorTime, anchorX, nextZoom);
    });
  };

  const setTrackFlag = (track: Track, flag: "muted" | "hidden" | "locked") => {
    void applyOps(
      [
        {
          type: "set_track",
          description: `${track.name}: toggle ${flag}`,
          scope: { type: "track", track_ids: [track.id] },
          parameters: { [flag]: !track[flag] },
        },
      ],
      `Track ${flag}`,
    );
  };

  if (!seq) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-ink-400">
        Loading timeline…
      </div>
    );
  }

  const step = rulerStep(zoom);
  const ticks = ticksInWindow(window_, step);
  const markers = atTimeInWindow(seq.markers, window_);

  return (
    <div className="flex h-full flex-col bg-ink-900">
      <TimelineToolbar
        seq={seq}
        zoom={zoom}
        setZoom={setZoom}
        snapping={snapping}
        toggleSnapping={toggleSnapping}
      />

      <div
        ref={attachScroller}
        className="relative flex-1 overflow-auto"
        onWheel={onWheel}
        onPointerDown={(e) => {
          if (e.target === e.currentTarget) clearSelection();
        }}
      >
        <div className="flex" style={{ width: HEADER_WIDTH + contentWidth }}>
          {/* Track headers. Absolutely positioned so they stay aligned with
              the lanes, which are virtualized and therefore not in flow. */}
          <div
            className="sticky left-0 z-20 shrink-0 border-r border-ink-750 bg-ink-900"
            style={{ width: HEADER_WIDTH }}
          >
            <div
              className="sticky top-0 z-10 border-b border-ink-750 bg-ink-900"
              style={{ height: RULER_HEIGHT }}
            />
            <div className="relative" style={{ height: layout.totalHeight }}>
              {rows.map((row) => (
                <div
                  key={row.track.id}
                  className="absolute inset-x-0"
                  style={{ top: row.top, height: row.height }}
                >
                  <TrackHeaderRow
                    track={row.track}
                    height={row.height}
                    onFlag={setTrackFlag}
                  />
                </div>
              ))}
              {layout.dividerTop > 0 && (
                <div
                  className="divider-x absolute inset-x-0"
                  style={{ top: layout.dividerTop }}
                />
              )}
            </div>
          </div>

          {/* Lanes */}
          <div ref={laneRef} className="relative" style={{ width: contentWidth }}>
            <div
              className="sticky top-0 z-10 cursor-ew-resize select-none border-b border-ink-750 bg-ink-900"
              style={{ height: RULER_HEIGHT }}
              onPointerDown={onRulerPointerDown}
            >
              {ticks.map((t) => (
                <div
                  key={t}
                  className="absolute top-0 h-full border-l border-ink-700/70 pl-1 pt-1 text-2xs tabular-nums text-ink-400"
                  style={{ left: t * zoom }}
                >
                  {formatTimecode(t, seq.fps, step < 1)}
                </div>
              ))}
              {markers.map((m) => {
                // A beat grid can run to hundreds of marks, so beats are thin
                // ticks that read as a grid while a note stays a full flag.
                const beat = m.kind === "beat";
                return (
                  <div
                    key={m.id}
                    title={m.name ? `${m.name} · ${m.time.toFixed(2)}s` : m.kind}
                    className={`absolute bottom-0 ${
                      beat ? "h-1.5 w-px opacity-70" : "h-2 w-0.5"
                    }`}
                    style={{ left: m.time * zoom, background: m.color }}
                  />
                );
              })}
            </div>

            <div className="relative" style={{ height: layout.totalHeight }}>
              {rows.map((row) => (
                <VirtualLane
                  key={row.track.id}
                  row={row}
                  index={indexByTrack.get(row.track.id) ?? EMPTY_CLIP_INDEX}
                  timeWindow={window_}
                  zoom={zoom}
                  selection={selection}
                  assetById={assetById}
                  onPointerDown={beginDrag}
                  onSelect={toggleSelect}
                />
              ))}
              {layout.dividerTop > 0 && (
                <div
                  className="divider-x absolute inset-x-0"
                  style={{ top: layout.dividerTop }}
                />
              )}
              <Playhead zoom={zoom} />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------

function TimelineToolbar({
  seq,
  zoom,
  setZoom,
  snapping,
  toggleSnapping,
}: {
  seq: Sequence;
  zoom: number;
  setZoom: (z: number) => void;
  snapping: boolean;
  toggleSnapping: () => void;
}) {
  const applyOps = useEditor((s) => s.applyOps);
  const selection = useEditor((s) => s.selection);
  const [time, setTime] = useState(0);

  useEffect(() => subscribePlayhead(setTime), []);

  const split = () =>
    void applyOps(
      [
        {
          type: "split_clip",
          description: `Split at ${formatTimecode(getPlayhead(), seq.fps)}`,
          scope: selection.length
            ? { type: "clip", clip_ids: selection }
            : { type: "all" },
          parameters: { time: getPlayhead() },
        },
      ],
      "Split",
    );

  const remove = () =>
    void applyOps(
      [
        {
          type: "delete_clip",
          description: `Delete ${selection.length} clip(s)`,
          scope: { type: "clip", clip_ids: selection },
          parameters: {},
        },
      ],
      "Delete clip",
    );

  return (
    <div className="flex h-9 shrink-0 items-center gap-1.5 border-b border-ink-750 px-2.5">
      <span className="font-mono text-sm tabular-nums text-accent">
        {formatTimecode(time, seq.fps)}
      </span>
      <span className="ml-1 text-2xs text-ink-500">
        / {formatTimecode(sequenceDuration(seq), seq.fps)}
      </span>

      <div className="mx-2 h-4 w-px bg-ink-700" />

      <button className="btn btn-ghost" onClick={split} title="Split at playhead (S)">
        <IconSplit /> Split
      </button>
      <button
        className="btn btn-ghost btn-danger"
        onClick={remove}
        disabled={selection.length === 0}
        title="Delete selected (Del)"
      >
        <IconTrash /> Delete
      </button>
      <button
        className={`btn btn-ghost ${snapping ? "text-accent" : ""}`}
        onClick={toggleSnapping}
        title="Toggle snapping (N)"
      >
        <IconMagnet /> Snap
      </button>

      <div className="ml-auto flex items-center gap-2">
        <span className="label">Zoom</span>
        <input
          type="range"
          min={4}
          max={400}
          value={zoom}
          onChange={(e) => setZoom(Number(e.target.value))}
          className="h-1 w-32 accent-accent"
        />
      </div>
    </div>
  );
}

/**
 * One track's row, drawing only the clips inside the visible window.
 *
 * Memoized on its inputs, so scrolling one lane into view does not re-render
 * the others, and a drag that changes one clip does not redraw eight tracks.
 */
const VirtualLane = memo(function VirtualLane({
  row,
  index,
  timeWindow,
  zoom,
  selection,
  assetById,
  onPointerDown,
  onSelect,
}: {
  row: Row;
  index: ClipIndex;
  timeWindow: TimeWindow;
  zoom: number;
  selection: string[];
  assetById: Record<string, Asset | undefined>;
  onPointerDown: (
    e: React.PointerEvent,
    clip: Clip,
    track: Track,
    mode: DragMode,
  ) => void;
  onSelect: (clipId: string, additive: boolean) => void;
}) {
  const { track, top, height } = row;
  const visible = useMemo(
    () => clipsInWindow(index, timeWindow),
    [index, timeWindow],
  );

  return (
    <div
      className={`absolute inset-x-0 border-b border-ink-800/80 ${
        track.hidden || track.muted ? "opacity-40" : ""
      }`}
      style={{
        top,
        height,
        background:
          track.index % 2 === 0 ? "rgba(255,255,255,0.012)" : "transparent",
      }}
      data-track-id={track.id}
      data-clip-count={visible.length}
    >
      {visible.map((clip) => (
        <ClipView
          key={clip.id}
          clip={clip}
          track={track}
          zoom={zoom}
          height={height}
          selected={selection.includes(clip.id)}
          asset={clip.asset_id ? assetById[clip.asset_id] : undefined}
          onPointerDown={onPointerDown}
          onSelect={(e) => onSelect(clip.id, e.shiftKey || e.metaKey)}
        />
      ))}
    </div>
  );
});

function TrackHeaderRow({
  track,
  height,
  onFlag,
}: {
  track: Track;
  height: number;
  onFlag: (t: Track, f: "muted" | "hidden" | "locked") => void;
}) {
  const isVideo = track.kind === "video";
  return (
    <div
      className="flex items-center gap-1 border-b border-ink-800/80 px-2"
      style={{ height }}
    >
      <span
        className="h-4 w-1 rounded-full"
        style={{ background: isVideo ? "#3f7fd4" : "#3fae7f" }}
      />
      <span className="flex-1 truncate text-xs font-medium text-ink-200">
        {track.name}
        {track.is_adjustment && (
          <span className="ml-1 text-2xs text-adjust">adj</span>
        )}
      </span>
      <button
        className={`rounded px-1 text-2xs ${
          track.muted ? "text-danger" : "text-ink-500 hover:text-ink-200"
        }`}
        onClick={() => onFlag(track, "muted")}
        title={track.muted ? "Unmute track" : "Mute track"}
      >
        M
      </button>
      {isVideo && (
        <button
          className={`rounded px-1 text-2xs ${
            track.hidden ? "text-danger" : "text-ink-500 hover:text-ink-200"
          }`}
          onClick={() => onFlag(track, "hidden")}
          title={track.hidden ? "Show track" : "Hide track"}
        >
          V
        </button>
      )}
      <button
        className={`rounded px-1 text-2xs ${
          track.locked ? "text-accent" : "text-ink-500 hover:text-ink-200"
        }`}
        onClick={() => onFlag(track, "locked")}
        title={track.locked ? "Unlock track" : "Lock track"}
      >
        L
      </button>
    </div>
  );
}

const CLIP_COLORS: Record<string, string> = {
  video: "#26456e",
  image: "#2b5a63",
  audio: "#1f5744",
  text: "#4b2d63",
  solid: "#3a3a46",
  adjustment: "#63382a",
};

function ClipView({
  clip,
  track,
  zoom,
  height,
  selected,
  asset,
  onPointerDown,
  onSelect,
}: {
  clip: Clip;
  track: Track;
  zoom: number;
  height: number;
  selected: boolean;
  asset?: { filmstrip_url: string | null; waveform_url: string | null; name: string };
  onPointerDown: (e: React.PointerEvent, c: Clip, t: Track, m: DragMode) => void;
  onSelect: (e: React.PointerEvent) => void;
}) {
  const width = Math.max(2, clip.duration * zoom);
  const background = CLIP_COLORS[clip.kind] ?? "#2b3038";
  const showDetail = width > 44;

  return (
    <div
      className={`group absolute top-1 cursor-grab overflow-hidden rounded-[3px] border transition-shadow duration-100 ${
        selected
          ? "border-accent shadow-[0_0_0_1px_rgba(224,163,60,0.5)]"
          : "border-black/45 hover:border-ink-400/60"
      } ${clip.enabled ? "" : "opacity-40"}`}
      style={{
        left: clip.start * zoom,
        width,
        height: height - 8,
        background,
      }}
      onPointerDown={(e) => {
        onSelect(e);
        onPointerDown(e, clip, track, "move");
      }}
      title={`${clip.name || clip.kind} · ${clip.duration.toFixed(2)}s`}
    >
      {asset?.filmstrip_url && clip.kind === "video" && width > 60 && (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={asset.filmstrip_url}
          alt=""
          draggable={false}
          className="pointer-events-none absolute inset-0 h-full w-full object-cover opacity-30"
        />
      )}
      {asset?.waveform_url && clip.kind === "audio" && (
        <div className="pointer-events-none absolute inset-x-0 bottom-0 h-1/2 bg-gradient-to-t from-audio/25 to-transparent" />
      )}

      <div className="pointer-events-none relative flex h-full flex-col justify-between p-1">
        {showDetail && (
          <span className="truncate text-2xs font-medium leading-none text-white/90">
            {clip.name || clip.kind}
          </span>
        )}
        {showDetail && (
          <div className="flex items-center gap-1">
            {clip.speed !== 1 && (
              <span className="rounded-sm bg-black/45 px-1 text-2xs text-accent-soft">
                {clip.speed.toFixed(2)}×
              </span>
            )}
            {clip.muted && (
              <span className="rounded-sm bg-black/45 px-1 text-2xs text-danger">
                muted
              </span>
            )}
            {clip.effects.length > 0 && (
              <span className="rounded-sm bg-black/45 px-1 text-2xs text-ink-200">
                fx {clip.effects.length}
              </span>
            )}
          </div>
        )}
      </div>

      {/* Transition wedges */}
      {clip.transition_in.type !== "none" && (
        <div
          className="pointer-events-none absolute inset-y-0 left-0 bg-gradient-to-r from-white/30 to-transparent"
          style={{ width: Math.min(width / 2, clip.transition_in.duration * zoom) }}
        />
      )}
      {clip.transition_out.type !== "none" && (
        <div
          className="pointer-events-none absolute inset-y-0 right-0 bg-gradient-to-l from-white/30 to-transparent"
          style={{ width: Math.min(width / 2, clip.transition_out.duration * zoom) }}
        />
      )}

      {/* Keyframes, so an animated clip is identifiable without opening it. */}
      {selected && <KeyframeMarks clip={clip} zoom={zoom} />}

      <div
        className="absolute inset-y-0 left-0 w-1.5 cursor-w-resize bg-white/0 hover:bg-white/40"
        onPointerDown={(e) => onPointerDown(e, clip, track, "trim-start")}
      />
      <div
        className="absolute inset-y-0 right-0 w-1.5 cursor-e-resize bg-white/0 hover:bg-white/40"
        onPointerDown={(e) => onPointerDown(e, clip, track, "trim-end")}
      />
    </div>
  );
}

/**
 * Keyframe positions along the bottom of a selected clip.
 *
 * Only shown on selection: drawing every curve on every clip turns a busy
 * timeline into noise, and the diamonds matter most while you are working on
 * that one clip.
 */
function KeyframeMarks({ clip, zoom }: { clip: Clip; zoom: number }) {
  const curves = Object.entries(clip.transform.keyframes ?? {});
  if (curves.length === 0) return null;

  const times = new Map<number, string[]>();
  for (const [property, curve] of curves) {
    for (const keyframe of curve) {
      const key = Math.round(keyframe.time * 1000) / 1000;
      times.set(key, [...(times.get(key) ?? []), property]);
    }
  }

  return (
    <div className="pointer-events-none absolute inset-x-0 bottom-0 h-2">
      {[...times.entries()].map(([time, properties]) => (
        <span
          key={time}
          title={`${properties.join(", ")} at ${time.toFixed(2)}s`}
          className="absolute bottom-[1px] h-1.5 w-1.5 -translate-x-1/2 rotate-45 bg-accent"
          style={{ left: time * zoom }}
        />
      ))}
    </div>
  );
}


function Playhead({ zoom }: { zoom: number }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(
    () =>
      subscribePlayhead((t) => {
        if (ref.current) {
          ref.current.style.transform = `translateX(${t * zoom}px)`;
        }
      }),
    [zoom],
  );
  return (
    <div
      ref={ref}
      className="pointer-events-none absolute inset-y-0 left-0 z-30 w-px bg-accent will-change-transform"
    >
      <div className="absolute -left-[5px] top-0 h-2.5 w-2.5 rotate-45 bg-accent" />
    </div>
  );
}

function IconSplit() {
  return (
    <svg width="11" height="11" viewBox="0 0 16 16" fill="none" aria-hidden>
      <path d="M8 1v14M4 5l4-4 4 4" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  );
}
function IconTrash() {
  return (
    <svg width="11" height="11" viewBox="0 0 16 16" fill="none" aria-hidden>
      <path d="M3 4h10M6.5 4V2.5h3V4M4.5 4l.6 9h5.8l.6-9" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
    </svg>
  );
}
function IconMagnet() {
  return (
    <svg width="11" height="11" viewBox="0 0 16 16" fill="none" aria-hidden>
      <path d="M4 3v5a4 4 0 0 0 8 0V3M4 6h3M9 6h3" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  );
}
