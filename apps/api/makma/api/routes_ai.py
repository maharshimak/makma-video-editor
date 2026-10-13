"""AI Director routes: chat, plan, apply."""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..ai.context import AssetBrief
from ..ai.director import plan_edit
from ..ai.router import Capability, get_router
from ..ai.validation import safety_problems
from ..db import get_db
from ..editing import AssetInfo, EditContext, apply_plan
from ..jobs.queue import enqueue
from ..models import Asset, ChatMessage, ModelUsage, Project, User
from ..schema.operations import EditPlan
from ..versions import load_document, save_version
from .deps import get_current_user, get_project
from .schemas import (
    ApplyPlanRequest,
    ApplyResponse,
    ChatMessageOut,
    ChatRequest,
    ChatResponse,
    UsageLine,
    UsageReport,
)

router = APIRouter(prefix="/api/projects/{project_id}/ai", tags=["ai"])


def _assets(db: Session, project_id: str) -> list[Asset]:
    return db.query(Asset).filter(Asset.project_id == project_id).all()


def _briefs(assets: list[Asset]) -> list[AssetBrief]:
    return [
        AssetBrief(
            id=a.id, name=a.name, kind=a.kind, duration=a.duration or 0.0,
            width=a.width, height=a.height, fps=a.fps,
            has_audio=bool(a.has_audio), analysis=a.analysis or {},
        )
        for a in assets
    ]


def _infos(assets: list[Asset]) -> dict[str, AssetInfo]:
    return {
        a.id: AssetInfo(
            id=a.id, name=a.name, kind=a.kind, duration=a.duration or 5.0,
            width=a.width, height=a.height, fps=a.fps, has_audio=bool(a.has_audio),
        )
        for a in assets
    }


def _reply_text(plan: EditPlan) -> str:
    parts: list[str] = []
    if plan.summary:
        parts.append(plan.summary)
    if not plan.operations and not plan.unsupported:
        parts.append(
            "I could not turn that into an edit. Try naming the change and where "
            "it applies -- for example \"make the first 10 seconds cinematic\" or "
            "\"slow the selected clip to 50%\"."
        )
    if plan.unsupported:
        parts.append(
            "Not available in this build yet: "
            + ", ".join(u.replace("_", " ") for u in plan.unsupported)
            + "."
        )
    for warning in plan.warnings:
        parts.append(warning)
    return "\n\n".join(parts)


@router.post("/chat", response_model=ChatResponse)
def chat(body: ChatRequest, project: Project = Depends(get_project),
         db: Session = Depends(get_db),
         user: User = Depends(get_current_user)) -> ChatResponse:
    message = body.message.strip()
    if not message:
        raise HTTPException(400, "Say what you want changed.")

    document = load_document(project)
    assets = _assets(db, project.id)

    db.add(ChatMessage(project_id=project.id, role="user", content=message))
    db.commit()

    prior = (
        db.query(ChatMessage)
        .filter(ChatMessage.project_id == project.id)
        .order_by(ChatMessage.created_at.desc())
        .limit(9)
        .all()
    )
    history = [
        {"role": m.role, "content": m.content}
        for m in reversed(prior) if m.content
    ][:-1]

    plan = plan_edit(
        command=message,
        document=document,
        assets=_briefs(assets),
        selection=body.selection,
        playhead=body.playhead,
        sequence_id=body.sequence_id,
        history=history,
        memory={**(project.memory or {}), "creator": (user.preferences or {})},
        project_id=project.id,
    )

    applied = False
    version_id = None
    errors: list[str] = []
    updated = document

    should_apply = body.auto_apply or not plan.requires_confirmation
    if plan.operations and should_apply:
        ctx = EditContext(
            assets=_infos(assets), selection=body.selection,
            playhead=body.playhead, sequence_id=body.sequence_id,
        )
        updated, result = apply_plan(document, plan, ctx, atomic=True)
        errors = [r.message for r in result.results if not r.ok]
        if not errors:
            version = save_version(
                db, project, updated, plan.summary[:120] or message[:120],
                source="ai",
                operations=[o.model_dump(mode="json") for o in plan.operations],
            )
            version_id = version.id
            applied = True
        else:
            updated = document

    # Exports are queued only once the edits they describe are actually in
    # place, so a render can never capture a half-applied plan.
    queued_jobs: list[str] = []
    if plan.exports and (applied or not plan.operations):
        for request in plan.exports[:4]:
            job = enqueue(
                db, "export",
                {"preset": request.preset, "sequence_id": body.sequence_id,
                 "quality": request.quality},
                project_id=project.id, user_id=user.id, priority=4,
            )
            queued_jobs.append(job.id)

    reply = _reply_text(plan)
    if queued_jobs:
        labels = ", ".join(e.label or e.preset or "master" for e in plan.exports[:4])
        reply += (
            f"\n\nQueued {len(queued_jobs)} render ({labels}). "
            "Follow it in Render & Export."
        )
    elif plan.exports and not applied:
        reply += (
            "\n\nThe render is waiting on the edit above. Apply it and I will "
            "queue the export."
        )
    if errors:
        reply += "\n\nI could not apply the plan: " + "; ".join(errors)

    assistant = ChatMessage(
        project_id=project.id, role="assistant", content=reply,
        plan=plan.model_dump(mode="json"), applied=applied,
        version_id=version_id or "",
    )
    db.add(assistant)
    db.commit()
    db.refresh(assistant)

    return ChatResponse(
        message_id=assistant.id, reply=reply, plan=plan, applied=applied,
        document=updated if applied else None, version_id=version_id,
        errors=errors, job_ids=queued_jobs,
    )


@router.post("/apply", response_model=ApplyResponse)
def apply(body: ApplyPlanRequest, project: Project = Depends(get_project),
          db: Session = Depends(get_db)) -> ApplyResponse:
    """Apply a plan the user reviewed in the plan preview."""
    # The plan makes a round trip through the browser between proposal and
    # approval, so what comes back here is client input, not the plan we
    # produced. It gets the same safety check as anything else.
    unsafe = safety_problems(body.plan.operations)
    if unsafe:
        raise HTTPException(400, {
            "code": "INVALID_PLAN",
            "message": "This plan contains values that cannot be applied.",
            "problems": [str(p) for p in unsafe],
        })

    document = load_document(project)
    assets = _assets(db, project.id)
    ctx = EditContext(
        assets=_infos(assets), selection=body.selection,
        playhead=body.playhead, sequence_id=body.sequence_id,
    )
    updated, result = apply_plan(document, body.plan, ctx, atomic=True)
    errors = [r.message for r in result.results if not r.ok]
    if errors:
        return ApplyResponse(ok=False, document=document, errors=errors)

    version = save_version(
        db, project, updated, body.plan.summary[:120] or "AI edit", source="ai",
        operations=[o.model_dump(mode="json") for o in body.plan.operations],
    )
    if body.message_id:
        message = db.get(ChatMessage, body.message_id)
        if message is not None and message.project_id == project.id:
            message.applied = True
            message.version_id = version.id
            db.commit()

    # A plan held back for review carries its exports with it, so approving
    # the edit is what finally starts the render.
    job_ids: list[str] = []
    for request in body.plan.exports[:4]:
        job = enqueue(
            db, "export",
            {"preset": request.preset, "sequence_id": body.sequence_id,
             "quality": request.quality},
            project_id=project.id, user_id=project.owner_id, priority=4,
        )
        job_ids.append(job.id)

    changed = sorted({c for r in result.results for c in r.changed_clip_ids})
    return ApplyResponse(ok=True, document=updated, version_id=version.id,
                         changed_clip_ids=changed, job_ids=job_ids)


@router.get("/messages", response_model=list[ChatMessageOut])
def messages(project: Project = Depends(get_project),
             db: Session = Depends(get_db)) -> list[ChatMessageOut]:
    rows = (
        db.query(ChatMessage)
        .filter(ChatMessage.project_id == project.id)
        .order_by(ChatMessage.created_at.asc())
        .limit(300)
        .all()
    )
    return [
        ChatMessageOut(
            id=m.id, role=m.role, content=m.content, plan=m.plan or None,
            applied=m.applied,
            created_at=m.created_at.isoformat() if m.created_at else "",
        )
        for m in rows
    ]


@router.delete("/messages", status_code=204)
def clear_messages(project: Project = Depends(get_project),
                   db: Session = Depends(get_db)) -> None:
    db.query(ChatMessage).filter(ChatMessage.project_id == project.id).delete()
    db.commit()


@router.get("/usage", response_model=UsageReport)
def usage(days: int = 30, project: Project = Depends(get_project),
          db: Session = Depends(get_db)) -> UsageReport:
    """What this project has spent on model calls.

    Cost is reported as a lower bound when any call used a model with no
    published price. Rolling unknown costs into the total as zero would make
    a cheap-looking number out of a bill nobody measured.
    """
    since = datetime.utcnow() - timedelta(days=max(1, min(days, 365)))
    rows = (
        db.query(ModelUsage)
        .filter(ModelUsage.project_id == project.id,
                ModelUsage.created_at >= since)
        .all()
    )

    report = UsageReport()
    groups: dict[str, dict[str, dict]] = {"capability": {}, "model": {}, "day": {}}

    for row in rows:
        priced = bool(row.cost_known)
        report.calls += 1
        if not row.ok:
            report.failed_calls += 1
        report.tokens_in += row.tokens_in
        report.tokens_out += row.tokens_out
        if priced:
            report.cost_usd += row.cost_usd
        else:
            report.unpriced_calls += 1

        day = row.created_at.date().isoformat() if row.created_at else "unknown"
        for dimension, key in (("capability", row.capability or "unknown"),
                               ("model", row.model or "unknown"),
                               ("day", day)):
            line = groups[dimension].setdefault(
                key, {"calls": 0, "tokens_in": 0, "tokens_out": 0,
                      "cost_usd": 0.0, "unpriced_calls": 0})
            line["calls"] += 1
            line["tokens_in"] += row.tokens_in
            line["tokens_out"] += row.tokens_out
            if priced:
                line["cost_usd"] += row.cost_usd
            else:
                line["unpriced_calls"] += 1

    def lines(dimension: str, by_cost: bool) -> list[UsageLine]:
        items = [UsageLine(key=k, **{**v, "cost_usd": round(v["cost_usd"], 6)})
                 for k, v in groups[dimension].items()]
        if by_cost:
            return sorted(items, key=lambda line: -line.cost_usd)
        return sorted(items, key=lambda line: line.key)

    report.cost_usd = round(report.cost_usd, 6)
    report.cost_complete = report.unpriced_calls == 0
    report.by_capability = lines("capability", by_cost=True)
    report.by_model = lines("model", by_cost=True)
    report.by_day = lines("day", by_cost=False)
    return report
