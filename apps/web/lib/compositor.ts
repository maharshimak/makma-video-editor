"use client";

/**
 * Canvas preview compositor.
 *
 * This draws the timeline live in the browser from the 720p proxies: it walks
 * the video tracks bottom-to-top exactly like the FFmpeg compiler does, so
 * layer order, transforms, opacity, transitions and text land where the render
 * will put them.
 *
 * It is an approximation in one specific way, and the UI says so: colour work
 * is expressed as canvas `filter` strings, which are close to but not identical
 * to FFmpeg's `eq`/`colorbalance`/`curves` maths. Anything requiring real
 * pixel processing -- grain, stabilisation, wipes -- is not drawn here. Press
 * Render for the exact result.
 */
import type { Asset, Clip, Easing, Effect, Keyframe, Sequence } from "./types";

export interface CompositorStats {
  drawnClips: number;
  waiting: number;
  approximated: string[];
}

type MediaElement = HTMLVideoElement | HTMLImageElement | HTMLAudioElement;

/** Effects the canvas preview cannot represent; listed in the UI, not faked. */
const NOT_PREVIEWED = new Set([
  "film_grain", "stabilize", "denoise", "camera_shake", "glow", "lut",
  "curves", "pixelate", "zoom",
]);

/** Effects drawn as a full-frame overlay after the clip itself. */
const FRAME_EFFECTS = new Set(["letterbox", "vignette"]);

const GRADE_FILTERS: Record<string, string> = {
  cinematic_dark: "contrast(1.14) brightness(0.94) saturate(0.90)",
  teal_orange: "contrast(1.12) saturate(1.08) hue-rotate(-6deg)",
  luxury: "contrast(1.08) brightness(0.96) saturate(0.86)",
  warm: "contrast(1.05) saturate(1.08) sepia(0.12)",
  cool: "contrast(1.05) saturate(0.98) hue-rotate(8deg)",
  high_contrast: "contrast(1.3) saturate(1.05)",
  flat: "contrast(0.88) saturate(0.92) brightness(1.03)",
  bw: "grayscale(1) contrast(1.16)",
  vintage: "sepia(0.35) contrast(0.96) saturate(0.78)",
  cyberpunk: "contrast(1.22) saturate(1.35) hue-rotate(-12deg)",
  night: "contrast(1.1) brightness(0.78) saturate(0.72) hue-rotate(12deg)",
  golden_hour: "contrast(1.06) saturate(1.12) sepia(0.16)",
};

function num(value: unknown, fallback: number): number {
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n : fallback;
}

/** Build a canvas filter string that approximates a clip's effect stack. */
export function filterFor(effects: Effect[], approximated: string[]): string {
  const parts: string[] = [];
  for (const fx of effects) {
    if (!fx.enabled) continue;
    if (FRAME_EFFECTS.has(fx.type)) continue; // painted separately
    if (NOT_PREVIEWED.has(fx.type)) {
      if (!approximated.includes(fx.type)) approximated.push(fx.type);
      continue;
    }
    const p = fx.parameters ?? {};
    switch (fx.type) {
      case "color_grade": {
        const preset = String(p.preset ?? "cinematic_dark");
        const intensity = Math.max(0, Math.min(1, num(p.intensity, 1)));
        const base = GRADE_FILTERS[preset] ?? GRADE_FILTERS.cinematic_dark;
        parts.push(intensity >= 0.99 ? base : scaleFilter(base, intensity));
        break;
      }
      case "exposure":
        parts.push(`brightness(${(1 + num(p.stops, 0) * 0.18).toFixed(3)})`);
        break;
      case "color":
        parts.push(
          `contrast(${num(p.contrast, 1).toFixed(3)})`,
          `saturate(${num(p.saturation, 1).toFixed(3)})`,
          `brightness(${(1 + num(p.brightness, 0)).toFixed(3)})`,
        );
        break;
      case "hue":
        parts.push(
          `hue-rotate(${num(p.hue, 0).toFixed(1)}deg)`,
          `saturate(${num(p.saturation, 1).toFixed(3)})`,
        );
        break;
      case "temperature": {
        const k = num(p.kelvin, 6500);
        parts.push(k < 6500 ? `sepia(${Math.min(0.4, (6500 - k) / 9000).toFixed(3)})`
          : `hue-rotate(${Math.min(18, (k - 6500) / 400).toFixed(1)}deg)`);
        break;
      }
      case "blur":
        parts.push(`blur(${(num(p.sigma, 8) * 0.6).toFixed(2)}px)`);
        break;
      case "sharpen":
        parts.push(`contrast(${(1 + num(p.amount, 0.8) * 0.06).toFixed(3)})`);
        break;
      case "mirror":
        break; // handled as a transform, not a filter
      default:
        break;
    }
  }
  return parts.join(" ");
}

function scaleFilter(filter: string, k: number): string {
  return filter.replace(/([a-z-]+)\(([-\d.]+)(\w*)\)/g, (_m, fn, value, unit) => {
    const v = Number(value);
    const neutral = unit === "deg" ? 0 : fn === "grayscale" || fn === "sepia" ? 0 : 1;
    return `${fn}(${(neutral + (v - neutral) * k).toFixed(3)}${unit})`;
  });
}

/** Ease a 0..1 progress value. Mirrors `_ease` in the Python schema. */
function ease(easing: Easing, p: number): number {
  switch (easing) {
    case "linear": return p;
    case "ease_in": return p * p;
    case "ease_out": return 1 - (1 - p) * (1 - p);
    case "hold": return 0;
    default: return p < 0.5 ? 2 * p * p : 1 - 2 * (1 - p) * (1 - p);
  }
}

/**
 * Sample a keyframe curve at a point in clip-local time.
 *
 * This is the same interpolation the render performs, so the monitor and the
 * export agree on where an animated property sits at any moment.
 */
export function sampleCurve(curve: Keyframe[], localTime: number): number | null {
  if (!curve || curve.length === 0) return null;
  const ordered = [...curve].sort((a, b) => a.time - b.time);
  if (localTime <= ordered[0].time) return ordered[0].value;
  const last = ordered[ordered.length - 1];
  if (localTime >= last.time) return last.value;

  for (let i = 0; i < ordered.length - 1; i += 1) {
    const a = ordered[i];
    const b = ordered[i + 1];
    if (localTime >= a.time && localTime <= b.time) {
      const span = b.time - a.time;
      if (span <= 0) return b.value;
      return a.value + (b.value - a.value) * ease(a.easing, (localTime - a.time) / span);
    }
  }
  return last.value;
}

/** A transform property's value now: its curve if animated, else its static value. */
export function transformAt(clip: Clip, name: "x" | "y" | "scale" | "rotation" | "opacity",
                            time: number): number {
  const curve = clip.transform.keyframes?.[name];
  const sampled = curve ? sampleCurve(curve, time - clip.start) : null;
  return sampled ?? clip.transform[name];
}

/** 0..1 alpha contribution from the clip's in/out transitions. */
export function transitionAlpha(clip: Clip, time: number): number {
  let alpha = 1;
  const local = time - clip.start;
  const ti = clip.transition_in;
  const to = clip.transition_out;
  if (ti.type !== "none" && ti.duration > 0.01 && local < ti.duration) {
    alpha *= Math.max(0, Math.min(1, local / ti.duration));
  }
  const outStart = clip.duration - to.duration;
  if (to.type !== "none" && to.duration > 0.01 && local > outStart) {
    alpha *= Math.max(0, Math.min(1, (clip.duration - local) / to.duration));
  }
  return alpha;
}

export class Compositor {
  private pool = new Map<string, MediaElement>();
  private offscreen: HTMLCanvasElement | null = null;

  constructor(private getAsset: (id: string) => Asset | undefined) {}

  dispose() {
    this.pool.forEach((el) => {
      if (el instanceof HTMLVideoElement || el instanceof HTMLAudioElement) {
        el.pause();
        el.removeAttribute("src");
        el.load();
      }
    });
    this.pool.clear();
  }

  /** Get (or lazily create) the media element backing an asset. */
  element(assetId: string): MediaElement | null {
    const cached = this.pool.get(assetId);
    if (cached) return cached;

    const asset = this.getAsset(assetId);
    if (!asset) return null;
    const src = asset.proxy_url ?? asset.original_url;
    if (!src) return null;

    let el: MediaElement;
    if (asset.kind === "image") {
      const img = new Image();
      img.crossOrigin = "anonymous";
      img.src = asset.original_url ?? src;
      el = img;
    } else if (asset.kind === "audio") {
      const audio = new Audio();
      audio.src = src;
      audio.preload = "auto";
      el = audio;
    } else {
      const video = document.createElement("video");
      video.src = src;
      video.preload = "auto";
      video.playsInline = true;
      video.muted = false;
      // Nothing is appended to the DOM: these elements exist purely as
      // decode sources for the canvas and as audio outputs.
      el = video;
    }
    this.pool.set(assetId, el);
    return el;
  }

  /** Align every media element with the timeline position. */
  sync(seq: Sequence, time: number, playing: boolean, masterVolume = 1) {
    const active = new Set<string>();

    for (const track of seq.tracks) {
      for (const clip of track.clips) {
        if (!clip.enabled || !clip.asset_id) continue;
        if (clip.kind === "text" || clip.kind === "solid" || clip.kind === "adjustment") continue;
        const inside = time >= clip.start && time < clip.start + clip.duration;
        const el = this.element(clip.asset_id);
        if (!el || el instanceof HTMLImageElement) continue;

        if (!inside) continue;
        active.add(clip.asset_id);

        const sourceTime = clip.in_point + (time - clip.start) * clip.speed;
        const audible =
          !clip.muted && !track.muted && !(clip.kind === "video" && track.hidden);
        el.volume = Math.max(0, Math.min(1, clip.volume * track.volume * masterVolume));
        el.muted = !audible;
        el.playbackRate = Math.max(0.0625, Math.min(16, clip.speed));

        const drift = Math.abs(el.currentTime - sourceTime);
        if (drift > (playing ? 0.28 : 0.02)) {
          try {
            el.currentTime = sourceTime;
          } catch {
            /* seeking before metadata arrives throws; the next frame retries */
          }
        }
        if (playing && el.paused) void el.play().catch(() => undefined);
        if (!playing && !el.paused) el.pause();
      }
    }

    // Anything not under the playhead must go silent immediately.
    this.pool.forEach((el, assetId) => {
      if (active.has(assetId)) return;
      if (el instanceof HTMLVideoElement || el instanceof HTMLAudioElement) {
        if (!el.paused) el.pause();
      }
    });
  }

  /** Draw one frame of the sequence. */
  draw(
    canvas: HTMLCanvasElement,
    seq: Sequence,
    time: number,
  ): CompositorStats {
    const ctx = canvas.getContext("2d");
    const stats: CompositorStats = { drawnClips: 0, waiting: 0, approximated: [] };
    if (!ctx) return stats;

    const W = seq.width;
    const H = seq.height;
    if (canvas.width !== W || canvas.height !== H) {
      canvas.width = W;
      canvas.height = H;
    }

    ctx.save();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.filter = "none";
    ctx.globalAlpha = 1;
    ctx.fillStyle = seq.background || "#000000";
    ctx.fillRect(0, 0, W, H);

    const videoTracks = seq.tracks
      .filter((t) => t.kind === "video" && !t.hidden)
      .sort((a, b) => a.index - b.index);

    for (const track of videoTracks) {
      const clips = [...track.clips].sort((a, b) => a.start - b.start);
      for (const clip of clips) {
        if (!clip.enabled) continue;
        if (time < clip.start || time >= clip.start + clip.duration) continue;

        if (clip.kind === "text") {
          this.drawText(ctx, clip, time, W, H);
          stats.drawnClips += 1;
        } else if (clip.kind === "adjustment" || track.is_adjustment) {
          this.applyAdjustment(canvas, ctx, clip, stats, W, H);
        } else if (clip.kind === "solid") {
          ctx.save();
          ctx.globalAlpha = clip.transform.opacity * transitionAlpha(clip, time);
          ctx.fillStyle = clip.color ?? "#000000";
          ctx.fillRect(0, 0, W, H);
          ctx.restore();
          stats.drawnClips += 1;
        } else {
          const drawn = this.drawMedia(ctx, clip, time, W, H, stats);
          if (drawn) stats.drawnClips += 1;
          else stats.waiting += 1;
          this.drawFrameEffects(ctx, clip, W, H);
        }
      }
    }

    ctx.restore();
    return stats;
  }

  private drawMedia(
    ctx: CanvasRenderingContext2D,
    clip: Clip,
    time: number,
    W: number,
    H: number,
    stats: CompositorStats,
  ): boolean {
    if (!clip.asset_id) return false;
    const el = this.element(clip.asset_id);
    if (!el) return false;

    let sw = 0;
    let sh = 0;
    if (el instanceof HTMLVideoElement) {
      if (el.readyState < 2) return false;
      sw = el.videoWidth;
      sh = el.videoHeight;
    } else if (el instanceof HTMLImageElement) {
      if (!el.complete || !el.naturalWidth) return false;
      sw = el.naturalWidth;
      sh = el.naturalHeight;
    } else {
      return true; // audio-only: nothing to draw, but it is not "waiting"
    }
    if (!sw || !sh) return false;

    const tf = clip.transform;

    // Source crop, in source pixels.
    const cx = sw * tf.crop_left;
    const cy = sh * tf.crop_top;
    const cw = Math.max(1, sw * (1 - tf.crop_left - tf.crop_right));
    const ch = Math.max(1, sh * (1 - tf.crop_top - tf.crop_bottom));

    // Fit into the canvas, then apply the clip's own scale.
    let dw = W;
    let dh = H;
    const srcRatio = cw / ch;
    const canvasRatio = W / H;
    if (tf.fit === "contain") {
      if (srcRatio > canvasRatio) { dw = W; dh = W / srcRatio; }
      else { dh = H; dw = H * srcRatio; }
    } else if (tf.fit === "cover") {
      if (srcRatio > canvasRatio) { dh = H; dw = H * srcRatio; }
      else { dw = W; dh = W / srcRatio; }
    } else if (tf.fit === "none") {
      dw = cw; dh = ch;
    }
    // Animated properties are sampled from their curve; the rest use their
    // static value. `transformAt` mirrors the render's interpolation exactly.
    const scale = transformAt(clip, "scale", time);
    const rotation = transformAt(clip, "rotation", time);
    const offsetX = transformAt(clip, "x", time);
    const offsetY = transformAt(clip, "y", time);
    dw *= scale;
    dh *= scale;

    const alpha = transformAt(clip, "opacity", time) * transitionAlpha(clip, time);
    if (alpha <= 0.001) return true;

    const mirrored = clip.effects.some(
      (f) => f.enabled && f.type === "mirror",
    );

    ctx.save();
    ctx.globalAlpha = Math.max(0, Math.min(1, alpha));
    const filter = filterFor(clip.effects, stats.approximated);
    ctx.filter = filter || "none";
    ctx.translate(W / 2 + offsetX, H / 2 + offsetY);
    if (rotation) ctx.rotate((rotation * Math.PI) / 180);
    if (mirrored) ctx.scale(-1, 1);

    // `cover` overflows the canvas by design; clipping keeps it in frame.
    if (tf.fit === "cover") {
      ctx.beginPath();
      ctx.rect(-W / 2 - offsetX, -H / 2 - offsetY, W, H);
      ctx.clip();
    }
    try {
      ctx.drawImage(el as CanvasImageSource, cx, cy, cw, ch, -dw / 2, -dh / 2, dw, dh);
    } catch {
      ctx.restore();
      return false;
    }
    ctx.restore();
    return true;
  }

  /**
   * Letterbox bars and vignette.
   *
   * The render applies these per clip, before compositing. Drawing them as a
   * full-frame overlay matches that for the normal case of a clip that fills
   * the frame, and it matters that they appear at all: framing decisions get
   * made against this monitor.
   */
  private drawFrameEffects(
    ctx: CanvasRenderingContext2D,
    clip: Clip,
    W: number,
    H: number,
  ) {
    for (const fx of clip.effects) {
      if (!fx.enabled) continue;
      const p = fx.parameters ?? {};

      if (fx.type === "letterbox") {
        const ratio = String(p.aspect_ratio ?? "2.39:1").replace("/", ":");
        const [a, b] = ratio.split(":").map(Number);
        const target = Number.isFinite(a) && Number.isFinite(b) && b ? a / b : 2.39;
        ctx.save();
        ctx.filter = "none";
        ctx.globalAlpha = 1;
        ctx.fillStyle = "#000000";
        if (target > W / H + 1e-3) {
          // Wider than the canvas: bars top and bottom.
          const bar = Math.max(0, Math.round((H - W / target) / 2));
          ctx.fillRect(0, 0, W, bar);
          ctx.fillRect(0, H - bar, W, bar);
        } else {
          // Narrower than the canvas: pillarbox.
          const bar = Math.max(0, Math.round((W - H * target) / 2));
          ctx.fillRect(0, 0, bar, H);
          ctx.fillRect(W - bar, 0, bar, H);
        }
        ctx.restore();
      }

      if (fx.type === "vignette") {
        const intensity = Math.max(0, Math.min(1, num(p.intensity, 0.4)));
        if (intensity <= 0.01) continue;
        const gradient = ctx.createRadialGradient(
          W / 2, H / 2, Math.min(W, H) * 0.28,
          W / 2, H / 2, Math.max(W, H) * 0.72,
        );
        gradient.addColorStop(0, "rgba(0,0,0,0)");
        gradient.addColorStop(1, `rgba(0,0,0,${(intensity * 0.85).toFixed(3)})`);
        ctx.save();
        ctx.filter = "none";
        ctx.globalAlpha = 1;
        ctx.fillStyle = gradient;
        ctx.fillRect(0, 0, W, H);
        ctx.restore();
      }
    }
  }

  private applyAdjustment(
    canvas: HTMLCanvasElement,
    ctx: CanvasRenderingContext2D,
    clip: Clip,
    stats: CompositorStats,
    W: number,
    H: number,
  ) {
    const filter = filterFor(clip.effects, stats.approximated);
    if (!filter) return;
    if (!this.offscreen) this.offscreen = document.createElement("canvas");
    const off = this.offscreen;
    off.width = W;
    off.height = H;
    const offCtx = off.getContext("2d");
    if (!offCtx) return;
    offCtx.clearRect(0, 0, W, H);
    offCtx.drawImage(canvas, 0, 0);

    ctx.save();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.globalAlpha = 1;
    ctx.filter = filter;
    ctx.clearRect(0, 0, W, H);
    ctx.drawImage(off, 0, 0);
    ctx.restore();
  }

  private drawText(
    ctx: CanvasRenderingContext2D,
    clip: Clip,
    time: number,
    W: number,
    H: number,
  ) {
    const content = clip.text;
    if (!content || !content.text.trim()) return;
    const style = content.style;
    const local = time - clip.start;

    // Matches the render's alpha ramp: in over d, hold, out over d.
    const d = Math.min(0.4, clip.duration / 3);
    let alpha = 1;
    if (content.animation !== "none" && d > 0.02) {
      if (local < d) alpha = local / d;
      else if (local > clip.duration - d) alpha = (clip.duration - local) / d;
    }
    alpha = Math.max(0, Math.min(1, alpha)) * clip.transform.opacity;
    if (alpha <= 0.001) return;

    const text = style.uppercase ? content.text.toUpperCase() : content.text;
    const lines = text.split("\n");
    const size = style.font_size;
    const weight = style.bold ? 700 : 400;
    const italic = style.italic ? "italic " : "";

    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.filter = "none";
    ctx.font = `${italic}${weight} ${size}px ${style.font_family}, system-ui, sans-serif`;
    ctx.textAlign = style.align === "left" ? "left" : style.align === "right" ? "right" : "center";
    ctx.textBaseline = "middle";

    const lineHeight = size * style.line_height;
    const blockHeight = lineHeight * lines.length;
    const widest = Math.max(...lines.map((l) => ctx.measureText(l).width));

    const x = (W - widest) * content.anchor_x + (style.align === "center" ? widest / 2
      : style.align === "right" ? widest : 0);
    const top = (H - blockHeight) * content.anchor_y;

    if (style.background) {
      const pad = style.background_padding;
      ctx.fillStyle = style.background;
      ctx.globalAlpha = alpha * 0.75;
      ctx.fillRect(
        (W - widest) * content.anchor_x - pad,
        top - pad,
        widest + pad * 2,
        blockHeight + pad * 2,
      );
      ctx.globalAlpha = alpha;
    }

    lines.forEach((line, i) => {
      const y = top + lineHeight * (i + 0.5);
      if (style.shadow) {
        ctx.fillStyle = "rgba(0,0,0,0.55)";
        ctx.fillText(line, x + 3, y + 3);
      }
      if (style.stroke_color && style.stroke_width > 0) {
        ctx.lineWidth = style.stroke_width * 2;
        ctx.strokeStyle = style.stroke_color;
        ctx.lineJoin = "round";
        ctx.strokeText(line, x, y);
      }
      ctx.fillStyle = style.color;
      ctx.fillText(line, x, y);
    });
    ctx.restore();
  }
}
