"""Request/response models for the HTTP API."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, EmailStr, Field

from ..schema.core import ProjectDocument
from ..schema.operations import EditOperation, EditPlan


# --- auth ------------------------------------------------------------------

class SignUpRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    name: str = ""


class SignInRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: "UserOut"


class UserOut(BaseModel):
    id: str
    email: str
    name: str
    preferences: dict = Field(default_factory=dict)


# --- projects --------------------------------------------------------------

class ProjectCreate(BaseModel):
    name: str = "Untitled Project"
    width: int = 1920
    height: int = 1080
    fps: float = 30.0


class ProjectSummary(BaseModel):
    id: str
    name: str
    description: str = ""
    created_at: str
    updated_at: str
    duration: float = 0.0
    asset_count: int = 0
    thumbnail_url: str | None = None


class ProjectOut(BaseModel):
    id: str
    name: str
    description: str = ""
    document: ProjectDocument
    created_at: str
    updated_at: str
    version_id: str | None = None
    #: Monotonic head. Send it back with a write to detect a concurrent edit.
    version: int = 0
    can_undo: bool = False
    can_redo: bool = False


class ProjectUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


class DocumentUpdate(BaseModel):
    """A whole-document save from the editor (drag, trim, property edits)."""
    document: ProjectDocument
    label: str = "Edit"
    #: The version this edit was made against. Omit to force last-write-wins.
    expected_version: int | None = None


# --- assets ----------------------------------------------------------------

class AssetOut(BaseModel):
    id: str
    project_id: str
    name: str
    kind: str
    status: str
    error: str = ""
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_audio: bool = False
    size_bytes: int = 0
    created_at: str
    proxy_url: str | None = None
    thumbnail_url: str | None = None
    filmstrip_url: str | None = None
    waveform_url: str | None = None
    original_url: str | None = None
    scene_count: int = 0
    analysis: dict | None = None


# --- ai --------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str
    selection: list[str] = Field(default_factory=list)
    playhead: float = 0.0
    sequence_id: str | None = None
    #: Apply immediately instead of returning the plan for review.
    auto_apply: bool = False


class ChatResponse(BaseModel):
    message_id: str
    reply: str
    plan: EditPlan
    applied: bool = False
    document: ProjectDocument | None = None
    version_id: str | None = None
    errors: list[str] = Field(default_factory=list)
    #: Render jobs this turn started, so the UI can follow them immediately.
    job_ids: list[str] = Field(default_factory=list)


class ApplyPlanRequest(BaseModel):
    plan: EditPlan
    selection: list[str] = Field(default_factory=list)
    playhead: float = 0.0
    sequence_id: str | None = None
    message_id: str | None = None


class ApplyOperationsRequest(BaseModel):
    """Direct operation application, used by the timeline UI itself."""
    operations: list[EditOperation]
    selection: list[str] = Field(default_factory=list)
    playhead: float = 0.0
    sequence_id: str | None = None
    label: str = "Edit"
    expected_version: int | None = None


class ApplyResponse(BaseModel):
    ok: bool
    document: ProjectDocument
    version_id: str | None = None
    version: int = 0
    errors: list[str] = Field(default_factory=list)
    changed_clip_ids: list[str] = Field(default_factory=list)
    #: Render jobs an approved plan started.
    job_ids: list[str] = Field(default_factory=list)


class ChatMessageOut(BaseModel):
    id: str
    role: str
    content: str
    plan: dict | None = None
    applied: bool = False
    created_at: str


# --- jobs ------------------------------------------------------------------

class RenderRequest(BaseModel):
    sequence_id: str | None = None
    preset: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    crf: int | None = None
    quality: str | None = None       # draft | final
    filename: str | None = None


class JobOut(BaseModel):
    id: str
    type: str
    status: str
    stage: str = ""
    progress: float = 0.0
    project_id: str | None = None
    result: dict = Field(default_factory=dict)
    error: str = ""
    log: list = Field(default_factory=list)
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    estimated_cost: float = 0.0


# --- ai usage --------------------------------------------------------------

class UsageLine(BaseModel):
    """One grouping of model calls: a capability, a model, or a day."""
    key: str
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    #: Calls in this line whose model had no published price. Their cost is
    #: not in `cost_usd`, so a line with unpriced calls is a lower bound.
    unpriced_calls: int = 0


class UsageReport(BaseModel):
    calls: int = 0
    failed_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    unpriced_calls: int = 0
    #: True when every call in the report had a known price, so `cost_usd` is
    #: the whole bill rather than part of it.
    cost_complete: bool = True
    by_capability: list[UsageLine] = Field(default_factory=list)
    by_model: list[UsageLine] = Field(default_factory=list)
    by_day: list[UsageLine] = Field(default_factory=list)


# --- versions --------------------------------------------------------------

class VersionOut(BaseModel):
    id: str
    seq: int
    label: str
    source: str
    created_at: str
    operation_count: int = 0
    is_current: bool = False


TokenResponse.model_rebuild()
