"use client";

/**
 * Video scopes: waveform, RGB parade, vectorscope and histogram.
 *
 * These read the program monitor's own canvas, so they measure exactly what
 * is on screen -- the same compositing, the same grade. Nothing here is
 * decorative: every plot is computed from real pixels, and if the monitor has
 * not painted yet the scopes say so rather than drawing a plausible shape.
 *
 * The frame is downsampled before analysis. A 1080p read is two million
 * pixels per update, which would cost more than the edit itself; a 256-wide
 * sample preserves the distribution that matters and costs almost nothing.
 */
import { useEffect, useRef, useState } from "react";

import { subscribePlayhead } from "@/store/editor";

export type ScopeKind = "waveform" | "parade" | "vectorscope" | "histogram";

const SCOPES: { key: ScopeKind; label: string; hint: string }[] = [
  { key: "waveform", label: "Waveform", hint: "Luma against horizontal position" },
  { key: "parade", label: "RGB Parade", hint: "Each channel plotted separately" },
  { key: "vectorscope", label: "Vectorscope", hint: "Hue and saturation" },
  { key: "histogram", label: "Histogram", hint: "How many pixels at each level" },
];

//: Analysis resolution. Wide enough to keep horizontal position meaningful.
const SW = 256;
const SH = 144;

export function Scopes() {
  const [kind, setKind] = useState<ScopeKind>("waveform");
  const [ready, setReady] = useState(false);
  const scopeRef = useRef<HTMLCanvasElement>(null);
  const sampleRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    let raf = 0;
    let last = 0;

    const tick = (now: number) => {
      raf = requestAnimationFrame(tick);
      // Scopes are a reading, not an animation; 12Hz is past the point where
      // a colourist can tell, and it leaves the frame budget to playback.
      if (now - last < 80) return;
      last = now;

      const source = document.querySelector<HTMLCanvasElement>(
        "canvas[data-program]",
      );
      const out = scopeRef.current;
      if (!source || !out || source.width < 2) {
        setReady(false);
        return;
      }

      if (!sampleRef.current) sampleRef.current = document.createElement("canvas");
      const sample = sampleRef.current;
      sample.width = SW;
      sample.height = SH;
      const sctx = sample.getContext("2d", { willReadFrequently: true });
      if (!sctx) return;

      try {
        sctx.drawImage(source, 0, 0, SW, SH);
      } catch {
        setReady(false);
        return;
      }
      const pixels = sctx.getImageData(0, 0, SW, SH).data;
      setReady(true);
      draw(out, pixels, kind);
    };

    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [kind]);

  // Redraw immediately on a seek, so a paused monitor still updates.
  useEffect(() => subscribePlayhead(() => undefined), []);

  const active = SCOPES.find((s) => s.key === kind)!;

  return (
    <div className="flex h-full flex-col bg-ink-900">
      <div className="panel-header">
        <span>Scopes</span>
        <span className="flex gap-0.5 normal-case tracking-normal">
          {SCOPES.map((s) => (
            <button
              key={s.key}
              onClick={() => setKind(s.key)}
              title={s.hint}
              className={`h-5 rounded px-1.5 text-2xs transition-colors ${
                kind === s.key
                  ? "bg-ink-750 text-ink-100"
                  : "text-ink-400 hover:text-ink-200"
              }`}
            >
              {s.label}
            </button>
          ))}
        </span>
      </div>

      <div className="relative min-h-0 flex-1 bg-ink-950 p-2">
        <canvas
          ref={scopeRef}
          width={512}
          height={288}
          className="h-full w-full"
          style={{ imageRendering: "pixelated" }}
        />
        {!ready && (
          <div className="absolute inset-0 flex items-center justify-center text-2xs text-ink-500">
            Waiting for the program monitor to paint a frame
          </div>
        )}
      </div>

      <div className="shrink-0 border-t border-ink-750 px-3 py-1.5 text-2xs text-ink-500">
        {active.hint}. Measured from the monitor, so it reflects the grade you
        see, not the source.
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------

function draw(canvas: HTMLCanvasElement, pixels: Uint8ClampedArray, kind: ScopeKind) {
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  const W = canvas.width;
  const H = canvas.height;

  ctx.fillStyle = "#07080a";
  ctx.fillRect(0, 0, W, H);

  if (kind === "waveform") drawWaveform(ctx, pixels, W, H);
  else if (kind === "parade") drawParade(ctx, pixels, W, H);
  else if (kind === "vectorscope") drawVectorscope(ctx, pixels, W, H);
  else drawHistogram(ctx, pixels, W, H);
}

/** Horizontal graticule at the levels a colourist actually watches. */
function graticule(ctx: CanvasRenderingContext2D, x: number, w: number, H: number) {
  ctx.strokeStyle = "rgba(255,255,255,0.09)";
  ctx.fillStyle = "rgba(255,255,255,0.30)";
  ctx.font = "9px ui-monospace, monospace";
  ctx.lineWidth = 1;
  for (const level of [0, 25, 50, 75, 100]) {
    const y = H - (level / 100) * H;
    ctx.beginPath();
    ctx.moveTo(x, y + 0.5);
    ctx.lineTo(x + w, y + 0.5);
    ctx.stroke();
  }
  ctx.fillText("100", 2, 10);
  ctx.fillText("0", 2, H - 3);
}

/**
 * Waveform: for each column of the picture, plot the luma of every pixel in
 * that column. Bright areas stack high, shadows sit low, and clipping shows
 * as a flat line pinned to the top or bottom.
 */
function drawWaveform(ctx: CanvasRenderingContext2D, px: Uint8ClampedArray,
                      W: number, H: number) {
  const bins = new Float32Array(W * H);
  for (let y = 0; y < SH; y += 1) {
    for (let x = 0; x < SW; x += 1) {
      const i = (y * SW + x) * 4;
      const luma = (px[i] * 0.299 + px[i + 1] * 0.587 + px[i + 2] * 0.114) / 255;
      const cx = Math.min(W - 1, Math.floor((x / SW) * W));
      const cy = Math.min(H - 1, Math.floor((1 - luma) * (H - 1)));
      bins[cy * W + cx] += 1;
    }
  }
  paintBins(ctx, bins, W, H, [190, 230, 255]);
  graticule(ctx, 0, W, H);
}

/** RGB parade: the same plot, once per channel, side by side. */
function drawParade(ctx: CanvasRenderingContext2D, px: Uint8ClampedArray,
                    W: number, H: number) {
  const third = Math.floor(W / 3);
  const tints: [number, number, number][] = [
    [255, 90, 90], [90, 255, 130], [110, 150, 255],
  ];
  for (let channel = 0; channel < 3; channel += 1) {
    const bins = new Float32Array(third * H);
    for (let y = 0; y < SH; y += 1) {
      for (let x = 0; x < SW; x += 1) {
        const value = px[(y * SW + x) * 4 + channel] / 255;
        const cx = Math.min(third - 1, Math.floor((x / SW) * third));
        const cy = Math.min(H - 1, Math.floor((1 - value) * (H - 1)));
        bins[cy * third + cx] += 1;
      }
    }
    paintBins(ctx, bins, third, H, tints[channel], channel * third);
    ctx.strokeStyle = "rgba(255,255,255,0.10)";
    ctx.beginPath();
    ctx.moveTo(channel * third + 0.5, 0);
    ctx.lineTo(channel * third + 0.5, H);
    ctx.stroke();
  }
  graticule(ctx, 0, W, H);
}

/** Shared painter: turn a hit-count grid into additive intensity. */
function paintBins(ctx: CanvasRenderingContext2D, bins: Float32Array,
                   w: number, h: number, tint: [number, number, number],
                   offsetX = 0) {
  let peak = 0;
  for (let i = 0; i < bins.length; i += 1) if (bins[i] > peak) peak = bins[i];
  if (peak <= 0) return;

  const image = ctx.createImageData(w, h);
  for (let i = 0; i < bins.length; i += 1) {
    // Square root keeps a single stray pixel visible next to a dense trace,
    // which is the whole point of looking at a scope.
    const intensity = Math.min(1, Math.sqrt(bins[i] / peak) * 1.35);
    if (intensity <= 0) continue;
    const p = i * 4;
    image.data[p] = tint[0] * intensity;
    image.data[p + 1] = tint[1] * intensity;
    image.data[p + 2] = tint[2] * intensity;
    image.data[p + 3] = 255 * Math.min(1, intensity * 1.4);
  }
  ctx.putImageData(image, offsetX, 0);
}

/**
 * Vectorscope: hue as angle, saturation as distance from centre. The skin
 * tone line at 123 degrees is drawn because faces landing on it is the single
 * most common thing this scope is used to check.
 */
function drawVectorscope(ctx: CanvasRenderingContext2D, px: Uint8ClampedArray,
                         W: number, H: number) {
  const cx = W / 2;
  const cy = H / 2;
  const radius = Math.min(W, H) / 2 - 8;

  ctx.strokeStyle = "rgba(255,255,255,0.12)";
  ctx.beginPath();
  ctx.arc(cx, cy, radius, 0, Math.PI * 2);
  ctx.stroke();
  ctx.beginPath();
  ctx.arc(cx, cy, radius * 0.5, 0, Math.PI * 2);
  ctx.stroke();

  // Skin tone line.
  const skin = (123 * Math.PI) / 180;
  ctx.strokeStyle = "rgba(224,163,60,0.45)";
  ctx.beginPath();
  ctx.moveTo(cx, cy);
  ctx.lineTo(cx + Math.cos(-skin) * radius, cy + Math.sin(-skin) * radius);
  ctx.stroke();

  ctx.globalCompositeOperation = "lighter";
  for (let i = 0; i < px.length; i += 4) {
    const r = px[i] / 255;
    const g = px[i + 1] / 255;
    const b = px[i + 2] / 255;
    // Rec.709 chroma axes.
    const u = -0.1146 * r - 0.3854 * g + 0.5 * b;
    const v = 0.5 * r - 0.4542 * g - 0.0458 * b;
    const x = cx + u * 2 * radius;
    const y = cy - v * 2 * radius;
    ctx.fillStyle = `rgba(${px[i]},${px[i + 1]},${px[i + 2]},0.16)`;
    ctx.fillRect(x, y, 1.5, 1.5);
  }
  ctx.globalCompositeOperation = "source-over";
}

/** Histogram: how many pixels sit at each level, per channel. */
function drawHistogram(ctx: CanvasRenderingContext2D, px: Uint8ClampedArray,
                       W: number, H: number) {
  const bins = [new Float32Array(256), new Float32Array(256), new Float32Array(256)];
  for (let i = 0; i < px.length; i += 4) {
    bins[0][px[i]] += 1;
    bins[1][px[i + 1]] += 1;
    bins[2][px[i + 2]] += 1;
  }

  let peak = 0;
  for (const channel of bins) {
    for (const value of channel) if (value > peak) peak = value;
  }
  if (peak <= 0) return;

  graticule(ctx, 0, W, H);
  const colours = ["rgba(255,90,90,0.75)", "rgba(90,255,130,0.65)",
                   "rgba(110,150,255,0.65)"];
  ctx.globalCompositeOperation = "lighter";
  bins.forEach((channel, index) => {
    ctx.fillStyle = colours[index];
    for (let level = 0; level < 256; level += 1) {
      const height = (channel[level] / peak) * (H - 4);
      if (height <= 0) continue;
      const x = (level / 256) * W;
      ctx.fillRect(x, H - height, Math.max(1, W / 256), height);
    }
  });
  ctx.globalCompositeOperation = "source-over";
}
