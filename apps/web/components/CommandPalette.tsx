"use client";

/**
 * The command palette (Cmd/Ctrl+K).
 *
 * One surface for both halves of the product: named actions and effects for
 * when you know exactly what you want, and a direct line to the director for
 * when you would rather describe it. Anything typed that does not match a
 * command is sent to the director as an instruction, so the palette is never
 * a dead end.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api } from "@/lib/api";
import type { Asset, EffectSpec } from "@/lib/types";
import { getPlayhead, isPlaying, setPlaying, useEditor } from "@/store/editor";

interface Command {
  id: string;
  label: string;
  hint?: string;
  group: string;
  shortcut?: string;
  /** Returned string is shown as confirmation; void means silent success. */
  run: () => void | Promise<void>;
  disabled?: boolean;
}

/** Subsequence match, the behaviour people expect from a palette. */
function score(query: string, text: string): number {
  if (!query) return 1;
  const q = query.toLowerCase();
  const t = text.toLowerCase();

  const exact = t.indexOf(q);
  if (exact === 0) return 1000;
  if (exact > 0) return 700 - exact;

  let qi = 0;
  let hits = 0;
  let streak = 0;
  let best = 0;
  for (let ti = 0; ti < t.length && qi < q.length; ti += 1) {
    if (t[ti] === q[qi]) {
      qi += 1;
      hits += 1;
      streak += 1;
      best = Math.max(best, streak);
    } else {
      streak = 0;
    }
  }
  return qi === q.length ? 100 + hits * 4 + best * 6 : 0;
}

export function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [busy, setBusy] = useState(false);
  const [specs, setSpecs] = useState<EffectSpec[]>([]);
  const [gradePresets, setGradePresets] = useState<string[]>([]);
  const listRef = useRef<HTMLDivElement>(null);

  const projectId = useEditor((s) => s.projectId);
  const assets = useEditor((s) => s.assets);
  const selection = useEditor((s) => s.selection);
  const applyOps = useEditor((s) => s.applyOps);
  const toast = useEditor((s) => s.toast);
  const patchDocument = useEditor((s) => s.patchDocument);

  useEffect(() => {
    api
      .effects()
      .then((r) => {
        setSpecs(r.effects);
        setGradePresets(r.grade_presets);
      })
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((v) => !v);
        setQuery("");
        setActive(0);
      } else if (e.key === "Escape") {
        setOpen(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const scoped = selection.length
    ? { type: "clip" as const, clip_ids: selection }
    : { type: "all" as const };
  const scopeLabel = selection.length
    ? `${selection.length} selected clip${selection.length === 1 ? "" : "s"}`
    : "the whole sequence";

  const ask = useCallback(
    async (message: string) => {
      if (!projectId) return;
      setBusy(true);
      try {
        const response = await api.chat(projectId, {
          message,
          selection,
          playhead: getPlayhead(),
          sequence_id: useEditor.getState().document?.active_sequence_id ?? null,
        });
        if (response.applied && response.document) {
          patchDocument(response.document);
          useEditor.setState({ canUndo: true, canRedo: false });
          toast({ kind: "success", title: response.plan.summary || "Edit applied" });
        } else {
          toast({
            kind: "info",
            title: "Sent to the director",
            detail: "Review the plan in the AI Director panel.",
          });
        }
      } catch (err) {
        toast({
          kind: "error",
          title: "The director could not answer",
          detail: err instanceof Error ? err.message : undefined,
        });
      } finally {
        setBusy(false);
      }
    },
    [projectId, selection, patchDocument, toast],
  );

  const commands = useMemo<Command[]>(() => {
    const out: Command[] = [];

    // --- editing ---
    out.push(
      {
        id: "split",
        label: "Split at playhead",
        group: "Edit",
        shortcut: "S",
        run: () =>
          void applyOps(
            [{
              type: "split_clip",
              description: "Split at the playhead",
              scope: selection.length ? scoped : { type: "all" },
              parameters: { time: getPlayhead() },
            }],
            "Split",
          ),
      },
      {
        id: "delete",
        label: "Delete selected clips",
        group: "Edit",
        shortcut: "Del",
        disabled: selection.length === 0,
        run: () =>
          void applyOps(
            [{
              type: "delete_clip",
              description: `Delete ${selection.length} clip(s)`,
              scope: scoped,
              parameters: {},
            }],
            "Delete clip",
          ),
      },
      {
        id: "undo",
        label: "Undo",
        group: "Edit",
        shortcut: "Ctrl+Z",
        run: () => void useEditor.getState().undo(),
      },
      {
        id: "redo",
        label: "Redo",
        group: "Edit",
        shortcut: "Ctrl+Shift+Z",
        run: () => void useEditor.getState().redo(),
      },
    );

    // --- looks ---
    for (const preset of gradePresets) {
      out.push({
        id: `grade-${preset}`,
        label: `Grade: ${preset.replace(/_/g, " ")}`,
        hint: `Apply to ${scopeLabel}`,
        group: "Look",
        run: () =>
          void applyOps(
            [{
              type: "add_effect",
              description: `Apply the ${preset.replace(/_/g, " ")} grade`,
              scope: { ...scoped, kinds: ["video", "image"] },
              parameters: { type: "color_grade", parameters: { preset } },
            }],
            "Grade",
          ),
      });
    }

    for (const spec of specs) {
      out.push({
        id: `fx-${spec.type}`,
        label: `Add effect: ${spec.label}`,
        hint: `${spec.category} · ${scopeLabel}`,
        group: "Effects",
        run: () =>
          void applyOps(
            [{
              type: "add_effect",
              description: `Add ${spec.label}`,
              scope: scoped,
              parameters: { type: spec.type, parameters: {} },
            }],
            `Add ${spec.label}`,
          ),
      });
    }

    // --- media ---
    for (const asset of assets.filter((a: Asset) => a.status === "ready")) {
      out.push({
        id: `media-${asset.id}`,
        label: `Add to timeline: ${asset.name}`,
        hint: asset.kind,
        group: "Media",
        run: () =>
          void applyOps(
            [{
              type: "add_clip",
              description: `Add ${asset.name}`,
              scope: { type: "sequence" },
              parameters: {
                asset_id: asset.id,
                start: "append",
                duration: asset.kind === "image" ? 5 : asset.duration,
              },
            }],
            `Add ${asset.name}`,
          ),
      });
    }

    // --- format + render ---
    for (const [label, ratio] of [
      ["16:9 horizontal", "16:9"], ["9:16 vertical", "9:16"],
      ["1:1 square", "1:1"], ["2.39:1 scope", "2.39:1"],
    ] as const) {
      out.push({
        id: `format-${ratio}`,
        label: `Reframe to ${label}`,
        group: "Format",
        run: () =>
          void applyOps(
            [
              {
                type: "set_format",
                description: `Set the canvas to ${ratio}`,
                scope: { type: "sequence" },
                parameters: { aspect_ratio: ratio },
              },
              {
                type: "set_transform",
                description: "Fill the new frame",
                scope: { type: "all", kinds: ["video", "image"] },
                parameters: { fit: "cover" },
              },
            ],
            `Reframe ${ratio}`,
          ),
      });
    }

    out.push(
      {
        id: "render-draft",
        label: "Render a quick draft",
        hint: "540p from the proxies",
        group: "Render",
        run: async () => {
          if (!projectId) return;
          await api.render(projectId, { preset: "draft_540p" });
          toast({ kind: "working", title: "Draft render queued" });
        },
      },
      {
        id: "export-master",
        label: "Export master 1080p",
        group: "Render",
        shortcut: "Ctrl+E",
        run: async () => {
          if (!projectId) return;
          await api.export(projectId, { preset: "master_1080p" });
          toast({ kind: "working", title: "Export queued" });
        },
      },
      {
        id: "play",
        label: "Play / pause",
        group: "Playback",
        shortcut: "Space",
        run: () => setPlaying(!isPlaying()),
      },
    );

    return out;
  }, [applyOps, assets, gradePresets, specs, selection, projectId, toast, scopeLabel]);

  const results = useMemo(() => {
    const trimmed = query.trim();
    const ranked = commands
      .map((c) => ({ c, s: score(trimmed, `${c.label} ${c.hint ?? ""}`) }))
      .filter((r) => r.s > 0)
      .sort((a, b) => b.s - a.s)
      .slice(0, 40)
      .map((r) => r.c);

    // A query that matches nothing is not a dead end -- it is an instruction.
    if (trimmed.length > 2) {
      ranked.push({
        id: "ask",
        label: `Ask the director: “${trimmed}”`,
        hint: `Scoped to ${scopeLabel}`,
        group: "AI Director",
        run: () => ask(trimmed),
      });
    }
    return ranked;
  }, [commands, query, ask, scopeLabel]);

  useEffect(() => setActive(0), [query]);

  useEffect(() => {
    const el = listRef.current?.querySelector(`[data-index="${active}"]`);
    el?.scrollIntoView({ block: "nearest" });
  }, [active]);

  if (!open) return null;

  const runActive = async () => {
    const command = results[active];
    if (!command || command.disabled) return;
    setOpen(false);
    await command.run();
  };

  let lastGroup = "";

  return (
    <div
      className="fixed inset-0 z-[70] flex items-start justify-center bg-black/60 pt-[14vh]"
      onClick={() => setOpen(false)}
    >
      <div
        className="panel animate-in flex max-h-[62vh] w-[600px] flex-col overflow-hidden shadow-pop"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 border-b border-ink-750 px-3">
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden
               className="shrink-0 text-ink-400">
            <circle cx="7" cy="7" r="4.5" stroke="currentColor" strokeWidth="1.5" />
            <path d="M10.5 10.5 14 14" stroke="currentColor" strokeWidth="1.5"
                  strokeLinecap="round" />
          </svg>
          <input
            autoFocus
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "ArrowDown") {
                e.preventDefault();
                setActive((i) => Math.min(results.length - 1, i + 1));
              } else if (e.key === "ArrowUp") {
                e.preventDefault();
                setActive((i) => Math.max(0, i - 1));
              } else if (e.key === "Enter") {
                e.preventDefault();
                void runActive();
              }
            }}
            placeholder="Search commands, or describe the edit you want…"
            className="h-11 flex-1 bg-transparent text-sm text-ink-100 placeholder:text-ink-500 focus:outline-none"
          />
          {busy && <span className="chip animate-pulse-soft">thinking</span>}
          <kbd className="chip font-mono">esc</kbd>
        </div>

        <div ref={listRef} className="min-h-0 flex-1 overflow-y-auto py-1">
          {results.length === 0 && (
            <p className="px-3 py-6 text-center text-xs text-ink-500">
              Nothing matches. Keep typing to send it to the director.
            </p>
          )}
          {results.map((command, i) => {
            const header = command.group !== lastGroup ? command.group : null;
            lastGroup = command.group;
            return (
              <div key={command.id}>
                {header && (
                  <div className="label px-3 pb-1 pt-2">{header}</div>
                )}
                <button
                  data-index={i}
                  onMouseEnter={() => setActive(i)}
                  onClick={() => void runActive()}
                  disabled={command.disabled}
                  className={`flex w-full items-center gap-2 px-3 py-1.5 text-left transition-colors ${
                    i === active ? "bg-ink-750" : ""
                  } ${command.disabled ? "opacity-35" : ""}`}
                >
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-xs text-ink-100">
                      {command.label}
                    </span>
                    {command.hint && (
                      <span className="block truncate text-2xs text-ink-500">
                        {command.hint}
                      </span>
                    )}
                  </span>
                  {command.shortcut && (
                    <kbd className="chip shrink-0 font-mono">{command.shortcut}</kbd>
                  )}
                </button>
              </div>
            );
          })}
        </div>

        <div className="flex items-center gap-3 border-t border-ink-750 px-3 py-1.5 text-2xs text-ink-500">
          <span><kbd className="font-mono text-ink-300">↑↓</kbd> navigate</span>
          <span><kbd className="font-mono text-ink-300">↵</kbd> run</span>
          <span className="ml-auto">Acting on {scopeLabel}</span>
        </div>
      </div>
    </div>
  );
}
