"""Project version history: save, undo, redo, restore.

Every mutation appends an immutable snapshot and moves a pointer. Undo never
deletes anything, so an accidental undo followed by a new edit loses only the
redo branch -- and even that branch is recoverable from the version list until
it is explicitly truncated.
"""
from __future__ import annotations

import logging

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import Project, ProjectVersion, utcnow
from .schema.core import ProjectDocument

log = logging.getLogger("makma.versions")

#: How many versions to keep per project before trimming the oldest.
MAX_VERSIONS = 200


def load_document(project: Project) -> ProjectDocument:
    return ProjectDocument(**(project.document or {}))


class VersionConflict(RuntimeError):
    """Someone else advanced the project since this client last read it.

    Carries the current head so the caller can tell the client what it is now
    looking at, rather than just refusing.
    """

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"This project has moved on since you loaded it "
            f"(you have version {expected}, it is now {actual})."
        )


def save_version(
    db: Session,
    project: Project,
    document: ProjectDocument,
    label: str = "Edit",
    source: str = "user",
    operations: list | None = None,
    expected_version: int | None = None,
) -> ProjectVersion:
    """Commit a new state, discarding any redo branch ahead of the pointer.

    `expected_version` is optimistic concurrency. A client that read version 7
    and sends 7 is writing against what it saw; if the head has moved to 9
    because another tab saved, the write is refused rather than silently
    discarding those two versions. Omitting it keeps the old last-write-wins
    behaviour, which is what internal callers want.
    """
    if expected_version is not None and expected_version != project.head_seq:
        raise VersionConflict(expected_version, project.head_seq)

    db.execute(
        delete(ProjectVersion)
        .where(ProjectVersion.project_id == project.id)
        .where(ProjectVersion.seq > project.head_seq)
    )

    seq = project.head_seq + 1
    version = ProjectVersion(
        project_id=project.id,
        seq=seq,
        label=label[:200],
        document=document.model_dump(mode="json"),
        operations=operations or [],
        source=source,
    )
    db.add(version)

    project.document = version.document
    project.head_seq = seq
    project.max_seq = seq
    project.updated_at = utcnow()
    db.commit()
    db.refresh(version)

    _trim(db, project)
    return version


def _trim(db: Session, project: Project) -> None:
    total = db.query(ProjectVersion).filter(
        ProjectVersion.project_id == project.id
    ).count()
    if total <= MAX_VERSIONS:
        return
    stale = (
        db.execute(
            select(ProjectVersion.id)
            .where(ProjectVersion.project_id == project.id)
            .order_by(ProjectVersion.seq.asc())
            .limit(total - MAX_VERSIONS)
        ).scalars().all()
    )
    if stale:
        db.execute(delete(ProjectVersion).where(ProjectVersion.id.in_(stale)))
        db.commit()


def _version_at(db: Session, project: Project, seq: int) -> ProjectVersion | None:
    return db.execute(
        select(ProjectVersion)
        .where(ProjectVersion.project_id == project.id)
        .where(ProjectVersion.seq == seq)
    ).scalars().first()


def can_undo(db: Session, project: Project) -> bool:
    return project.head_seq > 0 and _version_at(db, project, project.head_seq - 1) is not None


def can_redo(db: Session, project: Project) -> bool:
    return project.head_seq < project.max_seq


def undo(db: Session, project: Project) -> ProjectDocument | None:
    target = _version_at(db, project, project.head_seq - 1)
    if target is None:
        return None
    project.document = target.document
    project.head_seq = target.seq
    project.updated_at = utcnow()
    db.commit()
    return ProjectDocument(**target.document)


def redo(db: Session, project: Project) -> ProjectDocument | None:
    if project.head_seq >= project.max_seq:
        return None
    target = _version_at(db, project, project.head_seq + 1)
    if target is None:
        return None
    project.document = target.document
    project.head_seq = target.seq
    project.updated_at = utcnow()
    db.commit()
    return ProjectDocument(**target.document)


def restore(db: Session, project: Project, version_id: str) -> ProjectDocument | None:
    """Jump to any version. Recorded as a new version so the jump is undoable."""
    version = db.get(ProjectVersion, version_id)
    if version is None or version.project_id != project.id:
        return None
    document = ProjectDocument(**version.document)
    save_version(db, project, document, f"Restore '{version.label}'", source="system")
    return document


def history(db: Session, project: Project, limit: int = 100) -> list[ProjectVersion]:
    return list(
        db.execute(
            select(ProjectVersion)
            .where(ProjectVersion.project_id == project.id)
            .order_by(ProjectVersion.seq.desc())
            .limit(limit)
        ).scalars()
    )
