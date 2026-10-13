"""Structured edit operations.

Natural language is never the source of truth. Every user instruction --
whether typed into the AI Director or produced by dragging a clip -- is
converted into one of these operations before anything changes. That makes
edits inspectable, replayable, reversible and testable.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from .core import new_id


class OpType(str, Enum):
    # --- timeline structure ---
    ADD_CLIP = "add_clip"
    DELETE_CLIP = "delete_clip"
    MOVE_CLIP = "move_clip"
    TRIM_CLIP = "trim_clip"
    SPLIT_CLIP = "split_clip"
    DUPLICATE_CLIP = "duplicate_clip"
    REMOVE_RANGE = "remove_range"      # ripple delete a time range
    REORDER = "reorder"
    SET_TRACK = "set_track"            # mute / hide / lock / volume

    # --- clip properties ---
    SET_KEYFRAMES = "set_keyframes"
    SET_DURATION = "set_duration"
    SET_SPEED = "set_speed"
    SET_VOLUME = "set_volume"
    SET_TRANSFORM = "set_transform"
    SET_ENABLED = "set_enabled"

    # --- looks ---
    ADD_EFFECT = "add_effect"
    UPDATE_EFFECT = "update_effect"
    REMOVE_EFFECT = "remove_effect"
    SET_TRANSITION = "set_transition"

    # --- content ---
    ADD_TEXT = "add_text"
    ADD_MARKER = "add_marker"

    # --- sequence ---
    SET_FORMAT = "set_format"          # resolution / fps / aspect
    CREATE_SEQUENCE = "create_sequence"

    # --- not yet implemented; the planner may propose these and the UI
    #     renders them as "coming next" rather than silently doing nothing ---
    SEGMENT_SUBJECT = "segment_subject"
    REMOVE_OBJECT = "remove_object"
    REPLACE_BACKGROUND = "replace_background"
    GENERATE_BROLL = "generate_broll"
    RELIGHT = "relight"
    AUTO_CAPTION = "auto_caption"
    BEAT_SYNC = "beat_sync"


#: Operations the render/edit engine can actually execute today.
IMPLEMENTED_OPS: set[str] = {
    OpType.ADD_CLIP, OpType.DELETE_CLIP, OpType.MOVE_CLIP, OpType.TRIM_CLIP,
    OpType.SPLIT_CLIP, OpType.DUPLICATE_CLIP, OpType.REMOVE_RANGE,
    OpType.REORDER, OpType.SET_TRACK, OpType.SET_KEYFRAMES, OpType.SET_DURATION,
    OpType.SET_SPEED,
    OpType.SET_VOLUME,
    OpType.SET_TRANSFORM, OpType.SET_ENABLED, OpType.ADD_EFFECT,
    OpType.UPDATE_EFFECT, OpType.REMOVE_EFFECT, OpType.SET_TRANSITION,
    OpType.ADD_TEXT, OpType.ADD_MARKER, OpType.SET_FORMAT,
    OpType.CREATE_SEQUENCE,
}


class Scope(BaseModel):
    """What an operation applies to.

    Resolution order: explicit clip_ids > track_ids+range > range > selection
    > all. An unresolvable scope is an error, never a silent no-op.
    """
    type: Literal["clip", "track", "range", "selection", "all", "sequence"] = "selection"
    clip_ids: list[str] = Field(default_factory=list)
    track_ids: list[str] = Field(default_factory=list)
    start: float | None = None
    end: float | None = None
    kinds: list[str] = Field(default_factory=list)  # filter by clip kind


class EditOperation(BaseModel):
    id: str = Field(default_factory=lambda: new_id("op"))
    type: OpType
    scope: Scope = Field(default_factory=Scope)
    parameters: dict[str, Any] = Field(default_factory=dict)
    #: Human-readable line shown in the AI edit-plan preview.
    description: str = ""

    @property
    def implemented(self) -> bool:
        return self.type in IMPLEMENTED_OPS


class CostEstimate(BaseModel):
    tier: Literal["instant", "light", "moderate", "heavy"] = "light"
    seconds: float = 0.0
    gpu_seconds: float = 0.0
    usd: float = 0.0
    notes: str = ""


class ExportRequest(BaseModel):
    """A render the plan asks for.

    Exports are not timeline operations -- they do not change project state --
    so they travel beside the operations rather than inside them, and the API
    turns them into real jobs.
    """
    preset: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    quality: str = "final"
    label: str = ""


class EditPlan(BaseModel):
    """The AI Director's answer to a user instruction."""
    id: str = Field(default_factory=lambda: new_id("plan"))
    command: str = ""
    summary: str = ""
    rationale: str = ""
    operations: list[EditOperation] = Field(default_factory=list)
    estimate: CostEstimate = Field(default_factory=CostEstimate)
    #: Renders this instruction asks for, queued after the edits apply.
    exports: list[ExportRequest] = Field(default_factory=list)
    #: Ops the planner wanted but that this build cannot execute yet.
    unsupported: list[str] = Field(default_factory=list)
    #: Whether the UI should require an explicit APPLY click.
    requires_confirmation: bool = False
    planner: str = "heuristic"
    warnings: list[str] = Field(default_factory=list)


class OperationResult(BaseModel):
    operation_id: str
    ok: bool
    message: str = ""
    changed_clip_ids: list[str] = Field(default_factory=list)


class ApplyResult(BaseModel):
    plan_id: str | None = None
    results: list[OperationResult] = Field(default_factory=list)
    version_id: str | None = None

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)
