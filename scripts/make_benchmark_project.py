"""Build the benchmark project.

The performance targets in the docs are meaningless without something to
measure them against, and "a project I happened to have open" is not a
baseline. This builds the same project every time:

    30 minutes, 100 video clips, 30 photos, 5 audio clips, 10 titles,
    145 clips over 7 tracks, 480 beat markers, a colour grade on a third of
    the visual clips, and keyframed motion on ten.

Media is synthesised with FFmpeg rather than shipped, so the repo stays small
and the fixture is reproducible on any machine.

    python scripts/make_benchmark_project.py
    python scripts/make_benchmark_project.py --name "Bench 2" --keep-media

It writes through the real API, so what comes out is a project the editor can
open, not a hand-built JSON blob that only exists in a test.
"""
from __future__ import annotations

import argparse
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))

from makma.config import settings  # noqa: E402

# Drain the queue from this process only. With the inline worker also running,
# the drain loop can return while a job it never saw is still in flight, and
# the script reports a healthy asset as failed.
object.__setattr__(settings, "inline_worker", False)

#: Fixed, so two runs produce the same project.
SEED = 20260910

VIDEO_CLIPS = 100
PHOTOS = 30
AUDIO_CLIPS = 5
TITLES = 10
VIDEO_TRACKS = 5
TARGET_MINUTES = 30

#: Distinct source files, reused across clips. A real 30-minute edit does not
#: have 100 separate camera files either.
SOURCE_VIDEOS = 8
SOURCE_PHOTOS = 6
SOURCE_AUDIO = 3


def run(*args: str) -> None:
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"{args[0]} failed:\n{result.stderr[-2000:]}"
        )


def synthesise_media(directory: Path) -> dict[str, list[Path]]:
    """Make source files with FFmpeg. Different content, so proxies differ."""
    ffmpeg = settings.ffmpeg_path
    media: dict[str, list[Path]] = {"video": [], "image": [], "audio": []}

    print(f"synthesising media in {directory}")
    for i in range(SOURCE_VIDEOS):
        path = directory / f"shot_{i:02d}.mp4"
        run(ffmpeg, "-y", "-v", "error",
            "-f", "lavfi", "-i",
            f"testsrc2=size=1280x720:rate=30:duration=24,hue=h={i * 45}",
            "-f", "lavfi", "-i", f"sine=frequency={220 + i * 40}:duration=24",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(path))
        media["video"].append(path)

    for i in range(SOURCE_PHOTOS):
        path = directory / f"photo_{i:02d}.jpg"
        run(ffmpeg, "-y", "-v", "error",
            "-f", "lavfi", "-i",
            f"gradients=size=1920x1280:duration=1:speed=0.1:nb_colors=3:seed={i}",
            "-frames:v", "1", str(path))
        media["image"].append(path)

    for i in range(SOURCE_AUDIO):
        path = directory / f"music_{i:02d}.m4a"
        run(ffmpeg, "-y", "-v", "error",
            "-f", "lavfi", "-i",
            f"sine=frequency={110 * (i + 1)}:duration=200",
            "-c:a", "aac", str(path))
        media["audio"].append(path)

    return media


def build(name: str, media_dir: Path) -> str:
    from fastapi.testclient import TestClient

    from makma.jobs.queue import claim_next, run_job
    from makma.main import app

    rng = random.Random(SEED)
    started = time.time()

    with TestClient(app) as client:
        project = client.post("/api/projects", json={
            "name": name, "width": 1920, "height": 1080, "fps": 30,
        }).json()
        project_id = project["id"]
        print(f"project {project_id}")

        # --- upload and ingest -------------------------------------------
        media = synthesise_media(media_dir)
        assets: dict[str, list[str]] = {"video": [], "image": [], "audio": []}
        for kind, paths in media.items():
            for path in paths:
                with open(path, "rb") as fh:
                    resp = client.post(
                        f"/api/projects/{project_id}/assets",
                        files={"file": (path.name, fh, "application/octet-stream")},
                    )
                resp.raise_for_status()
                assets[kind].append(resp.json()["id"])

        print("ingesting...")
        while (job := claim_next("bench")) is not None:
            run_job(job)

        # An API server may be running against the same database and may have
        # claimed some of these jobs itself, in which case the drain loop above
        # finds nothing left to take while work is still in flight. Wait for a
        # terminal state rather than assuming this process did all of it.
        deadline = time.time() + 300
        while True:
            ready = client.get(f"/api/projects/{project_id}/assets").json()
            pending = [a for a in ready if a["status"] not in ("ready", "failed")]
            if not pending:
                break
            if time.time() > deadline:
                raise RuntimeError(
                    "gave up waiting for: "
                    + ", ".join(f"{a['name']} ({a['status']})" for a in pending)
                )
            print(f"  waiting on {len(pending)} asset(s)...")
            time.sleep(3)
            while (job := claim_next("bench")) is not None:
                run_job(job)

        broken = [f"{a['name']}: {a.get('error') or a['status']}"
                  for a in ready if a["status"] != "ready"]
        if broken:
            raise RuntimeError("assets failed to ingest:\n  "
                               + "\n  ".join(broken))

        # --- tracks -------------------------------------------------------
        operations: list[dict] = []
        target_seconds = float(TARGET_MINUTES * 60)

        # --- visual clips, spread across the whole 30 minutes -------------
        # Butting clips together fills only the first few minutes and leaves
        # the rest of the timeline empty, which makes the fixture flatter than
        # anything real: scrolling past minute five would find nothing to
        # draw and the virtualization numbers would look better than they are.
        # Each track instead gets its own stride across the full duration.
        visual_plan: list[tuple[int, str]] = (
            [(i % VIDEO_TRACKS, "video") for i in range(VIDEO_CLIPS)]
            + [(i % VIDEO_TRACKS, "image") for i in range(PHOTOS)]
        )
        per_track: dict[int, list[str]] = {}
        for track_index, kind in visual_plan:
            per_track.setdefault(track_index, []).append(kind)

        for track_index, kinds in per_track.items():
            rng.shuffle(kinds)
            stride = target_seconds / len(kinds)
            for position, kind in enumerate(kinds):
                duration = round(
                    rng.uniform(4.0, 14.0) if kind == "video"
                    else rng.uniform(2.5, 6.0), 2)
                # Jitter within the slot, so tracks do not line up in columns.
                offset = rng.uniform(0.0, max(0.0, stride - duration))
                start = round(position * stride + offset, 2)
                parameters = {
                    "asset_id": rng.choice(assets[kind]),
                    "track_index": track_index,
                    "start": start,
                    "duration": duration,
                }
                if kind == "video":
                    parameters["in_point"] = round(rng.uniform(0, 8.0), 2)
                operations.append({
                    "type": "add_clip",
                    "description": f"{kind.title()} at {start:.0f}s",
                    "scope": {"type": "sequence"},
                    "parameters": parameters,
                })

        for i in range(AUDIO_CLIPS):
            operations.append({
                "type": "add_clip",
                "description": f"Music bed {i + 1}",
                "scope": {"type": "sequence"},
                "parameters": {
                    "asset_id": rng.choice(assets["audio"]),
                    "start": round(i * (target_seconds / AUDIO_CLIPS), 2),
                    "duration": round(target_seconds / AUDIO_CLIPS, 2),
                    "volume": 0.4,
                },
            })

        for i in range(TITLES):
            operations.append({
                "type": "add_text",
                "description": f"Title {i + 1}",
                "scope": {"type": "sequence"},
                "parameters": {
                    "text": f"CHAPTER {i + 1}",
                    "start": round(i * (target_seconds / TITLES), 2),
                    "duration": 4.0,
                    "position": "bottom",
                    "font_size": 72,
                },
            })

        # A beat grid, because markers are drawn per pixel too.
        operations.append({
            "type": "add_marker",
            "description": "Beat grid",
            "scope": {"type": "sequence"},
            "parameters": {
                "times": [round(t * 0.5, 2) for t in range(480)],
                "kind": "beat",
            },
        })

        print(f"applying {len(operations)} operations...")
        resp = client.post(f"/api/projects/{project_id}/operations", json={
            "operations": operations, "label": "Benchmark layout",
        })
        resp.raise_for_status()
        body = resp.json()
        if not body["ok"]:
            raise RuntimeError(f"layout failed: {body['errors'][:3]}")

        # --- effects and animation on a subset ----------------------------
        document = body["document"]
        sequence = document["sequences"][0]
        visual = [c["id"] for t in sequence["tracks"] if t["kind"] == "video"
                  for c in t["clips"]]
        rng.shuffle(visual)

        extra: list[dict] = []
        for clip_id in visual[:len(visual) // 3]:
            extra.append({
                "type": "add_effect",
                "description": "Grade",
                "scope": {"type": "clip", "clip_ids": [clip_id]},
                "parameters": {
                    "type": "color_grade",
                    "parameters": {"preset": rng.choice(["teal_orange", "warm",
                                                         "cool", "noir"])},
                },
            })
        for clip_id in visual[:10]:
            extra.append({
                "type": "set_keyframes",
                "description": "Drift",
                "scope": {"type": "clip", "clip_ids": [clip_id]},
                "parameters": {
                    "property": "x",
                    "relative": True,
                    "keyframes": [
                        {"time": 0.0, "value": -40, "easing": "ease_in_out"},
                        {"time": 1.0, "value": 40},
                    ],
                },
            })

        print(f"applying {len(extra)} effect and animation operations...")
        resp = client.post(f"/api/projects/{project_id}/operations", json={
            "operations": extra, "label": "Benchmark looks",
        })
        resp.raise_for_status()
        if not resp.json()["ok"]:
            raise RuntimeError(f"looks failed: {resp.json()['errors'][:3]}")

        # --- report -------------------------------------------------------
        final = client.get(f"/api/projects/{project_id}").json()["document"]
        sequence = final["sequences"][0]
        clips = [c for t in sequence["tracks"] for c in t["clips"]]
        duration = max((c["start"] + c["duration"] for c in clips), default=0.0)
        video = sum(1 for t in sequence["tracks"] if t["kind"] == "video")
        audio = sum(1 for t in sequence["tracks"] if t["kind"] == "audio")
        keyframed = sum(
            1 for c in clips if (c.get("transform") or {}).get("keyframes")
        )

        print()
        print(f"  project id     {project_id}")
        print(f"  duration       {duration / 60:.1f} min")
        print(f"  tracks         {len(sequence['tracks'])} "
              f"({video} video, {audio} audio)")
        print(f"  clips          {len(clips)}")
        print(f"  effects        {sum(len(c.get('effects') or []) for c in clips)}")
        print(f"  keyframed      {keyframed}")
        print(f"  markers        {len(sequence['markers'])}")
        print(f"  built in       {time.time() - started:.1f}s")
        print()
        print(f"  open  http://localhost:3000/editor/{project_id}")
        return project_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="Benchmark 30min")
    parser.add_argument("--keep-media", action="store_true",
                        help="leave the synthesised source files on disk")
    args = parser.parse_args()

    if args.keep_media:
        directory = REPO_ROOT / "data" / "benchmark-media"
        directory.mkdir(parents=True, exist_ok=True)
        build(args.name, directory)
        print(f"  media kept in  {directory}")
    else:
        with tempfile.TemporaryDirectory(prefix="makma-bench-") as tmp:
            build(args.name, Path(tmp))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
