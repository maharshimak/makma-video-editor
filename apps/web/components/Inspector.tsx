"use client";

/**
 * The Inspector.
 *
 * Every creative action has two routes: say it to the director, or set it by
 * hand here. The AI must make professional controls easier to reach, never
 * replace them -- so anything the director can do, this panel can too.
 */
import { useEffect, useMemo, useState } from "react";

import { api } from "@/lib/api";
import type { AnimatableTransform, Clip, Easing, EffectSpec, Keyframe, TransitionType } from "@/lib/types";
import {
  KeyframeStrip,
  KeyframeToggle,
  curveOf,
  keyframeAt,
  localTime,
  valueNow,
  writeKeyframe,
} from "@/components/Keyframes";
import {
  getPlayhead,
  setPlayhead,
  subscribePlayhead,
  useEditor,
  withSequence,
  findClip,
} from "@/store/editor";

const TRANSITIONS: TransitionType[] = [
  "none", "dissolve", "fade", "slide_left", "slide_right",
  "slide_up", "slide_down", "wipe_left", "wipe_right",
];

const FITS = ["contain", "cover", "stretch", "none"] as const;

export function Inspector() {
  const selection = useEditor((s) => s.selection);
  const document_ = useEditor((s) => s.document);
  const applyOps = useEditor((s) => s.applyOps);
  const patchDocument = useEditor((s) => s.patchDocument);
  const commitDocument = useEditor((s) => s.commitDocument);

  const [specs, setSpecs] = useState<EffectSpec[]>([]);
  const [gradePresets, setGradePresets] = useState<string[]>([]);
  const [addingEffect, setAddingEffect] = useState(false);

  useEffect(() => {
    api
      .effects()
      .then((r) => {
        setSpecs(r.effects);
        setGradePresets(r.grade_presets);
      })
      .catch(() => undefined);
  }, []);

  const clip = useMemo<Clip | null>(() => {
    if (selection.length !== 1 || !document_) return null;
    const seq =
      document_.sequences.find((s) => s.id === document_.active_sequence_id) ??
      document_.sequences[0];
    return seq ? findClip(seq, selection[0])?.clip ?? null : null;
  }, [selection, document_]);

  /** Live-edit a clip field locally, committing one version per gesture. */
  const patchClip = (mutate: (c: Clip) => void, commit = false, label = "Adjust clip") => {
    const doc = useEditor.getState().document;
    if (!doc || !clip) return;
    const next = withSequence(doc, (seq) => {
      const hit = findClip(seq, clip.id);
      if (hit) mutate(hit.clip);
    });
    patchDocument(next);
    if (commit) void commitDocument(label);
  };

  if (selection.length === 0) {
    return (
      <PanelShell title="Inspector">
        <p className="px-3 py-6 text-center text-xs text-ink-400">
          Select a clip to edit its properties, or ask the director.
        </p>
      </PanelShell>
    );
  }

  if (selection.length > 1) {
    return (
      <PanelShell title="Inspector">
        <div className="p-3">
          <p className="mb-2 text-xs text-ink-300">
            {selection.length} clips selected.
          </p>
          <div className="grid grid-cols-2 gap-1.5">
            <button
              className="btn"
              onClick={() =>
                void applyOps(
                  [{
                    type: "set_transition",
                    description: "Cross-dissolve the selection",
                    scope: { type: "clip", clip_ids: selection },
                    parameters: { transition: "dissolve", duration: 0.4, where: "both" },
                  }],
                  "Dissolve",
                )
              }
            >
              Dissolve all
            </button>
            <button
              className="btn"
              onClick={() =>
                void applyOps(
                  [{
                    type: "add_effect",
                    description: "Grade the selection",
                    scope: { type: "clip", clip_ids: selection },
                    parameters: { type: "color_grade", parameters: { preset: "cinematic_dark" } },
                  }],
                  "Grade",
                )
              }
            >
              Cinematic grade
            </button>
          </div>
        </div>
      </PanelShell>
    );
  }

  if (!clip) return <PanelShell title="Inspector"><div className="p-3" /></PanelShell>;

  return (
    <PanelShell title="Inspector" subtitle={clip.kind}>
      <div className="space-y-4 p-3">
        <Section title={clip.name || clip.kind}>
          <Row label="Start">
            <NumberField
              value={clip.start}
              step={0.05}
              onChange={(v) => patchClip((c) => { c.start = Math.max(0, v); })}
              onCommit={() => void commitDocument("Set clip start")}
              suffix="s"
            />
          </Row>
          <Row label="Duration">
            <NumberField
              value={clip.duration}
              step={0.05}
              min={0.02}
              onChange={(v) => patchClip((c) => { c.duration = Math.max(0.02, v); })}
              onCommit={() => void commitDocument("Set clip duration")}
              suffix="s"
            />
          </Row>
          {clip.asset_id && (
            <Row label="Speed">
              <NumberField
                value={clip.speed}
                step={0.05}
                min={0.05}
                max={20}
                onChange={(v) =>
                  void applyOps(
                    [{
                      type: "set_speed",
                      description: `Set speed to ${v.toFixed(2)}×`,
                      scope: { type: "clip", clip_ids: [clip.id] },
                      parameters: { speed: v },
                    }],
                    "Set speed",
                  )
                }
                suffix="×"
              />
            </Row>
          )}
          <Row label="Enabled">
            <Toggle
              value={clip.enabled}
              onChange={(v) => patchClip((c) => { c.enabled = v; }, true, "Toggle clip")}
            />
          </Row>
        </Section>

        {clip.kind !== "text" && clip.kind !== "adjustment" && (
          <Section title="Transform">
            <Row label="Fit">
              <select
                className="field"
                value={clip.transform.fit}
                onChange={(e) =>
                  patchClip(
                    (c) => { c.transform.fit = e.target.value as Clip["transform"]["fit"]; },
                    true,
                    "Set fit",
                  )
                }
              >
                {FITS.map((f) => <option key={f} value={f}>{f}</option>)}
              </select>
            </Row>
            <Slider
              label="Scale"
              value={clip.transform.scale}
              min={0.05}
              max={4}
              step={0.01}
              onChange={(v) => patchClip((c) => { c.transform.scale = v; })}
              onCommit={() => void commitDocument("Set scale")}
            />
            <AnimatableSlider
              clip={clip}
              property="opacity"
              label="Opacity"
              min={0}
              max={1}
              step={0.01}
              patchClip={patchClip}
              commit={commitDocument}
            />
            <Slider
              label="Rotation"
              value={clip.transform.rotation}
              min={-180}
              max={180}
              step={1}
              suffix="°"
              onChange={(v) => patchClip((c) => { c.transform.rotation = v; })}
              onCommit={() => void commitDocument("Set rotation")}
            />
            <AnimatableSlider
              clip={clip}
              property="x"
              label="Position X"
              min={-clip.transform.scale * 2000}
              max={clip.transform.scale * 2000}
              step={1}
              suffix="px"
              patchClip={patchClip}
              commit={commitDocument}
            />
            <AnimatableSlider
              clip={clip}
              property="y"
              label="Position Y"
              min={-2000}
              max={2000}
              step={1}
              suffix="px"
              patchClip={patchClip}
              commit={commitDocument}
            />
          </Section>
        )}

        {(clip.kind === "video" || clip.kind === "audio") && (
          <Section title="Audio">
            <Slider
              label="Volume"
              value={clip.volume}
              min={0}
              max={2}
              step={0.01}
              onChange={(v) => patchClip((c) => { c.volume = v; })}
              onCommit={() => void commitDocument("Set volume")}
            />
            <Row label="Muted">
              <Toggle
                value={clip.muted}
                onChange={(v) => patchClip((c) => { c.muted = v; }, true, "Mute clip")}
              />
            </Row>
          </Section>
        )}

        {clip.kind === "text" && clip.text && (
          <Section title="Text">
            <textarea
              rows={2}
              className="w-full resize-none rounded border border-ink-700 bg-ink-850 px-2 py-1.5 text-xs text-ink-100"
              value={clip.text.text}
              onChange={(e) => patchClip((c) => { if (c.text) c.text.text = e.target.value; })}
              onBlur={() => void commitDocument("Edit text")}
            />
            <Row label="Size">
              <NumberField
                value={clip.text.style.font_size}
                step={2}
                min={4}
                onChange={(v) => patchClip((c) => { if (c.text) c.text.style.font_size = v; })}
                onCommit={() => void commitDocument("Set text size")}
                suffix="px"
              />
            </Row>
            <Row label="Colour">
              <input
                type="color"
                className="h-7 w-full cursor-pointer rounded border border-ink-700 bg-ink-850"
                value={clip.text.style.color}
                onChange={(e) => patchClip((c) => { if (c.text) c.text.style.color = e.target.value; })}
                onBlur={() => void commitDocument("Set text colour")}
              />
            </Row>
            <Row label="Uppercase">
              <Toggle
                value={clip.text.style.uppercase}
                onChange={(v) =>
                  patchClip((c) => { if (c.text) c.text.style.uppercase = v; }, true, "Text case")
                }
              />
            </Row>
            <Slider
              label="Vertical"
              value={clip.text.anchor_y}
              min={0}
              max={1}
              step={0.01}
              onChange={(v) => patchClip((c) => { if (c.text) c.text.anchor_y = v; })}
              onCommit={() => void commitDocument("Move text")}
            />
          </Section>
        )}

        <Section title="Transitions">
          <Row label="In">
            <select
              className="field"
              value={clip.transition_in.type}
              onChange={(e) =>
                void applyOps(
                  [{
                    type: "set_transition",
                    description: `Set the in transition to ${e.target.value}`,
                    scope: { type: "clip", clip_ids: [clip.id] },
                    parameters: {
                      transition: e.target.value,
                      duration: clip.transition_in.duration || 0.5,
                      where: "in",
                    },
                  }],
                  "Transition in",
                )
              }
            >
              {TRANSITIONS.map((t) => (
                <option key={t} value={t}>{t.replace(/_/g, " ")}</option>
              ))}
            </select>
          </Row>
          <Row label="Out">
            <select
              className="field"
              value={clip.transition_out.type}
              onChange={(e) =>
                void applyOps(
                  [{
                    type: "set_transition",
                    description: `Set the out transition to ${e.target.value}`,
                    scope: { type: "clip", clip_ids: [clip.id] },
                    parameters: {
                      transition: e.target.value,
                      duration: clip.transition_out.duration || 0.5,
                      where: "out",
                    },
                  }],
                  "Transition out",
                )
              }
            >
              {TRANSITIONS.map((t) => (
                <option key={t} value={t}>{t.replace(/_/g, " ")}</option>
              ))}
            </select>
          </Row>
        </Section>

        <Section
          title="Effects"
          action={
            <button
              className="btn btn-ghost h-5 px-1.5 text-2xs"
              onClick={() => setAddingEffect((v) => !v)}
            >
              {addingEffect ? "Close" : "+ Add"}
            </button>
          }
        >
          {addingEffect && (
            <div className="mb-2 max-h-52 overflow-y-auto rounded border border-ink-700 bg-ink-850 p-1.5">
              {Object.entries(
                specs.reduce<Record<string, EffectSpec[]>>((acc, s) => {
                  (acc[s.category] ??= []).push(s);
                  return acc;
                }, {}),
              ).map(([category, items]) => (
                <div key={category} className="mb-1.5">
                  <div className="label mb-1 px-1">{category}</div>
                  <div className="flex flex-wrap gap-1">
                    {items.map((spec) => (
                      <button
                        key={spec.type}
                        className="chip transition-colors hover:border-accent/50 hover:text-ink-100"
                        onClick={() => {
                          setAddingEffect(false);
                          void applyOps(
                            [{
                              type: "add_effect",
                              description: `Add ${spec.label}`,
                              scope: { type: "clip", clip_ids: [clip.id] },
                              parameters: { type: spec.type, parameters: {} },
                            }],
                            `Add ${spec.label}`,
                          );
                        }}
                      >
                        {spec.label}
                      </button>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}

          {clip.effects.length === 0 ? (
            <p className="text-2xs text-ink-500">No effects on this clip.</p>
          ) : (
            <div className="space-y-1.5">
              {clip.effects.map((fx) => (
                <div key={fx.id} className="rounded border border-ink-700 bg-ink-850 p-1.5">
                  <div className="flex items-center gap-1.5">
                    <button
                      className={`h-3 w-3 shrink-0 rounded-sm border ${
                        fx.enabled ? "border-accent bg-accent" : "border-ink-500"
                      }`}
                      onClick={() =>
                        patchClip(
                          (c) => {
                            const target = c.effects.find((f) => f.id === fx.id);
                            if (target) target.enabled = !target.enabled;
                          },
                          true,
                          "Toggle effect",
                        )
                      }
                      title={fx.enabled ? "Disable" : "Enable"}
                    />
                    <span className="flex-1 truncate text-xs text-ink-200">
                      {specs.find((s) => s.type === fx.type)?.label ?? fx.type}
                    </span>
                    <button
                      className="text-2xs text-ink-500 hover:text-danger"
                      onClick={() =>
                        void applyOps(
                          [{
                            type: "remove_effect",
                            description: `Remove ${fx.type}`,
                            scope: { type: "clip", clip_ids: [clip.id] },
                            parameters: { effect_id: fx.id },
                          }],
                          "Remove effect",
                        )
                      }
                    >
                      ✕
                    </button>
                  </div>

                  {fx.type === "color_grade" && (
                    <select
                      className="field mt-1.5"
                      value={String(fx.parameters.preset ?? "cinematic_dark")}
                      onChange={(e) =>
                        patchClip(
                          (c) => {
                            const target = c.effects.find((f) => f.id === fx.id);
                            if (target) target.parameters.preset = e.target.value;
                          },
                          true,
                          "Change grade",
                        )
                      }
                    >
                      {gradePresets.map((p) => (
                        <option key={p} value={p}>{p.replace(/_/g, " ")}</option>
                      ))}
                    </select>
                  )}

                  {Object.entries(
                    specs.find((s) => s.type === fx.type)?.defaults ?? {},
                  )
                    .filter(([key, def]) => typeof def === "number" && key !== "preset")
                    .map(([key, def]) => (
                      <Slider
                        key={key}
                        label={key.replace(/_/g, " ")}
                        value={Number(fx.parameters[key] ?? def)}
                        min={0}
                        max={Number(def) > 4 ? Number(def) * 4 : 2}
                        step={0.01}
                        onChange={(v) =>
                          patchClip((c) => {
                            const target = c.effects.find((f) => f.id === fx.id);
                            if (target) target.parameters[key] = v;
                          })
                        }
                        onCommit={() => void commitDocument("Adjust effect")}
                      />
                    ))}
                </div>
              ))}
            </div>
          )}
        </Section>
      </div>
    </PanelShell>
  );
}

// --- primitives ------------------------------------------------------------

function PanelShell({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex h-full flex-col bg-ink-900">
      <div className="panel-header">
        <span>{title}</span>
        {subtitle && <span className="chip normal-case tracking-normal">{subtitle}</span>}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
    </div>
  );
}

function Section({
  title,
  action,
  children,
}: {
  title: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div>
      <div className="mb-1.5 flex items-center justify-between">
        <span className="label truncate">{title}</span>
        {action}
      </div>
      <div className="space-y-1.5">{children}</div>
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex items-center gap-2">
      <span className="w-16 shrink-0 text-2xs text-ink-400">{label}</span>
      <span className="min-w-0 flex-1">{children}</span>
    </label>
  );
}

function NumberField({
  value,
  onChange,
  onCommit,
  step = 1,
  min,
  max,
  suffix,
}: {
  value: number;
  onChange: (v: number) => void;
  onCommit?: () => void;
  step?: number;
  min?: number;
  max?: number;
  suffix?: string;
}) {
  const [text, setText] = useState(String(round(value)));
  useEffect(() => setText(String(round(value))), [value]);

  return (
    <span className="relative block">
      <input
        className="field pr-6 font-mono tabular-nums"
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          const parsed = Number(e.target.value);
          if (Number.isFinite(parsed)) onChange(clamp(parsed, min, max));
        }}
        onBlur={onCommit}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            (e.target as HTMLInputElement).blur();
          } else if (e.key === "ArrowUp" || e.key === "ArrowDown") {
            e.preventDefault();
            const delta = (e.key === "ArrowUp" ? 1 : -1) * step * (e.shiftKey ? 10 : 1);
            const next = clamp(value + delta, min, max);
            onChange(next);
            setText(String(round(next)));
          }
        }}
      />
      {suffix && (
        <span className="pointer-events-none absolute right-1.5 top-1/2 -translate-y-1/2 text-2xs text-ink-500">
          {suffix}
        </span>
      )}
    </span>
  );
}

/**
 * A slider whose property can be animated.
 *
 * When the property is static this behaves like any other slider. Once
 * animated, the readout follows the curve at the playhead and dragging writes
 * into the keyframe under the playhead -- so adjusting a value never silently
 * flattens an animation the user built.
 */
function AnimatableSlider({
  clip,
  property,
  label,
  min,
  max,
  step,
  suffix,
  patchClip,
  commit,
}: {
  clip: Clip;
  property: AnimatableTransform;
  label: string;
  min: number;
  max: number;
  step: number;
  suffix?: string;
  patchClip: (mutate: (c: Clip) => void, commit?: boolean, label?: string) => void;
  commit: (label: string) => Promise<void>;
}) {
  const [playhead, setPlayheadState] = useState(0);
  useEffect(() => subscribePlayhead(setPlayheadState), []);

  const curve = curveOf(clip, property);
  const animated = curve !== null;
  const local = localTime(clip, playhead);
  const current = valueNow(clip, property, playhead, clip.transform[property]);
  const hasKeyHere = animated ? keyframeAt(curve, local) !== null : false;

  const write = (value: number, commitNow: boolean) => {
    patchClip((c) => {
      if (!curveOf(c, property)) {
        c.transform[property] = value;
        return;
      }
      c.transform.keyframes[property] = writeKeyframe(
        c.transform.keyframes[property], local, value,
      );
    }, commitNow, `Set ${label.toLowerCase()}`);
  };

  const toggleAnimation = () => {
    patchClip((c) => {
      if (curveOf(c, property)) {
        // Turning animation off holds the value that was on screen.
        c.transform[property] = current;
        delete c.transform.keyframes[property];
      } else {
        c.transform.keyframes = {
          ...c.transform.keyframes,
          [property]: writeKeyframe([], local, c.transform[property]),
        };
      }
    }, true, animated ? `Stop animating ${label}` : `Animate ${label}`);
  };

  const toggleKeyHere = () => {
    patchClip((c) => {
      const existing = c.transform.keyframes[property] ?? [];
      const here = keyframeAt(existing, local);
      c.transform.keyframes[property] = here
        ? existing.filter((k) => k.id !== here.id)
        : writeKeyframe(existing, local, current);
      // Removing the last keyframe ends the animation rather than leaving an
      // empty curve behind.
      if (c.transform.keyframes[property].length === 0) {
        c.transform[property] = current;
        delete c.transform.keyframes[property];
      }
    }, true, hasKeyHere ? `Remove ${label} keyframe` : `Add ${label} keyframe`);
  };

  return (
    <div>
      <div className="mb-0.5 flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5">
          <KeyframeToggle
            animated={animated}
            hasKeyAtPlayhead={hasKeyHere}
            onToggleAnimation={toggleAnimation}
            onToggleKey={toggleKeyHere}
            label={label}
          />
          <span className="text-2xs text-ink-400">{label}</span>
        </span>
        <span
          className={`font-mono text-2xs tabular-nums ${
            animated ? "text-accent" : "text-ink-300"
          }`}
        >
          {Math.round(current * 1000) / 1000}
          {suffix ?? ""}
        </span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={current}
        onChange={(e) => write(Number(e.target.value), false)}
        onPointerUp={() => void commit(`Set ${label.toLowerCase()}`)}
        className={`h-1 w-full ${animated ? "accent-accent" : "accent-ink-400"}`}
      />
      {animated && curve && (
        <KeyframeStrip
          curve={curve}
          clip={clip}
          playhead={playhead}
          onSeek={setPlayhead}
          onRemove={(keyframe) =>
            patchClip((c) => {
              const rest = (c.transform.keyframes[property] ?? [])
                .filter((k) => k.id !== keyframe.id);
              if (rest.length === 0) {
                c.transform[property] = current;
                delete c.transform.keyframes[property];
              } else {
                c.transform.keyframes[property] = rest;
              }
            }, true, `Remove ${label} keyframe`)
          }
          onEasing={(keyframe, easing) =>
            patchClip((c) => {
              const target = (c.transform.keyframes[property] ?? [])
                .find((k) => k.id === keyframe.id);
              if (target) target.easing = easing;
            }, true, `Set ${label} easing`)
          }
        />
      )}
    </div>
  );
}


function Slider({
  label,
  value,
  min,
  max,
  step,
  suffix,
  onChange,
  onCommit,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  suffix?: string;
  onChange: (v: number) => void;
  onCommit?: () => void;
}) {
  return (
    <div>
      <div className="mb-0.5 flex items-center justify-between">
        <span className="text-2xs capitalize text-ink-400">{label}</span>
        <span className="font-mono text-2xs tabular-nums text-ink-300">
          {round(value)}{suffix ?? ""}
        </span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        onPointerUp={onCommit}
        className="h-1 w-full accent-accent"
      />
    </div>
  );
}

function Toggle({ value, onChange }: { value: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      onClick={() => onChange(!value)}
      className={`h-4 w-7 rounded-full border transition-colors ${
        value ? "border-accent bg-accent/80" : "border-ink-600 bg-ink-750"
      }`}
      role="switch"
      aria-checked={value}
    >
      <span
        className={`block h-3 w-3 rounded-full bg-ink-950 transition-transform ${
          value ? "translate-x-3.5" : "translate-x-0.5"
        }`}
      />
    </button>
  );
}

function round(v: number) {
  return Math.round(v * 1000) / 1000;
}

function clamp(v: number, min?: number, max?: number) {
  if (min !== undefined) v = Math.max(min, v);
  if (max !== undefined) v = Math.min(max, v);
  return v;
}
