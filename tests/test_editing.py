"""Edit engine tests.

The operation layer is the single path that mutates a timeline, so its edge
cases -- ripple deletes that straddle a clip, trims that would run past the
source, atomic failure -- are tested directly rather than only through the API.
"""
from __future__ import annotations

import pytest

from makma.editing import AssetInfo, EditContext, apply_operations
from makma.schema.core import Clip, ClipKind, ProjectDocument, default_sequence
from makma.schema.operations import EditOperation, OpType, Scope


def make_doc(*clips: Clip) -> ProjectDocument:
    seq = default_sequence(width=1920, height=1080, fps=30)
    for clip in clips:
        target = seq.audio_tracks()[0] if clip.kind == ClipKind.AUDIO else seq.video_tracks()[0]
        target.clips.append(clip)
    return ProjectDocument(name="Test", sequences=[seq])


def ctx(**kw) -> EditContext:
    return EditContext(
        assets={
            "a1": AssetInfo(id="a1", name="a.mp4", duration=30.0, has_audio=True),
            "img": AssetInfo(id="img", name="p.png", kind="image", duration=0.0),
        },
        **kw,
    )


def op(type_: OpType, scope: Scope | None = None, **params) -> EditOperation:
    return EditOperation(type=type_, scope=scope or Scope(type="all"),
                         parameters=params, description="test")


def clips_of(doc: ProjectDocument) -> list[Clip]:
    return sorted(doc.active().all_clips(), key=lambda c: c.start)


# --- add / delete ----------------------------------------------------------


def test_add_clip_appends_after_the_last_clip():
    doc = make_doc(Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=4))
    out, result = apply_operations(
        doc, [op(OpType.ADD_CLIP, asset_id="a1", start="append", duration=3)], ctx()
    )
    assert result.ok
    assert [c.start for c in clips_of(out)] == [0, 4]


def test_add_clip_with_unknown_asset_fails_clearly():
    doc = make_doc()
    out, result = apply_operations(doc, [op(OpType.ADD_CLIP, asset_id="nope")], ctx())
    assert not result.ok
    assert "Unknown asset" in result.results[0].message
    assert out is doc, "a failed operation must not change the document"


def test_image_clip_gets_a_sensible_default_duration():
    doc = make_doc()
    out, result = apply_operations(doc, [op(OpType.ADD_CLIP, asset_id="img")], ctx())
    assert result.ok
    assert clips_of(out)[0].duration == pytest.approx(5.0)


def test_delete_with_ripple_closes_the_gap():
    doc = make_doc(
        Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=3),
        Clip(kind=ClipKind.VIDEO, asset_id="a1", start=3, duration=3),
        Clip(kind=ClipKind.VIDEO, asset_id="a1", start=6, duration=3),
    )
    target = clips_of(doc)[1].id
    out, result = apply_operations(
        doc,
        [op(OpType.DELETE_CLIP, Scope(type="clip", clip_ids=[target]), ripple=True)],
        ctx(),
    )
    assert result.ok
    assert [c.start for c in clips_of(out)] == [0, 3]


# --- trim ------------------------------------------------------------------


def test_trim_head_advances_the_source_in_point():
    """Trimming the head must not make the picture jump."""
    doc = make_doc(Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=10,
                        in_point=2))
    cid = clips_of(doc)[0].id
    out, result = apply_operations(
        doc, [op(OpType.TRIM_CLIP, Scope(type="clip", clip_ids=[cid]), start=3)], ctx()
    )
    assert result.ok
    clip = clips_of(out)[0]
    assert clip.start == pytest.approx(3)
    assert clip.duration == pytest.approx(7)
    assert clip.in_point == pytest.approx(5), "in-point did not follow the trim"


def test_trim_head_cannot_run_past_the_start_of_the_source():
    doc = make_doc(Clip(kind=ClipKind.VIDEO, asset_id="a1", start=5, duration=5,
                        in_point=1))
    cid = clips_of(doc)[0].id
    out, _ = apply_operations(
        doc, [op(OpType.TRIM_CLIP, Scope(type="clip", clip_ids=[cid]), start=0)], ctx()
    )
    clip = clips_of(out)[0]
    assert clip.in_point == 0
    assert clip.start == pytest.approx(4), "clip may only extend by the 1s available"


def test_trim_to_nothing_is_refused():
    doc = make_doc(Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=5))
    cid = clips_of(doc)[0].id
    _out, result = apply_operations(
        doc,
        [op(OpType.TRIM_CLIP, Scope(type="clip", clip_ids=[cid]), start=0, end=0.001)],
        ctx(),
    )
    assert not result.ok
    assert "one frame" in result.results[0].message


# --- split -----------------------------------------------------------------


def test_split_divides_the_clip_and_keeps_the_source_continuous():
    doc = make_doc(Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=10,
                        in_point=4))
    out, result = apply_operations(doc, [op(OpType.SPLIT_CLIP, time=6)], ctx())
    assert result.ok
    left, right = clips_of(out)
    assert left.duration == pytest.approx(6)
    assert right.start == pytest.approx(6)
    assert right.duration == pytest.approx(4)
    assert right.in_point == pytest.approx(10), "the second half must continue the source"
    assert left.id != right.id


def test_split_respects_clip_speed():
    doc = make_doc(Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=10,
                        in_point=0, speed=0.5))
    out, _ = apply_operations(doc, [op(OpType.SPLIT_CLIP, time=4)], ctx())
    right = clips_of(out)[1]
    # 4s of a 0.5x clip consumes 2s of source.
    assert right.in_point == pytest.approx(2)


def test_split_outside_any_clip_is_an_error_not_a_no_op():
    doc = make_doc(Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=3))
    _out, result = apply_operations(doc, [op(OpType.SPLIT_CLIP, time=9)], ctx())
    assert not result.ok
    assert "nothing to split" in result.results[0].message


# --- remove_range ----------------------------------------------------------


def test_remove_range_inside_one_clip_splits_and_closes_the_gap():
    doc = make_doc(Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=10))
    out, result = apply_operations(
        doc, [op(OpType.REMOVE_RANGE, start=3, end=5, ripple=True)], ctx()
    )
    assert result.ok
    result_clips = clips_of(out)
    assert len(result_clips) == 2
    assert result_clips[0].duration == pytest.approx(3)
    assert result_clips[1].start == pytest.approx(3), "the gap was not closed"
    assert result_clips[1].in_point == pytest.approx(5)
    assert out.active().duration == pytest.approx(8)


def test_remove_range_drops_clips_fully_inside_it():
    doc = make_doc(
        Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=2),
        Clip(kind=ClipKind.VIDEO, asset_id="a1", start=2, duration=2),
        Clip(kind=ClipKind.VIDEO, asset_id="a1", start=4, duration=2),
    )
    out, result = apply_operations(
        doc, [op(OpType.REMOVE_RANGE, start=2, end=4, ripple=True)], ctx()
    )
    assert result.ok
    assert len(clips_of(out)) == 2
    assert [round(c.start, 3) for c in clips_of(out)] == [0, 2]


def test_remove_range_trims_a_clip_that_straddles_the_start():
    doc = make_doc(Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=6))
    out, _ = apply_operations(
        doc, [op(OpType.REMOVE_RANGE, start=4, end=8, ripple=True)], ctx()
    )
    assert clips_of(out)[0].duration == pytest.approx(4)


def test_remove_range_rejects_a_backwards_range():
    doc = make_doc(Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=6))
    _out, result = apply_operations(
        doc, [op(OpType.REMOVE_RANGE, start=5, end=2)], ctx()
    )
    assert not result.ok
    assert "must be after" in result.results[0].message


# --- speed -----------------------------------------------------------------


def test_slowing_a_clip_lengthens_it_and_keeps_the_same_source():
    doc = make_doc(Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=4))
    out, result = apply_operations(doc, [op(OpType.SET_SPEED, speed=0.5)], ctx())
    assert result.ok
    clip = clips_of(out)[0]
    assert clip.speed == pytest.approx(0.5)
    assert clip.duration == pytest.approx(8)
    assert clip.source_duration == pytest.approx(4), "source window must be preserved"


def test_speed_of_zero_is_refused():
    doc = make_doc(Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=4))
    _out, result = apply_operations(doc, [op(OpType.SET_SPEED, speed=0)], ctx())
    assert not result.ok


# --- effects ---------------------------------------------------------------


def test_adding_the_same_effect_twice_refines_rather_than_stacks():
    doc = make_doc(Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=4))
    doc, _ = apply_operations(
        doc, [op(OpType.ADD_EFFECT, type="color_grade", parameters={"preset": "warm"})],
        ctx(),
    )
    doc, _ = apply_operations(
        doc, [op(OpType.ADD_EFFECT, type="color_grade", parameters={"preset": "night"})],
        ctx(),
    )
    effects = clips_of(doc)[0].effects
    assert len(effects) == 1
    assert effects[0].parameters["preset"] == "night"


def test_unknown_effect_type_lists_what_is_available():
    doc = make_doc(Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=4))
    _out, result = apply_operations(doc, [op(OpType.ADD_EFFECT, type="hologram")], ctx())
    assert not result.ok
    assert "not a known effect" in result.results[0].message
    assert "color_grade" in result.results[0].message


# --- scope -----------------------------------------------------------------


def test_selection_scope_targets_only_the_selected_clips():
    doc = make_doc(
        Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=3),
        Clip(kind=ClipKind.VIDEO, asset_id="a1", start=3, duration=3),
    )
    first = clips_of(doc)[0].id
    out, result = apply_operations(
        doc,
        [op(OpType.ADD_EFFECT, Scope(type="selection"), type="film_grain")],
        ctx(selection=[first]),
    )
    assert result.ok
    by_id = {c.id: c for c in clips_of(out)}
    assert len(by_id[first].effects) == 1
    assert all(len(c.effects) == 0 for c in clips_of(out) if c.id != first)


def test_empty_selection_means_the_whole_sequence():
    doc = make_doc(
        Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=3),
        Clip(kind=ClipKind.VIDEO, asset_id="a1", start=3, duration=3),
    )
    out, result = apply_operations(
        doc, [op(OpType.ADD_EFFECT, Scope(type="selection"), type="vignette")], ctx()
    )
    assert result.ok
    assert all(len(c.effects) == 1 for c in clips_of(out))


def test_range_scope_only_touches_overlapping_clips():
    doc = make_doc(
        Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=3),
        Clip(kind=ClipKind.VIDEO, asset_id="a1", start=5, duration=3),
    )
    out, result = apply_operations(
        doc,
        [op(OpType.ADD_EFFECT, Scope(type="range", start=4, end=9), type="blur")],
        ctx(),
    )
    assert result.ok
    assert [len(c.effects) for c in clips_of(out)] == [0, 1]


# --- atomicity -------------------------------------------------------------


def test_a_failing_operation_rolls_back_the_whole_plan():
    doc = make_doc(Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=6))
    out, result = apply_operations(
        doc,
        [
            op(OpType.ADD_EFFECT, type="film_grain"),
            op(OpType.SET_SPEED, speed=-1),           # invalid
            op(OpType.ADD_EFFECT, type="vignette"),
        ],
        ctx(),
        atomic=True,
    )
    assert not result.ok
    assert clips_of(out)[0].effects == [], "a partial plan was left applied"
    assert len(result.results) == 2, "execution should stop at the failure"


def test_format_change_rejects_an_unknown_aspect_ratio():
    doc = make_doc(Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=4))
    _out, result = apply_operations(
        doc, [op(OpType.SET_FORMAT, aspect_ratio="7:3")], ctx()
    )
    assert not result.ok
    assert "Unsupported aspect ratio" in result.results[0].message


def test_vertical_sequence_can_be_created_from_the_active_one():
    doc = make_doc(Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=4))
    out, result = apply_operations(
        doc,
        [op(OpType.CREATE_SEQUENCE, Scope(type="sequence"), name="Vertical",
            width=1080, height=1920, copy_from_active=True)],
        ctx(),
    )
    assert result.ok
    assert len(out.sequences) == 2
    vertical = out.active()
    assert (vertical.width, vertical.height) == (1080, 1920)
    assert len(vertical.all_clips()) == 1
    assert vertical.all_clips()[0].transform.fit == "cover", "reframe must fill"


# --- keyframes -------------------------------------------------------------
# One operation creates every curve, so a diamond clicked in the inspector and
# a curve the director asked for are the same data.


def kf_op(scope: Scope | None = None, **params) -> EditOperation:
    return op(OpType.SET_KEYFRAMES, scope or Scope(type="all"), **params)


def only_clip(doc: ProjectDocument) -> Clip:
    return clips_of(doc)[0]


def test_animating_a_property_stores_a_sorted_curve():
    doc = make_doc(Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=4))
    out, result = apply_operations(
        doc,
        [kf_op(property="x", keyframes=[
            {"time": 4, "value": 200, "easing": "ease_out"},
            {"time": 0, "value": -200},
        ])],
        ctx(),
    )
    assert result.ok
    curve = only_clip(out).transform.keyframes["x"]
    assert [k.time for k in curve] == [0.0, 4.0], "keyframes must be time-ordered"
    assert curve[1].easing.value == "ease_out"


def test_a_curve_is_sampled_with_its_easing():
    doc = make_doc(Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=4))
    out, _ = apply_operations(
        doc,
        [kf_op(property="x", keyframes=[
            {"time": 0, "value": 0, "easing": "linear"},
            {"time": 4, "value": 100, "easing": "linear"},
        ])],
        ctx(),
    )
    transform = only_clip(out).transform
    assert transform.value_at("x", 0) == pytest.approx(0)
    assert transform.value_at("x", 2) == pytest.approx(50)
    assert transform.value_at("x", 4) == pytest.approx(100)
    # Outside the curve it holds the end values rather than extrapolating.
    assert transform.value_at("x", 99) == pytest.approx(100)


def test_relative_times_are_scaled_to_the_clip():
    doc = make_doc(Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=6))
    out, _ = apply_operations(
        doc,
        [kf_op(property="opacity", relative=True,
               keyframes=[{"time": 0, "value": 0}, {"time": 0.5, "value": 1}])],
        ctx(),
    )
    assert [k.time for k in only_clip(out).transform.keyframes["opacity"]] == [0.0, 3.0]


def test_an_empty_curve_removes_the_animation():
    doc = make_doc(Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=4))
    doc, _ = apply_operations(
        doc, [kf_op(property="x", keyframes=[{"time": 0, "value": 5}])], ctx()
    )
    assert "x" in only_clip(doc).transform.keyframes

    out, result = apply_operations(doc, [kf_op(property="x", keyframes=[])], ctx())
    assert result.ok
    assert "x" not in only_clip(out).transform.keyframes


def test_animating_scale_is_refused_with_the_alternative_named():
    """The editor must not offer an animation the render cannot produce."""
    doc = make_doc(Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=4))
    _out, result = apply_operations(
        doc,
        [kf_op(property="scale",
               keyframes=[{"time": 0, "value": 1}, {"time": 4, "value": 1.4}])],
        ctx(),
    )
    assert not result.ok
    message = result.results[0].message
    assert "cannot be animated" in message
    assert "zoom" in message, "the refusal should name what does work"


def test_easing_alone_retimes_an_existing_curve():
    doc = make_doc(Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=4))
    doc, _ = apply_operations(
        doc,
        [kf_op(property="x", keyframes=[
            {"time": 0, "value": 0, "easing": "linear"},
            {"time": 4, "value": 100, "easing": "linear"},
        ])],
        ctx(),
    )
    out, result = apply_operations(
        doc, [kf_op(property="x", easing="ease_in_out")], ctx()
    )
    assert result.ok
    assert all(k.easing.value == "ease_in_out"
               for k in only_clip(out).transform.keyframes["x"])


def test_retiming_something_that_is_not_animated_says_so():
    doc = make_doc(Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=4))
    _out, result = apply_operations(
        doc, [kf_op(property="y", easing="ease_out")], ctx()
    )
    assert not result.ok
    assert "Animate it first" in result.results[0].message


def test_a_malformed_keyframe_is_rejected_not_coerced():
    doc = make_doc(Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=4))
    _out, result = apply_operations(
        doc, [kf_op(property="x", keyframes=[{"time": 0}])], ctx()
    )
    assert not result.ok
    assert "numeric" in result.results[0].message


def test_effect_parameters_can_be_animated_too():
    doc = make_doc(Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=4))
    doc, _ = apply_operations(
        doc, [op(OpType.ADD_EFFECT, type="color", parameters={"saturation": 1.0})],
        ctx(),
    )
    out, result = apply_operations(
        doc,
        [kf_op(target="effect", effect_type="color", property="saturation",
               keyframes=[{"time": 0, "value": 0}, {"time": 4, "value": 2}])],
        ctx(),
    )
    assert result.ok
    effect = only_clip(out).effects[0]
    assert [k.value for k in effect.keyframes["saturation"]] == [0.0, 2.0]
