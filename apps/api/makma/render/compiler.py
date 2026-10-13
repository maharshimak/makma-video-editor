"""Sequence -> FFmpeg filter graph compiler.

Compositing model
-----------------
A `color` source of the sequence's exact duration is the canvas. Every clip
becomes its own input, is normalised to the sequence frame rate, shifted onto
the timeline with `setpts`, and composited with `overlay`. Video tracks are
processed bottom-to-top, so track order in the schema is literally the
compositing order. Adjustment layers and text clips apply to the accumulated
canvas at their own position in that stack, which is what makes "this grade
sits above V1 but below V3" behave the way an editor expects.

Audio clips are delayed onto the timeline with `adelay` and summed with
`amix`, so a clip's audio always lands under its picture.
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..config import settings
from ..media.ffmpeg import FFmpegCommand, quote_filter_arg
from ..schema.core import (
    Clip,
    ClipKind,
    Effect,
    FitMode,
    Sequence,
    TransitionType,
)
from .effects import (
    AUDIO_EFFECTS,
    EffectContext,
    build_effect,
    keyframe_expression,
)
from .fonts import resolve_font
from .scratch import stage_readable, write_text_asset


@dataclass
class AssetSource:
    """What the compiler needs to know about a media file."""
    asset_id: str
    path: Path
    kind: str = "video"            # video | audio | image
    width: int = 0
    height: int = 0
    fps: float = 0.0
    duration: float = 0.0
    has_audio: bool = False


@dataclass
class RenderSettings:
    width: int = 1920
    height: int = 1080
    fps: float = 30.0
    crf: int = 20
    preset: str = "medium"
    video_codec: str = "libx264"
    audio_codec: str = "aac"
    audio_bitrate: str = "192k"
    pix_fmt: str = "yuv420p"
    sample_rate: int = 48000
    #: Draft renders use proxies and a fast preset; finals use originals.
    quality: str = "final"          # draft | preview | final
    range: tuple[float, float] | None = None   # partial render window


@dataclass
class CompiledRender:
    command: FFmpegCommand
    duration: float
    warnings: list[str] = field(default_factory=list)
    temp_files: list[Path] = field(default_factory=list)
    has_video: bool = True
    has_audio: bool = False

    def cleanup(self) -> None:
        for path in self.temp_files:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


class CompileError(RuntimeError):
    pass


def _atempo_chain(speed: float) -> list[str]:
    """atempo is stable per-stage; chain stages for extreme speeds."""
    filters: list[str] = []
    remaining = speed
    while remaining > 2.0 + 1e-9:
        filters.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5 - 1e-9:
        filters.append("atempo=0.5")
        remaining /= 0.5
    if abs(remaining - 1.0) > 1e-6:
        filters.append(f"atempo={remaining:.6f}")
    return filters


def _hex_to_ffmpeg(color: str | None, default: str = "black") -> str:
    if not color:
        return default
    c = color.strip()
    if c.startswith("#"):
        return f"0x{c[1:]}"
    return c


class SequenceCompiler:
    def __init__(
        self,
        sequence: Sequence,
        assets: dict[str, AssetSource],
        settings_: RenderSettings | None = None,
    ) -> None:
        self.seq = sequence
        self.assets = assets
        self.rs = settings_ or RenderSettings(
            width=sequence.width, height=sequence.height, fps=sequence.fps
        )
        self.warnings: list[str] = []
        self.temp_files: list[Path] = []
        self.chains: list[str] = []
        self.cmd = FFmpegCommand()
        self._label_n = 0

    # -- helpers --------------------------------------------------------
    def _label(self, prefix: str) -> str:
        self._label_n += 1
        return f"{prefix}{self._label_n}"

    def _warn(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)

    def _temp_text_file(self, text: str) -> Path:
        # Content-addressed and left in place: two clips with the same title
        # share one file, and nothing is deleted out from under a running
        # render.
        return write_text_asset(text)

    # -- public ---------------------------------------------------------
    def compile(self) -> CompiledRender:
        duration = self.seq.duration
        if self.rs.range:
            duration = max(0.0, self.rs.range[1] - self.rs.range[0])
        if duration <= 0:
            raise CompileError(
                "This sequence is empty. Add at least one clip to the timeline "
                "before rendering."
            )

        W, H, fps = self.rs.width, self.rs.height, self.rs.fps
        bg = _hex_to_ffmpeg(self.seq.background, "black")

        # Canvas.
        base_idx = self.cmd.add_input(
            f"color=c={bg}:s={W}x{H}:r={fps}:d={duration:.4f}", "-f", "lavfi"
        )
        acc = f"{base_idx}:v"

        audio_labels: list[str] = []

        for track in self.seq.video_tracks():
            if track.hidden:
                continue
            for clip in track.sorted_clips():
                if not clip.enabled:
                    continue
                if clip.kind == ClipKind.TEXT:
                    acc = self._apply_text(acc, clip)
                elif clip.kind == ClipKind.ADJUSTMENT or track.is_adjustment:
                    acc = self._apply_adjustment(acc, clip)
                else:
                    acc = self._composite_clip(acc, clip)
                    a = self._build_clip_audio(clip, track.volume)
                    if a:
                        audio_labels.append(a)

        for track in self.seq.audio_tracks():
            if track.muted:
                continue
            for clip in track.sorted_clips():
                if clip.enabled and not clip.muted:
                    a = self._build_clip_audio(clip, track.volume)
                    if a:
                        audio_labels.append(a)

        # Final video normalisation.
        vout = self._label("vout")
        self.chains.append(
            f"[{acc}]format={self.rs.pix_fmt},fps={fps},setsar=1[{vout}]"
        )

        aout: str | None = None
        if audio_labels:
            aout = self._mix_audio(audio_labels)

        self.cmd.filter_complex = ";".join(c for c in self.chains if c)
        self.cmd.expected_duration = duration
        self._add_output(vout, aout, duration)

        return CompiledRender(
            command=self.cmd,
            duration=duration,
            warnings=self.warnings,
            temp_files=self.temp_files,
            has_audio=aout is not None,
        )

    # -- inputs ---------------------------------------------------------
    def _add_clip_input(self, clip: Clip) -> int | None:
        """Register the media input for a clip, seeking/trimming at the source."""
        if clip.kind == ClipKind.SOLID:
            color = _hex_to_ffmpeg(clip.color, "black")
            return self.cmd.add_input(
                f"color=c={color}:s={self.rs.width}x{self.rs.height}"
                f":r={self.rs.fps}:d={clip.duration:.4f}",
                "-f", "lavfi",
            )

        if not clip.asset_id:
            self._warn(f"Clip '{clip.name or clip.id}' has no media and was skipped.")
            return None
        asset = self.assets.get(clip.asset_id)
        if asset is None:
            self._warn(
                f"Media for clip '{clip.name or clip.id}' is missing or still "
                "ingesting; it was skipped."
            )
            return None

        source_dur = clip.duration * clip.speed
        if asset.kind == "image" or clip.kind == ClipKind.IMAGE:
            return self.cmd.add_input(
                asset.path, "-loop", "1", "-framerate", f"{self.rs.fps}",
                "-t", f"{source_dur:.4f}",
            )

        available = asset.duration - clip.in_point
        if available > 0 and source_dur > available + 0.05:
            self._warn(
                f"Clip '{clip.name or clip.id}' asks for {source_dur:.2f}s from "
                f"{available:.2f}s of remaining media; the last frame is held."
            )
        # Input-level seek is a keyframe seek and is far cheaper than trim.
        return self.cmd.add_input(
            asset.path, "-ss", f"{max(0.0, clip.in_point):.4f}",
            "-t", f"{source_dur:.4f}",
        )

    # -- video ----------------------------------------------------------
    def _composite_clip(self, acc: str, clip: Clip) -> str:
        idx = self._add_clip_input(clip)
        if idx is None:
            return acc

        asset = self.assets.get(clip.asset_id or "")
        filters: list[str] = ["setpts=PTS-STARTPTS", f"fps={self.rs.fps}"]

        if clip.reverse:
            filters.append("reverse")
        if abs(clip.speed - 1.0) > 1e-6:
            filters.append(f"setpts={1.0 / clip.speed:.6f}*PTS")

        tf = clip.transform
        if any((tf.crop_left, tf.crop_right, tf.crop_top, tf.crop_bottom)):
            kw = max(0.02, 1.0 - tf.crop_left - tf.crop_right)
            kh = max(0.02, 1.0 - tf.crop_top - tf.crop_bottom)
            filters.append(
                f"crop=w=iw*{kw:.5f}:h=ih*{kh:.5f}"
                f":x=iw*{tf.crop_left:.5f}:y=ih*{tf.crop_top:.5f}"
            )

        filters += self._fit_filters(tf.fit)
        scale = self._static_transform(clip, "scale")
        if abs(scale - 1.0) > 1e-6:
            filters.append(
                f"scale=w=trunc(iw*{scale:.5f}/2)*2:h=trunc(ih*{scale:.5f}/2)*2"
            )

        ctx = EffectContext(
            width=self.rs.width, height=self.rs.height, fps=self.rs.fps,
            clip_start=clip.start, clip_duration=clip.duration,
            warnings=self.warnings, lut_paths=self._lut_paths(),
        )
        for effect in clip.effects:
            if effect.type in AUDIO_EFFECTS:
                continue
            filters += build_effect(effect, ctx)

        filters.append("format=rgba")

        # Rotation: `rotate` evaluates its angle per frame, so a keyframed
        # rotation compiles straight to an expression. The output box is sized
        # for the largest angle in the curve, otherwise corners clip mid-spin.
        rotation = self._static_transform(clip, "rotation")
        if abs(rotation) > 1e-6:
            rad = math.radians(rotation)
            filters.append(
                f"rotate={rad:.6f}:c=none:ow=rotw({rad:.6f}):oh=roth({rad:.6f})"
            )

        # Opacity: `colorchannelmixer` takes only a constant, so an animated
        # opacity is drawn onto the alpha plane with `geq`. That is per pixel
        # and slower, which is why a constant opacity keeps the cheap path.
        if tf.animated("opacity"):
            self._warn(
                "Animated opacity is drawn per pixel and renders more slowly "
                "than a constant one."
            )
            expr = self._transform_expr(clip, "opacity", local=True, time_var="T")
            filters.append(
                "geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)'"
                f":a='alpha(X,Y)*clip({expr},0,1)'"
            )
        else:
            opacity = max(0.0, min(1.0, tf.opacity))
            if opacity < 0.999:
                filters.append(f"colorchannelmixer=aa={opacity:.4f}")

        filters += self._transition_filters(clip)
        filters.append(f"setpts=PTS+{clip.start:.6f}/TB")

        label = self._label("v")
        self.chains.append(f"[{idx}:v]{','.join(filters)}[{label}]")

        out = self._label("ov")
        slide_x, slide_y = self._slide_offsets(clip)
        # Position: overlay's x/y are per-frame expressions, so keyframed
        # movement is exact and costs nothing extra.
        px = (self._transform_expr(clip, "x") if tf.animated("x")
              else f"{tf.x:.3f}")
        py = (self._transform_expr(clip, "y") if tf.animated("y")
              else f"{tf.y:.3f}")
        x = f"(W-w)/2+{px}" + (f"+{slide_x}" if slide_x else "")
        y = f"(H-h)/2+{py}" + (f"+{slide_y}" if slide_y else "")
        self.chains.append(
            f"[{acc}][{label}]overlay=x='{x}':y='{y}'"
            f":eof_action=pass:repeatlast=0:format=auto"
            f":enable='between(t,{clip.start:.4f},{clip.end:.4f})'[{out}]"
        )
        return out

    def _lut_paths(self) -> dict:
        """LUT assets in this project, by id.

        Built from the resolved asset map, so a LUT can only ever be a file
        this project owns.
        """
        return {
            asset_id: source.path
            for asset_id, source in self.assets.items()
            if source.kind == "lut"
        }

    # -- transform animation ---------------------------------------------
    def _transform_expr(self, clip: Clip, name: str, local: bool = False,
                        degrees_to_radians: bool = False,
                        time_var: str = "t") -> str:
        """Compile a transform curve into an FFmpeg expression.

        `local` selects the time base: filters inside the clip's own chain see
        clip-local time, because the chain is shifted onto the timeline
        afterwards. `overlay` runs after that shift and sees sequence time.
        `time_var` covers `geq`, which calls time `T` rather than `t`.
        """
        curve = clip.transform.keyframes.get(name) or []
        offset = 0.0 if local else clip.start
        expr = keyframe_expression(curve, offset, time_var=time_var)
        if degrees_to_radians:
            expr = f"({expr})*{math.pi / 180:.9f}"
        return expr

    def _static_transform(self, clip: Clip, name: str) -> float:
        """The value to bake for a property FFmpeg cannot animate.

        Layer scale is the notable one: `scale` fixes its output size at
        init, so an animated scale cannot change the layer's size over time.
        Rather than silently ignore the curve, the midpoint is used and the
        render says what happened and what to use instead.
        """
        tf = clip.transform
        if not tf.animated(name):
            return float(getattr(tf, name))
        # `scale` fixes its output size when the filter starts, and `rotate`
        # reads its angle once. Neither can follow a curve, so the render bakes
        # a value and says which one it used.
        hint = (" For an animated push-in, add the Push In effect instead."
                if name == "scale" else "")
        self._warn(
            f"'{name}' is keyframed, but FFmpeg cannot animate it, so the "
            f"render used the value at the clip midpoint.{hint}"
        )
        return tf.value_at(name, clip.duration / 2)

    def _fit_filters(self, fit: FitMode) -> list[str]:
        W, H = self.rs.width, self.rs.height
        if fit == FitMode.NONE:
            return []
        if fit == FitMode.STRETCH:
            return [f"scale={W}:{H}:flags=bicubic"]
        if fit == FitMode.COVER:
            return [
                f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=bicubic",
                f"crop={W}:{H}",
            ]
        return [f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=bicubic"]

    WIPES = (TransitionType.WIPE_LEFT, TransitionType.WIPE_RIGHT)
    SLIDES = (TransitionType.SLIDE_LEFT, TransitionType.SLIDE_RIGHT,
              TransitionType.SLIDE_UP, TransitionType.SLIDE_DOWN)

    def _transition_filters(self, clip: Clip) -> list[str]:
        """Transitions expressed on the clip itself, in clip-local time.

        Fades and dissolves are the same `fade` filter differing only in
        whether they touch alpha; a dissolve therefore reveals whatever is
        composited beneath, while a fade goes to black. Slides are handled at
        the overlay stage instead, because animating a position is far cheaper
        than rewriting pixels.
        """
        out: list[str] = []
        for trans, is_in in ((clip.transition_in, True), (clip.transition_out, False)):
            if trans.type == TransitionType.NONE or trans.duration <= 0.01:
                continue
            d = min(trans.duration, clip.duration)
            st = 0.0 if is_in else max(0.0, clip.duration - d)

            if trans.type == TransitionType.FADE:
                out.append(
                    f"fade=t={'in' if is_in else 'out'}:st={st:.4f}:d={d:.4f}:alpha=0"
                )
            elif trans.type == TransitionType.DISSOLVE:
                out.append(
                    f"fade=t={'in' if is_in else 'out'}:st={st:.4f}:d={d:.4f}:alpha=1"
                )
            elif trans.type in self.WIPES:
                out.append(self._wipe_filter(trans.type, d, st, is_in))
        return out

    def _wipe_filter(self, ttype: TransitionType, d: float, st: float,
                     is_in: bool) -> str:
        """A hard-edged wipe, as an animated alpha mask.

        FFmpeg's `crop` cannot animate its width, so the mask is drawn with
        `geq`. That is pixel-wise and noticeably slower than a dissolve, which
        is why it is only used when a wipe is explicitly asked for.
        """
        self._warn(
            "Wipe transitions are drawn per-pixel and render more slowly than "
            "fades or slides."
        )
        p = (f"min(1,max(0,T/{d:.4f}))" if is_in
             else f"min(1,max(0,(T-{st:.4f})/{d:.4f}))")
        if ttype == TransitionType.WIPE_LEFT:
            edge = f"lt(X,W*{p})" if is_in else f"gt(X,W*{p})"
        else:
            edge = f"gt(X,W*(1-{p}))" if is_in else f"lt(X,W*(1-{p}))"
        return (
            "geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)'"
            f":a='if({edge},alpha(X,Y),0)'"
        )

    def _slide_offsets(self, clip: Clip) -> tuple[str, str]:
        """Additive overlay x/y expressions implementing slide transitions."""
        xs: list[str] = []
        ys: list[str] = []
        for trans, is_in in ((clip.transition_in, True), (clip.transition_out, False)):
            if trans.type not in self.SLIDES or trans.duration <= 0.01:
                continue
            d = min(trans.duration, clip.duration)
            if is_in:
                # 1 at the start, 0 once the clip has fully arrived.
                k = f"(1-min(1,max(0,(t-{clip.start:.4f})/{d:.4f})))"
            else:
                st = clip.end - d
                k = f"min(1,max(0,(t-{st:.4f})/{d:.4f}))"
            sign = -1 if is_in else 1
            if trans.type == TransitionType.SLIDE_LEFT:
                xs.append(f"({sign * -1}*W*{k})")
            elif trans.type == TransitionType.SLIDE_RIGHT:
                xs.append(f"({sign}*W*{k})")
            elif trans.type == TransitionType.SLIDE_UP:
                ys.append(f"({sign * -1}*H*{k})")
            else:
                ys.append(f"({sign}*H*{k})")
        return ("+".join(xs), "+".join(ys))

    # -- adjustment layers ----------------------------------------------
    def _apply_adjustment(self, acc: str, clip: Clip) -> str:
        """Apply a clip's effects to everything composited beneath it."""
        from .effects import REGISTRY

        ctx = EffectContext(
            width=self.rs.width, height=self.rs.height, fps=self.rs.fps,
            clip_start=clip.start, clip_duration=clip.duration,
            on_adjustment=True, warnings=self.warnings,
            lut_paths=self._lut_paths(),
        )
        gate = f"enable='between(t,{clip.start:.4f},{clip.end:.4f})'"
        filters: list[str] = []
        for effect in clip.effects:
            if effect.type in AUDIO_EFFECTS:
                continue
            spec = REGISTRY.get(effect.type)
            built = build_effect(effect, ctx)
            if not built:
                continue
            if spec is not None and not spec.timeline_gated:
                self._warn(
                    f"'{spec.label}' cannot be time-gated by FFmpeg, so on an "
                    "adjustment layer it applies for the whole sequence. Put it "
                    "on the clip instead to limit it."
                )
                filters += built
            else:
                filters += [f"{f}:{gate}" if "=" in f else f"{f}={gate}"
                            for f in built]
        if not filters:
            return acc
        out = self._label("adj")
        self.chains.append(f"[{acc}]{','.join(filters)}[{out}]")
        return out

    # -- text ------------------------------------------------------------
    def _apply_text(self, acc: str, clip: Clip) -> str:
        content = clip.text
        if content is None or not content.text.strip():
            return acc
        style = content.style
        font = resolve_font(style.font_family, style.bold)
        if not font:
            self._warn(
                "No usable font was found on this machine, so text layers were "
                "skipped in the render."
            )
            return acc

        text = content.text.upper() if style.uppercase else content.text
        text_file = self._temp_text_file(text)

        opts = [
            f"fontfile={quote_filter_arg(stage_readable(font))}",
            f"textfile={quote_filter_arg(text_file)}",
            f"fontsize={max(4, int(style.font_size))}",
            f"fontcolor={_hex_to_ffmpeg(style.color, 'white')}",
            f"x=(w-text_w)*{max(0.0, min(1.0, content.anchor_x)):.4f}",
            f"y=(h-text_h)*{max(0.0, min(1.0, content.anchor_y)):.4f}",
            f"line_spacing={int((style.line_height - 1.0) * style.font_size)}",
            "text_align=" + {"left": "L", "center": "C", "right": "R"}[style.align],
            f"enable='between(t,{clip.start:.4f},{clip.end:.4f})'",
        ]
        if style.stroke_color and style.stroke_width > 0:
            opts.append(f"borderw={int(style.stroke_width)}")
            opts.append(f"bordercolor={_hex_to_ffmpeg(style.stroke_color, 'black')}")
        if style.shadow:
            opts += ["shadowcolor=black@0.55", "shadowx=3", "shadowy=3"]
        if style.background:
            opts += [
                "box=1",
                f"boxcolor={_hex_to_ffmpeg(style.background, 'black')}@0.75",
                f"boxborderw={int(style.background_padding)}",
            ]

        alpha = self._text_alpha_expr(clip, content.animation)
        if alpha:
            opts.append(f"alpha='{alpha}'")

        out = self._label("txt")
        self.chains.append(f"[{acc}]drawtext={':'.join(opts)}[{out}]")
        return out

    def _text_alpha_expr(self, clip: Clip, animation: str) -> str | None:
        if animation in ("none", ""):
            return None
        d = min(0.4, clip.duration / 3.0)
        if d <= 0.02:
            return None
        s, e = clip.start, clip.end
        # Ramp in over d, hold, ramp out over d -- clamped to the clip window.
        return (
            f"if(lt(t,{s:.4f}),0,"
            f"if(lt(t,{s + d:.4f}),(t-{s:.4f})/{d:.4f},"
            f"if(lt(t,{e - d:.4f}),1,"
            f"max(0,({e:.4f}-t)/{d:.4f}))))"
        )

    # -- audio -----------------------------------------------------------
    def _build_clip_audio(self, clip: Clip, track_volume: float) -> str | None:
        if clip.muted or clip.kind in (ClipKind.TEXT, ClipKind.SOLID,
                                       ClipKind.ADJUSTMENT, ClipKind.IMAGE):
            return None
        asset = self.assets.get(clip.asset_id or "")
        if asset is None or not asset.has_audio:
            return None

        # The video pass already registered an input with the right -ss/-t;
        # registering a second one keeps the audio graph independent of
        # filter ordering and costs only a demux.
        idx = self._add_clip_input(clip)
        if idx is None:
            return None

        filters: list[str] = [
            "asetpts=PTS-STARTPTS",
            f"aformat=sample_fmts=fltp:sample_rates={self.rs.sample_rate}"
            ":channel_layouts=stereo",
        ]
        if clip.reverse:
            filters.append("areverse")
        if abs(clip.speed - 1.0) > 1e-6:
            filters += _atempo_chain(clip.speed)

        ctx = EffectContext(
            width=self.rs.width, height=self.rs.height, fps=self.rs.fps,
            clip_start=clip.start, clip_duration=clip.duration,
            warnings=self.warnings,
        )
        for effect in clip.effects:
            if effect.type in AUDIO_EFFECTS:
                filters += build_effect(effect, ctx)

        gain = max(0.0, clip.volume) * max(0.0, track_volume)
        if abs(gain - 1.0) > 1e-6:
            filters.append(f"volume={gain:.4f}")

        for trans, is_in in ((clip.transition_in, True), (clip.transition_out, False)):
            if trans.type != TransitionType.NONE and trans.duration > 0.01:
                d = min(trans.duration, clip.duration)
                if is_in:
                    filters.append(f"afade=t=in:st=0:d={d:.4f}")
                else:
                    filters.append(
                        f"afade=t=out:st={max(0.0, clip.duration - d):.4f}:d={d:.4f}"
                    )

        # Trim to the clip's exact length before delaying it into place, so a
        # long source can never bleed past the clip's out point.
        filters.append(f"atrim=duration={clip.duration:.6f}")
        filters.append("asetpts=PTS-STARTPTS")
        if clip.start > 0.0005:
            ms = int(round(clip.start * 1000))
            filters.append(f"adelay={ms}:all=1")

        label = self._label("a")
        self.chains.append(f"[{idx}:a]{','.join(filters)}[{label}]")
        return label

    def _mix_audio(self, labels: list[str]) -> str:
        if len(labels) == 1:
            out = self._label("aout")
            self.chains.append(
                f"[{labels[0]}]aformat=sample_fmts=fltp"
                f":sample_rates={self.rs.sample_rate}:channel_layouts=stereo,"
                f"alimiter=limit=0.97[{out}]"
            )
            return out
        joined = "".join(f"[{l}]" for l in labels)
        out = self._label("aout")
        # normalize=0 keeps levels the editor set; a limiter catches sums that
        # would otherwise clip.
        self.chains.append(
            f"{joined}amix=inputs={len(labels)}:normalize=0"
            f":dropout_transition=0:duration=longest,"
            f"alimiter=limit=0.97[{out}]"
        )
        return out

    # -- output ----------------------------------------------------------
    def _add_output(self, vout: str, aout: str | None, duration: float) -> None:
        opts: list[str] = ["-map", f"[{vout}]"]
        if aout:
            opts += ["-map", f"[{aout}]"]
        opts += [
            "-c:v", self.rs.video_codec,
            "-preset", self.rs.preset,
            "-crf", str(self.rs.crf),
            "-pix_fmt", self.rs.pix_fmt,
            "-r", f"{self.rs.fps}",
            "-movflags", "+faststart",
        ]
        if aout:
            opts += [
                "-c:a", self.rs.audio_codec,
                "-b:a", self.rs.audio_bitrate,
                "-ar", str(self.rs.sample_rate),
                "-ac", "2",
            ]
        opts += ["-t", f"{duration:.4f}"]
        # Output path is attached by the renderer.
        self._output_options = opts

    @property
    def output_options(self) -> list[str]:
        return getattr(self, "_output_options", [])


def compile_sequence(
    sequence: Sequence,
    assets: dict[str, AssetSource],
    render_settings: RenderSettings | None = None,
    output_path: Path | None = None,
) -> CompiledRender:
    compiler = SequenceCompiler(sequence, assets, render_settings)
    compiled = compiler.compile()
    if output_path is not None:
        compiled.command.add_output(output_path, *compiler.output_options)
    return compiled
