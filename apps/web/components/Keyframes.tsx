"use client";

/**
 * Keyframe controls.
 *
 * The diamond is the whole contract: off means the property holds one value,
 * on means it follows a curve. Toggling it on captures the current value at
 * the playhead so nothing jumps, and editing the value while a property is
 * animated writes to the keyframe under the playhead rather than overwriting
 * the curve.
 *
 * Only properties the render can genuinely animate get a diamond. Showing one
 * on a property that exports static would be a lie the preview tells.
 */
import type { Clip, Easing, Keyframe } from "@/lib/types";
import { sampleCurve } from "@/lib/compositor";

export const EASINGS: { value: Easing; label: string }[] = [
  { value: "linear", label: "Linear" },
  { value: "ease_in", label: "Ease in" },
  { value: "ease_out", label: "Ease out" },
  { value: "ease_in_out", label: "Ease in and out" },
  { value: "hold", label: "Hold" },
];

/** Keyframes within this many seconds of the playhead count as "here". */
const NEAR = 0.04;

export function curveOf(clip: Clip, property: string): Keyframe[] | null {
  const curve = clip.transform.keyframes?.[property];
  return curve && curve.length > 0 ? curve : null;
}

export function localTime(clip: Clip, playhead: number): number {
  return Math.max(0, Math.min(clip.duration, playhead - clip.start));
}

export function keyframeAt(curve: Keyframe[], time: number): Keyframe | null {
  return curve.find((k) => Math.abs(k.time - time) <= NEAR) ?? null;
}

export function valueNow(clip: Clip, property: string, playhead: number,
                         fallback: number): number {
  const curve = curveOf(clip, property);
  if (!curve) return fallback;
  return sampleCurve(curve, localTime(clip, playhead)) ?? fallback;
}

/**
 * Write `value` into a curve at `time`, replacing a keyframe already there.
 * Returns a new array so React state updates stay predictable.
 */
export function writeKeyframe(
  curve: Keyframe[],
  time: number,
  value: number,
  easing: Easing = "ease_in_out",
): Keyframe[] {
  const existing = keyframeAt(curve, time);
  const next = curve.filter((k) => k !== existing);
  next.push({
    id: existing?.id ?? `kf_${Math.random().toString(36).slice(2, 12)}`,
    time,
    value,
    easing: existing?.easing ?? easing,
  });
  return next.sort((a, b) => a.time - b.time);
}

export function KeyframeToggle({
  animated,
  hasKeyAtPlayhead,
  onToggleAnimation,
  onToggleKey,
  label,
}: {
  animated: boolean;
  hasKeyAtPlayhead: boolean;
  onToggleAnimation: () => void;
  onToggleKey: () => void;
  label: string;
}) {
  const title = !animated
    ? `Animate ${label} — adds a keyframe at the playhead`
    : hasKeyAtPlayhead
      ? `Remove the ${label} keyframe at the playhead`
      : `Add a ${label} keyframe at the playhead`;

  return (
    <span className="flex items-center gap-0.5">
      <button
        onClick={animated ? onToggleKey : onToggleAnimation}
        title={title}
        aria-label={title}
        aria-pressed={animated}
        className={`grid h-4 w-4 place-items-center transition-colors ${
          animated
            ? hasKeyAtPlayhead
              ? "text-accent"
              : "text-accent/45 hover:text-accent"
            : "text-ink-600 hover:text-ink-300"
        }`}
      >
        <Diamond filled={animated && hasKeyAtPlayhead} />
      </button>
      {animated && (
        <button
          onClick={onToggleAnimation}
          title={`Stop animating ${label} and hold one value`}
          aria-label={`Stop animating ${label}`}
          className="text-2xs leading-none text-ink-600 hover:text-danger"
        >
          ✕
        </button>
      )}
    </span>
  );
}

function Diamond({ filled }: { filled: boolean }) {
  return (
    <svg width="9" height="9" viewBox="0 0 10 10" aria-hidden>
      <path
        d="M5 0.6 9.4 5 5 9.4 0.6 5Z"
        fill={filled ? "currentColor" : "none"}
        stroke="currentColor"
        strokeWidth="1.3"
      />
    </svg>
  );
}

/**
 * A miniature curve strip: every keyframe on the property, positioned across
 * the clip, with the playhead marked. Clicking a diamond jumps to it.
 */
export function KeyframeStrip({
  curve,
  clip,
  playhead,
  onSeek,
  onRemove,
  onEasing,
}: {
  curve: Keyframe[];
  clip: Clip;
  playhead: number;
  onSeek: (time: number) => void;
  onRemove: (keyframe: Keyframe) => void;
  onEasing: (keyframe: Keyframe, easing: Easing) => void;
}) {
  const local = localTime(clip, playhead);
  const inside = playhead >= clip.start && playhead <= clip.start + clip.duration;
  const at = (time: number) =>
    `${Math.max(0, Math.min(100, (time / Math.max(clip.duration, 0.001)) * 100))}%`;

  return (
    <div className="relative mt-1 h-5 rounded-sm bg-ink-900">
      <div className="absolute inset-x-1 top-1/2 h-px -translate-y-1/2 bg-ink-700" />
      {inside && (
        <div
          className="absolute inset-y-0 w-px bg-accent/60"
          style={{ left: at(local) }}
        />
      )}
      {curve.map((keyframe) => (
        <button
          key={keyframe.id}
          onClick={() => onSeek(clip.start + keyframe.time)}
          onContextMenu={(e) => {
            e.preventDefault();
            onRemove(keyframe);
          }}
          onDoubleClick={() => {
            const order = EASINGS.map((e) => e.value);
            const next = order[(order.indexOf(keyframe.easing) + 1) % order.length];
            onEasing(keyframe, next);
          }}
          title={
            `${keyframe.time.toFixed(2)}s · ${Math.round(keyframe.value * 1000) / 1000}` +
            ` · ${keyframe.easing.replace(/_/g, " ")}\n` +
            "Click to jump · double-click to change easing · right-click to remove"
          }
          className="absolute top-1/2 grid h-4 w-4 -translate-x-1/2 -translate-y-1/2 place-items-center text-accent hover:text-accent-soft"
          style={{ left: at(keyframe.time) }}
        >
          <Diamond filled />
        </button>
      ))}
    </div>
  );
}
