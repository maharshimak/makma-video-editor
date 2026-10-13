"""ORM models.

Project *state* is stored as a normalized JSON document (validated by
`schema.core.ProjectDocument`) rather than shredded across tables. The
timeline is edited as a whole, versioned as a whole, and rendered as a
whole -- a document column keeps those operations atomic. Everything that
is queried independently (assets, jobs, versions, chat) gets real columns.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(40), primary_key=True,
                                    default=lambda: _uid("usr"))
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    #: Creator memory -- learned preferences, always user-visible/editable.
    preferences: Mapped[dict] = mapped_column(JSON, default=dict)

    projects: Mapped[list["Project"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(40), primary_key=True,
                                    default=lambda: _uid("proj"))
    owner_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), default="Untitled Project")
    description: Mapped[str] = mapped_column(Text, default="")
    #: Serialized ProjectDocument.
    document: Mapped[dict] = mapped_column(JSON, default=dict)
    #: Hierarchical project memory for the AI Director (see ai/memory.py).
    memory: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )
    archived: Mapped[bool] = mapped_column(Boolean, default=False)

    #: Undo/redo is a pointer into the version list, not a mutation of it.
    #: `head_seq` is the version currently loaded; `max_seq` is the newest
    #: version that still exists, so redo is available while head < max.
    head_seq: Mapped[int] = mapped_column(Integer, default=0)
    max_seq: Mapped[int] = mapped_column(Integer, default=0)

    owner: Mapped[User] = relationship(back_populates="projects")
    assets: Mapped[list["Asset"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    versions: Mapped[list["ProjectVersion"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    jobs: Mapped[list["Job"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class ProjectVersion(Base):
    """Immutable snapshot of a ProjectDocument.

    Undo/redo and branching are implemented by moving a pointer along this
    list, so no edit can ever corrupt an earlier state.
    """
    __tablename__ = "project_versions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True,
                                    default=lambda: _uid("ver"))
    project_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer, default=0)
    label: Mapped[str] = mapped_column(String(200), default="")
    document: Mapped[dict] = mapped_column(JSON, default=dict)
    #: The plan/operations that produced this version, for audit + training.
    operations: Mapped[list] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String(40), default="user")  # user|ai|system
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    project: Mapped[Project] = relationship(back_populates="versions")

    __table_args__ = (Index("ix_version_project_seq", "project_id", "seq"),)


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(String(40), primary_key=True,
                                    default=lambda: _uid("asset"))
    project_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(300), default="")
    kind: Mapped[str] = mapped_column(String(20), default="video")
    status: Mapped[str] = mapped_column(String(20), default="uploading")
    error: Mapped[str] = mapped_column(Text, default="")

    original_key: Mapped[str] = mapped_column(String(500), default="")
    proxy_key: Mapped[str] = mapped_column(String(500), default="")
    audio_key: Mapped[str] = mapped_column(String(500), default="")
    thumbnail_key: Mapped[str] = mapped_column(String(500), default="")
    waveform_key: Mapped[str] = mapped_column(String(500), default="")

    checksum: Mapped[str] = mapped_column(String(64), default="", index=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    mime_type: Mapped[str] = mapped_column(String(120), default="")

    duration: Mapped[float] = mapped_column(Float, default=0.0)
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    fps: Mapped[float] = mapped_column(Float, default=0.0)
    has_audio: Mapped[bool] = mapped_column(Boolean, default=False)

    probe: Mapped[dict] = mapped_column(JSON, default=dict)
    analysis: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    project: Mapped[Project] = relationship(back_populates="assets")


class Job(Base):
    """A unit of asynchronous work: ingest, render, export, analysis."""
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True,
                                    default=lambda: _uid("job"))
    project_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("projects.id", ondelete="CASCADE"),
        index=True, nullable=True
    )
    user_id: Mapped[str] = mapped_column(String(40), default="")
    type: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(80), default="")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    priority: Mapped[int] = mapped_column(Integer, default=5)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=2)

    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    log: Mapped[list] = mapped_column(JSON, default=list)

    #: Cost accounting -- every AI/GPU operation records what it consumed.
    worker: Mapped[str] = mapped_column(String(80), default="")
    cpu_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    gpu_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost: Mapped[float] = mapped_column(Float, default=0.0)
    actual_cost: Mapped[float] = mapped_column(Float, default=0.0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    project: Mapped[Project] = relationship(back_populates="jobs")


class ChatMessage(Base):
    """AI Director conversation, with the plan that each turn produced."""
    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(String(40), primary_key=True,
                                    default=lambda: _uid("msg"))
    project_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(20), default="user")
    content: Mapped[str] = mapped_column(Text, default="")
    plan: Mapped[dict] = mapped_column(JSON, default=dict)
    applied: Mapped[bool] = mapped_column(Boolean, default=False)
    version_id: Mapped[str] = mapped_column(String(40), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class ModelUsage(Base):
    """Per-call accounting for the model router."""
    __tablename__ = "model_usage"

    id: Mapped[str] = mapped_column(String(40), primary_key=True,
                                    default=lambda: _uid("use"))
    project_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    capability: Mapped[str] = mapped_column(String(40), default="")
    provider: Mapped[str] = mapped_column(String(60), default="")
    model: Mapped[str] = mapped_column(String(120), default="")
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    #: False when the model had no published price. A total that silently
    #: includes these understates the bill.
    cost_known: Mapped[bool] = mapped_column(Boolean, default=True)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
