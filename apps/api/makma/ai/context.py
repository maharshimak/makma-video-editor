"""Project context assembly for the AI Director.

The LLM never watches the footage. Ingestion already produced a structured
index -- shots, exposure, energy, silences, highlights -- and this module
turns the project plus that index into a compact brief. A 30-minute project
is summarised into a few kilobytes, which is what makes long-form editing
tractable at all.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..schema.core import ClipKind, ProjectDocument, Sequence

MAX_CLIPS_INLINE = 60
MAX_SCENES_PER_ASSET = 14
MAX_HIGHLIGHTS = 12


@dataclass
class AssetBrief:
    id: str
    name: str
    kind: str
    duration: float
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_audio: bool = False
    analysis: dict | None = None


def _round(value: float, places: int = 2) -> float:
    return round(float(value), places)


def summarise_sequence(seq: Sequence) -> dict:
    tracks = []
    for track in sorted(seq.tracks, key=lambda t: (t.kind.value, t.index)):
        clips = []
        for clip in track.sorted_clips()[:MAX_CLIPS_INLINE]:
            entry: dict[str, Any] = {
                "id": clip.id,
                "kind": clip.kind.value,
                "start": _round(clip.start),
                "duration": _round(clip.duration),
            }
            if clip.name:
                entry["name"] = clip.name[:60]
            if clip.asset_id:
                entry["asset_id"] = clip.asset_id
            if abs(clip.speed - 1.0) > 1e-6:
                entry["speed"] = _round(clip.speed)
            if clip.in_point:
                entry["in_point"] = _round(clip.in_point)
            if clip.effects:
                entry["effects"] = [
                    {"id": f.id, "type": f.type,
                     **({"preset": f.parameters["preset"]}
                        if "preset" in f.parameters else {})}
                    for f in clip.effects
                ]
            if clip.kind == ClipKind.TEXT and clip.text:
                entry["text"] = clip.text.text[:60]
            if not clip.enabled:
                entry["enabled"] = False
            if clip.muted:
                entry["muted"] = True
            clips.append(entry)

        summary: dict[str, Any] = {
            "id": track.id,
            "name": track.name,
            "kind": track.kind.value,
            "index": track.index,
            "clip_count": len(track.clips),
            "clips": clips,
        }
        if len(track.clips) > MAX_CLIPS_INLINE:
            summary["truncated"] = len(track.clips) - MAX_CLIPS_INLINE
        for flag in ("muted", "hidden", "locked", "is_adjustment"):
            if getattr(track, flag):
                summary[flag] = True
        tracks.append(summary)

    return {
        "id": seq.id,
        "name": seq.name,
        "resolution": f"{seq.width}x{seq.height}",
        "fps": seq.fps,
        "duration": _round(seq.duration),
        "tracks": tracks,
        "markers": [
            {"time": _round(m.time), "name": m.name, "kind": m.kind}
            for m in sorted(seq.markers, key=lambda m: m.time)[:40]
        ],
    }


def summarise_asset(brief: AssetBrief) -> dict:
    """Compact per-asset brief: shape, then what analysis found in it."""
    out: dict[str, Any] = {
        "id": brief.id,
        "name": brief.name,
        "kind": brief.kind,
        "duration": _round(brief.duration),
    }
    if brief.width:
        out["resolution"] = f"{brief.width}x{brief.height}"
    if brief.fps:
        out["fps"] = round(brief.fps, 2)
    out["has_audio"] = brief.has_audio

    analysis = brief.analysis or {}
    scenes = analysis.get("scenes") or []
    if scenes:
        out["scene_count"] = len(scenes)
        step = max(1, len(scenes) // MAX_SCENES_PER_ASSET)
        out["scenes"] = [
            {
                "i": s.get("index"),
                "start": _round(s.get("start", 0)),
                "end": _round(s.get("end", 0)),
                "quality": s.get("quality_score"),
                "energy": s.get("energy_score"),
                "brightness": s.get("brightness"),
            }
            for s in scenes[::step][:MAX_SCENES_PER_ASSET]
        ]
    highlights = analysis.get("highlights") or []
    if highlights:
        out["highlights"] = [
            {"start": _round(h["start"]), "end": _round(h["end"]),
             "score": h["score"], "why": h.get("reason", "")}
            for h in highlights[:MAX_HIGHLIGHTS]
        ]
    silences = analysis.get("silences") or []
    if silences:
        out["silence_count"] = len(silences)
        out["silence_seconds"] = _round(
            sum(s["end"] - s["start"] for s in silences), 1
        )
        out["silences"] = [
            {"start": _round(s["start"]), "end": _round(s["end"])}
            for s in silences[:12]
        ]
    if analysis.get("loudness_lufs") is not None:
        out["loudness_lufs"] = analysis["loudness_lufs"]
    return out


def build_context(
    document: ProjectDocument,
    assets: list[AssetBrief],
    selection: list[str],
    playhead: float,
    sequence_id: str | None = None,
    memory: dict | None = None,
) -> dict:
    seq = document.sequence(sequence_id)
    selected = []
    for clip_id in selection[:20]:
        hit = seq.find_clip(clip_id)
        if hit:
            _track, clip = hit
            selected.append({
                "id": clip.id, "name": clip.name, "kind": clip.kind.value,
                "start": _round(clip.start), "duration": _round(clip.duration),
            })

    return {
        "project": {
            "name": document.name,
            "sequence_count": len(document.sequences),
            "sequences": [
                {"id": s.id, "name": s.name, "duration": _round(s.duration),
                 "resolution": f"{s.width}x{s.height}"}
                for s in document.sequences
            ],
        },
        "active_sequence": summarise_sequence(seq),
        "assets": [summarise_asset(a) for a in assets],
        "selection": selected,
        "playhead": _round(playhead),
        "creator_memory": memory or {},
    }


def context_size(context: dict) -> int:
    import json

    return len(json.dumps(context))
