"""Render engine tests -- every assertion is made against a real MP4."""
from __future__ import annotations

from pathlib import Path

import pytest

from makma.media.probe import probe
from makma.render.compiler import (
    AssetSource,
    CompileError,
    RenderSettings,
    compile_sequence,
)
from makma.schema.core import (
    Clip,
    ClipKind,
    Effect,
    Sequence,
    TextContent,
    TextStyle,
    Track,
    TrackKind,
    Transition,
    TransitionType,
    default_sequence,
)


def source_for(path: Path, kind: str = "video") -> AssetSource:
    info = probe(path)
    v = info.primary_video
    return AssetSource(
        asset_id="a1",
        path=path,
        kind=kind,
        width=v.width if v else 0,
        height=v.height if v else 0,
        fps=v.fps if v else 0.0,
        duration=info.duration,
        has_audio=bool(info.audio),
    )


def render(seq: Sequence, assets: dict[str, AssetSource], out: Path,
           rs: RenderSettings | None = None):
    rs = rs or RenderSettings(width=seq.width, height=seq.height, fps=seq.fps,
                              preset="ultrafast", crf=28)
    compiled = compile_sequence(seq, assets, rs, out)
    try:
        compiled.command.run()
    finally:
        compiled.cleanup()
    assert out.exists() and out.stat().st_size > 0, "render produced no file"
    return compiled, probe(out)


def small_sequence(**kw) -> Sequence:
    seq = default_sequence(width=320, height=180, fps=25, **kw)
    return seq


# ---------------------------------------------------------------------------


def test_single_clip_renders_with_correct_duration(sample_video, out_path):
    seq = small_sequence()
    seq.video_tracks()[0].clips.append(
        Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=2.0)
    )
    _c, info = render(seq, {"a1": source_for(sample_video)}, out_path)

    assert info.duration == pytest.approx(2.0, abs=0.15)
    assert info.primary_video is not None
    assert (info.primary_video.width, info.primary_video.height) == (320, 180)
    assert info.audio, "source had audio, so the render should too"


def test_trim_takes_the_requested_source_window(sample_video, out_path):
    seq = small_sequence()
    seq.video_tracks()[0].clips.append(
        Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=1.5, in_point=3.0)
    )
    _c, info = render(seq, {"a1": source_for(sample_video)}, out_path)
    assert info.duration == pytest.approx(1.5, abs=0.15)


def test_gap_between_clips_becomes_real_timeline_time(sample_video, out_path):
    """A clip starting at 3s must sit at 3s, not be concatenated to 0s."""
    seq = small_sequence()
    track = seq.video_tracks()[0]
    track.clips.append(Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0.0, duration=1.0))
    track.clips.append(Clip(kind=ClipKind.VIDEO, asset_id="a1", start=3.0, duration=1.0))
    _c, info = render(seq, {"a1": source_for(sample_video)}, out_path)
    assert info.duration == pytest.approx(4.0, abs=0.2)


def test_speed_change_alters_clip_length(sample_video, out_path):
    seq = small_sequence()
    # 2s of source at 0.5x occupies 4s of timeline.
    seq.video_tracks()[0].clips.append(
        Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=4.0, speed=0.5)
    )
    _c, info = render(seq, {"a1": source_for(sample_video)}, out_path)
    assert info.duration == pytest.approx(4.0, abs=0.2)


def test_multi_track_composite_and_transform(sample_video, sample_image, out_path):
    seq = small_sequence()
    seq.video_tracks()[0].clips.append(
        Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=2.0)
    )
    overlay = Clip(kind=ClipKind.IMAGE, asset_id="img", start=0.5, duration=1.0)
    overlay.transform.scale = 0.4
    overlay.transform.x = 40
    overlay.transform.opacity = 0.8
    seq.video_tracks()[1].clips.append(overlay)

    assets = {"a1": source_for(sample_video), "img": source_for(sample_image, "image")}
    _c, info = render(seq, assets, out_path)
    assert info.duration == pytest.approx(2.0, abs=0.2)


def test_effects_chain_renders(sample_video, out_path):
    seq = small_sequence()
    clip = Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=1.5)
    clip.effects = [
        Effect(type="color_grade", parameters={"preset": "cinematic_dark"}),
        Effect(type="film_grain", parameters={"intensity": 0.2}),
        Effect(type="vignette", parameters={"intensity": 0.5}),
        Effect(type="letterbox", parameters={"aspect_ratio": "2.39:1"}),
        Effect(type="blur", parameters={"sigma": 2.0}),
    ]
    seq.video_tracks()[0].clips.append(clip)
    compiled, info = render(seq, {"a1": source_for(sample_video)}, out_path)
    assert info.duration == pytest.approx(1.5, abs=0.15)
    assert compiled.warnings == []


def test_every_registered_video_effect_compiles(sample_video, out_path):
    """Guards against an effect whose filter string is malformed."""
    from makma.render.effects import REGISTRY

    src = source_for(sample_video)
    failures = []
    for fx_type, spec in sorted(REGISTRY.items()):
        if spec.category == "audio" or fx_type == "lut":
            continue  # lut needs a real .cube file
        seq = small_sequence()
        clip = Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=0.4)
        clip.effects = [Effect(type=fx_type)]
        seq.video_tracks()[0].clips.append(clip)
        try:
            render(seq, {"a1": src}, out_path,
                   RenderSettings(width=160, height=90, fps=12, preset="ultrafast",
                                  crf=32))
        except Exception as exc:  # noqa: BLE001 - we want the whole list
            failures.append(f"{fx_type}: {type(exc).__name__} {exc}"[:300])
    assert not failures, "effects failed to render:\n" + "\n".join(failures)


def test_every_registered_audio_effect_compiles(sample_video, out_path):
    from makma.render.effects import REGISTRY

    src = source_for(sample_video)
    failures = []
    for fx_type, spec in sorted(REGISTRY.items()):
        if spec.category != "audio":
            continue
        seq = small_sequence()
        clip = Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=0.6)
        clip.effects = [Effect(type=fx_type)]
        seq.video_tracks()[0].clips.append(clip)
        try:
            render(seq, {"a1": src}, out_path,
                   RenderSettings(width=160, height=90, fps=12, preset="ultrafast",
                                  crf=32))
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{fx_type}: {type(exc).__name__} {exc}"[:300])
    assert not failures, "audio effects failed:\n" + "\n".join(failures)


def test_text_layer_renders(sample_video, out_path):
    seq = small_sequence()
    seq.video_tracks()[0].clips.append(
        Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=2.0)
    )
    seq.video_tracks()[2].clips.append(
        Clip(
            kind=ClipKind.TEXT, start=0.3, duration=1.4,
            text=TextContent(
                text="NIGHT DRIVE",
                style=TextStyle(font_size=28, uppercase=True, stroke_color="#000000",
                                stroke_width=2),
            ),
        )
    )
    compiled, info = render(seq, {"a1": source_for(sample_video)}, out_path)
    assert info.duration == pytest.approx(2.0, abs=0.2)
    assert "font" not in " ".join(compiled.warnings).lower()


def test_transitions_render(sample_video, out_path):
    seq = small_sequence()
    track = seq.video_tracks()[0]
    a = Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0.0, duration=1.5)
    a.transition_in = Transition(type=TransitionType.FADE, duration=0.4)
    a.transition_out = Transition(type=TransitionType.DISSOLVE, duration=0.4)
    b = Clip(kind=ClipKind.VIDEO, asset_id="a1", start=1.2, duration=1.5, in_point=2.0)
    b.transition_in = Transition(type=TransitionType.WIPE_LEFT, duration=0.4)
    track.clips += [a, b]
    _c, info = render(seq, {"a1": source_for(sample_video)}, out_path)
    assert info.duration == pytest.approx(2.7, abs=0.2)


def test_adjustment_layer_applies_over_lower_tracks(sample_video, out_path):
    seq = small_sequence()
    seq.video_tracks()[0].clips.append(
        Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=2.0)
    )
    adj = Clip(kind=ClipKind.ADJUSTMENT, start=0.5, duration=1.0)
    adj.effects = [Effect(type="color", parameters={"saturation": 0.0})]
    seq.video_tracks()[1].clips.append(adj)
    _c, info = render(seq, {"a1": source_for(sample_video)}, out_path)
    assert info.duration == pytest.approx(2.0, abs=0.2)


def test_audio_only_clip_mixes_with_video(sample_video, sample_audio, out_path):
    seq = small_sequence()
    seq.video_tracks()[0].clips.append(
        Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=2.0, volume=0.4)
    )
    seq.audio_tracks()[0].clips.append(
        Clip(kind=ClipKind.AUDIO, asset_id="mus", start=0.0, duration=2.0, volume=0.8)
    )
    assets = {
        "a1": source_for(sample_video),
        "mus": source_for(sample_audio, "audio"),
    }
    _c, info = render(seq, assets, out_path)
    assert info.audio, "mixed audio track missing from the render"
    assert info.duration == pytest.approx(2.0, abs=0.2)


def test_video_without_audio_still_renders(silent_video, out_path):
    seq = small_sequence()
    seq.video_tracks()[0].clips.append(
        Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=1.5)
    )
    src = source_for(silent_video)
    assert src.has_audio is False
    _c, info = render(seq, {"a1": src}, out_path)
    assert info.duration == pytest.approx(1.5, abs=0.15)
    assert not info.audio


def test_keyframed_parameter_animates(sample_video, out_path):
    from makma.schema.core import Keyframe

    seq = small_sequence()
    clip = Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=2.0)
    clip.effects = [
        Effect(
            type="color",
            parameters={"saturation": 1.0},
            keyframes={"saturation": [Keyframe(time=0.0, value=0.0),
                                      Keyframe(time=2.0, value=2.0)]},
        )
    ]
    seq.video_tracks()[0].clips.append(clip)
    compiled, info = render(seq, {"a1": source_for(sample_video)}, out_path)
    assert "eval=frame" in (compiled.command.filter_complex or "")
    assert info.duration == pytest.approx(2.0, abs=0.2)


def test_vertical_reframe_uses_cover_fit(sample_video, out_path):
    seq = default_sequence(width=180, height=320, fps=25)
    clip = Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=1.5)
    clip.transform.fit = "cover"
    seq.video_tracks()[0].clips.append(clip)
    _c, info = render(seq, {"a1": source_for(sample_video)}, out_path)
    assert (info.primary_video.width, info.primary_video.height) == (180, 320)


def test_empty_sequence_raises_a_useful_error():
    seq = small_sequence()
    with pytest.raises(CompileError, match="empty"):
        compile_sequence(seq, {}, RenderSettings(), Path("unused.mp4"))


def test_missing_asset_is_reported_not_silently_dropped(sample_video, out_path):
    seq = small_sequence()
    track = seq.video_tracks()[0]
    track.clips.append(Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=1.0))
    track.clips.append(Clip(kind=ClipKind.VIDEO, asset_id="gone", start=1.0,
                            duration=1.0, name="missing"))
    compiled, _info = render(seq, {"a1": source_for(sample_video)}, out_path)
    assert any("missing" in w or "ingesting" in w for w in compiled.warnings)
