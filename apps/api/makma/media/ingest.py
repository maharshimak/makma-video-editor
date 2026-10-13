"""Media ingestion pipeline.

Originals are immutable. Ingestion derives everything the editor needs --
proxy, thumbnail, waveform, scene index -- and never touches the source file.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..config import settings
from ..schema.media import AssetKind, MediaAnalysis, ProbeResult
from ..storage.base import Storage, StorageKey
from .analyze import analyze_audio, analyze_video
from .ffmpeg import FFmpegCommand, FFmpegError
from .probe import classify, probe

log = logging.getLogger("makma.ingest")

Reporter = Callable[[float, str], None]

#: Extensions that promise a decodable stream. Anything here that probes with
#: no video and no audio is a broken file, not an unknown one.
MEDIA_EXTS = {
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpg", ".mpeg", ".mts",
    ".m2ts", ".wmv", ".flv", ".mxf",
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".aiff", ".wma",
}


class UnreadableMedia(ValueError):
    """An upload that FFmpeg cannot decode. The message is shown to the user."""


def _noop(_p: float, _s: str) -> None:
    return None


@dataclass
class IngestResult:
    kind: AssetKind
    probe: ProbeResult
    analysis: MediaAnalysis
    checksum: str
    proxy_key: str = ""
    audio_key: str = ""
    thumbnail_key: str = ""
    waveform_key: str = ""


def checksum_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _even(value: int) -> int:
    """H.264 requires even dimensions."""
    return value if value % 2 == 0 else value - 1


def build_proxy(src: Path, dest: Path, probe_result: ProbeResult,
                report: Reporter = _noop) -> None:
    """Create the 720p editing proxy the browser actually plays.

    Proxies are keyframe-dense and faststart so scrubbing is responsive;
    quality is irrelevant here because final renders use the original.
    """
    video = probe_result.primary_video
    height = min(settings.proxy_height, video.height if video and video.height else settings.proxy_height)
    height = max(2, _even(height))

    cmd = FFmpegCommand(expected_duration=probe_result.duration)
    cmd.add_input(src)
    cmd.filter_complex = None
    opts = [
        "-vf", f"scale=-2:{height}:flags=bicubic",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", str(settings.proxy_crf),
        "-pix_fmt", "yuv420p",
        # A keyframe every 0.5s: seeking in the browser stays snappy.
        "-g", "15", "-keyint_min", "15", "-sc_threshold", "0",
        "-movflags", "+faststart",
    ]
    if probe_result.audio:
        opts += ["-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "48000"]
    else:
        opts += ["-an"]
    cmd.add_output(dest, *opts)
    cmd.run(on_progress=lambda p, s: report(p, "proxy"))


def build_audio_proxy(src: Path, dest: Path, probe_result: ProbeResult,
                      report: Reporter = _noop) -> None:
    cmd = FFmpegCommand(expected_duration=probe_result.duration)
    cmd.add_input(src)
    cmd.add_output(
        dest, "-vn", "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
        "-movflags", "+faststart",
    )
    cmd.run(on_progress=lambda p, s: report(p, "audio"))


def build_thumbnail(src: Path, dest: Path, at: float = 0.0, width: int | None = None) -> None:
    width = width or settings.thumbnail_width
    cmd = FFmpegCommand()
    # Seek before -i for a fast keyframe seek; clamp to 0 for short media.
    cmd.add_input(src, "-ss", f"{max(0.0, at):.3f}")
    cmd.add_output(
        dest, "-frames:v", "1", "-vf", f"scale={width}:-2:flags=bicubic",
        "-q:v", "4",
    )
    cmd.run()


def build_filmstrip(src: Path, dest: Path, duration: float, count: int = 40,
                    tile_width: int = 160) -> None:
    """A horizontal strip of frames used as the clip background on the timeline."""
    if duration <= 0:
        return
    fps = max(0.02, count / duration)
    cmd = FFmpegCommand()
    cmd.add_input(src)
    cmd.add_output(
        dest,
        "-vf", f"fps={fps},scale={tile_width}:-2:flags=bicubic,tile={count}x1",
        "-frames:v", "1", "-q:v", "5",
    )
    cmd.run()


def ingest_file(
    local_path: Path,
    project_id: str,
    asset_id: str,
    storage: Storage,
    report: Reporter = _noop,
    deep_analysis: bool = True,
) -> IngestResult:
    """Run the full ingestion pipeline for one uploaded file."""
    report(0.02, "checksum")
    digest = checksum_file(local_path)

    report(0.08, "probe")
    probe_result = probe(local_path)
    kind = classify(local_path, probe_result)

    # A file whose extension promises media but that holds no readable stream
    # is broken, not an "other" asset. Letting it through as ready puts a row
    # in the media panel that can never reach the timeline, which reads to the
    # user as the upload having silently vanished.
    if kind == AssetKind.OTHER and local_path.suffix.lower() in MEDIA_EXTS:
        raise UnreadableMedia(
            f"'{local_path.name}' could not be decoded: FFmpeg found no video "
            "or audio stream in it. The file may be incomplete, still copying, "
            "or in a container this build cannot read."
        )

    result = IngestResult(
        kind=kind, probe=probe_result, analysis=MediaAnalysis(), checksum=digest
    )

    if kind in (AssetKind.LUT, AssetKind.FONT, AssetKind.OTHER):
        report(1.0, "done")
        return result

    if kind == AssetKind.IMAGE:
        report(0.4, "thumbnail")
        thumb_key = StorageKey.thumbnail(project_id, asset_id)
        thumb_path = storage.stage_output(thumb_key)
        build_thumbnail(local_path, thumb_path, at=0.0)
        storage.commit_output(thumb_key, thumb_path)
        result.thumbnail_key = thumb_key
        report(1.0, "done")
        return result

    if kind == AssetKind.AUDIO:
        report(0.15, "audio proxy")
        audio_key = StorageKey.audio_proxy(project_id, asset_id)
        audio_path = storage.stage_output(audio_key)
        build_audio_proxy(local_path, audio_path, probe_result,
                          lambda p, s: report(0.15 + p * 0.35, s))
        storage.commit_output(audio_key, audio_path)
        result.audio_key = audio_key

        report(0.55, "waveform")
        result.waveform_key = _write_waveform(
            local_path, project_id, asset_id, storage, probe_result.duration
        )
        if deep_analysis:
            report(0.8, "analysis")
            result.analysis = analyze_audio(local_path, probe_result)
        result.analysis.analyzed_at = datetime.now(timezone.utc).isoformat()
        report(1.0, "done")
        return result

    # --- video -----------------------------------------------------------
    report(0.12, "proxy")
    proxy_key = StorageKey.proxy(project_id, asset_id)
    proxy_path = storage.stage_output(proxy_key)
    build_proxy(local_path, proxy_path, probe_result,
                lambda p, s: report(0.12 + p * 0.38, "proxy"))
    storage.commit_output(proxy_key, proxy_path)
    result.proxy_key = proxy_key

    report(0.52, "thumbnail")
    thumb_key = StorageKey.thumbnail(project_id, asset_id)
    thumb_path = storage.stage_output(thumb_key)
    poster_at = min(1.0, probe_result.duration * 0.1) if probe_result.duration else 0.0
    try:
        build_thumbnail(proxy_path, thumb_path, at=poster_at)
        storage.commit_output(thumb_key, thumb_path)
        result.thumbnail_key = thumb_key
    except FFmpegError as exc:
        log.warning("thumbnail failed for %s: %s", asset_id, exc)

    try:
        strip_key = f"thumbnails/{project_id}/{asset_id}/filmstrip.jpg"
        strip_path = storage.stage_output(strip_key)
        build_filmstrip(proxy_path, strip_path, probe_result.duration)
        storage.commit_output(strip_key, strip_path)
    except FFmpegError as exc:
        log.warning("filmstrip failed for %s: %s", asset_id, exc)

    if probe_result.audio:
        report(0.62, "waveform")
        result.waveform_key = _write_waveform(
            proxy_path, project_id, asset_id, storage, probe_result.duration
        )

    if deep_analysis:
        report(0.72, "scene analysis")
        # Analysis runs on the proxy: 720p is plenty for shot detection and
        # exposure statistics, and it is an order of magnitude faster.
        result.analysis = analyze_video(proxy_path, probe_result)
        _write_scene_thumbnails(proxy_path, result, project_id, asset_id, storage)

    result.analysis.analyzed_at = datetime.now(timezone.utc).isoformat()
    report(1.0, "done")
    return result


def _write_waveform(path: Path, project_id: str, asset_id: str,
                    storage: Storage, duration: float) -> str:
    import json

    from .analyze import build_waveform

    data = build_waveform(path, duration)
    key = StorageKey.waveform(project_id, asset_id)
    storage.write_bytes(key, json.dumps(data).encode())
    return key


def _write_scene_thumbnails(proxy: Path, result: IngestResult, project_id: str,
                            asset_id: str, storage: Storage, limit: int = 60) -> None:
    """One poster frame per scene, capped so long videos stay cheap."""
    scenes = result.analysis.scenes[:limit]
    for scene in scenes:
        key = f"thumbnails/{project_id}/{asset_id}/scene_{scene.index:04d}.jpg"
        try:
            dest = storage.stage_output(key)
            build_thumbnail(proxy, dest, at=scene.start + min(0.4, scene.duration / 2),
                            width=240)
            storage.commit_output(key, dest)
            scene.thumbnail = key
        except FFmpegError:
            continue
