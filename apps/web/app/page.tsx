"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { api, ApiError } from "@/lib/api";
import { DropOverlay, useFileDrop } from "@/lib/useFileDrop";
import { formatDuration } from "@/lib/time";
import type { Capabilities, ProjectSummary } from "@/lib/types";

const FORMATS = [
  { label: "16:9 · 1080p", width: 1920, height: 1080, fps: 30 },
  { label: "9:16 · Vertical", width: 1080, height: 1920, fps: 30 },
  { label: "1:1 · Square", width: 1080, height: 1080, fps: 30 },
  { label: "16:9 · 4K", width: 3840, height: 2160, fps: 30 },
];

export default function ProjectsPage() {
  const router = useRouter();
  const [projects, setProjects] = useState<ProjectSummary[] | null>(null);
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [format, setFormat] = useState(0);

  const refresh = useCallback(async () => {
    try {
      setProjects(await api.listProjects());
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reach the API.");
    }
  }, []);

  useEffect(() => {
    void refresh();
    api.capabilities().then(setCaps).catch(() => setCaps(null));
  }, [refresh]);

  const create = async (files?: File[]) => {
    setCreating(true);
    try {
      const f = FORMATS[format];
      const fallback = files?.length
        ? files[0].name.replace(/\.[^.]+$/, "")
        : "Untitled Project";
      const project = await api.createProject({
        name: name.trim() || fallback,
        width: f.width,
        height: f.height,
        fps: f.fps,
      });

      // Dropping footage on this page means "start here with these files",
      // so the upload happens before the editor opens and the media panel is
      // already filling in when it does.
      if (files?.length) {
        setStatus(`Importing ${files.length} file${files.length === 1 ? "" : "s"}…`);
        await Promise.all(
          files.map((file) => api.uploadAsset(project.id, file)),
        );
      }
      router.push(`/editor/${project.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create the project.");
      setStatus(null);
      setCreating(false);
    }
  };

  // A file dropped on the project list has nowhere to go yet, so it creates
  // the project it obviously wants.
  const dragging = useFileDrop((files) => void create(files));

  return (
    <div className="h-screen overflow-y-auto bg-ink-950">
      <DropOverlay
        active={dragging}
        title="Drop to start a project"
        detail="A new project is created and your footage imported straight into it."
      />
      <div className="mx-auto max-w-6xl px-8 py-14">
        <header className="mb-12 flex items-end justify-between gap-8">
          <div>
            <div className="mb-2 flex items-center gap-2.5">
              <Mark />
              <span className="text-2xs font-semibold uppercase tracking-[0.28em] text-ink-400">
                Mak&rsquo;ma Editz
              </span>
            </div>
            <h1 className="text-3xl font-semibold leading-tight text-ink-100">
              Describe the edit. <span className="text-ink-400">Get the cut.</span>
            </h1>
            <p className="mt-2 max-w-xl text-sm leading-relaxed text-ink-300">
              A professional timeline with an AI director on top. Upload footage,
              say what you want, and every change lands as a real, editable,
              reversible operation.
            </p>
          </div>
          <SystemBadge caps={caps} />
        </header>

        <section className="panel mb-10 p-5">
          <div className="mb-3 flex items-center gap-2">
            <h2 className="text-sm font-semibold text-ink-100">New project</h2>
          </div>
          <div className="flex flex-wrap items-end gap-3">
            <label className="min-w-56 flex-1">
              <span className="label mb-1.5 block">Name</span>
              <input
                className="field h-8"
                placeholder="Night Drive"
                value={name}
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && void create()}
              />
            </label>
            <div>
              <span className="label mb-1.5 block">Format</span>
              <div className="flex gap-1.5">
                {FORMATS.map((f, i) => (
                  <button
                    key={f.label}
                    onClick={() => setFormat(i)}
                    className={`btn h-8 ${
                      i === format ? "border-accent/60 bg-ink-700 text-ink-100" : ""
                    }`}
                  >
                    {f.label}
                  </button>
                ))}
              </div>
            </div>
            <button
              className="btn btn-accent h-8 px-4"
              onClick={() => void create()}
              disabled={creating}
            >
              {creating ? status ?? "Creating…" : "Create project"}
            </button>
          </div>
        </section>

        {error && (
          <div className="mb-6 rounded border border-danger/40 bg-danger/10 px-4 py-3 text-sm text-danger">
            {error}
          </div>
        )}

        <section>
          <h2 className="label mb-3">Projects</h2>
          {projects === null ? (
            <div className="grid grid-cols-2 gap-4 md:grid-cols-3">
              {[0, 1, 2].map((i) => (
                <div key={i} className="panel h-40 animate-pulse-soft" />
              ))}
            </div>
          ) : projects.length === 0 ? (
            <div className="panel px-6 py-12 text-center">
              <p className="text-sm text-ink-300">No projects yet.</p>
              <p className="mt-1 text-xs text-ink-400">
                Create one above, drop in some footage, and tell the director what
                you want.
              </p>
            </div>
          ) : (
            <div className="grid grid-cols-2 gap-4 md:grid-cols-3">
              {projects.map((p) => (
                <Link
                  key={p.id}
                  href={`/editor/${p.id}`}
                  className="panel group overflow-hidden transition-colors duration-150 hover:border-accent/40"
                >
                  <div className="checkerboard relative aspect-video overflow-hidden">
                    {p.thumbnail_url ? (
                      // eslint-disable-next-line @next/next/no-img-element
                      <img
                        src={p.thumbnail_url}
                        alt=""
                        className="h-full w-full object-cover opacity-90 transition-opacity group-hover:opacity-100"
                      />
                    ) : (
                      <div className="flex h-full items-center justify-center text-2xs text-ink-500">
                        No media yet
                      </div>
                    )}
                  </div>
                  <div className="p-3">
                    <div className="truncate text-sm font-medium text-ink-100">
                      {p.name}
                    </div>
                    <div className="mt-1 flex items-center gap-2 text-2xs text-ink-400">
                      <span>{p.asset_count} asset{p.asset_count === 1 ? "" : "s"}</span>
                      <span className="text-ink-600">·</span>
                      <span>{formatDuration(p.duration)}</span>
                    </div>
                  </div>
                </Link>
              ))}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}

function Mark() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden>
      <rect x="1.5" y="4.5" width="21" height="15" rx="2.5" stroke="#e0a33c" strokeWidth="1.6" />
      <path d="M9.5 9.2v5.6l5-2.8-5-2.8Z" fill="#e0a33c" />
    </svg>
  );
}

function SystemBadge({ caps }: { caps: Capabilities | null }) {
  if (!caps) {
    return (
      <div className="chip h-6">
        <Dot className="bg-ink-500" />
        Connecting…
      </div>
    );
  }
  const ffmpeg = caps.media.ffmpeg.available;
  return (
    <div className="flex flex-col items-end gap-1.5">
      <div className="chip h-6">
        <Dot className={ffmpeg ? "bg-audio" : "bg-danger"} />
        {ffmpeg ? "Render engine ready" : "FFmpeg not found"}
      </div>
      <div className="chip h-6">
        <Dot className={caps.ai.llm_available ? "bg-audio" : "bg-accent"} />
        {caps.ai.llm_available ? "AI director online" : "Offline planner only"}
      </div>
    </div>
  );
}

function Dot({ className }: { className: string }) {
  return <span className={`h-1.5 w-1.5 rounded-full ${className}`} />;
}
