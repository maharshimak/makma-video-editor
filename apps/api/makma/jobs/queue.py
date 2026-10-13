"""Durable job queue.

Video work takes minutes, so nothing heavy runs inside an HTTP request. Jobs
live in the database, which means they survive a restart, can be resumed, and
carry their own progress, log and cost accounting. The default backend is the
database itself (no Redis required); the interface is deliberately small so a
Redis/queue backend can be dropped in without touching callers.
"""
from __future__ import annotations

import logging
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import session_scope
from ..models import Job, utcnow

log = logging.getLogger("makma.jobs")

#: Serialises claim-and-mark so two workers cannot take the same job. On
#: SQLite this is the whole story; on Postgres the row-level UPDATE below is
#: what actually guarantees it.
_claim_lock = threading.Lock()


class JobStatus:
    QUEUED = "queued"
    PLANNING = "planning"
    PROCESSING = "processing"
    RENDERING = "rendering"
    QUALITY_CHECK = "quality_check"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    TERMINAL = {COMPLETED, FAILED, CANCELLED}
    ACTIVE = {PLANNING, PROCESSING, RENDERING, QUALITY_CHECK}


class JobCancelled(RuntimeError):
    """Raised inside a handler when the user cancels the job."""


@dataclass
class JobContext:
    """Handle passed to a job handler: progress, logging, cancellation."""
    job_id: str
    project_id: str
    user_id: str
    payload: dict

    def progress(self, value: float, stage: str = "") -> None:
        with session_scope() as db:
            job = db.get(Job, self.job_id)
            if job is None:
                return
            if job.status == JobStatus.CANCELLED:
                raise JobCancelled(f"Job {self.job_id} was cancelled.")
            job.progress = max(0.0, min(1.0, float(value)))
            if stage:
                job.stage = stage[:80]
            job.heartbeat_at = utcnow()

    def log(self, message: str) -> None:
        log.info("[%s] %s", self.job_id, message)
        with session_scope() as db:
            job = db.get(Job, self.job_id)
            if job is None:
                return
            entries = list(job.log or [])
            entries.append(
                {"t": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "message": message[:500]}
            )
            job.log = entries[-200:]

    def set_status(self, status: str) -> None:
        with session_scope() as db:
            job = db.get(Job, self.job_id)
            if job is not None and job.status not in JobStatus.TERMINAL:
                job.status = status

    def check_cancelled(self) -> None:
        with session_scope() as db:
            job = db.get(Job, self.job_id)
            if job is not None and job.status == JobStatus.CANCELLED:
                raise JobCancelled(f"Job {self.job_id} was cancelled.")


Handler = Callable[[JobContext], dict]
HANDLERS: dict[str, Handler] = {}


def register(job_type: str):
    def wrap(fn: Handler) -> Handler:
        HANDLERS[job_type] = fn
        return fn
    return wrap


def enqueue(
    db: Session,
    job_type: str,
    payload: dict,
    project_id: str | None = None,
    user_id: str = "",
    priority: int = 5,
    estimated_cost: float = 0.0,
) -> Job:
    if job_type not in HANDLERS:
        raise ValueError(
            f"No handler is registered for job type '{job_type}'. "
            f"Known types: {', '.join(sorted(HANDLERS)) or 'none'}"
        )
    job = Job(
        type=job_type,
        payload=payload,
        project_id=project_id,
        user_id=user_id,
        priority=priority,
        status=JobStatus.QUEUED,
        estimated_cost=estimated_cost,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    log.info("queued %s job %s", job_type, job.id)
    return job


def claim_next(worker: str) -> Job | None:
    """Atomically take the highest-priority queued job."""
    with _claim_lock, session_scope() as db:
        stmt = (
            select(Job)
            .where(Job.status == JobStatus.QUEUED)
            .order_by(Job.priority.asc(), Job.created_at.asc())
            .limit(1)
        )
        job = db.execute(stmt).scalars().first()
        if job is None:
            return None
        job.status = JobStatus.PROCESSING
        job.worker = worker
        job.attempts += 1
        job.started_at = utcnow()
        job.heartbeat_at = utcnow()
        db.flush()
        db.expunge(job)
        return job


def run_job(job: Job) -> None:
    """Execute one claimed job, recording its outcome either way."""
    handler = HANDLERS.get(job.type)
    ctx = JobContext(
        job_id=job.id, project_id=job.project_id or "",
        user_id=job.user_id or "", payload=dict(job.payload or {}),
    )
    if handler is None:
        _finish(job.id, JobStatus.FAILED, error=f"No handler for '{job.type}'.")
        return

    started = utcnow()
    try:
        result = handler(ctx) or {}
        _finish(job.id, JobStatus.COMPLETED, result=result, started=started)
    except JobCancelled:
        _finish(job.id, JobStatus.CANCELLED, error="Cancelled by the user.",
                started=started)
    except Exception as exc:  # noqa: BLE001 - a job failure must not kill the worker
        detail = f"{type(exc).__name__}: {exc}"
        log.exception("job %s failed", job.id)
        with session_scope() as db:
            row = db.get(Job, job.id)
            retryable = row is not None and row.attempts < row.max_attempts
        if retryable:
            with session_scope() as db:
                row = db.get(Job, job.id)
                row.status = JobStatus.QUEUED
                row.stage = "retrying"
                row.error = detail[:4000]
            log.warning("job %s will be retried", job.id)
        else:
            _finish(job.id, JobStatus.FAILED, error=detail,
                    trace=traceback.format_exc(), started=started)


def _finish(job_id: str, status: str, result: dict | None = None,
            error: str = "", trace: str = "",
            started: datetime | None = None) -> None:
    with session_scope() as db:
        job = db.get(Job, job_id)
        if job is None:
            return
        job.status = status
        job.finished_at = utcnow()
        job.progress = 1.0 if status == JobStatus.COMPLETED else job.progress
        if result is not None:
            job.result = result
        if error:
            job.error = (error + ("\n\n" + trace if trace else ""))[:8000]
        if started is not None:
            job.cpu_seconds = round(
                (job.finished_at - started.replace(tzinfo=None)
                 if job.finished_at.tzinfo is None else job.finished_at - started
                 ).total_seconds(), 2
            )


def cancel(db: Session, job_id: str) -> bool:
    job = db.get(Job, job_id)
    if job is None or job.status in JobStatus.TERMINAL:
        return False
    job.status = JobStatus.CANCELLED
    job.finished_at = utcnow()
    db.commit()
    return True


def _as_naive_utc(value: datetime | None) -> datetime | None:
    """Strip the offset from an aware datetime, keeping the instant.

    Timestamps are written as aware UTC but stored in a column with no
    timezone, so SQLite hands them back naive. Comparing the two raises, and
    because the only comparison lived inside the worker loop, the failure was
    a log line every second rather than anything visible: stalled-job recovery
    silently never ran.
    """
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def requeue_stalled(older_than_minutes: int = 30) -> int:
    """Recover jobs whose worker died mid-flight."""
    cutoff = _as_naive_utc(utcnow() - timedelta(minutes=older_than_minutes))
    count = 0
    with session_scope() as db:
        stmt = select(Job).where(Job.status.in_(list(JobStatus.ACTIVE)))
        for job in db.execute(stmt).scalars():
            beat = _as_naive_utc(job.heartbeat_at or job.started_at)
            if beat is not None and beat < cutoff:
                if job.attempts < job.max_attempts:
                    job.status = JobStatus.QUEUED
                    job.stage = "recovered"
                else:
                    job.status = JobStatus.FAILED
                    job.error = "Worker stopped responding."
                count += 1
    return count
