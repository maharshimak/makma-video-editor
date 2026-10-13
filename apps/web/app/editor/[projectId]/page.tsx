"use client";

/**
 * The editor shell.
 *
 * Desktop-first three-column layout over a full-width timeline, with
 * draggable dividers. Panel sizes are the user's, so they persist.
 */
import { use, useCallback, useEffect, useRef, useState } from "react";

import { CommandPalette } from "@/components/CommandPalette";
import { DirectorPanel } from "@/components/DirectorPanel";
import { ExportDialog } from "@/components/ExportDialog";
import { Inspector } from "@/components/Inspector";
import { MediaPanel } from "@/components/MediaPanel";
import { Preview } from "@/components/Preview";
import { Scopes } from "@/components/Scopes";
import { Timeline } from "@/components/Timeline";
import { Toasts } from "@/components/Toasts";
import { TopBar } from "@/components/TopBar";
import { DropOverlay, useFileDrop } from "@/lib/useFileDrop";
import { getPlayhead, setPlaying, useEditor } from "@/store/editor";

const LAYOUT_KEY = "makma.layout";

interface Layout {
  left: number;
  right: number;
  timeline: number;
}

const DEFAULT_LAYOUT: Layout = { left: 264, right: 340, timeline: 260 };

export default function EditorPage({
  params,
}: {
  params: Promise<{ projectId: string }>;
}) {
  const { projectId } = use(params);

  const load = useEditor((s) => s.load);
  const loading = useEditor((s) => s.loading);
  const error = useEditor((s) => s.error);
  const workspace = useEditor((s) => s.workspace);
  const selection = useEditor((s) => s.selection);
  const applyOps = useEditor((s) => s.applyOps);

  const [layout, setLayout] = useState<Layout>(DEFAULT_LAYOUT);
  const [exportOpen, setExportOpen] = useState(false);

  // Files can be dropped on any part of the editor, not just the media panel.
  const uploadFiles = useEditor((s) => s.uploadFiles);
  const dragging = useFileDrop((files) => void uploadFiles(files));

  useEffect(() => {
    void load(projectId);
  }, [projectId, load]);

  useEffect(() => {
    try {
      const stored = window.localStorage.getItem(LAYOUT_KEY);
      if (stored) setLayout({ ...DEFAULT_LAYOUT, ...JSON.parse(stored) });
    } catch {
      /* a corrupt layout should never block the editor */
    }
  }, []);

  const persist = useCallback((next: Layout) => {
    setLayout(next);
    window.localStorage.setItem(LAYOUT_KEY, JSON.stringify(next));
  }, []);

  // --- global shortcuts ----------------------------------------------------
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (target && ["INPUT", "TEXTAREA"].includes(target.tagName)) return;
      const mod = e.ctrlKey || e.metaKey;

      if (mod && e.key.toLowerCase() === "z") {
        e.preventDefault();
        void (e.shiftKey ? useEditor.getState().redo() : useEditor.getState().undo());
      } else if (mod && e.key.toLowerCase() === "y") {
        e.preventDefault();
        void useEditor.getState().redo();
      } else if (mod && e.key.toLowerCase() === "e") {
        e.preventDefault();
        setExportOpen(true);
      } else if (e.key.toLowerCase() === "s" && !mod) {
        e.preventDefault();
        void applyOps(
          [{
            type: "split_clip",
            description: "Split at the playhead",
            scope: selection.length ? { type: "clip", clip_ids: selection } : { type: "all" },
            parameters: { time: getPlayhead() },
          }],
          "Split",
        );
      } else if ((e.key === "Delete" || e.key === "Backspace") && selection.length) {
        e.preventDefault();
        void applyOps(
          [{
            type: "delete_clip",
            description: `Delete ${selection.length} clip(s)`,
            scope: { type: "clip", clip_ids: selection },
            parameters: {},
          }],
          "Delete clip",
        );
      } else if (e.key.toLowerCase() === "n" && !mod) {
        useEditor.getState().toggleSnapping();
      } else if (e.key === "Escape") {
        useEditor.getState().clearSelection();
        setPlaying(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selection, applyOps]);

  if (error) {
    return (
      <div className="flex h-screen items-center justify-center bg-ink-950">
        <div className="panel max-w-md p-6 text-center">
          <h1 className="mb-2 text-sm font-medium text-danger">
            Could not open this project
          </h1>
          <p className="text-xs leading-relaxed text-ink-300">{error}</p>
          <a href="/" className="btn mt-4">Back to projects</a>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-ink-950">
      <TopBar onExport={() => setExportOpen(true)} />

      <div className="flex min-h-0 flex-1">
        <div style={{ width: layout.left }} className="shrink-0 border-r border-ink-750">
          {workspace === "assets" ? <MediaPanel /> : <MediaPanel />}
        </div>

        <Divider
          orientation="vertical"
          onDrag={(dx) =>
            persist({ ...layout, left: clamp(layout.left + dx, 180, 460) })
          }
        />

        <div className="flex min-w-0 flex-1 flex-col">
          <div className="min-h-0 flex-1">
            <Preview />
          </div>
          <Divider
            orientation="horizontal"
            onDrag={(dy) =>
              persist({ ...layout, timeline: clamp(layout.timeline - dy, 140, 620) })
            }
          />
          <div style={{ height: layout.timeline }} className="shrink-0 border-t border-ink-750">
            {loading ? (
              <div className="flex h-full items-center justify-center text-xs text-ink-400">
                Opening project…
              </div>
            ) : (
              <Timeline />
            )}
          </div>
        </div>

        <Divider
          orientation="vertical"
          onDrag={(dx) =>
            persist({ ...layout, right: clamp(layout.right - dx, 260, 560) })
          }
        />

        <div
          style={{ width: layout.right }}
          className="flex shrink-0 flex-col border-l border-ink-750"
        >
          {/* Colour work wants measurement where the conversation usually
              sits; everything else wants the director. */}
          <div className="min-h-0 flex-1">
            {workspace === "color" ? <Scopes /> : <DirectorPanel />}
          </div>
          <div className="h-[46%] min-h-0 border-t border-ink-750">
            <Inspector />
          </div>
        </div>
      </div>

      <DropOverlay
        active={dragging}
        title="Drop to import"
        detail="Video, photos, audio, LUTs and fonts land in this project's media."
      />
      <CommandPalette />
      <ExportDialog open={exportOpen} onClose={() => setExportOpen(false)} />
      <Toasts />
    </div>
  );
}

function Divider({
  orientation,
  onDrag,
}: {
  orientation: "vertical" | "horizontal";
  onDrag: (delta: number) => void;
}) {
  const last = useRef(0);
  const vertical = orientation === "vertical";

  return (
    <div
      className={`group relative shrink-0 ${
        vertical ? "w-px cursor-col-resize" : "h-px cursor-row-resize"
      } bg-ink-750`}
      onPointerDown={(e) => {
        e.preventDefault();
        last.current = vertical ? e.clientX : e.clientY;
        const move = (ev: PointerEvent) => {
          const current = vertical ? ev.clientX : ev.clientY;
          onDrag(current - last.current);
          last.current = current;
        };
        const up = () => {
          window.removeEventListener("pointermove", move);
          window.removeEventListener("pointerup", up);
        };
        window.addEventListener("pointermove", move);
        window.addEventListener("pointerup", up);
      }}
    >
      <span
        className={`absolute bg-accent/0 transition-colors duration-150 group-hover:bg-accent/50 ${
          vertical ? "-inset-x-1 inset-y-0" : "-inset-y-1 inset-x-0"
        }`}
      />
    </div>
  );
}

function clamp(v: number, min: number, max: number) {
  return Math.max(min, Math.min(max, v));
}
