"""Concurrent editing and job-queue consistency.

Two tabs on one project is the normal case, not an edge case. Without
optimistic concurrency the second save silently discards whatever the first
one did, and the user never learns that it happened.
"""
from __future__ import annotations

import threading

import pytest

from makma.jobs.queue import claim_next, run_job


def make_project(client, name: str = "Concurrent") -> str:
    resp = client.post("/api/projects", json={"name": name, "width": 320,
                                              "height": 180, "fps": 25})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def read(client, project_id: str) -> dict:
    return client.get(f"/api/projects/{project_id}").json()


def add_text(client, project_id: str, text: str, expected: int | None = None):
    body = {
        "operations": [{
            "type": "add_text", "description": f"Add {text}",
            "scope": {"type": "sequence"},
            "parameters": {"text": text, "start": 0, "duration": 2},
        }],
        "label": f"Add {text}",
    }
    if expected is not None:
        body["expected_version"] = expected
    return client.post(f"/api/projects/{project_id}/operations", json=body)


# --- version reporting -----------------------------------------------------


def test_the_project_reports_a_version_that_advances_with_each_edit(client):
    pid = make_project(client, "Version counter")
    first = read(client, pid)["version"]

    add_text(client, pid, "ONE")
    second = read(client, pid)["version"]
    assert second > first

    add_text(client, pid, "TWO")
    assert read(client, pid)["version"] > second


def test_an_edit_returns_the_version_it_produced(client):
    pid = make_project(client, "Returned version")
    resp = add_text(client, pid, "ONE")
    assert resp.json()["version"] == read(client, pid)["version"]


# --- conflict detection ----------------------------------------------------


def test_a_stale_write_is_refused_rather_than_overwriting(client):
    """Two tabs read version N; the second must not discard the first."""
    pid = make_project(client, "Two tabs")
    version = read(client, pid)["version"]

    tab_a = add_text(client, pid, "FROM TAB A", expected=version)
    assert tab_a.status_code == 200

    tab_b = add_text(client, pid, "FROM TAB B", expected=version)
    assert tab_b.status_code == 409, "the stale write was accepted"

    detail = tab_b.json()["detail"]
    assert detail["code"] == "VERSION_CONFLICT"
    assert detail["expected_version"] == version
    assert detail["current_version"] > version

    # Tab A's work survived.
    document = read(client, pid)["document"]
    texts = [c["text"]["text"] for t in document["sequences"][0]["tracks"]
             for c in t["clips"] if c["kind"] == "text"]
    assert texts == ["FROM TAB A"]


def test_a_conflict_hands_back_the_current_document(client):
    """The client should be able to recover without a reload."""
    pid = make_project(client, "Conflict payload")
    version = read(client, pid)["version"]
    add_text(client, pid, "WINNER", expected=version)

    conflict = add_text(client, pid, "LOSER", expected=version).json()["detail"]
    texts = [c["text"]["text"] for t in conflict["document"]["sequences"][0]["tracks"]
             for c in t["clips"] if c["kind"] == "text"]
    assert texts == ["WINNER"], "the 409 should carry the state that won"


def test_retrying_against_the_new_version_succeeds(client):
    pid = make_project(client, "Retry")
    version = read(client, pid)["version"]
    add_text(client, pid, "FIRST", expected=version)

    conflict = add_text(client, pid, "SECOND", expected=version)
    current = conflict.json()["detail"]["current_version"]

    retry = add_text(client, pid, "SECOND", expected=current)
    assert retry.status_code == 200

    document = read(client, pid)["document"]
    texts = sorted(c["text"]["text"] for t in document["sequences"][0]["tracks"]
                   for c in t["clips"] if c["kind"] == "text")
    assert texts == ["FIRST", "SECOND"]


def test_omitting_the_version_keeps_last_write_wins(client):
    """Internal callers that have no version should not be blocked."""
    pid = make_project(client, "No version")
    add_text(client, pid, "ONE")
    assert add_text(client, pid, "TWO").status_code == 200


def test_undo_after_a_concurrent_edit_walks_real_history(client):
    pid = make_project(client, "Undo after conflict")
    add_text(client, pid, "ONE")
    add_text(client, pid, "TWO")

    undo = client.post(f"/api/projects/{pid}/undo").json()
    texts = [c["text"]["text"] for t in undo["document"]["sequences"][0]["tracks"]
             for c in t["clips"] if c["kind"] == "text"]
    assert texts == ["ONE"], "undo should step back exactly one edit"

    redo = client.post(f"/api/projects/{pid}/redo").json()
    assert len([c for t in redo["document"]["sequences"][0]["tracks"]
                for c in t["clips"] if c["kind"] == "text"]) == 2


# --- job queue -------------------------------------------------------------


def test_a_job_is_claimed_by_exactly_one_worker(client, sample_video):
    """Two workers racing for the same queue must not both take a job."""
    pid = make_project(client, "Job race")
    for i in range(6):
        with open(sample_video, "rb") as fh:
            client.post(f"/api/projects/{pid}/assets",
                        files={"file": (f"race{i}.mp4", fh, "video/mp4")})

    claimed: list[str] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        while True:
            job = claim_next(name)
            if job is None:
                return
            with lock:
                claimed.append(job.id)
            run_job(job)

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert len(claimed) == len(set(claimed)), "a job was claimed twice"

    assets = client.get(f"/api/projects/{pid}/assets").json()
    assert len(assets) == 6
    assert all(a["status"] in ("ready", "failed") for a in assets), (
        "a job was left mid-flight"
    )


def test_several_renders_queue_without_corrupting_each_other(client, sample_video):
    pid = make_project(client, "Render queue")
    with open(sample_video, "rb") as fh:
        asset = client.post(f"/api/projects/{pid}/assets",
                            files={"file": ("q.mp4", fh, "video/mp4")}).json()
    while (job := claim_next("prep")) is not None:
        run_job(job)

    client.post(f"/api/projects/{pid}/operations", json={
        "operations": [{"type": "add_clip", "description": "Add",
                        "scope": {"type": "sequence"},
                        "parameters": {"asset_id": asset["id"], "start": 0,
                                       "duration": 1.0}}],
        "label": "Add",
    })

    ids = [
        client.post(f"/api/projects/{pid}/render",
                    json={"preset": "draft_540p"}).json()["id"]
        for _ in range(3)
    ]
    while (job := claim_next("render")) is not None:
        run_job(job)

    results = [client.get(f"/api/jobs/{i}").json() for i in ids]
    assert all(r["status"] == "completed" for r in results), [
        r["error"] for r in results
    ]
    # Each render wrote to its own key rather than fighting over one path.
    keys = {r["result"]["storage_key"] for r in results}
    assert len(keys) == 3


# --- stalled job recovery --------------------------------------------------


def test_a_job_whose_worker_died_is_recovered(client, sample_video):
    """The recovery pass must actually run, not raise into the worker loop.

    Timestamps are written aware and stored naive, so comparing them raised
    TypeError. Because the only comparison lived inside the worker's loop, the
    failure was a log line rather than anything visible, and stalled jobs were
    never recovered at all.
    """
    from datetime import datetime, timedelta

    from makma.db import SessionLocal
    from makma.jobs.queue import requeue_stalled
    from makma.models import Job

    pid = make_project(client, "Stalled")
    with open(sample_video, "rb") as fh:
        client.post(f"/api/projects/{pid}/assets",
                    files={"file": ("stalled.mp4", fh, "video/mp4")})

    job = claim_next("doomed")
    assert job is not None

    # Pretend the worker died an hour ago without finishing.
    db = SessionLocal()
    try:
        row = db.get(Job, job.id)
        row.heartbeat_at = datetime.utcnow() - timedelta(hours=1)
        db.commit()
    finally:
        db.close()

    assert requeue_stalled(older_than_minutes=30) == 1

    db = SessionLocal()
    try:
        assert db.get(Job, job.id).status == "queued"
    finally:
        db.close()


def test_a_healthy_job_is_left_alone(client, sample_video):
    from makma.jobs.queue import requeue_stalled

    pid = make_project(client, "Healthy")
    with open(sample_video, "rb") as fh:
        client.post(f"/api/projects/{pid}/assets",
                    files={"file": ("healthy.mp4", fh, "video/mp4")})

    job = claim_next("alive")
    assert job is not None
    assert requeue_stalled(older_than_minutes=30) == 0
    run_job(job)
