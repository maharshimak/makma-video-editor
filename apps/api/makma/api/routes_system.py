"""System capability routes.

The UI reads this to decide what to show as available and what to label
"coming next", so the product never advertises a feature this build cannot
actually perform.
"""
from __future__ import annotations

from fastapi import APIRouter

from ..ai.router import Capability, get_router
from ..config import settings
from ..media.ffmpeg import ffmpeg_available
from ..render.effects import GRADE_PRESETS, describe_registry
from ..render.fonts import available_families
from ..render.renderer import EXPORT_PRESETS
from ..schema.core import TransitionType
from ..schema.operations import IMPLEMENTED_OPS, OpType

router = APIRouter(prefix="/api/system", tags=["system"])

#: Features on the roadmap, surfaced so the UI can show them honestly.
PLANNED_FEATURES = [
    ("transcription", "Speech transcription with word timestamps", "V2"),
    ("transcript_editing", "Edit the video by editing the transcript", "V2"),
    ("segmentation", "Subject & object segmentation (SAM-family)", "V3"),
    ("tracking", "Video object tracking and tracked graphics", "V3"),
    ("object_removal", "Object removal / generative fill", "V4"),
    ("background_replace", "Background replacement", "V4"),
    ("generative_video", "Generative B-roll and shot extension", "V4"),
    ("relight", "Relighting", "V4"),
    ("captions", "Automatic transcription and animated captions", "V2"),
    ("beat_sync", "Beat detection and music-synced cutting", "V2"),
    ("semantic_search", "Semantic search across the project", "V2"),
    ("qc_agent", "Quality-control review pass on rendered drafts", "V5"),
    ("inspiration", "Trend research and Style DNA extraction", "V6"),
    ("branches", "Project branches and A/B creative variants", "V6"),
    ("localization", "Subtitle, graphic and voice localisation", "V6"),
    ("interchange", "OpenTimelineIO / XML / EDL / AAF interchange", "V7"),
    ("collaboration", "Comments, reviewer roles and approvals", "V7"),
]


@router.get("/capabilities")
def capabilities() -> dict:
    ok, version = ffmpeg_available()
    model_router = get_router()
    return {
        "app": settings.app_name,
        "environment": settings.environment,
        "media": {
            "ffmpeg": {"available": ok, "version": version,
                       "path": settings.ffmpeg_path},
            "proxy_height": settings.proxy_height,
            "max_upload_mb": settings.max_upload_mb,
        },
        "ai": {
            "planner_mode": settings.planner_mode,
            "llm_available": model_router.has(Capability.REASON),
            "router": model_router.describe(),
            "note": (
                "Natural-language editing works without a model key through the "
                "offline rule planner; a key unlocks open-ended instructions."
            ),
        },
        "shortcuts": {
            "command_palette": "Ctrl/Cmd+K",
            "play_pause": "Space",
            "split": "S",
            "delete": "Delete",
            "snapping": "N",
            "undo": "Ctrl/Cmd+Z",
            "redo": "Ctrl/Cmd+Shift+Z",
            "export": "Ctrl/Cmd+E",
            "step_frame": "Arrow left / right",
        },
        "editing": {
            "operations": sorted(o.value for o in OpType),
            "implemented_operations": sorted(o.value for o in IMPLEMENTED_OPS),
            "transitions": [t.value for t in TransitionType],
            "grade_presets": sorted(GRADE_PRESETS),
            "fonts": available_families(),
        },
        "export_presets": [
            {"key": p.key, "label": p.label, "width": p.width, "height": p.height,
             "fps": p.fps, "note": p.note}
            for p in EXPORT_PRESETS.values()
        ],
        "planned": [
            {"key": k, "label": label, "milestone": m}
            for k, label, m in PLANNED_FEATURES
        ],
    }


@router.get("/effects")
def effects() -> dict:
    return {"effects": describe_registry(), "grade_presets": sorted(GRADE_PRESETS)}


@router.get("/health")
def health() -> dict:
    ok, version = ffmpeg_available()
    return {"status": "ok" if ok else "degraded", "ffmpeg": version}
