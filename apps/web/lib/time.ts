/** Timecode and timeline-ruler helpers. */

export function formatTimecode(seconds: number, fps = 30, frames = true): string {
  const safe = Math.max(0, seconds);
  const whole = Math.floor(safe);
  const hh = Math.floor(whole / 3600);
  const mm = Math.floor((whole % 3600) / 60);
  const ss = whole % 60;
  const ff = Math.floor((safe - whole) * fps);
  const pad = (n: number, w = 2) => String(n).padStart(w, "0");
  const base = hh > 0 ? `${pad(hh)}:${pad(mm)}:${pad(ss)}` : `${pad(mm)}:${pad(ss)}`;
  return frames ? `${base}:${pad(ff)}` : base;
}

export function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "0:00";
  const mm = Math.floor(seconds / 60);
  const ss = Math.floor(seconds % 60);
  return `${mm}:${String(ss).padStart(2, "0")}`;
}

export function formatBytes(bytes: number): string {
  if (!bytes) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value.toFixed(value >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

export function snapToFrame(seconds: number, fps: number): number {
  if (!fps) return seconds;
  return Math.round(seconds * fps) / fps;
}

/**
 * Pick a ruler interval that keeps ticks legible at the current zoom.
 * Steps are the ones an editor expects to see, not arbitrary round numbers.
 */
const STEPS = [
  1 / 30, 1 / 15, 0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800,
];

export function rulerStep(pixelsPerSecond: number, minPixels = 78): number {
  for (const step of STEPS) {
    if (step * pixelsPerSecond >= minPixels) return step;
  }
  return STEPS[STEPS.length - 1];
}
