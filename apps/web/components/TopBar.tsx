"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import type { Capabilities } from "@/lib/types";
import { useEditor, type Workspace } from "@/store/editor";

const WORKSPACES: { key: Workspace; label: string; ready: boolean }[] = [
  { key: "edit", label: "Edit", ready: true },
  { key: "color", label: "Color", ready: true },
  { key: "audio", label: "Audio", ready: true },
  { key: "assets", label: "Assets", ready: true },
  { key: "export", label: "Export", ready: true },
];

export function TopBar({ onExport }: { onExport: () => void }) {
  const projectId = useEditor((s) => s.projectId);
  const projectName = useEditor((s) => s.projectName);
  const workspace = useEditor((s) => s.workspace);
  const setWorkspace = useEditor((s) => s.setWorkspace);
  const canUndo = useEditor((s) => s.canUndo);
  const canRedo = useEditor((s) => s.canRedo);
  const undo = useEditor((s) => s.undo);
  const redo = useEditor((s) => s.redo);
  const saving = useEditor((s) => s.saving);

  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [name, setName] = useState(projectName);
  const [editingName, setEditingName] = useState(false);

  useEffect(() => setName(projectName), [projectName]);
  useEffect(() => {
    api.capabilities().then(setCaps).catch(() => undefined);
  }, []);

  const commitName = async () => {
    setEditingName(false);
    const trimmed = name.trim();
    if (!projectId || !trimmed || trimmed === projectName) {
      setName(projectName);
      return;
    }
    await api.renameProject(projectId, trimmed);
    useEditor.setState({ projectName: trimmed });
  };

  return (
    <header className="flex h-10 shrink-0 items-center gap-3 border-b border-ink-750 bg-ink-900 px-3">
      <Link href="/" className="flex items-center gap-2 text-ink-300 hover:text-ink-100">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden>
          <rect x="1.5" y="4.5" width="21" height="15" rx="2.5" stroke="currentColor" strokeWidth="1.6" />
          <path d="M9.5 9.2v5.6l5-2.8-5-2.8Z" fill="currentColor" />
        </svg>
      </Link>

      {editingName ? (
        <input
          autoFocus
          className="field h-6 w-52"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onBlur={commitName}
          onKeyDown={(e) => {
            if (e.key === "Enter") void commitName();
            if (e.key === "Escape") {
              setName(projectName);
              setEditingName(false);
            }
          }}
        />
      ) : (
        <button
          className="max-w-52 truncate text-sm font-medium text-ink-100 hover:text-accent"
          onClick={() => setEditingName(true)}
          title="Rename project"
        >
          {projectName || "Untitled Project"}
        </button>
      )}

      <span className="text-2xs text-ink-500">
        {saving ? "Saving…" : "Saved"}
      </span>

      <nav className="ml-4 flex items-center gap-0.5">
        {WORKSPACES.map((w) => (
          <button
            key={w.key}
            onClick={() => setWorkspace(w.key)}
            className={`h-7 rounded px-2.5 text-xs font-medium transition-colors ${
              workspace === w.key
                ? "bg-ink-750 text-ink-100"
                : "text-ink-400 hover:bg-ink-850 hover:text-ink-200"
            }`}
          >
            {w.label}
          </button>
        ))}
      </nav>

      <div className="ml-auto flex items-center gap-1.5">
        {caps && !caps.ai.llm_available && (
          <span
            className="chip text-accent"
            title={caps.ai.note}
          >
            Offline planner
          </span>
        )}
        {caps && !caps.media.ffmpeg.available && (
          <span className="chip text-danger" title={caps.media.ffmpeg.path}>
            FFmpeg missing
          </span>
        )}

        <button
          className="btn btn-ghost"
          onClick={() => void undo()}
          disabled={!canUndo}
          title="Undo (Ctrl+Z)"
        >
          <IconUndo />
        </button>
        <button
          className="btn btn-ghost"
          onClick={() => void redo()}
          disabled={!canRedo}
          title="Redo (Ctrl+Shift+Z)"
        >
          <IconRedo />
        </button>

        <div className="mx-1 h-4 w-px bg-ink-700" />

        <button className="btn btn-accent px-3" onClick={onExport}>
          Render &amp; Export
        </button>
      </div>
    </header>
  );
}

function IconUndo() {
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden>
      <path d="M3 7h6.5a3.5 3.5 0 1 1 0 7H6M3 7l3-3M3 7l3 3" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
function IconRedo() {
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden>
      <path d="M13 7H6.5a3.5 3.5 0 1 0 0 7H10M13 7l-3-3M13 7l-3 3" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
