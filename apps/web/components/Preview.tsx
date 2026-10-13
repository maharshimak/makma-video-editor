"use client";

/**
 * Program monitor.
 *
 * Runs the canvas compositor at display refresh rate. Playback advances the
 * playhead from wall-clock time rather than from a video element's clock, so
 * gaps, stacked layers and text-only stretches all play at the right speed.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Compositor, type CompositorStats } from "@/lib/compositor";
import { formatTimecode } from "@/lib/time";
import type { Sequence } from "@/lib/types";
import {
  getPlayhead,
  isPlaying,
  sequenceDuration,
  setPlayhead,
  setPlaying,
  subscribePlayhead,
  subscribePlaying,
  useEditor,
} from "@/store/editor";

export function Preview() {
  const document_ = useEditor((s) => s.document);
  const assets = useEditor((s) => s.assets);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const compositorRef = useRef<Compositor | null>(null);
  const seqRef = useRef<Sequence | null>(null);
  const lastFrameRef = useRef<number>(0);

  const [playing, setPlayingLocal] = useState(false);
  const [time, setTime] = useState(0);
  const [stats, setStats] = useState<CompositorStats>({
    drawnClips: 0,
    waiting: 0,
    approximated: [],
  });
  const [volume, setVolume] = useState(1);

  const seq = useMemo(() => {
    if (!document_) return null;
    return (
      document_.sequences.find((s) => s.id === document_.active_sequence_id) ??
      document_.sequences[0] ??
      null
    );
  }, [document_]);

  seqRef.current = seq;
  const duration = sequenceDuration(seq);

  // The asset list gets a new identity on every poll during ingestion. The
  // compositor reads it through a ref so those refreshes never tear down the
  // media pool -- rebuilding it would re-download every proxy mid-edit.
  const assetsRef = useRef(assets);
  assetsRef.current = assets;

  useEffect(() => {
    compositorRef.current = new Compositor((id) =>
      assetsRef.current.find((a) => a.id === id),
    );
    return () => {
      compositorRef.current?.dispose();
      compositorRef.current = null;
    };
  }, []);

  useEffect(() => subscribePlayhead(setTime), []);
  useEffect(() => subscribePlaying(setPlayingLocal), []);

  // --- render loop ---------------------------------------------------------
  /** Draw exactly one frame. Safe to call outside the rAF loop. */
  const renderFrame = useCallback(
    (advanceBy = 0) => {
      const sequence = seqRef.current;
      const compositor = compositorRef.current;
      const canvas = canvasRef.current;
      if (!sequence || !compositor || !canvas) return;

      const total = sequenceDuration(sequence);
      if (advanceBy > 0 && isPlaying()) {
        const next = getPlayhead() + Math.min(advanceBy, 0.25);
        if (next >= total) {
          setPlayhead(total);
          setPlaying(false);
        } else {
          setPlayhead(next);
        }
      }

      const t = getPlayhead();
      compositor.sync(sequence, t, isPlaying(), volume);
      setStats(compositor.draw(canvas, sequence, t));
    },
    [volume],
  );

  useEffect(() => {
    let raf = 0;
    lastFrameRef.current = performance.now();

    const frame = (now: number) => {
      raf = requestAnimationFrame(frame);
      const elapsed = (now - lastFrameRef.current) / 1000;
      lastFrameRef.current = now;
      renderFrame(elapsed);
    };

    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, [renderFrame]);

  // requestAnimationFrame does not run while the tab is hidden, and the
  // playhead can move without it (a seek from the timeline, an AI edit). Paint
  // once whenever the picture should have changed so the monitor is never
  // showing a stale or blank frame when the user looks back at it.
  useEffect(() => {
    renderFrame();
  }, [renderFrame, seq, assets]);

  useEffect(() => subscribePlayhead(() => {
    if (!isPlaying()) renderFrame();
  }), [renderFrame]);

  // --- transport -----------------------------------------------------------
  const togglePlay = useCallback(() => {
    if (!isPlaying() && getPlayhead() >= duration - 0.02) setPlayhead(0);
    setPlaying(!isPlaying());
  }, [duration]);

  const step = (frames: number) => {
    setPlaying(false);
    const fps = seq?.fps ?? 30;
    setPlayhead(Math.max(0, Math.min(duration, getPlayhead() + frames / fps)));
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (target && ["INPUT", "TEXTAREA"].includes(target.tagName)) return;
      if (e.code === "Space") {
        e.preventDefault();
        togglePlay();
      } else if (e.key === "ArrowLeft") {
        e.preventDefault();
        step(e.shiftKey ? -10 : -1);
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        step(e.shiftKey ? 10 : 1);
      } else if (e.key === "Home") {
        setPlaying(false);
        setPlayhead(0);
      } else if (e.key === "End") {
        setPlaying(false);
        setPlayhead(duration);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const progress = duration > 0 ? Math.min(1, time / duration) : 0;

  return (
    <div className="flex h-full flex-col bg-ink-900">
      <div className="panel-header">
        <span>Program</span>
        <span className="flex items-center gap-2 normal-case tracking-normal">
          {seq && (
            <span className="chip">
              {seq.width}×{seq.height} · {seq.fps}fps
            </span>
          )}
          {stats.waiting > 0 && (
            <span className="chip text-accent">buffering {stats.waiting}</span>
          )}
        </span>
      </div>

      <div className="checkerboard flex min-h-0 flex-1 items-center justify-center p-4">
        <canvas
          ref={canvasRef}
          data-program=""
          onClick={togglePlay}
          className="h-full w-full cursor-pointer bg-black object-contain shadow-pop"
          style={{ aspectRatio: seq ? `${seq.width} / ${seq.height}` : "16 / 9" }}
        />
      </div>

      {stats.approximated.length > 0 && (
        <div className="border-t border-ink-750 bg-ink-850 px-3 py-1.5 text-2xs text-ink-400">
          Preview approximates colour and does not draw{" "}
          <span className="text-ink-200">
            {stats.approximated.join(", ").replace(/_/g, " ")}
          </span>
          . Render for the exact result.
        </div>
      )}

      <div className="shrink-0 border-t border-ink-750 px-3 py-2">
        <div
          className="group relative mb-2 h-1.5 cursor-pointer rounded-full bg-ink-750"
          onPointerDown={(e) => {
            const rect = e.currentTarget.getBoundingClientRect();
            const seek = (clientX: number) => {
              setPlaying(false);
              setPlayhead(
                Math.max(0, Math.min(duration, ((clientX - rect.left) / rect.width) * duration)),
              );
            };
            seek(e.clientX);
            const move = (ev: PointerEvent) => seek(ev.clientX);
            const up = () => {
              window.removeEventListener("pointermove", move);
              window.removeEventListener("pointerup", up);
            };
            window.addEventListener("pointermove", move);
            window.addEventListener("pointerup", up);
          }}
        >
          <div
            className="absolute inset-y-0 left-0 rounded-full bg-accent/80"
            style={{ width: `${progress * 100}%` }}
          />
          <div
            className="absolute top-1/2 h-3 w-3 -translate-x-1/2 -translate-y-1/2 rounded-full bg-accent opacity-0 transition-opacity group-hover:opacity-100"
            style={{ left: `${progress * 100}%` }}
          />
        </div>

        <div className="flex items-center gap-1.5">
          <button className="btn btn-ghost" onClick={() => { setPlaying(false); setPlayhead(0); }} title="Go to start (Home)">
            <IconStart />
          </button>
          <button className="btn btn-ghost" onClick={() => step(-1)} title="Previous frame (←)">
            <IconPrev />
          </button>
          <button
            className="btn btn-accent w-9"
            onClick={togglePlay}
            title="Play / pause (Space)"
          >
            {playing ? <IconPause /> : <IconPlay />}
          </button>
          <button className="btn btn-ghost" onClick={() => step(1)} title="Next frame (→)">
            <IconNext />
          </button>

          <span className="ml-2 font-mono text-xs tabular-nums text-ink-200">
            {formatTimecode(time, seq?.fps ?? 30)}
          </span>
          <span className="text-2xs text-ink-500">
            / {formatTimecode(duration, seq?.fps ?? 30)}
          </span>

          <div className="ml-auto flex items-center gap-1.5">
            <IconVolume muted={volume === 0} />
            <input
              type="range"
              min={0}
              max={1}
              step={0.02}
              value={volume}
              onChange={(e) => setVolume(Number(e.target.value))}
              className="h-1 w-20 accent-accent"
              title="Preview volume"
            />
          </div>
        </div>
      </div>
    </div>
  );
}

function IconPlay() {
  return <svg width="12" height="12" viewBox="0 0 16 16" aria-hidden><path d="M4 2.5v11l9-5.5-9-5.5Z" fill="currentColor" /></svg>;
}
function IconPause() {
  return <svg width="12" height="12" viewBox="0 0 16 16" aria-hidden><path d="M4 2.5h3v11H4zM9 2.5h3v11H9z" fill="currentColor" /></svg>;
}
function IconPrev() {
  return <svg width="12" height="12" viewBox="0 0 16 16" aria-hidden><path d="M11 3v10L4 8l7-5Z" fill="currentColor" /></svg>;
}
function IconNext() {
  return <svg width="12" height="12" viewBox="0 0 16 16" aria-hidden><path d="M5 3v10l7-5-7-5Z" fill="currentColor" /></svg>;
}
function IconStart() {
  return <svg width="12" height="12" viewBox="0 0 16 16" aria-hidden><path d="M4 3v10h1.5V3H4Zm8 0-6 5 6 5V3Z" fill="currentColor" /></svg>;
}
function IconVolume({ muted }: { muted: boolean }) {
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden className="text-ink-400">
      <path d="M3 6h2.5L9 3v10L5.5 10H3V6Z" fill="currentColor" />
      {!muted && (
        <path d="M11 6.2a2.6 2.6 0 0 1 0 3.6" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
      )}
    </svg>
  );
}
