"""Project health checks.

Every issue here is one that renders *something* rather than failing, which
is why it has to be found before export rather than after: a clip whose media
is gone still occupies its place and draws black, and the encode succeeds.
"""
from __future__ import annotations

import pytest

from makma.health import AssetFacts, check_project
from makma.schema.core import (
    Clip,
    ClipKind,
    ProjectDocument,
    TextContent,
    default_sequence,
)


def doc(*clips: Clip, width: int = 1920, height: int = 1080) -> ProjectDocument:
    seq = default_sequence(width=width, height=height, fps=30)
    for clip in clips:
        target = (seq.audio_tracks()[0] if clip.kind == ClipKind.AUDIO
                  else seq.video_tracks()[0])
        target.clips.append(clip)
    return ProjectDocument(name="Health", sequences=[seq])


def facts(**kw) -> dict[str, AssetFacts]:
    base = dict(id="a1", name="clip.mp4", width=1920, height=1080,
                duration=30.0, has_audio=True)
    base.update(kw)
    return {base["id"]: AssetFacts(**base)}


def codes(report) -> list[str]:
    return [i.code for i in report.issues]


def test_a_clean_project_is_ready_to_export():
    report = check_project(
        doc(Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=10)),
        facts(),
    )
    assert report.ready_to_export
    assert [i for i in report.issues if i.severity == "error"] == []


def test_an_empty_sequence_blocks_export():
    report = check_project(doc(), {})
    assert not report.ready_to_export
    assert "empty_sequence" in codes(report)


def test_media_that_is_gone_is_an_error_with_its_position():
    report = check_project(
        doc(Clip(kind=ClipKind.VIDEO, asset_id="missing", start=4, duration=6)),
        facts(),
    )
    assert not report.ready_to_export
    issue = next(i for i in report.issues if i.code == "offline_media")
    assert issue.time == pytest.approx(4.0)
    assert issue.fix, "an error should say what to do about it"


def test_media_that_failed_to_decode_is_an_error():
    report = check_project(
        doc(Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=5)),
        facts(status="failed"),
    )
    assert not report.ready_to_export
    assert "unreadable_media" in codes(report)


def test_upscaling_is_flagged_but_does_not_block():
    report = check_project(
        doc(Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=5)),
        facts(width=640, height=360),
    )
    assert report.ready_to_export, "soft footage is a judgement call, not a blocker"
    assert "low_resolution" in codes(report)


def test_a_clip_running_past_its_source_is_flagged():
    report = check_project(
        doc(Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=20,
                 in_point=25)),
        facts(duration=30.0),
    )
    assert "past_end_of_media" in codes(report)


def test_text_outside_the_safe_area_is_flagged():
    clip = Clip(kind=ClipKind.TEXT, start=0, duration=3,
                text=TextContent(text="EDGE", anchor_y=0.99))
    report = check_project(doc(clip), {})
    assert "text_outside_safe_area" in codes(report)


def test_a_gap_on_the_bottom_track_is_reported_as_information():
    report = check_project(
        doc(
            Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=2),
            Clip(kind=ClipKind.VIDEO, asset_id="a1", start=5, duration=2),
        ),
        facts(),
    )
    gap = next(i for i in report.issues if i.code == "gap")
    assert gap.severity == "info", "black between clips is often deliberate"
    assert gap.time == pytest.approx(2.0)


def test_a_silent_timeline_is_mentioned():
    report = check_project(
        doc(Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=5)),
        facts(has_audio=False),
    )
    assert "no_audio" in codes(report)


def test_clipping_audio_is_flagged():
    report = check_project(
        doc(Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=5)),
        facts(peak_db=0.4),
    )
    assert "audio_clipping" in codes(report)


def test_errors_are_listed_before_warnings():
    report = check_project(
        doc(
            Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=5),
            Clip(kind=ClipKind.VIDEO, asset_id="gone", start=8, duration=5),
        ),
        facts(width=320, height=180),
    )
    severities = [i.severity for i in report.issues]
    assert severities == sorted(
        severities, key=lambda s: {"error": 0, "warning": 1, "info": 2}[s]
    )
