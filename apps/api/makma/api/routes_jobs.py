"""Render, export and job-status routes."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ..db import SessionLocal, get_db
from ..jobs.queue import JobStatus, cancel, enqueue
from ..models import Job, Project, User
from ..render.renderer import EXPORT_PRESETS, RenderSettings, estimate_render, preset_settings
from ..versions import load_document
from .deps import get_current_user, get_project
from .schemas import JobOut, RenderRequest

router = APIRouter(prefix="/api", tags=["jobs"])


def _job_out(job: Job) -> JobOut:
    return JobOut(
        id=job.id, type=job.type, status=job.status, stage=job.stage,
        progress=job.progress, project_id=job.project_id, result=job.result or {},
        error=job.error, log=job.log or [],
        created_at=job.created_at.isoformat() if job.created_at else "",
        started_at=job.started_at.isoformat() if job.started_at else None,
        finished_at=job.finished_at.isoformat() if job.finished_at else None,
        estimated_cost=job.estimated_cost,
    )


def _queue_render(kind: str, body: RenderRequest, project: Project,
                  db: Session, user: User) -> JobOut:
    document = load_document(project)
    sequence = document.sequence(body.sequence_id)
    if sequence.duration <= 0:
        raise HTTPException(
            400, "This sequence is empty. Add a clip to the timeline first."
        )

    if body.preset and body.preset not in EXPORT_PRESETS:
        raise HTTPException(
            400,
            f"Unknown preset '{body.preset}'. Options: "
            + ", ".join(sorted(EXPORT_PRESETS)),
        )

    rs = (preset_settings(body.preset, sequence) if body.preset
          else RenderSettings(
              width=body.width or sequence.width,
              height=body.height or sequence.height,
              fps=body.fps or sequence.fps,
              quality="final" if kind == "export" else "draft",
              preset="medium" if kind == "export" else "veryfast",
          ))
    estimate = estimate_render(document, body.sequence_id, rs)

    job = enqueue(
        db, kind, body.model_dump(exclude_none=True), project_id=project.id,
        user_id=user.id, priority=4 if kind == "export" else 5,
    )
    job.result = {"estimate": estimate}
    db.commit()
    db.refresh(job)
    return _job_out(job)


@router.post("/projects/{project_id}/render", response_model=JobOut, status_code=202)
def render(body: RenderRequest, project: Project = Depends(get_project),
           db: Session = Depends(get_db),
           user: User = Depends(get_current_user)) -> JobOut:
    """Queue a fast proxy-quality render for review inside the editor."""
    return _queue_render("render", body, project, db, user)


@router.post("/projects/{project_id}/export", response_model=JobOut, status_code=202)
def export(body: RenderRequest, project: Project = Depends(get_project),
           db: Session = Depends(get_db),
           user: User = Depends(get_current_user)) -> JobOut:
    """Queue a final-quality export from the original media."""
    return _queue_render("export", body, project, db, user)


@router.get("/projects/{project_id}/render/estimate")
def render_estimate(sequence_id: str | None = None, preset: str | None = None,
                    project: Project = Depends(get_project)) -> dict:
    document = load_document(project)
    sequence = document.sequence(sequence_id)
    rs = preset_settings(preset, sequence) if preset else RenderSettings(
        width=sequence.width, height=sequence.height, fps=sequence.fps
    )
    return estimate_render(document, sequence_id, rs)


@router.get("/projects/{project_id}/health")
def project_health(sequence_id: str | None = None,
                   project: Project = Depends(get_project),
                   db: Session = Depends(get_db)) -> dict:
    """Pre-flight check: everything that would render wrong rather than fail."""
    from ..health import AssetFacts, check_project
    from ..models import Asset

    document = load_document(project)
    facts = {
        a.id: AssetFacts(
            id=a.id, name=a.name, status=a.status, width=a.width,
            height=a.height, duration=a.duration, kind=a.kind,
            has_audio=bool(a.has_audio),
            peak_db=(a.analysis or {}).get("peak_db"),
        )
        for a in db.query(Asset).filter(Asset.project_id == project.id).all()
    }
    return check_project(document, facts, sequence_id).to_dict()


@router.get("/projects/{project_id}/jobs", response_model=list[JobOut])
def list_jobs(limit: int = 40, project: Project = Depends(get_project),
              db: Session = Depends(get_db)) -> list[JobOut]:
    jobs = (
        db.query(Job)
        .filter(Job.project_id == project.id)
        .order_by(Job.created_at.desc())
        .limit(min(limit, 200))
        .all()
    )
    return [_job_out(j) for j in jobs]


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, db: Session = Depends(get_db),
            user: User = Depends(get_current_user)) -> JobOut:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found.")
    if job.user_id and job.user_id != user.id:
        raise HTTPException(404, "Job not found.")
    return _job_out(job)


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
def cancel_job(job_id: str, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)) -> JobOut:
    job = db.get(Job, job_id)
    if job is None or (job.user_id and job.user_id != user.id):
        raise HTTPException(404, "Job not found.")
    if not cancel(db, job_id):
        raise HTTPException(409, f"That job is already {job.status}.")
    db.refresh(job)
    return _job_out(job)


@router.get("/jobs/{job_id}/stream")
async def stream_job(job_id: str, request: Request):
    """Server-sent events so the UI shows real progress, not a mystery spinner."""

    async def events():
        last = None
        while True:
            if await request.is_disconnected():
                break
            db = SessionLocal()
            try:
                job = db.get(Job, job_id)
                if job is None:
                    yield f"event: error\ndata: {json.dumps({'detail': 'not found'})}\n\n"
                    return
                payload = _job_out(job).model_dump()
                payload.pop("log", None)
                snapshot = json.dumps(payload, default=str)
                if snapshot != last:
                    yield f"data: {snapshot}\n\n"
                    last = snapshot
                if job.status in JobStatus.TERMINAL:
                    return
            finally:
                db.close()
            await asyncio.sleep(0.6)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )
