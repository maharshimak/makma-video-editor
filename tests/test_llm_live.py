"""Live model tests.

These are the only tests that talk to a real provider, and they are the only
evidence that the AI Director works at all rather than merely being wired up
plausibly. Everything else in the suite runs against the offline planner or a
scripted provider.

They skip without credentials rather than failing, so the suite stays green on
a machine with no key. That is a real gap, not a passing grade: a green run
on a keyless machine says nothing about whether the model produces usable
plans. `docs/AUDIT_NOTES.md` §2.1 says so too.

Run them with:

    MAKMA_ANTHROPIC_API_KEY=sk-... python -m pytest tests/test_llm_live.py -v

They cost money. Each test is one or two model calls.
"""
from __future__ import annotations

import pytest

from makma.ai.router import Capability, get_router, reset_router
from makma.config import settings
from makma.schema.core import Clip, ProjectDocument, default_sequence
from makma.schema.operations import IMPLEMENTED_OPS, OpType

pytestmark = pytest.mark.live


def _has_credentials() -> bool:
    reset_router()
    return get_router().has(Capability.REASON)


live_only = pytest.mark.skipif(
    not _has_credentials(),
    reason=(
        "No model provider is configured. Set MAKMA_ANTHROPIC_API_KEY (or "
        "MAKMA_OPENAI_API_KEY with MAKMA_OPENAI_BASE_URL) to run the live "
        "director tests. Skipping them does NOT verify the LLM path."
    ),
)


@pytest.fixture
def project():
    """A small, unambiguous project: two shots and a photo."""
    from makma.ai.context import AssetBrief

    document = ProjectDocument(
        name="Live", sequences=[default_sequence("Main", 1920, 1080, 30)])
    track = document.sequences[0].tracks[0]
    track.clips.append(Clip(id="clip_one", kind="video", start=0.0,
                            duration=8.0, asset_id="asset_one"))
    track.clips.append(Clip(id="clip_two", kind="video", start=8.0,
                            duration=6.0, asset_id="asset_two"))

    assets = [
        AssetBrief(id="asset_one", name="rooftop.mp4", kind="video",
                   duration=8.0, width=1920, height=1080, fps=30.0,
                   has_audio=True),
        AssetBrief(id="asset_two", name="street.mp4", kind="video",
                   duration=6.0, width=1920, height=1080, fps=30.0,
                   has_audio=True),
        AssetBrief(id="asset_photo", name="portrait.jpg", kind="image",
                   duration=5.0, width=3000, height=2000),
    ]
    return document, assets


def direct(command: str, project, **kw):
    from makma.ai.director import plan_edit

    document, assets = project
    return plan_edit(command=command, document=document, assets=assets,
                     mode="llm", **kw)


# --- the shape of what comes back ------------------------------------------


@live_only
def test_the_model_returns_a_usable_plan(project):
    plan = direct("cut the first 3 seconds", project)

    assert plan.planner == "llm", (
        f"fell back to the offline planner: {plan.warnings}"
    )
    assert plan.operations, "the model returned no executable operations"
    assert plan.summary, "a plan with no summary tells the user nothing"


@live_only
def test_every_operation_the_model_returns_is_one_this_build_can_run(project):
    plan = direct("make this cinematic and export it for TikTok", project)
    for operation in plan.operations:
        assert operation.type in IMPLEMENTED_OPS, operation.type


@live_only
def test_the_model_references_only_real_ids(project):
    """The validator enforces this; this test proves the model can satisfy it."""
    document, _ = project
    real_clips = {c.id for t in document.sequences[0].tracks for c in t.clips}
    real_tracks = {t.id for t in document.sequences[0].tracks}

    plan = direct("slow the second clip down", project)
    for operation in plan.operations:
        assert set(operation.scope.clip_ids) <= real_clips
        assert set(operation.scope.track_ids) <= real_tracks


@live_only
def test_no_parameter_carries_a_path_or_a_command(project):
    """The boundary claim, checked against real output rather than a fixture."""
    from makma.ai.validation import safety_problems

    plan = direct("give it a warm cinematic grade with a vignette", project)
    assert safety_problems(plan.operations) == []


# --- editorial behaviour ---------------------------------------------------


@live_only
def test_a_slow_motion_request_produces_a_speed_change(project):
    plan = direct("put the first clip in slow motion", project)
    speeds = [o for o in plan.operations if o.type == OpType.SET_SPEED]
    assert speeds, [o.type.value for o in plan.operations]
    assert speeds[0].parameters.get("speed", 1.0) < 1.0


@live_only
def test_a_reframe_request_changes_the_format(project):
    plan = direct("reframe this for Instagram stories", project)
    formats = [o for o in plan.operations if o.type == OpType.SET_FORMAT]
    assert formats, [o.type.value for o in plan.operations]

    parameters = formats[0].parameters
    vertical = (parameters.get("aspect_ratio") == "9:16"
                or (parameters.get("height", 0) > parameters.get("width", 1)))
    assert vertical, parameters


@live_only
def test_an_export_request_becomes_an_export_not_an_operation(project):
    plan = direct("export a 1080p master", project)
    assert plan.exports, "the export was not recognised"
    assert all(o.type != OpType.SET_FORMAT for o in plan.operations), (
        "an export must not silently change the sequence format"
    )


@live_only
def test_a_capability_this_build_lacks_is_declared_not_faked(project):
    plan = direct("remove the person walking behind me", project)
    assert plan.unsupported or "not available" in plan.summary.lower(), (
        f"an unavailable capability was answered as if it worked: {plan.summary}"
    )
    assert all(o.type in IMPLEMENTED_OPS for o in plan.operations)


@live_only
def test_an_instruction_with_nothing_to_do_does_not_invent_an_edit(project):
    plan = direct("what do you think of this footage?", project)
    destructive = [o for o in plan.operations
                   if o.type in (OpType.DELETE_CLIP, OpType.REMOVE_RANGE)]
    assert not destructive, "a question triggered a destructive edit"


# --- accounting ------------------------------------------------------------


@live_only
def test_a_live_call_reports_a_real_cost(project):
    plan = direct("trim two seconds off the end", project)
    assert plan.estimate.usd > 0, (
        "a billed call reported no cost; check the price table covers "
        f"{settings.director_model}"
    )


@live_only
def test_a_live_call_is_recorded_in_the_usage_table(project, client):
    from makma.db import SessionLocal
    from makma.models import ModelUsage

    db = SessionLocal()
    try:
        before = db.query(ModelUsage).count()
    finally:
        db.close()

    direct("add a title that says HELLO", project, project_id="proj_live_test")

    db = SessionLocal()
    try:
        rows = db.query(ModelUsage).all()
        assert len(rows) > before, "the call was not recorded"
        latest = rows[-1]
        assert latest.tokens_in > 0 and latest.tokens_out > 0
        assert latest.cost_known, (
            f"no price is configured for {latest.model}; the usage report "
            "will understate the bill"
        )
    finally:
        db.close()
