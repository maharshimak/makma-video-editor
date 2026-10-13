"""Asset upload, listing and private media delivery."""
from __future__ import annotations

import mimetypes
import os
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..jobs.queue import enqueue
from ..models import Asset, Project, User
from ..storage.base import StorageKey
from ..storage.local import get_storage, verify_key
from ..versions import load_document, save_version
from .deps import get_current_user, get_project
from .schemas import AssetOut

router = APIRouter(prefix="/api", tags=["assets"])

ALLOWED_EXTENSIONS = {
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpg", ".mpeg", ".mts",
    ".m2ts", ".wmv", ".flv", ".mxf", ".prores",
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".aiff", ".wma",
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif",
    ".cube", ".3dl", ".ttf", ".otf",
}

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ \-()]+")


def _safe_filename(name: str) -> str:
    cleaned = _SAFE_NAME.sub("_", Path(name).name).strip() or "upload"
    return cleaned[:180]


def _asset_out(asset: Asset) -> AssetOut:
    storage = get_storage()
    analysis = asset.analysis or {}
    filmstrip = f"thumbnails/{asset.project_id}/{asset.id}/filmstrip.jpg"
    return AssetOut(
        id=asset.id,
        project_id=asset.project_id,
        name=asset.name,
        kind=asset.kind,
        status=asset.status,
        error=asset.error,
        duration=asset.duration,
        width=asset.width,
        height=asset.height,
        fps=asset.fps,
        has_audio=bool(asset.has_audio),
        size_bytes=asset.size_bytes,
        created_at=asset.created_at.isoformat() if asset.created_at else "",
        proxy_url=storage.url(asset.proxy_key) if asset.proxy_key else (
            storage.url(asset.audio_key) if asset.audio_key else None
        ),
        thumbnail_url=storage.url(asset.thumbnail_key) if asset.thumbnail_key else None,
        filmstrip_url=storage.url(filmstrip) if storage.exists(filmstrip) else None,
        waveform_url=storage.url(asset.waveform_key) if asset.waveform_key else None,
        original_url=storage.url(asset.original_key) if asset.original_key else None,
        scene_count=len(analysis.get("scenes") or []),
        analysis=analysis or None,
    )


@router.get("/projects/{project_id}/assets", response_model=list[AssetOut])
def list_assets(project: Project = Depends(get_project),
                db: Session = Depends(get_db)) -> list[AssetOut]:
    assets = (
        db.query(Asset)
        .filter(Asset.project_id == project.id)
        .order_by(Asset.created_at.asc())
        .all()
    )
    return [_asset_out(a) for a in assets]


@router.post("/projects/{project_id}/assets", response_model=AssetOut, status_code=201)
async def upload_asset(
    file: UploadFile = File(...),
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AssetOut:
    """Accept an upload, store the original untouched, and queue ingestion."""
    filename = _safe_filename(file.filename or "upload")
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            400,
            f"'{ext or 'that file type'}' is not supported. Accepted types: "
            + ", ".join(sorted(ALLOWED_EXTENSIONS)),
        )

    asset = Asset(project_id=project.id, name=filename, status="uploading",
                  mime_type=file.content_type or "")
    db.add(asset)
    db.commit()
    db.refresh(asset)

    storage = get_storage()
    key = StorageKey.original(project.id, asset.id, filename)
    dest = storage.stage_output(key)

    limit = settings.max_upload_mb * 1024 * 1024
    written = 0
    try:
        with open(dest, "wb") as fh:
            while chunk := await file.read(4 * 1024 * 1024):
                written += len(chunk)
                if written > limit:
                    raise HTTPException(
                        413,
                        f"That file is larger than the {settings.max_upload_mb} MB "
                        "limit. Raise MAKMA_MAX_UPLOAD_MB to allow it.",
                    )
                fh.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        db.delete(asset)
        db.commit()
        raise

    asset.original_key = key
    asset.size_bytes = written
    asset.status = "ingesting"
    db.commit()

    enqueue(db, "ingest", {"asset_id": asset.id}, project_id=project.id,
            user_id=user.id, priority=3)
    db.refresh(asset)
    return _asset_out(asset)


@router.get("/projects/{project_id}/assets/{asset_id}", response_model=AssetOut)
def get_asset(asset_id: str, project: Project = Depends(get_project),
              db: Session = Depends(get_db)) -> AssetOut:
    asset = db.get(Asset, asset_id)
    if asset is None or asset.project_id != project.id:
        raise HTTPException(404, "Asset not found.")
    return _asset_out(asset)


@router.get("/projects/{project_id}/assets/{asset_id}/usage")
def asset_usage(asset_id: str, project: Project = Depends(get_project),
                db: Session = Depends(get_db)) -> dict:
    """Where a file is used, so the UI can warn before removing it."""
    asset = db.get(Asset, asset_id)
    if asset is None or asset.project_id != project.id:
        raise HTTPException(404, "Asset not found.")

    document = load_document(project)
    uses = [
        {
            "sequence_id": sequence.id,
            "sequence": sequence.name,
            "clip_id": clip.id,
            "start": round(clip.start, 3),
            "duration": round(clip.duration, 3),
        }
        for sequence in document.sequences
        for clip in sequence.all_clips()
        if clip.asset_id == asset_id
    ]
    return {"asset_id": asset_id, "name": asset.name, "clip_count": len(uses),
            "clips": uses}


@router.delete("/projects/{project_id}/assets/{asset_id}", status_code=204)
def delete_asset(asset_id: str, remove_clips: bool = False,
                 project: Project = Depends(get_project),
                 db: Session = Depends(get_db)) -> None:
    """Remove a file from the project.

    A file still on the timeline is refused rather than deleted, because
    deleting it would leave clips pointing at media that no longer exists --
    they keep their place in the sequence, draw nothing, and only surface as a
    warning at render time. Pass `remove_clips=true` to take the clips with it,
    which is recorded as a version so it can be undone.
    """
    asset = db.get(Asset, asset_id)
    if asset is None or asset.project_id != project.id:
        raise HTTPException(404, "Asset not found.")

    document = load_document(project)
    used_by = [
        clip.id
        for sequence in document.sequences
        for clip in sequence.all_clips()
        if clip.asset_id == asset_id
    ]

    if used_by and not remove_clips:
        raise HTTPException(
            409,
            f"'{asset.name}' is used by {len(used_by)} clip"
            f"{'' if len(used_by) == 1 else 's'} on the timeline. Remove those "
            "clips as well, or take them off the timeline first.",
        )

    if used_by:
        for sequence in document.sequences:
            for track in sequence.tracks:
                track.clips = [c for c in track.clips if c.asset_id != asset_id]
        save_version(
            db, project, document,
            f"Remove {asset.name} and {len(used_by)} clip"
            f"{'' if len(used_by) == 1 else 's'}",
            source="user",
        )

    storage = get_storage()
    for prefix in ("originals", "proxies", "thumbnails", "waveforms"):
        storage.delete_prefix(f"{prefix}/{project.id}/{asset.id}")
    db.delete(asset)
    db.commit()


@router.post("/projects/{project_id}/assets/{asset_id}/reanalyze", status_code=202)
def reanalyze(asset_id: str, project: Project = Depends(get_project),
              db: Session = Depends(get_db),
              user: User = Depends(get_current_user)) -> dict:
    asset = db.get(Asset, asset_id)
    if asset is None or asset.project_id != project.id:
        raise HTTPException(404, "Asset not found.")
    job = enqueue(db, "analyze", {"asset_id": asset.id}, project_id=project.id,
                  user_id=user.id)
    return {"job_id": job.id}


# --- media delivery --------------------------------------------------------

def _range_response(path: Path, request: Request, media_type: str) -> Response:
    """Serve a file with HTTP range support so the browser can scrub video."""
    size = path.stat().st_size
    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(
            path, media_type=media_type,
            headers={"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"},
        )

    match = re.match(r"bytes=(\d*)-(\d*)", range_header)
    if not match:
        raise HTTPException(416, "Malformed Range header.")
    start_s, end_s = match.groups()
    start = int(start_s) if start_s else 0
    end = int(end_s) if end_s else size - 1
    end = min(end, size - 1)
    if start > end or start >= size:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})

    length = end - start + 1

    def _iter():
        with open(path, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(1024 * 512, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        _iter(), status_code=206, media_type=media_type,
        headers={
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
            "Cache-Control": "private, max-age=3600",
        },
    )


@router.get("/media/{key:path}")
def serve_media(key: str, request: Request, e: str = Query(""), s: str = Query("")):
    """Serve private media through a time-limited signed URL.

    Storage is never publicly listable; the signature binds the URL to one key
    and an expiry, so a leaked link stops working rather than exposing a bucket.
    """
    if not verify_key(key, e, s):
        raise HTTPException(403, "This media link has expired. Reload the project.")

    storage = get_storage()
    try:
        if not storage.exists(key):
            raise HTTPException(404, "Media not found.")
        path = storage.local_path(key)
    except ValueError:
        # The storage layer refuses keys that would escape its root. Answer
        # exactly as if the file were absent: a probe should learn nothing
        # from the difference, and an unhandled error would leak internals.
        raise HTTPException(404, "Media not found.")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if media_type.startswith(("video/", "audio/")):
        return _range_response(path, request, media_type)
    return FileResponse(path, media_type=media_type,
                        headers={"Cache-Control": "private, max-age=86400"})
