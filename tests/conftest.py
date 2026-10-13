"""Shared fixtures.

Video features are only ever considered working once a real file has come out
of FFmpeg and been probed, so these fixtures synthesise tiny real media rather
than mocking the media layer.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
API_ROOT = REPO_ROOT / "apps" / "api"
sys.path.insert(0, str(API_ROOT))

# Point the app at a scratch data directory before anything imports config.
os.environ.setdefault("MAKMA_DATA_DIR", str(REPO_ROOT / "data" / "test"))
os.environ.setdefault("MAKMA_INLINE_WORKER", "false")
os.environ.setdefault("MAKMA_PLANNER_MODE", "heuristic")

from makma.config import settings  # noqa: E402
from makma.media.ffmpeg import ffmpeg_available  # noqa: E402


def pytest_configure(config):
    ok, version = ffmpeg_available()
    if not ok:
        pytest.exit(
            "FFmpeg is required for the test suite but was not found at "
            f"{settings.ffmpeg_path!r}. Run tools/fetch-ffmpeg or set "
            "MAKMA_FFMPEG_PATH.",
            returncode=1,
        )
    config._ffmpeg_version = version


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("media")


def _run(argv: list[str]) -> None:
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"fixture ffmpeg failed:\n{proc.stderr[-2000:]}")


@pytest.fixture(scope="session")
def sample_video(media_dir: Path) -> Path:
    """6s 640x360 25fps colour-bars with a 440Hz tone and hard scene cuts."""
    path = media_dir / "sample.mp4"
    if path.exists():
        return path
    _run([
        settings.ffmpeg_path, "-hide_banner", "-y", "-nostdin",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=6",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=6",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k", "-shortest", str(path),
    ])
    return path


@pytest.fixture(scope="session")
def silent_video(media_dir: Path) -> Path:
    """4s video with no audio track at all."""
    path = media_dir / "silent.mp4"
    if path.exists():
        return path
    _run([
        settings.ffmpeg_path, "-hide_banner", "-y", "-nostdin",
        "-f", "lavfi", "-i", "smptebars=size=320x240:rate=25:duration=4",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        str(path),
    ])
    return path


@pytest.fixture(scope="session")
def sample_audio(media_dir: Path) -> Path:
    path = media_dir / "tone.m4a"
    if path.exists():
        return path
    _run([
        settings.ffmpeg_path, "-hide_banner", "-y", "-nostdin",
        "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000:duration=5",
        "-c:a", "aac", "-b:a", "96k", str(path),
    ])
    return path


@pytest.fixture(scope="session")
def sample_image(media_dir: Path) -> Path:
    path = media_dir / "still.png"
    if path.exists():
        return path
    _run([
        settings.ffmpeg_path, "-hide_banner", "-y", "-nostdin",
        "-f", "lavfi", "-i", "color=c=0x2255aa:size=480x270",
        "-frames:v", "1", str(path),
    ])
    return path


@pytest.fixture
def out_path(tmp_path: Path) -> Path:
    return tmp_path / "out.mp4"


# ---------------------------------------------------------------------------
# A shared app instance for test files that do not need their own project
# state. Files that manage their own data directory (the end-to-end suites)
# define `client` themselves, and a module-level fixture shadows this one.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def app_client(tmp_path_factory):
    from fastapi.testclient import TestClient

    from makma.db import Base, engine
    from makma.main import app
    import makma.storage.local as local_storage

    data_dir = tmp_path_factory.mktemp("shared")
    object.__setattr__(settings, "data_dir", data_dir)
    (data_dir / "storage").mkdir(parents=True, exist_ok=True)
    (data_dir / "tmp").mkdir(parents=True, exist_ok=True)
    local_storage._storage = local_storage.LocalStorage(data_dir / "storage")

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def client(app_client):
    return app_client


@pytest.fixture(scope="session")
def shared_project(client) -> str:
    resp = client.post("/api/projects", json={"name": "Shared", "width": 320,
                                              "height": 180, "fps": 25})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.fixture(scope="session")
def uploaded(client, shared_project, sample_video) -> dict:
    """One ingested asset, for tests that need a real signed media URL."""
    from makma.jobs.queue import claim_next, run_job

    with open(sample_video, "rb") as fh:
        resp = client.post(
            f"/api/projects/{shared_project}/assets",
            files={"file": ("shared.mp4", fh, "video/mp4")},
        )
    assert resp.status_code == 201, resp.text

    for _ in range(10):
        job = claim_next("pytest-shared")
        if job is None:
            break
        run_job(job)

    detail = client.get(
        f"/api/projects/{shared_project}/assets/{resp.json()['id']}"
    )
    assert detail.status_code == 200
    return detail.json()
