"""Project, document, version and auth routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..ai.validation import safety_problems
from ..config import settings
from ..db import get_db
from ..editing import AssetInfo, EditContext, apply_operations
from ..models import Asset, Project, ProjectVersion, User
from ..schema.core import ProjectDocument, default_sequence
from ..storage.local import get_storage
from ..versions import (
    VersionConflict,
    can_redo,
    can_undo,
    history,
    load_document,
    redo,
    restore,
    save_version,
    undo,
)
from .deps import (
    create_token,
    get_current_user,
    get_project,
    hash_password,
    verify_password,
)
from .schemas import (
    ApplyOperationsRequest,
    ApplyResponse,
    DocumentUpdate,
    ProjectCreate,
    ProjectOut,
    ProjectSummary,
    ProjectUpdate,
    SignInRequest,
    SignUpRequest,
    TokenResponse,
    UserOut,
    VersionOut,
)

auth_router = APIRouter(prefix="/api/auth", tags=["auth"])
router = APIRouter(prefix="/api/projects", tags=["projects"])


def _iso(value) -> str:
    return value.isoformat() if value else ""


def _user_out(user: User) -> UserOut:
    return UserOut(id=user.id, email=user.email, name=user.name,
                   preferences=user.preferences or {})


def _project_out(db: Session, project: Project) -> ProjectOut:
    return ProjectOut(
        id=project.id,
        name=project.name,
        description=project.description,
        document=load_document(project),
        created_at=_iso(project.created_at),
        updated_at=_iso(project.updated_at),
        version=project.head_seq,
        can_undo=can_undo(db, project),
        can_redo=can_redo(db, project),
    )


def _conflict(db: Session, project: Project, conflict: VersionConflict) -> HTTPException:
    """Turn a stale write into a 409 the client can actually recover from.

    The response carries the current head and document, so the UI can show
    what changed instead of asking the user to reload and lose their place.
    """
    return HTTPException(
        status_code=409,
        detail={
            "code": "VERSION_CONFLICT",
            "message": str(conflict),
            "expected_version": conflict.expected,
            "current_version": conflict.actual,
            "document": load_document(project).model_dump(mode="json"),
        },
    )


def _asset_infos(db: Session, project_id: str) -> dict[str, AssetInfo]:
    infos: dict[str, AssetInfo] = {}
    for asset in db.query(Asset).filter(Asset.project_id == project_id).all():
        infos[asset.id] = AssetInfo(
            id=asset.id, name=asset.name, kind=asset.kind,
            duration=asset.duration or 5.0, width=asset.width,
            height=asset.height, fps=asset.fps, has_audio=bool(asset.has_audio),
        )
    return infos


# --- auth ------------------------------------------------------------------

@auth_router.post("/signup", response_model=TokenResponse)
def signup(body: SignUpRequest, db: Session = Depends(get_db)) -> TokenResponse:
    existing = db.query(User).filter(User.email == str(body.email)).first()
    if existing is not None:
        raise HTTPException(400, "An account with that email already exists.")
    user = User(
        email=str(body.email), name=body.name or str(body.email).split("@")[0],
        password_hash=hash_password(body.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return TokenResponse(access_token=create_token(user.id), user=_user_out(user))


@auth_router.post("/signin", response_model=TokenResponse)
def signin(body: SignInRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.query(User).filter(User.email == str(body.email)).first()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "That email and password do not match.")
    return TokenResponse(access_token=create_token(user.id), user=_user_out(user))


@auth_router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> UserOut:
    return _user_out(user)


@auth_router.patch("/me/preferences", response_model=UserOut)
def update_preferences(body: dict, db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)) -> UserOut:
    """Creator memory is always visible and editable by its owner."""
    user.preferences = {**(user.preferences or {}), **body}
    db.commit()
    return _user_out(user)


# --- projects --------------------------------------------------------------

@router.get("", response_model=list[ProjectSummary])
def list_projects(db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)) -> list[ProjectSummary]:
    storage = get_storage()
    projects = (
        db.query(Project)
        .filter(Project.owner_id == user.id, Project.archived.is_(False))
        .order_by(Project.updated_at.desc())
        .all()
    )
    counts = dict(
        db.execute(
            select(Asset.project_id, func.count(Asset.id)).group_by(Asset.project_id)
        ).all()
    )
    out = []
    for project in projects:
        document = load_document(project)
        poster = (
            db.query(Asset)
            .filter(Asset.project_id == project.id, Asset.thumbnail_key != "")
            .order_by(Asset.created_at.asc())
            .first()
        )
        out.append(
            ProjectSummary(
                id=project.id, name=project.name, description=project.description,
                created_at=_iso(project.created_at),
                updated_at=_iso(project.updated_at),
                duration=round(document.active().duration, 2),
                asset_count=counts.get(project.id, 0),
                thumbnail_url=storage.url(poster.thumbnail_key) if poster else None,
            )
        )
    return out


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(body: ProjectCreate, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)) -> ProjectOut:
    document = ProjectDocument(
        name=body.name,
        sequences=[default_sequence("Main", body.width, body.height, body.fps)],
    )
    project = Project(
        owner_id=user.id, name=body.name,
        document=document.model_dump(mode="json"),
    )
    db.add(project)
    db.commit()
    db.refresh(project)

    # Seed version 0 so the very first edit has something to undo back to.
    db.add(
        ProjectVersion(
            project_id=project.id, seq=0, label="Created",
            document=project.document, source="system",
        )
    )
    db.commit()
    return _project_out(db, project)


@router.get("/{project_id}", response_model=ProjectOut)
def get_one(project: Project = Depends(get_project),
            db: Session = Depends(get_db)) -> ProjectOut:
    return _project_out(db, project)


@router.patch("/{project_id}", response_model=ProjectOut)
def update_project(body: ProjectUpdate, project: Project = Depends(get_project),
                   db: Session = Depends(get_db)) -> ProjectOut:
    if body.name is not None:
        project.name = body.name
    if body.description is not None:
        project.description = body.description
    db.commit()
    return _project_out(db, project)


@router.delete("/{project_id}", status_code=204)
def delete_project(project: Project = Depends(get_project),
                   db: Session = Depends(get_db)) -> None:
    storage = get_storage()
    for prefix in ("originals", "proxies", "thumbnails", "waveforms", "renders"):
        storage.delete_prefix(f"{prefix}/{project.id}")
    db.delete(project)
    db.commit()


# --- document + operations -------------------------------------------------

@router.put("/{project_id}/document", response_model=ApplyResponse)
def save_document(body: DocumentUpdate, project: Project = Depends(get_project),
                  db: Session = Depends(get_db)) -> ApplyResponse:
    """Whole-document save from the editor (drags, trims, property panels)."""
    try:
        version = save_version(db, project, body.document, body.label,
                               source="user",
                               expected_version=body.expected_version)
    except VersionConflict as conflict:
        raise _conflict(db, project, conflict)
    return ApplyResponse(ok=True, document=body.document, version_id=version.id,
                         version=project.head_seq)


@router.post("/{project_id}/operations", response_model=ApplyResponse)
def apply_ops(body: ApplyOperationsRequest, project: Project = Depends(get_project),
              db: Session = Depends(get_db)) -> ApplyResponse:
    """Apply structured operations directly -- the same path the AI uses."""
    unsafe = safety_problems(body.operations)
    if unsafe:
        raise HTTPException(400, {
            "code": "INVALID_OPERATIONS",
            "message": "Some values in these operations cannot be applied.",
            "problems": [str(p) for p in unsafe],
        })

    document = load_document(project)
    ctx = EditContext(
        assets=_asset_infos(db, project.id), selection=body.selection,
        playhead=body.playhead, sequence_id=body.sequence_id,
    )
    updated, result = apply_operations(document, body.operations, ctx, atomic=True)
    errors = [r.message for r in result.results if not r.ok]
    if errors:
        return ApplyResponse(ok=False, document=document, errors=errors)

    try:
        version = save_version(
            db, project, updated, body.label, source="user",
            operations=[o.model_dump(mode="json") for o in body.operations],
            expected_version=body.expected_version,
        )
    except VersionConflict as conflict:
        raise _conflict(db, project, conflict)

    changed = sorted({c for r in result.results for c in r.changed_clip_ids})
    return ApplyResponse(ok=True, document=updated, version_id=version.id,
                         version=project.head_seq, changed_clip_ids=changed)


# --- history ---------------------------------------------------------------

@router.post("/{project_id}/undo", response_model=ApplyResponse)
def undo_edit(project: Project = Depends(get_project),
              db: Session = Depends(get_db)) -> ApplyResponse:
    document = undo(db, project)
    if document is None:
        return ApplyResponse(ok=False, document=load_document(project),
                             errors=["There is nothing left to undo."])
    return ApplyResponse(ok=True, document=document)


@router.post("/{project_id}/redo", response_model=ApplyResponse)
def redo_edit(project: Project = Depends(get_project),
              db: Session = Depends(get_db)) -> ApplyResponse:
    document = redo(db, project)
    if document is None:
        return ApplyResponse(ok=False, document=load_document(project),
                             errors=["There is nothing to redo."])
    return ApplyResponse(ok=True, document=document)


@router.get("/{project_id}/versions", response_model=list[VersionOut])
def list_versions(project: Project = Depends(get_project),
                  db: Session = Depends(get_db)) -> list[VersionOut]:
    return [
        VersionOut(
            id=v.id, seq=v.seq, label=v.label, source=v.source,
            created_at=_iso(v.created_at), operation_count=len(v.operations or []),
            is_current=(v.seq == project.head_seq),
        )
        for v in history(db, project)
    ]


@router.post("/{project_id}/versions/{version_id}/restore", response_model=ApplyResponse)
def restore_version(version_id: str, project: Project = Depends(get_project),
                    db: Session = Depends(get_db)) -> ApplyResponse:
    document = restore(db, project, version_id)
    if document is None:
        raise HTTPException(404, "That version does not exist in this project.")
    return ApplyResponse(ok=True, document=document)
