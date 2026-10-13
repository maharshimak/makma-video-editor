"""Media inspection: ffprobe output normalized into ProbeResult."""
from __future__ import annotations

from pathlib import Path

from ..schema.media import AssetKind, AudioStreamInfo, ProbeResult, VideoStreamInfo
from .ffmpeg import run_ffprobe

IMAGE_FORMATS = {
    "image2", "png_pipe", "jpeg_pipe", "webp_pipe", "gif", "bmp_pipe",
    "tiff_pipe", "svg_pipe",
}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif"}
LUT_EXTS = {".cube", ".3dl"}
FONT_EXTS = {".ttf", ".otf", ".woff", ".woff2"}


def _fraction(value: str | None) -> float:
    if not value:
        return 0.0
    if "/" in value:
        num, _, den = value.partition("/")
        try:
            n, d = float(num), float(den)
            return n / d if d else 0.0
        except ValueError:
            return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def _rotation(stream: dict) -> int:
    for entry in stream.get("side_data_list", []) or []:
        if "rotation" in entry:
            try:
                return int(float(entry["rotation"])) % 360
            except (TypeError, ValueError):
                pass
    tags = stream.get("tags") or {}
    try:
        return int(float(tags.get("rotate", 0))) % 360
    except (TypeError, ValueError):
        return 0


def probe(path: str | Path) -> ProbeResult:
    data = run_ffprobe(["-show_format", "-show_streams", str(path)])
    fmt = data.get("format", {}) or {}
    streams = data.get("streams", []) or []

    result = ProbeResult(
        format_name=fmt.get("format_name", ""),
        duration=float(fmt.get("duration") or 0.0),
        size=int(float(fmt.get("size") or 0)),
        bit_rate=int(float(fmt["bit_rate"])) if fmt.get("bit_rate") else None,
        tags={str(k): str(v) for k, v in (fmt.get("tags") or {}).items()},
    )

    for stream in streams:
        codec_type = stream.get("codec_type")
        duration = float(stream.get("duration") or result.duration or 0.0)
        if codec_type == "video":
            pix_fmt = stream.get("pix_fmt") or ""
            result.video.append(
                VideoStreamInfo(
                    index=int(stream.get("index", 0)),
                    codec=stream.get("codec_name", ""),
                    width=int(stream.get("width") or 0),
                    height=int(stream.get("height") or 0),
                    fps=_fraction(stream.get("avg_frame_rate"))
                    or _fraction(stream.get("r_frame_rate")),
                    duration=duration,
                    bit_rate=int(float(stream["bit_rate"])) if stream.get("bit_rate") else None,
                    pix_fmt=pix_fmt,
                    rotation=_rotation(stream),
                    has_alpha="a" in pix_fmt.replace("yuva", "a").replace("rgba", "a"),
                )
            )
        elif codec_type == "audio":
            result.audio.append(
                AudioStreamInfo(
                    index=int(stream.get("index", 0)),
                    codec=stream.get("codec_name", ""),
                    sample_rate=int(stream.get("sample_rate") or 0),
                    channels=int(stream.get("channels") or 0),
                    duration=duration,
                    bit_rate=int(float(stream["bit_rate"])) if stream.get("bit_rate") else None,
                )
            )

    # Images report no container duration; give them a nominal one.
    if result.duration <= 0 and result.video:
        result.duration = max((v.duration for v in result.video), default=0.0)
    return result


def classify(path: str | Path, result: ProbeResult) -> AssetKind:
    """Decide what kind of asset this is from extension + stream layout."""
    ext = Path(path).suffix.lower()
    if ext in LUT_EXTS:
        return AssetKind.LUT
    if ext in FONT_EXTS:
        return AssetKind.FONT
    if ext in IMAGE_EXTS or result.format_name in IMAGE_FORMATS:
        # An animated GIF/WebP is still usable as video, but a single frame
        # is an image; frame count is the honest discriminator.
        if result.duration > 0.5 and result.video and result.video[0].fps > 1:
            return AssetKind.VIDEO
        return AssetKind.IMAGE
    if result.video:
        return AssetKind.VIDEO
    if result.audio:
        return AssetKind.AUDIO
    return AssetKind.OTHER
