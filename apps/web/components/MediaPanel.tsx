"use client";

/** Media pool: upload, ingest status, scene index, and add-to-timeline. */
import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "@/lib/api";
import { formatBytes, formatDuration, formatTimecode } from "@/lib/time";
import type { Asset } from "@/lib/types";
import { getPlayhead, setPlayhead, useEditor } from "@/store/editor";

export function MediaPanel() {
  const projectId = useEditor((s) => s.projectId);
  const assets = useEditor((s) => s.assets);
  const refreshAssets = useEditor((s) => s.refreshAssets);
  const reload = useEditor((s) => s.reload);
  const applyOps = useEditor((s) => s.applyOps);
  const toast = useEditor((s) => s.toast);

  const inputRef = useRef<HTMLInputElement>(null);
  const uploads = useEditor((s) => s.uploads);
  const upload = useEditor((s) => s.uploadFiles);
  const [expanded, setExpanded] = useState<string | null>(null);

  // Ingestion runs in a worker; poll only while something is still working.
  const pending = assets.some((a) => a.status === "ingesting" || a.status === "uploading");
  useEffect(() => {
    if (!pending) return;
    const id = setInterval(() => void refreshAssets(), 1500);
    return () => clearInterval(id);
  }, [pending, refreshAssets]);

  /**
   * Remove a file from the project.
   *
   * A file still on the timeline takes its clips with it, but only after the
   * user says so -- deleting it silently would leave clips pointing at media
   * that no longer exists, which look normal until the render warns.
   */
  const removeAsset = async (asset: Asset) => {
    if (!projectId) return;
    try {
      const usage = await api.assetUsage(projectId, asset.id);
      if (usage.clip_count > 0) {
        const plural = usage.clip_count === 1 ? "clip" : "clips";
        const ok = window.confirm(
          `${asset.name} is used by ${usage.clip_count} ${plural} on the ` +
          `timeline.

Remove the file and those ${plural} too? ` +
          "You can undo this.",
        );
        if (!ok) return;
        await api.deleteAsset(projectId, asset.id, true);
        await reload();
        toast({
          kind: "success",
          title: `Removed ${asset.name}`,
          detail: `${usage.clip_count} ${plural} taken off the timeline. Ctrl+Z to undo.`,
        });
      } else {
        await api.deleteAsset(projectId, asset.id);
        toast({ kind: "success", title: `Removed ${asset.name}` });
      }
      await refreshAssets();
    } catch (err) {
      toast({
        kind: "error",
        title: `Could not remove ${asset.name}`,
        detail: err instanceof Error ? err.message : undefined,
      });
    }
  };

  const addToTimeline = (asset: Asset, at?: number) => {
    if (asset.status !== "ready") {
      toast({ kind: "info", title: "That file is still being ingested." });
      return;
    }
    void applyOps(
      [
        {
          type: "add_clip",
          description: `Add ${asset.name}`,
          scope: { type: "sequence" },
          parameters: {
            asset_id: asset.id,
            start: at ?? "append",
            duration: asset.kind === "image" ? 5 : asset.duration,
          },
        },
      ],
      `Add ${asset.name}`,
    );
  };

  const addScene = (asset: Asset, start: number, end: number) => {
    void applyOps(
      [
        {
          type: "add_clip",
          description: `Add shot ${start.toFixed(1)}s–${end.toFixed(1)}s`,
          scope: { type: "sequence" },
          parameters: {
            asset_id: asset.id,
            start: "append",
            in_point: start,
            duration: Math.max(0.1, end - start),
          },
        },
      ],
      "Add shot",
    );
  };

  return (
    // Dropping is handled window-wide by the editor shell, so files land here
    // wherever they are released.
    <div className="flex h-full flex-col bg-ink-900">
      <div className="panel-header">
        <span>Media</span>
        <button className="btn btn-ghost h-6" onClick={() => inputRef.current?.click()}>
          + Import
        </button>
      </div>

      <input
        ref={inputRef}
        type="file"
        multiple
        hidden
        onChange={(e) => e.target.files && void upload(e.target.files)}
      />

      <div className="min-h-0 flex-1 overflow-y-auto p-2">
        {Object.entries(uploads).map(([key, fraction]) => (
          <div key={key} className="mb-2 rounded bg-ink-850 px-2.5 py-2">
            <div className="mb-1 truncate text-2xs text-ink-300">
              {key.split("-")[0]}
            </div>
            <div className="h-1 overflow-hidden rounded-full bg-ink-700">
              <div
                className="h-full bg-accent transition-[width] duration-150"
                style={{ width: `${fraction * 100}%` }}
              />
            </div>
          </div>
        ))}

        {assets.length === 0 && Object.keys(uploads).length === 0 && (
          <button
            onClick={() => inputRef.current?.click()}
            className="w-full rounded border border-dashed border-ink-700 px-3 py-8 text-center transition-colors hover:border-ink-500"
          >
            <div className="text-xs text-ink-300">Drop footage here</div>
            <div className="mt-1 text-2xs text-ink-500">
              Video, audio, images, LUTs
            </div>
          </button>
        )}

        <div className="space-y-1">
          {assets.map((asset) => (
            <AssetRow
              key={asset.id}
              asset={asset}
              expanded={expanded === asset.id}
              onToggle={() => setExpanded(expanded === asset.id ? null : asset.id)}
              onAdd={() => addToTimeline(asset)}
              onAddScene={(s, e) => addScene(asset, s, e)}
              onDelete={() => void removeAsset(asset)}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

function AssetRow({
  asset,
  expanded,
  onToggle,
  onAdd,
  onAddScene,
  onDelete,
}: {
  asset: Asset;
  expanded: boolean;
  onToggle: () => void;
  onAdd: () => void;
  onAddScene: (start: number, end: number) => void;
  onDelete: () => void;
}) {
  const ingesting = asset.status === "ingesting" || asset.status === "uploading";
  const failed = asset.status === "failed";

  return (
    <div className="overflow-hidden rounded border border-ink-800 bg-ink-850">
      <div className="flex gap-2 p-1.5">
        <button
          onClick={onToggle}
          className="checkerboard relative h-11 w-[74px] shrink-0 overflow-hidden rounded-sm"
        >
          {asset.thumbnail_url ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img src={asset.thumbnail_url} alt="" className="h-full w-full object-cover" />
          ) : (
            <span className="flex h-full items-center justify-center text-2xs text-ink-500">
              {asset.kind}
            </span>
          )}
          {ingesting && (
            <span className="absolute inset-0 flex items-center justify-center bg-black/65 text-2xs text-accent animate-pulse-soft">
              ingesting
            </span>
          )}
          {asset.duration > 0 && !ingesting && (
            <span className="absolute bottom-0 right-0 bg-black/75 px-1 text-2xs tabular-nums text-white/90">
              {formatDuration(asset.duration)}
            </span>
          )}
        </button>

        <div className="min-w-0 flex-1">
          <div className="truncate text-xs text-ink-100" title={asset.name}>
            {asset.name}
          </div>
          <div className="mt-0.5 flex flex-wrap items-center gap-x-1.5 text-2xs text-ink-500">
            {asset.width > 0 && <span>{asset.width}×{asset.height}</span>}
            {asset.fps > 0 && <span>{asset.fps.toFixed(0)}fps</span>}
            {asset.has_audio && <span className="text-audio">audio</span>}
            {asset.scene_count > 0 && (
              <span className="text-ink-400">{asset.scene_count} shots</span>
            )}
            <span>{formatBytes(asset.size_bytes)}</span>
          </div>
          {failed && (
            <div className="mt-1 truncate text-2xs text-danger" title={asset.error}>
              {asset.error || "Ingestion failed"}
            </div>
          )}
        </div>

        <div className="flex shrink-0 flex-col gap-1">
          <button
            className="btn h-5 px-1.5 text-2xs"
            onClick={onAdd}
            disabled={asset.status !== "ready"}
            title="Append to the timeline"
          >
            Add
          </button>
          <button
            className="btn btn-ghost btn-danger h-5 px-1.5 text-2xs"
            onClick={onDelete}
            title="Remove from project"
          >
            Del
          </button>
        </div>
      </div>

      {expanded && asset.analysis && (
        <div className="border-t border-ink-800 bg-ink-900/60 p-2">
          {asset.analysis.highlights?.length > 0 && (
            <>
              <div className="label mb-1.5">Strongest moments</div>
              <div className="mb-2 space-y-1">
                {asset.analysis.highlights.slice(0, 5).map((h, i) => (
                  <button
                    key={i}
                    onClick={() => onAddScene(h.start, h.end)}
                    className="flex w-full items-center gap-2 rounded px-1.5 py-1 text-left text-2xs hover:bg-ink-800"
                    title="Add this range to the timeline"
                  >
                    <span className="w-14 shrink-0 font-mono tabular-nums text-ink-300">
                      {formatTimecode(h.start, 30, false)}
                    </span>
                    <span className="h-1 flex-1 overflow-hidden rounded-full bg-ink-750">
                      <span
                        className="block h-full bg-accent/70"
                        style={{ width: `${Math.min(100, h.score * 100)}%` }}
                      />
                    </span>
                    <span className="w-24 shrink-0 truncate text-ink-500">{h.reason}</span>
                  </button>
                ))}
              </div>
            </>
          )}

          {asset.analysis.scenes?.length > 0 && (
            <>
              <div className="label mb-1.5">Shots</div>
              <div className="flex flex-wrap gap-1">
                {asset.analysis.scenes.slice(0, 24).map((scene) => (
                  <button
                    key={scene.id}
                    onClick={() => onAddScene(scene.start, scene.end)}
                    className="rounded bg-ink-800 px-1.5 py-0.5 font-mono text-2xs tabular-nums text-ink-300 hover:bg-ink-700 hover:text-ink-100"
                    title={`Shot ${scene.index + 1} · quality ${(scene.quality_score * 100).toFixed(0)}% · energy ${(scene.energy_score * 100).toFixed(0)}%`}
                  >
                    {formatTimecode(scene.start, 30, false)}
                  </button>
                ))}
              </div>
            </>
          )}

          {asset.analysis.silences?.length > 0 && (
            <div className="mt-2 text-2xs text-ink-500">
              {asset.analysis.silences.length} silent gap
              {asset.analysis.silences.length === 1 ? "" : "s"} detected — ask the
              director to &ldquo;remove the pauses&rdquo;.
            </div>
          )}
        </div>
      )}
    </div>
  );
}
