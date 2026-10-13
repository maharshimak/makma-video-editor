"""End-to-end API test.

This is the MVP acceptance test from the product brief: upload real footage,
give the AI Director a natural-language instruction, have the timeline
actually change, render it, and probe the resulting MP4. Nothing here is
mocked -- the file that comes out is a real encode.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from makma.config import settings
from makma.db import Base, engine
from makma.jobs.queue import claim_next, run_job
from makma.main import app
from makma.media.probe import probe
from makma.schema.core import ProjectDocument
from makma.storage.local import get_storage


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("apidata")
    # Redirect all persistence at a scratch directory for this module.
    object.__setattr__(settings, "data_dir", data_dir)
    (data_dir / "storage").mkdir(parents=True, exist_ok=True)
    (data_dir / "tmp").mkdir(parents=True, exist_ok=True)

    import makma.storage.local as local_storage

    local_storage._storage = local_storage.LocalStorage(data_dir / "storage")

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with TestClient(app) as c:
        yield c


def drain_jobs(limit: int = 20) -> int:
    """Run queued jobs synchronously (the test process has no worker thread)."""
    done = 0
    for _ in range(limit):
        job = claim_next("pytest")
        if job is None:
            break
        run_job(job)
        done += 1
    return done


@pytest.fixture(scope="module")
def project(client) -> str:
    resp = client.post("/api/projects", json={"name": "Night Drive", "fps": 25,
                                              "width": 640, "height": 360})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


_SAMPLE_HOLDER: dict = {}


@pytest.fixture(scope="module", autouse=True)
def _remember_sample(sample_video):
    """Share the generated fixture media with tests that make their own project."""
    _SAMPLE_HOLDER["path"] = sample_video
    return sample_video


@pytest.fixture(scope="module")
def uploaded(client, project, sample_video) -> dict:
    with open(sample_video, "rb") as fh:
        resp = client.post(
            f"/api/projects/{project}/assets",
            files={"file": ("car_video.mp4", fh, "video/mp4")},
        )
    assert resp.status_code == 201, resp.text
    assert drain_jobs() >= 1, "ingest job did not run"

    asset_id = resp.json()["id"]
    detail = client.get(f"/api/projects/{project}/assets/{asset_id}")
    assert detail.status_code == 200
    return detail.json()


# ---------------------------------------------------------------------------


def test_system_capabilities_are_honest(client):
    body = client.get("/api/system/capabilities").json()
    assert body["media"]["ffmpeg"]["available"] is True
    # Unimplemented features must be advertised as planned, not as working.
    planned = {p["key"] for p in body["planned"]}
    assert {"segmentation", "object_removal", "generative_video"} <= planned
    assert "color_grade" in {e["type"] for e in
                             client.get("/api/system/effects").json()["effects"]}


def test_ingest_produces_proxy_thumbnail_and_scene_index(uploaded):
    assert uploaded["status"] == "ready", uploaded.get("error")
    assert uploaded["duration"] == pytest.approx(6.0, abs=0.3)
    assert uploaded["width"] == 640 and uploaded["height"] == 360
    assert uploaded["has_audio"] is True
    assert uploaded["proxy_url"] and uploaded["thumbnail_url"]
    assert uploaded["waveform_url"]
    assert uploaded["scene_count"] >= 1
    analysis = uploaded["analysis"]
    assert analysis["highlights"], "no highlights were ranked"
    for scene in analysis["scenes"]:
        assert 0.0 <= scene["quality_score"] <= 1.0


def test_signed_media_url_is_required(client, uploaded):
    url = uploaded["proxy_url"]
    assert client.get(url).status_code == 200
    unsigned = url.split("?")[0]
    assert client.get(unsigned).status_code == 403


def test_media_supports_range_requests(client, uploaded):
    """Scrubbing in the browser needs partial content, not a full download."""
    resp = client.get(uploaded["proxy_url"], headers={"Range": "bytes=0-1023"})
    assert resp.status_code == 206
    assert resp.headers["content-range"].startswith("bytes 0-1023/")
    assert len(resp.content) == 1024


def test_add_clip_to_timeline(client, project, uploaded):
    resp = client.post(
        f"/api/projects/{project}/operations",
        json={
            "operations": [
                {"type": "add_clip", "description": "Add the footage",
                 "scope": {"type": "sequence"},
                 "parameters": {"asset_id": uploaded["id"], "start": 0,
                                "duration": 6.0}}
            ],
            "label": "Add footage",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    seq = body["document"]["sequences"][0]
    clips = [c for t in seq["tracks"] for c in t["clips"]]
    assert len(clips) == 1
    assert clips[0]["duration"] == pytest.approx(6.0)


def test_ai_director_plans_and_applies_the_brief_command(client, project):
    """The exact instruction from the product brief must produce real edits."""
    resp = client.post(
        f"/api/projects/{project}/ai/chat",
        json={
            "message": "Make it cinematic, cut the boring first 3 seconds, "
                       "slow down the reveal and add the title \"NIGHT DRIVE\".",
            "playhead": 4.0,
            "auto_apply": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    plan = body["plan"]

    kinds = {op["type"] for op in plan["operations"]}
    assert "remove_range" in kinds, "the 3-second head cut was not planned"
    assert "add_effect" in kinds, "no look was applied"
    assert "set_speed" in kinds, "the slow-down was not planned"
    assert "add_text" in kinds, "the title was not planned"

    assert body["applied"] is True, body["errors"]
    document = body["document"]
    seq = document["sequences"][0]
    clips = [c for t in seq["tracks"] for c in t["clips"]]

    text_clips = [c for c in clips if c["kind"] == "text"]
    assert text_clips and text_clips[0]["text"]["text"] == "NIGHT DRIVE"

    video_clips = [c for c in clips if c["kind"] == "video"]
    assert video_clips[0]["speed"] < 1.0, "clip was not slowed"
    effect_types = {f["type"] for c in video_clips for f in c["effects"]}
    assert "color_grade" in effect_types
    assert "letterbox" in effect_types


def test_plan_is_inspectable_and_every_op_is_described(client, project):
    resp = client.post(
        f"/api/projects/{project}/ai/chat",
        json={"message": "add a subtle vignette", "auto_apply": False},
    )
    plan = resp.json()["plan"]
    assert plan["operations"], "planner produced nothing"
    for op in plan["operations"]:
        assert op["description"], "an operation shipped with no description"
        assert op["id"] and op["type"]
    assert plan["estimate"]["tier"] in ("instant", "light", "moderate", "heavy")


def test_unknown_instruction_fails_loudly_not_silently(client, project):
    resp = client.post(
        f"/api/projects/{project}/ai/chat",
        json={"message": "xyzzy plugh frobnicate the widget"},
    )
    body = resp.json()
    assert body["applied"] is False
    assert body["plan"]["operations"] == []
    assert "could not turn that into an edit" in body["reply"].lower()


def test_unsupported_capability_is_reported_not_faked(client, project):
    """A V3/V4 request must be refused clearly, never silently no-opped."""
    from makma.ai.director import _plan_from_result
    from makma.ai.validation import ProjectFacts, validate_plan

    document = client.get(f"/api/projects/{project}").json()["document"]
    facts = ProjectFacts.from_document(ProjectDocument(**document))

    result = validate_plan(
        {
            "summary": "Replace the background.",
            "operations": [
                {"type": "replace_background", "description": "Swap the sky",
                 "parameters": {}},
                {"type": "add_effect", "description": "Grade",
                 "parameters": {"type": "color_grade"}},
            ],
        },
        facts,
    )
    assert result.ok, result.repair_brief()

    plan = _plan_from_result(result, "replace the background with Tokyo at night")
    assert plan.unsupported == ["replace_background"]
    assert [o.type.value for o in plan.operations] == ["add_effect"]


def test_undo_and_redo_move_along_real_history(client, project):
    before = client.get(f"/api/projects/{project}").json()["document"]

    undo = client.post(f"/api/projects/{project}/undo")
    assert undo.status_code == 200 and undo.json()["ok"] is True
    after_undo = undo.json()["document"]
    assert after_undo != before

    redo = client.post(f"/api/projects/{project}/redo")
    assert redo.status_code == 200 and redo.json()["ok"] is True
    assert redo.json()["document"] == before

    versions = client.get(f"/api/projects/{project}/versions").json()
    assert len(versions) >= 3
    assert sum(1 for v in versions if v["is_current"]) == 1
    assert any(v["source"] == "ai" for v in versions)


def test_render_produces_a_real_playable_mp4(client, project):
    resp = client.post(f"/api/projects/{project}/render", json={})
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["id"]

    assert drain_jobs() >= 1
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "completed", job["error"]

    result = job["result"]
    assert result["size_bytes"] > 1000
    assert result["duration"] > 0

    download = client.get(result["url"])
    assert download.status_code == 200

    path = Path(get_storage().local_path(result["storage_key"]))
    info = probe(path)
    assert info.primary_video is not None
    assert info.duration == pytest.approx(result["duration"], abs=0.4)
    assert info.audio, "render lost the audio track"


def test_export_preset_changes_the_output_resolution(client, project):
    resp = client.post(f"/api/projects/{project}/export", json={"preset": "tiktok"})
    assert resp.status_code == 202
    job_id = resp.json()["id"]
    assert drain_jobs() >= 1

    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "completed", job["error"]
    path = Path(get_storage().local_path(job["result"]["storage_key"]))
    info = probe(path)
    assert (info.primary_video.width, info.primary_video.height) == (1080, 1920)


def test_render_of_empty_sequence_is_rejected_up_front(client):
    empty = client.post("/api/projects", json={"name": "Empty"}).json()["id"]
    resp = client.post(f"/api/projects/{empty}/render", json={})
    assert resp.status_code == 400
    assert "empty" in resp.json()["detail"].lower()


def test_project_isolation(client, project):
    """A project id belonging to someone else must 404, not leak."""
    from makma.db import SessionLocal
    from makma.models import Project, User

    db = SessionLocal()
    other = User(email="someone-else@example.com", name="Other")
    db.add(other)
    db.commit()
    foreign = Project(owner_id=other.id, name="Theirs", document={})
    db.add(foreign)
    db.commit()
    foreign_id = foreign.id
    db.close()

    assert client.get(f"/api/projects/{foreign_id}").status_code == 404
    assert client.get(f"/api/projects/{project}").status_code == 200


def test_an_undecodable_upload_fails_loudly(client, project):
    """A file FFmpeg cannot read must say so, not sit in the panel as ready.

    Marking it ready puts a row in the media list that can never reach the
    timeline, which reads to the user as the upload having vanished.
    """
    junk = b"\x00" * 4096
    resp = client.post(
        f"/api/projects/{project}/assets",
        files={"file": ("broken.mp4", junk, "video/mp4")},
    )
    assert resp.status_code == 201
    asset_id = resp.json()["id"]
    drain_jobs()

    asset = client.get(f"/api/projects/{project}/assets/{asset_id}").json()
    assert asset["status"] == "failed", "a broken upload must not sit on 'ingesting'"
    assert "broken.mp4" in asset["error"], "the message should name the file"
    assert "could not be decoded" in asset["error"]
    # The reason comes from FFmpeg, minus the server path the user never saw.
    assert "Invalid data" in asset["error"]
    assert "storage" not in asset["error"], "internal paths must not leak"


def test_an_unsupported_extension_is_rejected_at_upload(client, project):
    resp = client.post(
        f"/api/projects/{project}/assets",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert resp.status_code == 400
    assert "not supported" in resp.json()["detail"]


def test_deleting_media_in_use_is_refused_rather_than_orphaning_clips(client):
    """Deleting a file on the timeline used to leave clips pointing at nothing.

    Those clips keep their place in the sequence, draw nothing, and only
    surface as a render warning, so the refusal has to come first.
    """
    pid = client.post("/api/projects", json={"name": "Delete guard",
                                             "width": 320, "height": 180}).json()["id"]
    with open(_SAMPLE_HOLDER["path"], "rb") as fh:
        asset = client.post(f"/api/projects/{pid}/assets",
                            files={"file": ("clip.mp4", fh, "video/mp4")}).json()
    drain_jobs()

    client.post(f"/api/projects/{pid}/operations", json={
        "operations": [{"type": "add_clip", "description": "Add",
                        "scope": {"type": "sequence"},
                        "parameters": {"asset_id": asset["id"], "start": 0,
                                       "duration": 2.0}}],
        "label": "Add",
    })

    usage = client.get(f"/api/projects/{pid}/assets/{asset['id']}/usage").json()
    assert usage["clip_count"] == 1

    refused = client.delete(f"/api/projects/{pid}/assets/{asset['id']}")
    assert refused.status_code == 409
    assert "used by 1 clip" in refused.json()["detail"]
    assert client.get(f"/api/projects/{pid}/assets").json(), "asset was removed anyway"

    forced = client.delete(
        f"/api/projects/{pid}/assets/{asset['id']}?remove_clips=true"
    )
    assert forced.status_code == 204

    document = client.get(f"/api/projects/{pid}").json()["document"]
    clips = [c for t in document["sequences"][0]["tracks"] for c in t["clips"]]
    assert clips == [], "the clips should have gone with the media"

    # ...and taking the media out is undoable, like any other edit.
    undo = client.post(f"/api/projects/{pid}/undo").json()
    assert undo["ok"] is True


def test_deleting_unused_media_needs_no_confirmation(client, sample_image):
    pid = client.post("/api/projects", json={"name": "Unused"}).json()["id"]
    with open(sample_image, "rb") as fh:
        asset = client.post(f"/api/projects/{pid}/assets",
                            files={"file": ("still.png", fh, "image/png")}).json()
    drain_jobs()

    assert client.delete(f"/api/projects/{pid}/assets/{asset['id']}").status_code == 204
    assert client.get(f"/api/projects/{pid}/assets").json() == []
