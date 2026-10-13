"""Offline planner tests.

The rule planner is the floor the product stands on: it runs with no API key,
so these instructions must keep working forever. Each test states the
instruction a user would actually type and asserts the operations it becomes.
"""
from __future__ import annotations

import pytest

from makma.ai.heuristic import (
    MediaRef,
    PlanContext,
    find_range,
    parse_time,
    plan,
)
from makma.schema.core import Clip, ClipKind, ProjectDocument, default_sequence
from makma.schema.operations import OpType


def context(clips: list[Clip] | None = None, analyses: dict | None = None,
            media_photo: bool = False, media_music: bool = False,
            **kw) -> PlanContext:
    seq = default_sequence(width=1920, height=1080, fps=30)
    for clip in clips or []:
        seq.video_tracks()[0].clips.append(clip)
    doc = ProjectDocument(name="Test", sequences=[seq])

    media = [MediaRef(id="a1", name="drive.mp4", kind="video", duration=20.0)]
    if media_photo:
        media.append(MediaRef(id="p1", name="my photo.png", kind="image"))
    if media_music:
        media.append(MediaRef(id="m1", name="music.mp3", kind="audio",
                              duration=180.0))

    selection = kw.pop("selection", [c.id for c in (clips or [])])
    return PlanContext(
        document=doc, sequence=seq, analyses=analyses or {}, media=media,
        selection=selection, **kw,
    )


def video(start=0.0, duration=20.0, asset_id="a1", **kw) -> Clip:
    return Clip(kind=ClipKind.VIDEO, asset_id=asset_id, start=start,
                duration=duration, **kw)


def types(p) -> list[str]:
    return [o.type.value for o in p.operations]


def params_for(p, op_type: str) -> dict:
    for o in p.operations:
        if o.type.value == op_type:
            return o.parameters
    raise AssertionError(f"no {op_type} operation in plan: {types(p)}")


# --- parsing ---------------------------------------------------------------


@pytest.mark.parametrize(
    "token,expected",
    [("12", 12), ("12.5", 12.5), ("1:23", 83), ("0:05", 5), ("three", 3),
     ("5 seconds", 5), ("2:00.5", 120.5)],
)
def test_parse_time(token, expected):
    assert parse_time(token) == pytest.approx(expected)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("cut between 3:42 and 4:07", (222, 247)),
        ("remove from 5 to 9", (5, 9)),
        ("slow it down between 10 and 12 seconds", (10, 12)),
    ],
)
def test_find_range(text, expected):
    assert find_range(text) == pytest.approx(expected)


# --- looks -----------------------------------------------------------------


def test_cinematic_is_a_package_not_just_a_lut():
    p = plan("make this cinematic", context([video()]))
    effects = [o.parameters.get("type") for o in p.operations
               if o.type == OpType.ADD_EFFECT]
    assert "color_grade" in effects
    assert "letterbox" in effects, "cinematic should letterbox"
    assert "film_grain" in effects
    assert "vignette" in effects


@pytest.mark.parametrize(
    "text,preset",
    [
        ("make it look like night time", "night"),
        ("give it a luxury feel", "luxury"),
        ("teal and orange please", "teal_orange"),
        ("make it black and white", "bw"),
        ("go cyberpunk", "cyberpunk"),
        ("warmer please", "warm"),
        ("golden hour look", "golden_hour"),
    ],
)
def test_grade_vocabulary(text, preset):
    p = plan(text, context([video()]))
    assert params_for(p, "add_effect")["parameters"]["preset"] == preset


def test_subtle_reduces_grade_intensity():
    strong = plan("make it cinematic", context([video()]))
    subtle = plan("make it subtly cinematic", context([video()]))
    assert params_for(subtle, "add_effect")["parameters"]["intensity"] < \
        params_for(strong, "add_effect")["parameters"]["intensity"]


def test_a_quoted_title_does_not_drive_the_look():
    """'NIGHT DRIVE' is content, not an instruction to grade for night."""
    p = plan('add the title "NIGHT DRIVE"', context([video()]))
    grades = [o for o in p.operations if o.parameters.get("type") == "color_grade"]
    assert grades == []
    assert params_for(p, "add_text")["text"] == "NIGHT DRIVE"


# --- structure -------------------------------------------------------------


def test_cut_the_first_n_seconds():
    p = plan("cut the boring first 3 seconds", context([video()]))
    params = params_for(p, "remove_range")
    assert (params["start"], params["end"]) == (0.0, 3.0)
    assert params["ripple"] is True


def test_remove_an_explicit_range():
    p = plan("remove from 5 to 9 seconds", context([video()]))
    params = params_for(p, "remove_range")
    assert (params["start"], params["end"]) == (5.0, 9.0)


def test_slow_motion_uses_a_speed_below_one():
    p = plan("slow this down", context([video()]))
    assert params_for(p, "set_speed")["speed"] < 1.0


def test_explicit_multiplier_is_respected():
    assert params_for(plan("speed it up 3x", context([video()])), "set_speed")["speed"] \
        == pytest.approx(3.0)
    assert params_for(plan("slow it to 0.25x", context([video()])), "set_speed")["speed"] \
        == pytest.approx(0.25)


def test_reverse():
    p = plan("play it backwards", context([video()]))
    assert params_for(p, "set_speed")["reverse"] is True


def test_vertical_reframe_also_fills_the_frame():
    p = plan("make it vertical for tiktok", context([video()]))
    assert "set_format" in types(p)
    assert params_for(p, "set_format")["aspect_ratio"] == "9:16"
    assert params_for(p, "set_transform")["fit"] == "cover", \
        "a reframe that only letterboxes is not a reframe"


def test_title_extraction_preserves_case_and_position():
    p = plan('add the title "NIGHT DRIVE" at the bottom', context([video()]))
    params = params_for(p, "add_text")
    assert params["text"] == "NIGHT DRIVE"
    assert params["position"] == "bottom"


def test_transitions():
    p = plan("add cross dissolves between the clips", context([video()]))
    assert params_for(p, "set_transition")["transition"] == "dissolve"

    p = plan("fade in over 2 seconds", context([video()]))
    params = params_for(p, "set_transition")
    assert params["transition"] == "fade" and params["where"] == "in"


# --- audio -----------------------------------------------------------------


def test_voice_cleanup():
    p = plan("clean up my voice", context([video()]))
    assert params_for(p, "add_effect")["type"] == "a_voice"


def test_lower_the_music():
    p = plan("lower the music", context([video()]))
    assert params_for(p, "set_volume")["gain_db"] < 0


def test_normalise():
    p = plan("normalize the audio levels", context([video()]))
    assert params_for(p, "add_effect")["type"] == "a_normalize"


# --- analysis-driven -------------------------------------------------------


def test_remove_pauses_uses_the_detected_silences():
    analyses = {
        "a1": {
            "silences": [
                {"start": 2.0, "end": 3.5},
                {"start": 8.0, "end": 9.2},
                {"start": 12.0, "end": 12.2},   # too short to be worth cutting
            ]
        }
    }
    p = plan("remove the pauses", context([video()], analyses))
    ranges = [(o.parameters["start"], o.parameters["end"])
              for o in p.operations if o.type == OpType.REMOVE_RANGE]
    assert (2.0, 3.5) in ranges
    assert (8.0, 9.2) in ranges
    assert (12.0, 12.2) not in ranges, "sub-half-second gaps are not pauses"
    # Later ranges must be removed first so earlier positions stay valid.
    assert ranges == sorted(ranges, reverse=True)


def test_remove_pauses_without_analysis_produces_nothing_rather_than_guessing():
    p = plan("remove the pauses", context([video()]))
    assert p.operations == []
    assert "did not recognise" in p.rationale


def test_highlight_reel_keeps_only_the_best_ranges():
    analyses = {
        "a1": {
            "highlights": [
                {"start": 1.0, "end": 4.0, "score": 0.9, "reason": "high motion"},
                {"start": 10.0, "end": 13.0, "score": 0.8, "reason": "clean"},
                {"start": 16.0, "end": 18.0, "score": 0.2, "reason": "static"},
            ]
        }
    }
    p = plan("make a 8 second highlight reel", context([video()], analyses))
    removals = [(o.parameters["start"], o.parameters["end"])
                for o in p.operations if o.type == OpType.REMOVE_RANGE]
    assert removals, "nothing was cut"
    # The strongest window must survive.
    assert not any(s <= 1.5 <= e for s, e in removals)
    assert "set_transition" in types(p), "highlight cuts should be smoothed"


def test_scene_markers_come_from_the_shot_index():
    analyses = {"a1": {"scenes": [{"start": 0.0}, {"start": 4.0}, {"start": 9.5}]}}
    p = plan("mark every scene", context([video()], analyses))
    assert params_for(p, "add_marker")["times"] == [0.0, 4.0, 9.5]


# --- plan quality ----------------------------------------------------------


def test_unrecognised_instruction_produces_an_empty_plan():
    p = plan("xyzzy frobnicate the widget", context([video()]))
    assert p.operations == []
    assert p.summary == ""


def test_every_operation_carries_a_human_description():
    p = plan("make it cinematic and cut the first 2 seconds", context([video()]))
    assert p.operations
    assert all(o.description for o in p.operations)


def test_destructive_plans_require_confirmation():
    assert plan("remove from 2 to 6", context([video()])).requires_confirmation
    assert not plan("add a vignette", context([video()])).requires_confirmation


def test_plans_are_deduplicated():
    p = plan("make it cinematic, really cinematic, cinematic please",
             context([video()]))
    signatures = [(o.type, str(o.parameters)) for o in p.operations]
    assert len(signatures) == len(set(signatures))


# --- compound instructions -------------------------------------------------
# Real instructions arrive as several requests in one sentence. Rules run per
# clause, because matching the whole string lets unrelated clauses contaminate
# each other.


def test_clauses_split_on_commas_and_conjunctions():
    from makma.ai.heuristic import split_clauses

    assert split_clauses("cut the first 5 seconds, slow this down, and zoom in") == [
        "cut the first 5 seconds", "slow this down", "zoom in",
    ]


@pytest.mark.parametrize(
    "phrase",
    [
        "cut between 3:42 and 4:07",
        "make it black and white",
        "teal and orange please",
    ],
)
def test_terms_containing_a_joining_word_are_not_split(phrase):
    from makma.ai.heuristic import split_clauses

    assert split_clauses(phrase) == [phrase]


def test_the_full_mvp_instruction_produces_every_requested_change():
    """The compound command from the product brief, clause by clause."""
    ctx = context([video()], media_photo=True, media_music=True)
    p = plan(
        "Cut the first 5 seconds, make this clip slower, zoom toward me, "
        "put my photo here, lower the music, add NIGHT DRIVE, and make the "
        "whole scene cinematic.",
        ctx,
    )
    kinds = types(p)

    assert "remove_range" in kinds, "the head cut was dropped"
    assert "set_speed" in kinds, "'slower' was lost to another clause"
    assert "add_clip" in kinds, "the photo was never placed"
    assert "set_volume" in kinds, "the music level was not touched"
    assert "add_text" in kinds, "NIGHT DRIVE was not treated as a title"

    assert params_for(p, "set_speed")["speed"] < 1.0
    assert params_for(p, "add_text")["text"] == "NIGHT DRIVE"

    effects = [o.parameters.get("type") for o in p.operations
               if o.type == OpType.ADD_EFFECT]
    assert "zoom" in effects, "'zoom toward me' produced no push-in"
    assert "color_grade" in effects and "letterbox" in effects

    # NIGHT is content here. It must not have selected the night grade.
    grade = next(o for o in p.operations
                 if o.parameters.get("type") == "color_grade")
    assert grade.parameters["parameters"]["preset"] == "cinematic_dark"


def test_a_cut_in_one_clause_does_not_split_at_a_here_in_another():
    """'cut …' and '… here' used to combine into a split nobody asked for."""
    p = plan("cut the first 3 seconds, put my photo here",
             context([video()], media_photo=True))
    assert "split_clip" not in types(p)


def test_slower_and_zoom_in_the_same_command_both_survive():
    p = plan("make this slower and zoom toward me", context([video()]))
    assert "set_speed" in types(p)
    assert "zoom" in [o.parameters.get("type") for o in p.operations
                      if o.type == OpType.ADD_EFFECT]


def test_a_zoom_alone_is_a_camera_move_not_a_speed_change():
    p = plan("zoom in slowly", context([video()]))
    assert "set_speed" not in types(p)
    assert "zoom" in [o.parameters.get("type") for o in p.operations
                      if o.type == OpType.ADD_EFFECT]


def test_shouted_words_after_a_verb_become_a_title():
    p = plan("add NIGHT DRIVE", context([video()]))
    assert params_for(p, "add_text")["text"] == "NIGHT DRIVE"


def test_a_shouted_acronym_is_not_mistaken_for_a_title():
    p = plan("add AI generated b-roll", context([video()]))
    assert "add_text" not in types(p)


def test_exports_are_collected_from_any_clause():
    p = plan("make it cinematic, then export a TikTok version",
             context([video()]))
    assert [e.preset for e in p.exports] == ["tiktok"]


# --- keyframes -------------------------------------------------------------


def test_drift_produces_position_keyframes():
    p = plan("let it drift slowly to the right", context([video()]))
    params = params_for(p, "set_keyframes")
    assert params["property"] == "x"
    assert params["relative"] is True
    values = [k["value"] for k in params["keyframes"]]
    assert values[0] < values[-1], "a rightward drift must end further right"


def test_subtle_drift_moves_less_than_a_dramatic_one():
    subtle = params_for(plan("subtle drift to the left", context([video()])),
                        "set_keyframes")
    big = params_for(plan("dramatic drift to the left", context([video()])),
                     "set_keyframes")
    span = lambda pr: abs(pr["keyframes"][-1]["value"] - pr["keyframes"][0]["value"])
    assert span(subtle) < span(big)


def animated_clip() -> Clip:
    """A clip that already has a position curve to re-time."""
    from makma.schema.core import Keyframe

    clip = video()
    clip.transform.keyframes = {
        "x": [Keyframe(time=0, value=-100, easing="linear"),
              Keyframe(time=4, value=100, easing="linear")],
    }
    return clip


def test_smoother_movement_retimes_rather_than_replacing_the_curve():
    p = plan("make the movement smoother", context([animated_clip()]))
    params = params_for(p, "set_keyframes")
    assert params["easing"] == "ease_in_out"
    assert params["property"] == "x", "it should re-time the curve that exists"
    assert "keyframes" not in params, "re-timing must not invent a new curve"


def test_start_fast_and_slow_down_eases_an_existing_animation():
    p = plan("start fast and slow down", context([animated_clip()]))
    assert params_for(p, "set_keyframes")["easing"] == "ease_out"


def test_start_fast_and_slow_down_means_playback_speed_when_nothing_is_animated():
    """Same words, different meaning: with no curve to re-time it is a speed."""
    p = plan("start fast and slow down", context([video()]))
    assert "set_keyframes" not in types(p)
    assert params_for(p, "set_speed")["speed"] < 1.0


def test_a_zoom_stays_an_effect_because_scale_cannot_be_keyframed():
    p = plan("slowly zoom in", context([video()]))
    assert "set_keyframes" not in types(p)
    assert "zoom" in [o.parameters.get("type") for o in p.operations
                      if o.type == OpType.ADD_EFFECT]


# --- music sync ------------------------------------------------------------


def beat_analysis(bpm: float = 120.0, count: int = 16, offset: float = 0.0,
                  confidence: float = 0.9) -> dict:
    period = 60.0 / bpm
    return {
        "beats": {
            "bpm": bpm,
            "offset": offset,
            "confidence": confidence,
            "beats_per_bar": 4,
            "beats": [offset + i * period for i in range(count)],
            "downbeats": [offset + i * period * 4 for i in range(count // 4)],
            "onsets": [], "accents": [],
        }
    }


def test_cut_to_the_beat_snaps_existing_cuts():
    """Cuts move to the nearest beat; a cut already on one is left alone."""
    period = 0.5  # 120 BPM
    clips = [
        video(start=0.0, duration=2.0),
        video(start=2.12, duration=2.0),   # 120ms late, should snap to 2.0
        video(start=4.0, duration=2.0),    # already on a beat
    ]
    p = plan("cut to the beat", context(clips, {"a1": beat_analysis()}))

    moves = [o for o in p.operations if o.type == OpType.MOVE_CLIP]
    assert len(moves) == 1, "only the off-beat cut should move"
    assert moves[0].parameters["start"] == pytest.approx(2.0, abs=1e-3)


def test_a_cut_far_from_any_beat_is_left_alone():
    """Equidistant between two beats, neither has a claim on it.

    At 120 BPM a cut at 2.25s sits exactly between the beats at 2.0 and 2.5.
    Snapping it either way would be a guess, not a correction.
    """
    clips = [video(start=0.0, duration=2.0), video(start=2.25, duration=2.0)]
    p = plan("sync the cuts to the music", context(clips, {"a1": beat_analysis()}))
    assert [o for o in p.operations if o.type == OpType.MOVE_CLIP] == []


def test_beat_markers_land_on_the_grid():
    p = plan("show the beat grid", context([video()], {"a1": beat_analysis()}))
    params = params_for(p, "add_marker")
    assert params["kind"] == "beat"
    assert params["times"][:3] == [0.0, 0.5, 1.0]


def test_beats_are_shifted_by_where_the_music_sits_on_the_timeline():
    """Beats are found in the source file, not in sequence time."""
    clip = video(start=4.0, duration=8.0)
    p = plan("show the beat grid", context([clip], {"a1": beat_analysis()}))
    times = params_for(p, "add_marker")["times"]
    assert times[0] == pytest.approx(4.0), "the grid ignored the clip's position"


def test_music_sync_without_a_beat_grid_does_nothing_rather_than_guessing():
    p = plan("cut to the beat", context([video()]))
    assert p.operations == []


def test_a_low_confidence_grid_is_not_used():
    p = plan("cut to the beat",
             context([video()], {"a1": beat_analysis(confidence=0.1)}))
    assert p.operations == []
