"use client";

/**
 * Render and export.
 *
 * Every job shows its real stage and progress streamed from the worker --
 * "encoding 62%", not an unexplained spinner -- and any warning the render
 * engine emitted is shown next to the finished file rather than swallowed.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { api, watchJob } from "@/lib/api";
import { formatBytes, formatDuration } from "@/lib/time";
import type { Capabilities, HealthReport, Job } from "@/lib/types";
import { useEditor } from "@/store/editor";

export function ExportDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const projectId = useEditor((s) => s.projectId);
  const toast = useEditor((s) => s.toast);

  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [preset, setPreset] = useState("master_1080p");
  const [jobs, setJobs] = useState<Job[]>([]);
  const [health, setHealth] = useState<HealthReport | null>(null);
  const [starting, setStarting] = useState(false);
  const watchers = useRef(new Map<string, () => void>());

  useEffect(() => {
    api.capabilities().then(setCaps).catch(() => undefined);
  }, []);

  const loadJobs = useCallback(async () => {
    if (!projectId) return;
    const list = await api.jobs(projectId);
    const relevant = list.filter((j) => j.type === "render" || j.type === "export");
    setJobs(relevant);
    // Attach a live stream to anything still in flight.
    for (const job of relevant) {
      if (["completed", "failed", "cancelled"].includes(job.status)) continue;
      if (watchers.current.has(job.id)) continue;
      const stop = watchJob(
        job.id,
        (update) => setJobs((prev) => prev.map((j) => (j.id === update.id ? update : j))),
        (final) => {
          watchers.current.delete(final.id);
          setJobs((prev) => prev.map((j) => (j.id === final.id ? final : j)));
          if (final.status === "completed") {
            toast({
              kind: "success",
              title: "Render complete",
              detail: final.result.filename,
            });
          } else if (final.status === "failed") {
            toast({ kind: "error", title: "Render failed", detail: final.error.slice(0, 200) });
          }
        },
      );
      watchers.current.set(job.id, stop);
    }
  }, [projectId, toast]);

  useEffect(() => {
    if (!open) return;
    void loadJobs();
    // Re-run the pre-flight every time the dialog opens: the timeline has
    // usually changed since the last look.
    if (projectId) {
      api.health(projectId).then(setHealth).catch(() => setHealth(null));
    }
  }, [open, loadJobs, projectId]);

  useEffect(() => {
    const map = watchers.current;
    return () => {
      map.forEach((stop) => stop());
      map.clear();
    };
  }, []);

  const start = async (kind: "render" | "export") => {
    if (!projectId) return;
    setStarting(true);
    try {
      const job =
        kind === "export"
          ? await api.export(projectId, { preset })
          : await api.render(projectId, { preset: "draft_540p" });
      setJobs((prev) => [job, ...prev]);
      await loadJobs();
    } catch (err) {
      toast({
        kind: "error",
        title: "Could not start the render",
        detail: err instanceof Error ? err.message : undefined,
      });
    } finally {
      setStarting(false);
    }
  };

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/65 p-6"
      onClick={onClose}
    >
      <div
        className="panel animate-in flex max-h-[80vh] w-[620px] flex-col shadow-pop"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="panel-header">
          <span>Render &amp; Export</span>
          <button className="btn btn-ghost h-6" onClick={onClose}>Close</button>
        </div>

        {health && <PreFlight report={health} />}

        <div className="border-b border-ink-750 p-4">
          <div className="label mb-2">Preset</div>
          <div className="mb-3 grid grid-cols-3 gap-1.5">
            {(caps?.export_presets ?? []).map((p) => (
              <button
                key={p.key}
                onClick={() => setPreset(p.key)}
                title={p.note}
                className={`rounded border px-2 py-1.5 text-left transition-colors ${
                  preset === p.key
                    ? "border-accent/70 bg-ink-750"
                    : "border-ink-700 bg-ink-850 hover:border-ink-500"
                }`}
              >
                <div className="truncate text-xs text-ink-100">{p.label}</div>
                <div className="text-2xs text-ink-500">
                  {p.width}×{p.height} · {p.fps}fps
                </div>
              </button>
            ))}
          </div>

          <div className="flex items-center gap-2">
            <button
              className="btn btn-accent px-3"
              onClick={() => void start("export")}
              disabled={starting || health?.ready_to_export === false}
              title={
                health?.ready_to_export === false
                  ? "Fix the errors above first"
                  : undefined
              }
            >
              Export at full quality
            </button>
            <button
              className="btn"
              onClick={() => void start("render")}
              disabled={starting}
              title="Fast 540p render from the proxies, for review"
            >
              Quick draft
            </button>
            <span className="ml-auto text-2xs text-ink-500">
              Exports read the original media; drafts use proxies.
            </span>
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-3">
          {jobs.length === 0 ? (
            <p className="py-8 text-center text-xs text-ink-500">
              No renders yet.
            </p>
          ) : (
            <div className="space-y-2">
              {jobs.map((job) => (
                <JobRow key={job.id} job={job} />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/**
 * Pre-flight check.
 *
 * Everything listed here renders *something*, so the encode would succeed and
 * the result would be wrong. Errors block the export; warnings and notes are
 * judgement calls and stay out of the way.
 */
function PreFlight({ report }: { report: HealthReport }) {
  const [open, setOpen] = useState(false);
  const { error, warning, info } = report.counts;
  const total = error + warning + info;

  return (
    <div className="border-b border-ink-750 px-4 py-2.5">
      <button
        className="flex w-full items-center gap-2 text-left"
        onClick={() => setOpen((v) => !v)}
        disabled={total === 0}
      >
        <span
          className={`h-1.5 w-1.5 rounded-full ${
            error ? "bg-danger" : warning ? "bg-accent" : "bg-audio"
          }`}
        />
        <span className="text-xs text-ink-100">
          {error
            ? `${error} problem${error === 1 ? "" : "s"} to fix before export`
            : warning
              ? `Ready, with ${warning} thing${warning === 1 ? "" : "s"} worth checking`
              : "Pre-flight clean"}
        </span>
        <span className="ml-auto text-2xs text-ink-500">
          {report.clip_count} clip{report.clip_count === 1 ? "" : "s"} ·{" "}
          {report.duration.toFixed(1)}s
          {total > 0 && (open ? "  ▴" : "  ▾")}
        </span>
      </button>

      {open && (
        <ul className="mt-2 space-y-1.5">
          {report.issues.map((issue, i) => (
            <li key={i} className="flex gap-2">
              <span
                className={`mt-[3px] h-1.5 w-1.5 shrink-0 rounded-full ${
                  issue.severity === "error"
                    ? "bg-danger"
                    : issue.severity === "warning"
                      ? "bg-accent"
                      : "bg-ink-500"
                }`}
              />
              <span className="min-w-0 flex-1">
                <span className="block text-2xs leading-relaxed text-ink-200">
                  {issue.time !== null && (
                    <span className="mr-1.5 font-mono text-ink-400">
                      {issue.time.toFixed(1)}s
                    </span>
                  )}
                  {issue.message}
                </span>
                {issue.fix && (
                  <span className="block text-2xs text-ink-500">{issue.fix}</span>
                )}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

const STATUS_COLOR: Record<string, string> = {
  queued: "text-ink-400",
  processing: "text-accent",
  rendering: "text-accent",
  completed: "text-audio",
  failed: "text-danger",
  cancelled: "text-ink-500",
};

function JobRow({ job }: { job: Job }) {
  const [showLog, setShowLog] = useState(false);
  const active = !["completed", "failed", "cancelled"].includes(job.status);

  return (
    <div className="rounded border border-ink-800 bg-ink-850 p-2.5">
      <div className="flex items-center gap-2">
        <span className={`text-2xs font-semibold uppercase tracking-wider ${STATUS_COLOR[job.status] ?? ""}`}>
          {job.status}
        </span>
        <span className="text-2xs text-ink-500">{job.type}</span>
        {job.result.resolution && (
          <span className="chip">{job.result.resolution}</span>
        )}
        <span className="ml-auto flex items-center gap-1.5">
          {job.log.length > 0 && (
            <button
              className="btn btn-ghost h-5 px-1.5 text-2xs"
              onClick={() => setShowLog((v) => !v)}
            >
              Log
            </button>
          )}
          {active && (
            <button
              className="btn btn-ghost btn-danger h-5 px-1.5 text-2xs"
              onClick={() => void api.cancelJob(job.id)}
            >
              Cancel
            </button>
          )}
          {job.status === "completed" && job.result.url && (
            <a className="btn btn-accent h-5 px-2 text-2xs" href={job.result.url} download>
              Download
            </a>
          )}
        </span>
      </div>

      {active && (
        <div className="mt-2">
          <div className="mb-1 flex items-center justify-between text-2xs text-ink-400">
            <span>{job.stage || "waiting for a worker"}</span>
            <span className="tabular-nums">{Math.round(job.progress * 100)}%</span>
          </div>
          <div className="h-1 overflow-hidden rounded-full bg-ink-750">
            <div
              className="h-full bg-accent transition-[width] duration-200"
              style={{ width: `${Math.max(2, job.progress * 100)}%` }}
            />
          </div>
        </div>
      )}

      {job.status === "completed" && (
        <div className="mt-1.5 flex flex-wrap items-center gap-2 text-2xs text-ink-400">
          <span className="truncate text-ink-200">{job.result.filename}</span>
          <span>{formatDuration(job.result.duration ?? 0)}</span>
          <span>{formatBytes(job.result.size_bytes ?? 0)}</span>
          <span>encoded in {job.result.seconds_taken}s</span>
        </div>
      )}

      {(job.result.warnings ?? []).length > 0 && (
        <ul className="mt-1.5 space-y-0.5 border-t border-ink-800 pt-1.5">
          {job.result.warnings!.map((w, i) => (
            <li key={i} className="text-2xs leading-relaxed text-accent">
              {w}
            </li>
          ))}
        </ul>
      )}

      {job.status === "failed" && job.error && (
        <pre className="mt-1.5 max-h-28 overflow-auto whitespace-pre-wrap rounded bg-ink-950 p-2 text-2xs text-danger">
          {job.error.slice(0, 1500)}
        </pre>
      )}

      {showLog && (
        <div className="mt-1.5 max-h-40 space-y-0.5 overflow-y-auto rounded bg-ink-950 p-2">
          {job.log.map((entry, i) => (
            <div key={i} className="font-mono text-2xs text-ink-400">
              <span className="text-ink-600">{entry.t.slice(11, 19)}</span>{" "}
              {entry.message}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
