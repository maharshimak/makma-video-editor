"""Pixel-level render assertions.

`test_render_engine.py` proves a file comes out; these tests prove the file
looks right. A filter that silently becomes a no-op still produces a valid
MP4, so effects that make a visible, checkable change get checked here.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from makma.config import settings
from makma.render.compiler import AssetSource, RenderSettings, compile_sequence
from makma.schema.core import Clip, ClipKind, Effect, default_sequence

WIDTH, HEIGHT = 320, 180


def sample_frame(path: Path, at: float = 0.2) -> tuple[bytes, int, int]:
    """Decode one frame of a render as raw RGB."""
    argv = [
        settings.ffmpeg_path, "-hide_banner", "-v", "error", "-nostdin",
        "-ss", f"{at}", "-i", str(path), "-frames:v", "1",
        "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
    ]
    proc = subprocess.run(argv, capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")[-1500:]
    return proc.stdout, WIDTH, HEIGHT


def pixel(buf: bytes, x: int, y: int, w: int = WIDTH) -> tuple[int, int, int]:
    i = (y * w + x) * 3
    return buf[i], buf[i + 1], buf[i + 2]


def mean_luma(buf: bytes) -> float:
    total = 0
    count = 0
    for i in range(0, len(buf) - 2, 3 * 37):
        total += (buf[i] * 299 + buf[i + 1] * 587 + buf[i + 2] * 114) // 1000
        count += 1
    return total / max(1, count)


def render_clip(path: Path, out: Path, effects: list[Effect] | None = None,
                duration: float = 0.6, **clip_kwargs) -> Path:
    seq = default_sequence(width=WIDTH, height=HEIGHT, fps=15)
    clip = Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=duration,
                **clip_kwargs)
    clip.effects = effects or []
    seq.video_tracks()[0].clips.append(clip)

    compiled = compile_sequence(
        seq,
        {"a1": AssetSource(asset_id="a1", path=path, width=640, height=360,
                           fps=25, duration=6.0, has_audio=True)},
        RenderSettings(width=WIDTH, height=HEIGHT, fps=15, preset="ultrafast",
                       crf=20),
        out,
    )
    try:
        compiled.command.run()
    finally:
        compiled.cleanup()
    return out


# ---------------------------------------------------------------------------


def test_letterbox_paints_bars_top_and_bottom(sample_video, out_path):
    """A 2.39:1 target on a 16:9 canvas must produce real black bars.

    This is a regression guard: an inverted comparison here made the effect a
    silent no-op while still rendering a perfectly valid file.
    """
    render_clip(sample_video, out_path,
                [Effect(type="letterbox", parameters={"aspect_ratio": "2.39:1"})])
    buf, w, h = sample_frame(out_path)

    expected_bar = round((h - w / 2.39) / 2)
    assert expected_bar > 4, "test canvas is too small to show bars"

    for y in (1, expected_bar // 2, expected_bar - 3):
        assert sum(pixel(buf, w // 2, y)) < 24, f"row {y} should be a black bar"
    for y in (h - 2, h - expected_bar // 2, h - expected_bar + 3):
        assert sum(pixel(buf, w // 2, y)) < 24, f"row {y} should be a black bar"

    # ...and the picture in between must survive.
    middle = [pixel(buf, x, h // 2) for x in range(20, w - 20, 20)]
    assert any(sum(p) > 60 for p in middle), "the picture was blacked out entirely"


def test_letterbox_pillarboxes_a_narrow_target(sample_video, out_path):
    render_clip(sample_video, out_path,
                [Effect(type="letterbox", parameters={"aspect_ratio": "1:1"})])
    buf, w, h = sample_frame(out_path)

    expected_bar = round((w - h * 1.0) / 2)
    assert expected_bar > 4
    for x in (1, expected_bar // 2, expected_bar - 3):
        assert sum(pixel(buf, x, h // 2)) < 24, f"column {x} should be a black bar"
    for x in (w - 2, w - expected_bar // 2, w - expected_bar + 3):
        assert sum(pixel(buf, x, h // 2)) < 24, f"column {x} should be a black bar"


def test_black_and_white_grade_removes_colour(sample_video, out_path):
    render_clip(sample_video, out_path,
                [Effect(type="color_grade", parameters={"preset": "bw"})])
    buf, w, h = sample_frame(out_path)

    spread = []
    for x in range(10, w - 10, 13):
        for y in range(10, h - 10, 11):
            r, g, b = pixel(buf, x, y)
            spread.append(max(r, g, b) - min(r, g, b))
    # Chroma subsampling leaves a little residue; anything above this is colour.
    assert max(spread) < 26, f"grade left colour behind (max spread {max(spread)})"


def test_exposure_actually_changes_brightness(sample_video, out_path):
    render_clip(sample_video, out_path)
    base = mean_luma(sample_frame(out_path)[0])

    render_clip(sample_video, out_path,
                [Effect(type="exposure", parameters={"stops": 2.0})])
    brighter = mean_luma(sample_frame(out_path)[0])

    render_clip(sample_video, out_path,
                [Effect(type="exposure", parameters={"stops": -2.0})])
    darker = mean_luma(sample_frame(out_path)[0])

    assert brighter > base + 8, f"exposure up did nothing ({base:.1f} -> {brighter:.1f})"
    assert darker < base - 8, f"exposure down did nothing ({base:.1f} -> {darker:.1f})"


def test_opacity_composites_against_the_canvas(sample_video, out_path):
    """A 50% clip over the black canvas must come out darker, not identical."""
    render_clip(sample_video, out_path)
    opaque = mean_luma(sample_frame(out_path)[0])

    seq = default_sequence(width=WIDTH, height=HEIGHT, fps=15)
    clip = Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=0.6)
    clip.transform.opacity = 0.5
    seq.video_tracks()[0].clips.append(clip)
    compiled = compile_sequence(
        seq,
        {"a1": AssetSource(asset_id="a1", path=sample_video, width=640, height=360,
                           fps=25, duration=6.0, has_audio=True)},
        RenderSettings(width=WIDTH, height=HEIGHT, fps=15, preset="ultrafast", crf=20),
        out_path,
    )
    try:
        compiled.command.run()
    finally:
        compiled.cleanup()
    half = mean_luma(sample_frame(out_path)[0])
    assert half < opaque * 0.75, f"opacity ignored ({opaque:.1f} -> {half:.1f})"


def test_fade_in_starts_black(sample_video, out_path):
    from makma.schema.core import Transition, TransitionType

    seq = default_sequence(width=WIDTH, height=HEIGHT, fps=15)
    clip = Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=2.0)
    clip.transition_in = Transition(type=TransitionType.FADE, duration=1.0)
    seq.video_tracks()[0].clips.append(clip)
    compiled = compile_sequence(
        seq,
        {"a1": AssetSource(asset_id="a1", path=sample_video, width=640, height=360,
                           fps=25, duration=6.0, has_audio=True)},
        RenderSettings(width=WIDTH, height=HEIGHT, fps=15, preset="ultrafast", crf=20),
        out_path,
    )
    try:
        compiled.command.run()
    finally:
        compiled.cleanup()

    start = mean_luma(sample_frame(out_path, at=0.02)[0])
    later = mean_luma(sample_frame(out_path, at=1.5)[0])
    assert start < 12, f"fade-in did not start from black (luma {start:.1f})"
    assert later > start + 20, "the picture never faded up"


def test_text_layer_puts_pixels_on_screen(sample_video, out_path):
    """A title over a black canvas must brighten the region it occupies."""
    from makma.schema.core import TextContent, TextStyle

    def render(with_text: bool) -> float:
        seq = default_sequence(width=WIDTH, height=HEIGHT, fps=15)
        seq.video_tracks()[0].clips.append(
            Clip(kind=ClipKind.SOLID, start=0, duration=0.6, color="#000000")
        )
        if with_text:
            seq.video_tracks()[2].clips.append(
                Clip(
                    kind=ClipKind.TEXT, start=0, duration=0.6,
                    text=TextContent(
                        text="NIGHT DRIVE",
                        style=TextStyle(font_size=30, color="#FFFFFF"),
                        animation="none",
                    ),
                )
            )
        compiled = compile_sequence(
            seq, {}, RenderSettings(width=WIDTH, height=HEIGHT, fps=15,
                                    preset="ultrafast", crf=18),
            out_path,
        )
        try:
            compiled.command.run()
        finally:
            compiled.cleanup()
        return mean_luma(sample_frame(out_path, at=0.3)[0])

    without = render(False)
    with_ = render(True)
    assert without < 3, "the black solid was not black"
    assert with_ > without + 1.5, "the title never reached the picture"


# --- keyframes -------------------------------------------------------------
# A keyframed property must actually change over time in the render, not just
# in the schema. Each of these compares two moments of the same file.


def render_keyframed(path: Path, out: Path, curves: dict, duration: float = 2.0):
    from makma.schema.core import Keyframe

    seq = default_sequence(width=WIDTH, height=HEIGHT, fps=25)
    clip = Clip(kind=ClipKind.VIDEO, asset_id="a1", start=0, duration=duration)
    clip.transform.keyframes = {
        name: [Keyframe(time=t, value=v, easing="linear") for t, v in points]
        for name, points in curves.items()
    }
    seq.video_tracks()[0].clips.append(clip)

    compiled = compile_sequence(
        seq,
        {"a1": AssetSource(asset_id="a1", path=path, width=640, height=360,
                           fps=25, duration=6.0, has_audio=True)},
        RenderSettings(width=WIDTH, height=HEIGHT, fps=25, preset="ultrafast",
                       crf=18),
        out,
    )
    try:
        compiled.command.run()
    finally:
        compiled.cleanup()
    return compiled


def column_energy(buf: bytes, x: int, w: int = WIDTH, h: int = HEIGHT) -> int:
    """How much picture sits in one column -- 0 where the canvas shows through."""
    total = 0
    for y in range(2, h - 2):
        r, g, b = pixel(buf, x, y, w)
        total += r + g + b
    return total


def test_keyframed_position_moves_the_layer(sample_video, out_path):
    """The clip starts left of centre and ends right of it."""
    render_keyframed(sample_video, out_path,
                     {"x": [(0.0, -110.0), (2.0, 110.0)]})

    start = sample_frame(out_path, at=0.05)[0]
    end = sample_frame(out_path, at=1.9)[0]

    left, right = 20, WIDTH - 20
    # At the start the picture is pushed left: energy on the left, none right.
    assert column_energy(start, left) > column_energy(start, right) * 3
    # By the end it has crossed over.
    assert column_energy(end, right) > column_energy(end, left) * 3


def test_keyframed_opacity_fades_the_layer(sample_video, out_path):
    compiled = render_keyframed(sample_video, out_path,
                                {"opacity": [(0.0, 0.05), (2.0, 1.0)]})

    early = mean_luma(sample_frame(out_path, at=0.05)[0])
    late = mean_luma(sample_frame(out_path, at=1.9)[0])
    assert late > early * 2.5, f"opacity never rose ({early:.1f} -> {late:.1f})"

    # The cost of the per-pixel path is stated, not hidden.
    assert any("per pixel" in w for w in compiled.warnings)


def test_keyframed_rotation_is_reported_not_silently_ignored(sample_video, out_path):
    """`rotate` reads its angle once, so an animated rotation cannot render.

    It advertises a runtime flag, but the angle is not re-evaluated per frame,
    which is why the editor does not offer rotation as animatable at all.
    """
    compiled = render_keyframed(sample_video, out_path,
                                {"rotation": [(0.0, 0.0), (2.0, 90.0)]})
    assert any("rotation" in w and "midpoint" in w for w in compiled.warnings)


def test_only_properties_that_survive_the_render_are_offered_as_animatable():
    from makma.schema.core import ANIMATABLE_TRANSFORM

    assert set(ANIMATABLE_TRANSFORM) == {"x", "y", "opacity"}, (
        "the editor must not offer an animation the render cannot produce"
    )


def test_keyframed_scale_is_reported_not_silently_ignored(sample_video, out_path):
    """FFmpeg fixes a layer's size at init, and the render says so."""
    compiled = render_keyframed(sample_video, out_path,
                                {"scale": [(0.0, 0.4), (2.0, 1.0)]})
    assert any("keyframed" in w and "midpoint" in w for w in compiled.warnings)
    assert any("Push In" in w for w in compiled.warnings), \
        "the warning should name the effect that does animate"
