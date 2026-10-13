"""Signal analysis: scenes, motion, loudness, silence, waveforms.

Everything here is deterministic DSP -- no model calls. It produces the
compact structured index that the AI Director reasons over instead of
re-watching a 30-minute video on every request.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
from pathlib import Path

from ..config import settings
from ..schema.media import BeatAnalysis, MediaAnalysis, ProbeResult, Scene
from .ffmpeg import FFmpegError, run_ffmpeg

# Frames-per-second at which we sample a video for cheap visual statistics.
SAMPLE_FPS = 2.0
SAMPLE_W, SAMPLE_H = 64, 36

_SHOWINFO_PTS = re.compile(r"pts_time:([0-9.]+)")
_SILENCE_START = re.compile(r"silence_start:\s*(-?[0-9.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[0-9.]+)")
_LUFS = re.compile(r"^\s*I:\s*(-?[0-9.]+)\s*LUFS", re.M)
_PEAK = re.compile(r"^\s*Peak:\s*(-?[0-9.]+)\s*dBFS", re.M)


# ---------------------------------------------------------------------------
# Scene detection
# ---------------------------------------------------------------------------

def detect_scene_cuts(path: Path, threshold: float = 0.28) -> list[float]:
    """Return timestamps (seconds) where a shot change occurs."""
    argv = [
        settings.ffmpeg_path, "-hide_banner", "-nostdin",
        "-i", str(path),
        "-filter:v", f"select='gt(scene,{threshold})',showinfo",
        "-an", "-f", "null", "-",
    ]
    try:
        stderr = run_ffmpeg(argv)
    except FFmpegError:
        return []
    times = [float(m) for m in _SHOWINFO_PTS.findall(stderr)]
    return sorted(t for t in times if t > 0.05)


def build_scenes(duration: float, cuts: list[float], min_len: float = 0.6) -> list[Scene]:
    """Turn cut points into contiguous, non-degenerate scenes."""
    if duration <= 0:
        return []
    bounds = [0.0]
    for t in cuts:
        if t - bounds[-1] >= min_len and t < duration - 0.1:
            bounds.append(t)
    bounds.append(duration)

    scenes: list[Scene] = []
    for i in range(len(bounds) - 1):
        scenes.append(Scene(index=i, start=round(bounds[i], 3), end=round(bounds[i + 1], 3)))
    return scenes


# ---------------------------------------------------------------------------
# Visual statistics
# ---------------------------------------------------------------------------

def sample_frames(path: Path, duration: float) -> list[tuple[float, bytes]]:
    """Decode the video once at low resolution/framerate for cheap stats."""
    if duration <= 0:
        return []
    argv = [
        settings.ffmpeg_path, "-hide_banner", "-nostdin", "-v", "error",
        "-i", str(path),
        "-vf", f"fps={SAMPLE_FPS},scale={SAMPLE_W}:{SAMPLE_H}",
        "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
    ]
    proc = subprocess.run(argv, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        return []
    frame_size = SAMPLE_W * SAMPLE_H * 3
    raw = proc.stdout
    frames: list[tuple[float, bytes]] = []
    for i in range(len(raw) // frame_size):
        ts = i / SAMPLE_FPS
        frames.append((ts, raw[i * frame_size:(i + 1) * frame_size]))
    return frames


def _frame_stats(buf: bytes) -> tuple[float, float, float]:
    """(mean luma 0..1, luma std-dev 0..1, colorfulness 0..1)."""
    n = len(buf) // 3
    if n == 0:
        return 0.0, 0.0, 0.0
    total = 0
    total_sq = 0
    rg_sum = 0
    yb_sum = 0
    for i in range(0, len(buf), 3):
        r, g, b = buf[i], buf[i + 1], buf[i + 2]
        luma = (r * 299 + g * 587 + b * 114) // 1000
        total += luma
        total_sq += luma * luma
        rg_sum += abs(r - g)
        yb_sum += abs((r + g) // 2 - b)
    mean = total / n
    var = max(0.0, total_sq / n - mean * mean)
    return mean / 255.0, math.sqrt(var) / 128.0, (rg_sum + yb_sum) / (2 * n * 255.0)


def _frame_diff(a: bytes, b: bytes) -> float:
    """Mean absolute difference of two sampled frames, 0..1."""
    if len(a) != len(b) or not a:
        return 0.0
    step = 3  # every pixel's red channel is a good enough motion proxy
    total = 0
    count = 0
    for i in range(0, len(a), step * 4):
        total += abs(a[i] - b[i])
        count += 1
    return (total / count / 255.0) if count else 0.0


def annotate_scenes(scenes: list[Scene], frames: list[tuple[float, bytes]]) -> None:
    """Fill per-scene visual metrics from the sampled frames, in place."""
    if not frames:
        return
    stats = [(ts, *_frame_stats(buf)) for ts, buf in frames]
    motion = [0.0]
    for i in range(1, len(frames)):
        motion.append(_frame_diff(frames[i - 1][1], frames[i][1]))

    for scene in scenes:
        idx = [i for i, (ts, *_ ) in enumerate(stats) if scene.start <= ts < scene.end]
        if not idx:
            nearest = min(range(len(stats)), key=lambda i: abs(stats[i][0] - scene.start))
            idx = [nearest]
        scene.brightness = round(sum(stats[i][1] for i in idx) / len(idx), 4)
        scene.contrast = round(sum(stats[i][2] for i in idx) / len(idx), 4)
        scene.colorfulness = round(sum(stats[i][3] for i in idx) / len(idx), 4)
        scene.motion_score = round(sum(motion[i] for i in idx) / len(idx), 4)

        # Quality: penalise crushed blacks, blown highlights and flat frames.
        exposure = 1.0 - abs(scene.brightness - 0.46) * 2.0
        detail = min(1.0, scene.contrast * 2.2)
        scene.quality_score = round(
            max(0.0, min(1.0, 0.55 * max(0.0, exposure) + 0.45 * detail)), 4
        )
        # Energy: motion plus colour, which is what "exciting shot" looks like
        # to a signal-level heuristic.
        scene.energy_score = round(
            max(0.0, min(1.0, scene.motion_score * 3.2 + scene.colorfulness * 0.8)), 4
        )


# ---------------------------------------------------------------------------
# Audio analysis
# ---------------------------------------------------------------------------

def detect_silence(path: Path, noise_db: float = -32.0, min_dur: float = 0.45
                   ) -> list[dict[str, float]]:
    argv = [
        settings.ffmpeg_path, "-hide_banner", "-nostdin", "-i", str(path),
        "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}",
        "-vn", "-f", "null", "-",
    ]
    try:
        stderr = run_ffmpeg(argv)
    except FFmpegError:
        return []
    starts = [float(x) for x in _SILENCE_START.findall(stderr)]
    ends = [float(x) for x in _SILENCE_END.findall(stderr)]
    out: list[dict[str, float]] = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else None
        if e is not None and e > s:
            out.append({"start": round(max(0.0, s), 3), "end": round(e, 3)})
    return out


def measure_loudness(path: Path) -> tuple[float | None, float | None]:
    argv = [
        settings.ffmpeg_path, "-hide_banner", "-nostdin", "-i", str(path),
        "-af", "ebur128=peak=true", "-vn", "-f", "null", "-",
    ]
    try:
        stderr = run_ffmpeg(argv)
    except FFmpegError:
        return None, None
    lufs = _LUFS.findall(stderr)
    peak = _PEAK.findall(stderr)
    return (float(lufs[-1]) if lufs else None, float(peak[-1]) if peak else None)


def build_waveform(path: Path, duration: float, buckets: int = 2000) -> dict:
    """Peak envelope for the timeline, computed from decoded PCM."""
    if duration <= 0:
        return {"peaks": [], "duration": 0.0}
    rate = 8000
    argv = [
        settings.ffmpeg_path, "-hide_banner", "-nostdin", "-v", "error",
        "-i", str(path), "-vn", "-ac", "1", "-ar", str(rate),
        "-f", "s16le", "-acodec", "pcm_s16le", "-",
    ]
    proc = subprocess.run(argv, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        return {"peaks": [], "duration": duration}

    raw = proc.stdout
    total_samples = len(raw) // 2
    if total_samples == 0:
        return {"peaks": [], "duration": duration}
    buckets = max(1, min(buckets, total_samples))
    per = max(1, total_samples // buckets)

    peaks: list[float] = []
    mv = memoryview(raw)
    for b in range(buckets):
        lo = b * per * 2
        hi = min(len(raw), lo + per * 2)
        if lo >= hi:
            break
        chunk = mv[lo:hi]
        peak = 0
        # Stride so very long files stay fast; peak survives subsampling.
        step = 2 if per <= 512 else 2 * (per // 512)
        for i in range(0, len(chunk) - 1, step):
            val = int.from_bytes(chunk[i:i + 2], "little", signed=True)
            if val < 0:
                val = -val
            if val > peak:
                peak = val
        peaks.append(round(peak / 32768.0, 4))
    return {"peaks": peaks, "duration": duration, "sample_rate": rate}


# ---------------------------------------------------------------------------
# Highlights
# ---------------------------------------------------------------------------

def rank_highlights(scenes: list[Scene], limit: int = 20) -> list[dict]:
    """Score scenes as edit candidates: quality, energy and usable length."""
    ranked = []
    for s in scenes:
        length_fit = 1.0 if 1.2 <= s.duration <= 8.0 else (
            0.55 if s.duration < 1.2 else max(0.25, 8.0 / max(s.duration, 0.01))
        )
        score = 0.45 * s.quality_score + 0.35 * s.energy_score + 0.20 * length_fit
        ranked.append(
            {
                "scene_id": s.id,
                "start": s.start,
                "end": s.end,
                "score": round(score, 4),
                "reason": _highlight_reason(s),
            }
        )
    ranked.sort(key=lambda r: r["score"], reverse=True)
    return ranked[:limit]


def _highlight_reason(s: Scene) -> str:
    bits = []
    if s.energy_score > 0.55:
        bits.append("high motion")
    elif s.energy_score < 0.15:
        bits.append("static")
    if s.quality_score > 0.7:
        bits.append("clean exposure")
    elif s.quality_score < 0.35:
        bits.append("weak exposure")
    if s.brightness < 0.2:
        bits.append("dark")
    elif s.brightness > 0.75:
        bits.append("bright")
    return ", ".join(bits) or "neutral"


def analyze_video(path: Path, probe_result: ProbeResult) -> MediaAnalysis:
    """Full deterministic analysis pass for a video asset."""
    duration = probe_result.duration
    cuts = detect_scene_cuts(path)
    scenes = build_scenes(duration, cuts)
    frames = sample_frames(path, duration)
    annotate_scenes(scenes, frames)

    analysis = MediaAnalysis(scenes=scenes, highlights=rank_highlights(scenes))
    if probe_result.audio:
        analysis.silences = detect_silence(path)
        lufs, peak = measure_loudness(path)
        analysis.loudness_lufs = lufs
        analysis.peak_db = peak
        analysis.beats = _beat_analysis(path, duration)
    return analysis


def _beat_analysis(path: Path, duration: float) -> BeatAnalysis | None:
    """Tempo and beat grid. Returns None when the audio has no clear pulse."""
    from .beats import analyze_beats

    grid = analyze_beats(path, duration)
    if grid.bpm <= 0:
        return None
    return BeatAnalysis(**grid.to_dict())


def analyze_audio(path: Path, probe_result: ProbeResult) -> MediaAnalysis:
    analysis = MediaAnalysis()
    analysis.silences = detect_silence(path)
    lufs, peak = measure_loudness(path)
    analysis.loudness_lufs = lufs
    analysis.peak_db = peak
    analysis.beats = _beat_analysis(path, probe_result.duration)
    return analysis
