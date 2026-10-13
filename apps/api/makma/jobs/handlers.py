"""Job handlers: ingest, analyse, render, export.

Each handler is a plain function over a `JobContext`. It reports progress,
respects cancellation, and returns a result dict that the API surfaces to the
UI verbatim -- including warnings, so a render that succeeded with caveats
never looks like a clean one.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings
from ..db import session_scope
from ..media.ffmpeg import FFmpegError
from ..media.ingest import UnreadableMedia, ingest_file as _run_ingest
from ..models import Asset, Project
from ..render.renderer import (
    EXPORT_PRESETS,
    RenderSettings,
    preset_settings,
    render_sequence,
)
from ..schema.core import ProjectDocument
from ..schema.media import AssetStatus
from ..storage.base import StorageKey
from ..storage.local import get_storage
from .queue import JobContext, JobStatus, register

log = logging.getLogger("makma.jobs.handlers")


def _fail_asset(asset_id: str, reason: str) -> None:
    """Record why an upload could not be used, and stop it spinning."""
    with session_scope() as db:
        asset = db.get(Asset, asset_id)
        if asset is not None:
            asset.status = AssetStatus.FAILED.value
            asset.error = reason[:2000]


def _ffmpeg_reason(exc: FFmpegError) -> str:
    """The one line of FFmpeg stderr worth showing a person.

    FFmpeg prefixes its complaint with the absolute path it was given. That is
    a server path the user has never seen, so it is trimmed off -- they already
    know which file they uploaded.
    """
    for line in reversed((exc.stderr or "").strip().splitlines()):
        text = line.strip()
        if not text or text.startswith(("frame=", "size=", "  ", "[")):
            continue
        if ": " in text:
            head, _, tail = text.partition(": ")
            if "/" in head or "\\" in head:
                text = tail
        return text[:200]
    return "no readable error output"


@register("ingest")
def handle_ingest(ctx: JobContext) -> dict:
    """Probe, proxy, thumbnail, waveform and analyse one uploaded file."""
    asset_id = ctx.payload["asset_id"]
    deep = bool(ctx.payload.get("deep_analysis", True))
    storage = get_storage()

    with session_scope() as db:
        asset = db.get(Asset, asset_id)
        if asset is None:
            raise ValueError(f"Asset {asset_id} no longer exists.")
        asset.status = AssetStatus.INGESTING.value
        original_key = asset.original_key
        project_id = asset.project_id
        name = asset.name

    ctx.log(f"Ingesting {name}")
    local = storage.local_path(original_key)
    if not local.exists():
        raise FileNotFoundError(f"Uploaded file is missing from storage: {original_key}")

    try:
        result = _run_ingest(
            local_path=local,
            project_id=project_id,
            asset_id=asset_id,
            storage=storage,
            report=lambda p, s: ctx.progress(p, s),
            deep_analysis=deep,
        )
    except (UnreadableMedia, FFmpegError) as exc:
        # A file we cannot decode is marked failed with the reason on the
        # asset. Leaving it as "ingesting" is what makes an upload look like it
        # silently vanished, and retrying will never help, so this is terminal.
        reason = (
            str(exc) if isinstance(exc, UnreadableMedia)
            else (
                f"'{name}' could not be decoded. FFmpeg reported: "
                + _ffmpeg_reason(exc)
            )
        )
        _fail_asset(asset_id, reason)
        ctx.log(f"Rejected: {reason}")
        return {"asset_id": asset_id, "ok": False, "error": reason}
    except Exception as exc:
        # Anything else is ours, not the file's. Still mark the asset so the
        # panel stops spinning, then let the job system record the failure.
        _fail_asset(asset_id, f"Ingestion failed: {type(exc).__name__}: {exc}")
        raise

    video = result.probe.primary_video
    with session_scope() as db:
        asset = db.get(Asset, asset_id)
        asset.kind = result.kind.value
        asset.checksum = result.checksum
        asset.duration = result.probe.duration
        asset.width = video.width if video else 0
        asset.height = video.height if video else 0
        asset.fps = video.fps if video else 0.0
        asset.has_audio = bool(result.probe.audio)
        asset.proxy_key = result.proxy_key
        asset.audio_key = result.audio_key
        asset.thumbnail_key = result.thumbnail_key
        asset.waveform_key = result.waveform_key
        asset.probe = result.probe.model_dump()
        asset.analysis = result.analysis.model_dump()
        asset.size_bytes = storage.size(original_key)
        asset.status = AssetStatus.READY.value
        asset.error = ""

    scenes = len(result.analysis.scenes)
    ctx.log(f"Ready: {result.probe.duration:.1f}s, {scenes} shot(s) detected")
    return {
        "asset_id": asset_id,
        "kind": result.kind.value,
        "duration": result.probe.duration,
        "scenes": scenes,
        "highlights": len(result.analysis.highlights),
        "has_audio": bool(result.probe.audio),
    }


@register("analyze")
def handle_analyze(ctx: JobContext) -> dict:
    """Re-run scene/audio analysis on an already-ingested asset."""
    from ..media.analyze import analyze_audio, analyze_video
    from ..schema.media import ProbeResult

    asset_id = ctx.payload["asset_id"]
    storage = get_storage()
    with session_scope() as db:
        asset = db.get(Asset, asset_id)
        if asset is None:
            raise ValueError(f"Asset {asset_id} no longer exists.")
        key = asset.proxy_key or asset.audio_key or asset.original_key
        probe_result = ProbeResult(**(asset.probe or {}))
        kind = asset.kind

    ctx.progress(0.1, "analysing")
    path = storage.local_path(key)
    analysis = (analyze_audio(path, probe_result) if kind == "audio"
                else analyze_video(path, probe_result))
    analysis.analyzed_at = datetime.now(timezone.utc).isoformat()

    ctx.progress(0.95, "saving")
    with session_scope() as db:
        asset = db.get(Asset, asset_id)
        asset.analysis = analysis.model_dump()
    return {"asset_id": asset_id, "scenes": len(analysis.scenes)}


def _render_job(ctx: JobContext, is_export: bool) -> dict:
    project_id = ctx.project_id
    storage = get_storage()

    with session_scope() as db:
        project = db.get(Project, project_id)
        if project is None:
            raise ValueError(f"Project {project_id} no longer exists.")
        document = ProjectDocument(**(project.document or {}))
        project_name = project.name

    sequence_id = ctx.payload.get("sequence_id") or document.active_sequence_id
    sequence = document.sequence(sequence_id)

    preset_key = ctx.payload.get("preset")
    if preset_key and preset_key in EXPORT_PRESETS:
        rs = preset_settings(preset_key, sequence)
    else:
        rs = RenderSettings(
            width=int(ctx.payload.get("width") or sequence.width),
            height=int(ctx.payload.get("height") or sequence.height),
            fps=float(ctx.payload.get("fps") or sequence.fps),
            crf=int(ctx.payload.get("crf", 20 if is_export else 26)),
            preset=str(ctx.payload.get("encoder_preset",
                                       "medium" if is_export else "veryfast")),
            quality="final" if is_export else "draft",
        )
    if ctx.payload.get("quality"):
        rs.quality = str(ctx.payload["quality"])

    safe_name = "".join(
        c for c in (ctx.payload.get("filename") or project_name)
        if c.isalnum() or c in " -_"
    ).strip() or "render"
    filename = f"{safe_name}_{sequence.name}_{rs.width}x{rs.height}.mp4"
    key = StorageKey.render(project_id, ctx.job_id, filename)
    output = storage.stage_output(key)

    ctx.set_status(JobStatus.RENDERING)
    ctx.log(
        f"Rendering '{sequence.name}' at {rs.width}x{rs.height} "
        f"({'originals' if rs.quality == 'final' else 'proxies'})"
    )

    with session_scope() as db:
        outcome = render_sequence(
            db=db, project_id=project_id, document=document,
            sequence_id=sequence_id, output_path=output, storage=storage,
            render_settings=rs, report=lambda p, s: ctx.progress(p, s),
        )

    storage.commit_output(key, output)
    for warning in outcome.warnings:
        ctx.log(f"Warning: {warning}")
    ctx.log(
        f"Done in {outcome.seconds_taken}s -- "
        f"{outcome.size_bytes / 1_000_000:.1f} MB"
    )

    return {
        "storage_key": key,
        "url": storage.url(key),
        "filename": filename,
        "duration": outcome.duration,
        "size_bytes": outcome.size_bytes,
        "seconds_taken": outcome.seconds_taken,
        "resolution": f"{rs.width}x{rs.height}",
        "warnings": outcome.warnings,
        "sequence_id": sequence.id,
    }


@register("render")
def handle_render(ctx: JobContext) -> dict:
    """A fast proxy-quality render, used for review inside the editor."""
    return _render_job(ctx, is_export=False)


@register("export")
def handle_export(ctx: JobContext) -> dict:
    """A final-quality render from the original media."""
    return _render_job(ctx, is_export=True)
