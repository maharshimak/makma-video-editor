"""The MVP command list, exercised end to end.

Every instruction in this file is one the product promises works. Each test
drives the real API with real media, and asserts on the resulting project
state rather than on the plan alone -- a plan that parses but changes nothing
is not a working command.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from makma.config import settings
from makma.db import Base, engine
from makma.jobs.queue import claim_next, run_job
from makma.main import app
from makma.media.probe import probe
from makma.storage.local import get_storage


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("mvpdata")
    object.__setattr__(settings, "data_dir", data_dir)
    (data_dir / "storage").mkdir(parents=True, exist_ok=True)
    (data_dir / "tmp").mkdir(parents=True, exist_ok=True)

    import makma.storage.local as local_storage

    local_storage._storage = local_storage.LocalStorage(data_dir / "storage")
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with TestClient(app) as c:
        yield c


def drain(limit: int = 30) -> int:
    done = 0
    for _ in range(limit):
        job = claim_next("pytest")
        if job is None:
            break
        run_job(job)
        done += 1
    return done


@pytest.fixture(scope="module")
def project(client, sample_video, sample_audio, sample_image) -> dict:
    """A project holding footage, a still and a music track, all ingested."""
    pid = client.post(
        "/api/projects",
        json={"name": "MVP", "width": 640, "height": 360, "fps": 25},
    ).json()["id"]

    uploads = {
        "video": (sample_video, "drive.mp4", "video/mp4"),
        "photo": (sample_image, "Photo 4.png", "image/png"),
        "music": (sample_audio, "music.m4a", "audio/mp4"),
    }
    ids: dict[str, str] = {}
    for key, (path, name, mime) in uploads.items():
        with open(path, "rb") as fh:
            resp = client.post(
                f"/api/projects/{pid}/assets", files={"file": (name, fh, mime)}
            )
        assert resp.status_code == 201, resp.text
        ids[key] = resp.json()["id"]
    assert drain() >= 3

    for key, asset_id in ids.items():
        status = client.get(f"/api/projects/{pid}/assets/{asset_id}").json()["status"]
        assert status == "ready", f"{key} failed to ingest"

    # Lay the footage down so there is something to edit.
    client.post(
        f"/api/projects/{pid}/operations",
        json={
            "operations": [{
                "type": "add_clip", "description": "Add footage",
                "scope": {"type": "sequence"},
                "parameters": {"asset_id": ids["video"], "start": 0, "duration": 6},
            }],
            "label": "Add footage",
        },
    )
    return {"id": pid, "assets": ids}


def say(client, project, message: str, **kw) -> dict:
    body = {"message": message, "auto_apply": True, **kw}
    resp = client.post(f"/api/projects/{project['id']}/ai/chat", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def document(client, project) -> dict:
    return client.get(f"/api/projects/{project['id']}").json()["document"]


def clips(doc: dict, kind: str | None = None) -> list[dict]:
    seq = doc["sequences"][0]
    out = [c for t in seq["tracks"] for c in t["clips"]]
    if kind:
        out = [c for c in out if c["kind"] == kind]
    return sorted(out, key=lambda c: c["start"])


def only_video(client, project) -> dict:
    found = clips(document(client, project), "video")
    assert found, "the footage clip disappeared"
    return found[0]


def select_video(client, project) -> list[str]:
    return [only_video(client, project)["id"]]


# ---------------------------------------------------------------------------
# The section 84 list, in order.
# ---------------------------------------------------------------------------


def test_remove_the_first_seconds(client, project):
    before = only_video(client, project)["duration"]
    result = say(client, project, "Remove the first 2 seconds.")
    assert result["applied"] is True, result["errors"]
    assert only_video(client, project)["duration"] == pytest.approx(before - 2, abs=0.05)


def test_split_this_here(client, project):
    before = len(clips(document(client, project), "video"))
    result = say(client, project, "Split this here.", playhead=1.5)
    assert result["applied"] is True, result["errors"]
    assert len(clips(document(client, project), "video")) == before + 1


def test_put_photo_after_this_shot(client, project):
    """Naming a file in the media pool must place that exact file."""
    selection = select_video(client, project)
    result = say(client, project, "Put Photo 4 after this shot.",
                 selection=selection)
    assert result["applied"] is True, result["errors"]

    images = clips(document(client, project), "image")
    assert images, "the photo never reached the timeline"
    assert images[0]["asset_id"] == project["assets"]["photo"]


def test_keep_this_photo_for_three_seconds(client, project):
    images = clips(document(client, project), "image")
    result = say(client, project, "Keep this photo for 3 seconds.",
                 selection=[images[0]["id"]])
    assert result["applied"] is True, result["errors"]
    assert clips(document(client, project), "image")[0]["duration"] == pytest.approx(3.0)


def test_zoom_slowly_into_the_photo(client, project):
    """'Slowly' describes the move here, not the playback rate."""
    images = clips(document(client, project), "image")
    result = say(client, project, "Zoom slowly into the photo.",
                 selection=[images[0]["id"]])
    assert result["applied"] is True, result["errors"]

    photo = clips(document(client, project), "image")[0]
    assert "zoom" in {f["type"] for f in photo["effects"]}
    assert photo["speed"] == pytest.approx(1.0), "a push-in must not change speed"


def test_add_this_music(client, project):
    result = say(client, project, "Add this music.")
    assert result["applied"] is True, result["errors"]

    audio = clips(document(client, project), "audio")
    assert audio, "the music never reached the timeline"
    assert audio[0]["asset_id"] == project["assets"]["music"]


def test_lower_music_to_twenty_percent(client, project):
    result = say(client, project, "Lower music to 20%.")
    assert result["applied"] is True, result["errors"]

    doc = document(client, project)
    assert clips(doc, "audio")[0]["volume"] == pytest.approx(0.2)
    # Naming the music must not touch the picture's own audio.
    assert clips(doc, "video")[0]["volume"] == pytest.approx(1.0)


def test_fade_the_music_out(client, project):
    result = say(client, project, "Fade the music out.")
    assert result["applied"] is True, result["errors"]

    doc = document(client, project)
    assert clips(doc, "audio")[0]["transition_out"]["type"] == "fade"
    assert clips(doc, "video")[0]["transition_out"]["type"] == "none"


def test_make_this_clip_darker(client, project):
    selection = select_video(client, project)
    result = say(client, project, "Make this clip darker.", selection=selection)
    assert result["applied"] is True, result["errors"]

    effects = {f["type"]: f for f in only_video(client, project)["effects"]}
    assert "exposure" in effects
    assert float(effects["exposure"]["parameters"]["stops"]) < 0


def test_increase_contrast(client, project):
    selection = select_video(client, project)
    result = say(client, project, "Increase contrast.", selection=selection)
    assert result["applied"] is True, result["errors"]

    effects = {f["type"]: f for f in only_video(client, project)["effects"]}
    assert "color" in effects
    assert float(effects["color"]["parameters"]["contrast"]) > 1.0


def test_slow_this_section_to_seventy_percent(client, project):
    selection = select_video(client, project)
    result = say(client, project, "Slow this section to 70%.", selection=selection)
    assert result["applied"] is True, result["errors"]
    assert only_video(client, project)["speed"] == pytest.approx(0.7, abs=0.01)


def test_add_the_title_without_quotes(client, project):
    """An unquoted title must not be mistaken for a look instruction."""
    result = say(client, project, "Add the title NIGHT DRIVE.", playhead=1.0)
    assert result["applied"] is True, result["errors"]

    text_clips = clips(document(client, project), "text")
    assert text_clips, "no title was created"
    assert text_clips[-1]["text"]["text"] == "NIGHT DRIVE"

    grades = [
        op for op in result["plan"]["operations"]
        if op["parameters"].get("type") == "color_grade"
    ]
    assert grades == [], "the word NIGHT in a title triggered a night grade"


def test_fade_to_black(client, project):
    selection = select_video(client, project)
    result = say(client, project, "Fade to black.", selection=selection)
    assert result["applied"] is True, result["errors"]
    assert only_video(client, project)["transition_out"]["type"] == "fade"


def test_export_1080p_queues_a_real_render(client, project):
    result = say(client, project, "Export 1080p.")
    assert result["job_ids"], "no render job was queued"
    assert "Queued" in result["reply"]

    assert drain() >= 1
    job = client.get(f"/api/jobs/{result['job_ids'][0]}").json()
    assert job["status"] == "completed", job["error"]

    path = Path(get_storage().local_path(job["result"]["storage_key"]))
    info = probe(path)
    assert (info.primary_video.width, info.primary_video.height) == (1920, 1080)


def test_export_for_tiktok_picks_the_vertical_preset(client, project):
    result = say(client, project, "Now render a TikTok version.")
    assert result["job_ids"]
    assert drain() >= 1

    job = client.get(f"/api/jobs/{result['job_ids'][0]}").json()
    assert job["status"] == "completed", job["error"]
    info = probe(Path(get_storage().local_path(job["result"]["storage_key"])))
    assert (info.primary_video.width, info.primary_video.height) == (1080, 1920)


def test_every_edit_in_this_run_is_undoable(client, project):
    """The whole session must rewind, one version per command."""
    versions = client.get(f"/api/projects/{project['id']}/versions").json()
    assert len(versions) > 10
    assert sum(1 for v in versions if v["source"] == "ai") >= 10

    before = document(client, project)
    assert client.post(f"/api/projects/{project['id']}/undo").json()["ok"] is True
    assert document(client, project) != before
    assert client.post(f"/api/projects/{project['id']}/redo").json()["ok"] is True
    assert document(client, project) == before
