"""Render service: project + settings -> a real MP4 on disk."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from sqlalchemy.orm import Session

from ..models import Asset
from ..schema.core import ProjectDocument, Sequence
from ..storage.base import Storage
from .compiler import AssetSource, CompiledRender, RenderSettings, compile_sequence

log = logging.getLogger("makma.render")

Reporter = Callable[[float, str], None]


def _noop(_p: float, _s: str) -> None:
    return None


# --------------------------------------------------------------------------
# Export presets
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ExportPreset:
    key: str
    label: str
    width: int
    height: int
    fps: float
    crf: int
    preset: str
    audio_bitrate: str
    note: str = ""


EXPORT_PRESETS: dict[str, ExportPreset] = {
    p.key: p
    for p in [
        ExportPreset("master_1080p", "Master 1080p", 1920, 1080, 30, 18, "slow",
                     "256k", "High-quality horizontal master."),
        ExportPreset("master_4k", "Master 4K", 3840, 2160, 30, 18, "slow", "256k",
                     "Full-resolution master; slowest to encode."),
        ExportPreset("youtube_1080p", "YouTube 1080p", 1920, 1080, 30, 20, "medium",
                     "192k", "Balanced for YouTube's re-encode."),
        ExportPreset("tiktok", "TikTok 1080x1920", 1080, 1920, 30, 21, "medium",
                     "160k", "Vertical 9:16."),
        ExportPreset("reel", "Instagram Reel", 1080, 1920, 30, 21, "medium", "160k",
                     "Vertical 9:16."),
        ExportPreset("instagram_feed", "Instagram Feed 1:1", 1080, 1080, 30, 21,
                     "medium", "160k", "Square."),
        ExportPreset("linkedin", "LinkedIn 1080p", 1920, 1080, 30, 22, "medium",
                     "160k", "Horizontal, smaller file."),
        ExportPreset("x_720p", "X / Twitter 720p", 1280, 720, 30, 23, "fast", "128k",
                     "Small and fast."),
        ExportPreset("draft_540p", "Draft 540p", 960, 540, 30, 26, "veryfast", "96k",
                     "Quick look; uses proxies."),
    ]
}


def preset_settings(key: str, sequence: Sequence) -> RenderSettings:
    preset = EXPORT_PRESETS.get(key)
    if preset is None:
        return RenderSettings(
            width=sequence.width, height=sequence.height, fps=sequence.fps
        )
    return RenderSettings(
        width=preset.width,
        height=preset.height,
        fps=preset.fps,
        crf=preset.crf,
        preset=preset.preset,
        audio_bitrate=preset.audio_bitrate,
        quality="draft" if key == "draft_540p" else "final",
    )


# --------------------------------------------------------------------------
# Asset resolution
# --------------------------------------------------------------------------

def resolve_assets(
    db: Session,
    project_id: str,
    storage: Storage,
    use_proxy: bool,
    only: Iterable[str] | None = None,
) -> dict[str, AssetSource]:
    """Map asset ids to files FFmpeg can read.

    Draft renders read the 720p proxy; final renders read the original. If a
    proxy is missing the original is used, never a silent skip.
    """
    query = db.query(Asset).filter(Asset.project_id == project_id)
    if only is not None:
        ids = list(only)
        if not ids:
            return {}
        query = query.filter(Asset.id.in_(ids))

    sources: dict[str, AssetSource] = {}
    for asset in query.all():
        key = ""
        if use_proxy and asset.proxy_key and storage.exists(asset.proxy_key):
            key = asset.proxy_key
        elif use_proxy and asset.audio_key and storage.exists(asset.audio_key):
            key = asset.audio_key
        elif asset.original_key and storage.exists(asset.original_key):
            key = asset.original_key
        elif asset.proxy_key and storage.exists(asset.proxy_key):
            key = asset.proxy_key
        if not key:
            continue
        sources[asset.id] = AssetSource(
            asset_id=asset.id,
            path=storage.local_path(key),
            kind=asset.kind,
            width=asset.width,
            height=asset.height,
            fps=asset.fps,
            duration=asset.duration,
            has_audio=bool(asset.has_audio),
        )
    return sources


# --------------------------------------------------------------------------
# Render
# --------------------------------------------------------------------------

@dataclass
class RenderOutcome:
    path: Path
    duration: float
    seconds_taken: float
    warnings: list[str]
    command: str
    size_bytes: int


def render_sequence(
    db: Session,
    project_id: str,
    document: ProjectDocument,
    sequence_id: str | None,
    output_path: Path,
    storage: Storage,
    render_settings: RenderSettings | None = None,
    report: Reporter = _noop,
) -> RenderOutcome:
    sequence = document.sequence(sequence_id)
    rs = render_settings or RenderSettings(
        width=sequence.width, height=sequence.height, fps=sequence.fps
    )
    use_proxy = rs.quality in ("draft", "preview")

    report(0.02, "resolving media")
    used_ids = {c.asset_id for c in sequence.all_clips() if c.asset_id}
    assets = resolve_assets(db, project_id, storage, use_proxy, only=used_ids)

    missing = used_ids - set(assets)
    report(0.05, "compiling graph")
    compiled: CompiledRender = compile_sequence(sequence, assets, rs, output_path)
    if missing:
        compiled.warnings.append(
            f"{len(missing)} clip source(s) were unavailable and were skipped."
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    log.info("render %s -> %s", project_id, output_path.name)
    log.debug("filter_complex: %s", compiled.command.filter_complex)

    started = time.time()
    try:
        compiled.command.run(
            on_progress=lambda p, _s: report(0.05 + p * 0.94, "rendering")
        )
    finally:
        compiled.cleanup()

    report(1.0, "complete")
    return RenderOutcome(
        path=output_path,
        duration=compiled.duration,
        seconds_taken=round(time.time() - started, 2),
        warnings=compiled.warnings,
        command=compiled.command.to_string(),
        size_bytes=output_path.stat().st_size if output_path.exists() else 0,
    )


def estimate_render(document: ProjectDocument, sequence_id: str | None,
                    rs: RenderSettings) -> dict:
    """Rough wall-clock estimate, used by the plan preview and job UI."""
    sequence = document.sequence(sequence_id)
    duration = sequence.duration
    clips = [c for c in sequence.all_clips() if c.enabled]
    effects = sum(len(c.effects) for c in clips)

    pixels = rs.width * rs.height
    base = duration * (pixels / (1920 * 1080)) * 0.55
    preset_cost = {"veryfast": 0.5, "fast": 0.7, "medium": 1.0,
                   "slow": 1.9, "slower": 3.0}.get(rs.preset, 1.0)
    seconds = base * preset_cost + effects * duration * 0.05 + len(clips) * 0.12
    return {
        "seconds": round(max(1.0, seconds), 1),
        "duration": round(duration, 2),
        "clips": len(clips),
        "effects": effects,
        "resolution": f"{rs.width}x{rs.height}",
        "tier": "heavy" if seconds > 120 else "moderate" if seconds > 25 else "light",
    }
