"""Effect registry: schema effects -> FFmpeg filter fragments.

Each effect declares whether its parameters can be animated inside FFmpeg.
Filters like `eq` accept per-frame expressions, so keyframes compile to a
piecewise-linear expression in `t`. Filters that only take scalars (blur
sigma, grain strength) are evaluated once and the compiler records a warning
rather than silently pretending the animation happened.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..schema.core import Easing, Effect, Keyframe

# --------------------------------------------------------------------------
# Keyframe compilation
# --------------------------------------------------------------------------

_EASE_EXPR = {
    Easing.LINEAR: "{p}",
    Easing.EASE_IN: "({p}*{p})",
    Easing.EASE_OUT: "(1-(1-{p})*(1-{p}))",
    Easing.EASE_IN_OUT: "(if(lt({p},0.5),2*{p}*{p},1-2*(1-{p})*(1-{p})))",
    Easing.HOLD: "0",
}


def evaluate_keyframes(kfs: list[Keyframe], t: float) -> float:
    """Sample an animation curve in Python (used by previews and fallbacks)."""
    if not kfs:
        return 0.0
    ordered = sorted(kfs, key=lambda k: k.time)
    if t <= ordered[0].time:
        return ordered[0].value
    if t >= ordered[-1].time:
        return ordered[-1].value
    for a, b in zip(ordered, ordered[1:]):
        if a.time <= t <= b.time:
            span = b.time - a.time
            if span <= 0:
                return b.value
            p = (t - a.time) / span
            eased = _ease_python(a.easing, p)
            return a.value + (b.value - a.value) * eased
    return ordered[-1].value


def _ease_python(easing: Easing, p: float) -> float:
    if easing == Easing.LINEAR:
        return p
    if easing == Easing.EASE_IN:
        return p * p
    if easing == Easing.EASE_OUT:
        return 1 - (1 - p) * (1 - p)
    if easing == Easing.HOLD:
        return 0.0
    return 2 * p * p if p < 0.5 else 1 - 2 * (1 - p) * (1 - p)


def keyframe_expression(kfs: list[Keyframe], offset: float = 0.0,
                        time_var: str = "t") -> str:
    """Compile keyframes into an FFmpeg expression over time.

    `offset` is the clip's timeline start, because most filters see sequence
    time. `time_var` exists because filters disagree on the name: most use
    `t`, but `geq` evaluates per pixel and calls it `T`.
    """
    ordered = sorted(kfs, key=lambda k: k.time)
    if not ordered:
        return "0"
    if len(ordered) == 1:
        return f"{ordered[0].value:.6f}"

    local = f"({time_var}-{offset:.6f})"
    expr = f"{ordered[-1].value:.6f}"
    # Build nested if() from the last segment backwards.
    for a, b in reversed(list(zip(ordered, ordered[1:]))):
        span = max(1e-6, b.time - a.time)
        p = f"(({local}-{a.time:.6f})/{span:.6f})"
        eased = _EASE_EXPR[a.easing].format(p=p)
        seg = f"({a.value:.6f}+({b.value - a.value:.6f})*{eased})"
        expr = f"if(lt({local},{b.time:.6f}),{seg},{expr})"
    first = f"{ordered[0].value:.6f}"
    return f"if(lt({local},{ordered[0].time:.6f}),{first},{expr})"


# --------------------------------------------------------------------------
# Effect definitions
# --------------------------------------------------------------------------

@dataclass
class EffectSpec:
    type: str
    label: str
    category: str
    #: Builds one or more filter expressions from resolved parameters.
    build: Callable[[dict, "EffectContext"], list[str]]
    defaults: dict = field(default_factory=dict)
    animatable: set[str] = field(default_factory=set)
    #: Effects that cannot be gated by `enable=` must not go on adjustment layers.
    timeline_gated: bool = True


@dataclass
class EffectContext:
    """Everything a builder needs beyond its own parameters."""
    width: int
    height: int
    fps: float
    clip_start: float = 0.0
    clip_duration: float = 1.0
    on_adjustment: bool = False
    warnings: list[str] = field(default_factory=list)
    #: Expression-valued parameters, pre-compiled from keyframes.
    expressions: dict[str, str] = field(default_factory=dict)
    #: asset id -> on-disk LUT, resolved by the compiler. Effects never see a
    #: raw path, so a parameter cannot name a file outside the project.
    lut_paths: dict[str, object] = field(default_factory=dict)

    def value(self, params: dict, name: str, default=None):
        """Parameter as an FFmpeg-safe token, using an expression if animated."""
        if name in self.expressions:
            return f"'{self.expressions[name]}'"
        return params.get(name, default)


REGISTRY: dict[str, EffectSpec] = {}


def register(spec: EffectSpec) -> EffectSpec:
    REGISTRY[spec.type] = spec
    return spec


def _num(params: dict, key: str, default: float) -> float:
    try:
        return float(params.get(key, default))
    except (TypeError, ValueError):
        return default


# --- colour ---------------------------------------------------------------

def _build_exposure(p: dict, ctx: EffectContext) -> list[str]:
    # eq brightness is -1..1 additive; expose stops as a friendlier control.
    if "brightness" in ctx.expressions:
        return [f"eq=brightness='{ctx.expressions['brightness']}':eval=frame"]
    stops = _num(p, "stops", 0.0)
    brightness = max(-1.0, min(1.0, stops * 0.18))
    return [f"eq=brightness={brightness:.4f}"]


def _build_color(p: dict, ctx: EffectContext) -> list[str]:
    parts = []
    for name, default in (("contrast", 1.0), ("brightness", 0.0),
                          ("saturation", 1.0), ("gamma", 1.0)):
        if name in ctx.expressions:
            parts.append(f"{name}='{ctx.expressions[name]}'")
        else:
            val = _num(p, name, default)
            if abs(val - default) > 1e-6:
                parts.append(f"{name}={val:.4f}")
    if not parts:
        return []
    if ctx.expressions:
        parts.append("eval=frame")
    return ["eq=" + ":".join(parts)]


def _build_temperature(p: dict, ctx: EffectContext) -> list[str]:
    kelvin = _num(p, "kelvin", 6500)
    mix = _num(p, "mix", 1.0)
    return [f"colortemperature=temperature={kelvin:.0f}:mix={mix:.3f}"]


def _build_balance(p: dict, ctx: EffectContext) -> list[str]:
    keys = ("rs", "gs", "bs", "rm", "gm", "bm", "rh", "gh", "bh")
    parts = [f"{k}={_num(p, k, 0.0):.4f}" for k in keys if abs(_num(p, k, 0.0)) > 1e-6]
    if not parts:
        return []
    parts.append("pl=1")  # preserve lightness so grades do not crush luma
    return ["colorbalance=" + ":".join(parts)]


def _build_curves(p: dict, ctx: EffectContext) -> list[str]:
    preset = p.get("preset")
    if preset:
        return [f"curves=preset={preset}"]
    parts = []
    for ch in ("r", "g", "b", "all"):
        pts = p.get(ch)
        if pts:
            parts.append(f"{ch}='{pts}'")
    return ["curves=" + ":".join(parts)] if parts else []


def _build_lut(p: dict, ctx: EffectContext) -> list[str]:
    """Apply a 3D LUT that was uploaded to this project.

    The LUT is named by asset id, never by filesystem path. A path parameter
    would let anything that can write an operation -- including a language
    model -- point FFmpeg at an arbitrary file and have it read.
    """
    from ..media.ffmpeg import quote_filter_arg

    from .scratch import stage_readable

    asset_id = p.get("asset_id") or p.get("lut_id")
    if not asset_id:
        ctx.warnings.append(
            "LUT effect skipped: it needs an 'asset_id' naming a .cube file "
            "uploaded to this project."
        )
        return []

    path = ctx.lut_paths.get(str(asset_id))
    if path is None:
        ctx.warnings.append(
            f"LUT effect skipped: no LUT asset '{asset_id}' in this project."
        )
        return []

    interp = str(p.get("interp", "tetrahedral"))
    if interp not in ("nearest", "trilinear", "tetrahedral"):
        interp = "tetrahedral"
    return [f"lut3d=file={quote_filter_arg(stage_readable(path))}:interp={interp}"]


def _build_hue(p: dict, ctx: EffectContext) -> list[str]:
    h = _num(p, "hue", 0.0)
    s = _num(p, "saturation", 1.0)
    return [f"hue=h={h:.3f}:s={s:.3f}"]


# --- looks ----------------------------------------------------------------

GRADE_PRESETS: dict[str, list[str]] = {
    "cinematic_dark": [
        "eq=contrast=1.14:brightness=-0.035:saturation=0.90:gamma=0.96",
        "colorbalance=rs=-0.06:bs=0.10:rm=0.02:bm=0.02:rh=0.05:bh=-0.04:pl=1",
        "curves=all='0/0.02 0.25/0.20 0.75/0.80 1/0.96'",
    ],
    "teal_orange": [
        "eq=contrast=1.12:saturation=1.06",
        "colorbalance=rs=-0.10:gs=-0.02:bs=0.14:rh=0.10:gh=0.03:bh=-0.10:pl=1",
    ],
    "luxury": [
        "eq=contrast=1.08:brightness=-0.02:saturation=0.86:gamma=0.98",
        "colorbalance=rs=-0.03:bs=0.06:rh=0.06:gh=0.02:bh=-0.05:pl=1",
        "curves=all='0/0.03 0.5/0.5 1/0.94'",
    ],
    "warm": [
        "eq=contrast=1.05:saturation=1.08",
        "colorbalance=rm=0.08:gm=0.02:bm=-0.06:pl=1",
    ],
    "cool": [
        "eq=contrast=1.05:saturation=0.98",
        "colorbalance=rm=-0.06:bm=0.09:pl=1",
    ],
    "high_contrast": [
        "eq=contrast=1.30:saturation=1.05",
        "curves=all='0/0 0.25/0.16 0.75/0.86 1/1'",
    ],
    "flat": ["eq=contrast=0.88:saturation=0.92:gamma=1.05"],
    "bw": ["hue=s=0", "eq=contrast=1.16"],
    "vintage": [
        "curves=r='0/0.08 0.5/0.55 1/0.95':g='0/0.04 0.5/0.5 1/0.92':b='0/0.12 0.5/0.46 1/0.86'",
        "eq=saturation=0.78:contrast=0.96",
    ],
    "cyberpunk": [
        "eq=contrast=1.22:saturation=1.35",
        "colorbalance=rs=0.10:bs=0.22:gm=-0.05:rh=-0.08:bh=0.12:pl=1",
    ],
    "night": [
        "eq=contrast=1.10:brightness=-0.12:saturation=0.72:gamma=0.88",
        "colorbalance=rs=-0.10:bs=0.18:rm=-0.05:bm=0.10:pl=1",
    ],
    "golden_hour": [
        "eq=contrast=1.06:saturation=1.12",
        "colorbalance=rm=0.12:gm=0.04:bm=-0.10:rh=0.08:bh=-0.06:pl=1",
    ],
}


def _build_grade(p: dict, ctx: EffectContext) -> list[str]:
    preset = str(p.get("preset", "cinematic_dark"))
    chain = list(GRADE_PRESETS.get(preset, GRADE_PRESETS["cinematic_dark"]))
    intensity = max(0.0, min(1.0, _num(p, "intensity", 1.0)))
    if intensity < 0.999:
        # Blend toward neutral by scaling the eq terms; curves stay binary,
        # so drop them below half strength rather than misrepresent the look.
        scaled = []
        for f in chain:
            if f.startswith("eq=") or f.startswith("colorbalance="):
                scaled.append(_scale_filter(f, intensity))
            elif intensity >= 0.5:
                scaled.append(f)
        chain = scaled
    return chain


def _scale_filter(expr: str, k: float) -> str:
    """Scale a filter's numeric args toward their neutral value."""
    neutral = {"contrast": 1.0, "saturation": 1.0, "gamma": 1.0}
    name, _, args = expr.partition("=")
    out = []
    for arg in args.split(":"):
        key, _, val = arg.partition("=")
        try:
            v = float(val)
        except ValueError:
            out.append(arg)
            continue
        base = neutral.get(key, 0.0)
        out.append(f"{key}={base + (v - base) * k:.4f}")
    return f"{name}=" + ":".join(out)


def _build_grain(p: dict, ctx: EffectContext) -> list[str]:
    intensity = max(0.0, min(1.0, _num(p, "intensity", 0.18)))
    strength = int(round(intensity * 40))
    if strength <= 0:
        return []
    return [f"noise=alls={strength}:allf=t+u"]


def _build_vignette(p: dict, ctx: EffectContext) -> list[str]:
    strength = max(0.0, min(1.0, _num(p, "intensity", 0.4)))
    if strength <= 0.001:
        return []
    # vignette angle: PI/5 is a strong falloff, PI/12 is subtle.
    angle = 0.26 + strength * 0.5
    return [f"vignette=angle={angle:.4f}:mode=forward"]


def _build_blur(p: dict, ctx: EffectContext) -> list[str]:
    sigma = max(0.0, _num(p, "sigma", 8.0))
    if sigma <= 0.01:
        return []
    if "sigma" in ctx.expressions:
        ctx.warnings.append(
            "Blur strength is keyframed, but FFmpeg's gblur takes a constant "
            "sigma; the render uses the value at the clip midpoint."
        )
    steps = int(max(1, min(6, _num(p, "steps", 1))))
    return [f"gblur=sigma={sigma:.3f}:steps={steps}"]


def _build_sharpen(p: dict, ctx: EffectContext) -> list[str]:
    amount = _num(p, "amount", 0.8)
    return [f"unsharp=luma_msize_x=5:luma_msize_y=5:luma_amount={amount:.3f}"]


def _build_denoise(p: dict, ctx: EffectContext) -> list[str]:
    strength = max(0.0, min(1.0, _num(p, "strength", 0.4)))
    ls = 4 * strength
    cs = 3 * strength
    return [f"hqdn3d={ls:.2f}:{cs:.2f}:{ls * 1.5:.2f}:{cs * 1.5:.2f}"]


def _build_stabilize(p: dict, ctx: EffectContext) -> list[str]:
    # `deshake` is a single-pass stabiliser: no analysis file required, which
    # keeps it usable inside a normal render graph.
    rx = int(_num(p, "range", 16))
    return [f"deshake=rx={rx}:ry={rx}:edge=clamp:blocksize=8:contrast=125"]


def _build_letterbox(p: dict, ctx: EffectContext) -> list[str]:
    ratio = str(p.get("aspect_ratio", "2.39:1"))
    try:
        a, b = (float(x) for x in ratio.replace("/", ":").split(":"))
        target = a / b
    except (ValueError, ZeroDivisionError):
        target = 2.39
    canvas = ctx.width / max(1, ctx.height)
    if target > canvas + 1e-3:
        # Target is wider than the canvas: bars go top and bottom, and the
        # visible picture is only `width / target` tall.
        bar = int(round((ctx.height - ctx.width / target) / 2))
        bar = max(0, min(ctx.height // 2 - 2, bar))
        if bar <= 0:
            return []
        return [
            f"drawbox=x=0:y=0:w={ctx.width}:h={bar}:color=black@1:t=fill",
            f"drawbox=x=0:y={ctx.height - bar}:w={ctx.width}:h={bar}:color=black@1:t=fill",
        ]
    # Target is narrower than the canvas: pillarbox instead.
    bar = int(round((ctx.width - ctx.height * target) / 2))
    bar = max(0, min(ctx.width // 2 - 2, bar))
    if bar <= 0:
        return []
    return [
        f"drawbox=x=0:y=0:w={bar}:h={ctx.height}:color=black@1:t=fill",
        f"drawbox=x={ctx.width - bar}:y=0:w={bar}:h={ctx.height}:color=black@1:t=fill",
    ]


def _build_glow(p: dict, ctx: EffectContext) -> list[str]:
    # A bloom: blur a copy and screen it back over the original.
    intensity = max(0.0, min(1.0, _num(p, "intensity", 0.35)))
    sigma = max(1.0, _num(p, "sigma", 14))
    if intensity <= 0.01:
        return []
    return [
        f"split=2[gl_a][gl_b];[gl_b]gblur=sigma={sigma:.2f},"
        f"colorchannelmixer=aa={intensity:.3f}[gl_blur];"
        f"[gl_a][gl_blur]blend=all_mode=screen:all_opacity={intensity:.3f}"
    ]


def _build_shake(p: dict, ctx: EffectContext) -> list[str]:
    amount = max(0.0, _num(p, "amount", 6.0))
    freq = max(0.1, _num(p, "frequency", 7.0))
    if amount <= 0.1:
        return []
    pad = int(max(2, amount * 2))
    # Pad outward first so the shake never exposes the frame edge. crop's
    # x/y are evaluated per frame in every FFmpeg version; w/h are not, which
    # is exactly the split we want here.
    return [
        f"scale=iw+{pad * 2}:ih+{pad * 2}",
        f"crop=w=iw-{pad * 2}:h=ih-{pad * 2}"
        f":x='{pad}+{amount:.2f}*sin(t*{freq:.2f})'"
        f":y='{pad}+{amount:.2f}*sin(t*{freq * 1.37:.2f}+1.1)'",
    ]


def _build_zoom(p: dict, ctx: EffectContext) -> list[str]:
    """A slow push-in/out implemented as an animated crop (constant output size)."""
    start = max(1.0, _num(p, "start", 1.0))
    end = max(1.0, _num(p, "end", 1.08))
    dur = max(0.01, ctx.clip_duration)
    # crop cannot animate its width (w/h are init-only), so the push uses
    # zoompan, whose zoom expression is evaluated per frame. The input is
    # normalised to the canvas first so the framing is well-defined.
    z = (f"({start:.4f}+({end - start:.4f})"
         f"*min(1,max(0,in_time/{dur:.4f})))")
    return [
        f"scale={ctx.width}:{ctx.height}:force_original_aspect_ratio=increase",
        f"crop={ctx.width}:{ctx.height}",
        f"zoompan=z='{z}':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":s={ctx.width}x{ctx.height}:fps={ctx.fps:g}",
    ]


def _build_mirror(p: dict, ctx: EffectContext) -> list[str]:
    axis = str(p.get("axis", "horizontal"))
    return ["hflip"] if axis == "horizontal" else ["vflip"]


def _build_pixelate(p: dict, ctx: EffectContext) -> list[str]:
    size = max(2, int(_num(p, "size", 16)))
    return [
        f"scale=iw/{size}:ih/{size}:flags=neighbor",
        f"scale={ctx.width}:{ctx.height}:flags=neighbor",
    ]


# --- audio ----------------------------------------------------------------

def _build_a_volume(p: dict, ctx: EffectContext) -> list[str]:
    if "gain_db" in ctx.expressions:
        return [f"volume='{ctx.expressions['gain_db']}dB':eval=frame"]
    return [f"volume={_num(p, 'gain_db', 0.0):.3f}dB"]


def _build_a_eq(p: dict, ctx: EffectContext) -> list[str]:
    parts = []
    low = _num(p, "low_db", 0.0)
    mid = _num(p, "mid_db", 0.0)
    high = _num(p, "high_db", 0.0)
    if abs(low) > 0.01:
        parts.append(f"equalizer=f=110:width_type=o:width=1.6:g={low:.2f}")
    if abs(mid) > 0.01:
        parts.append(f"equalizer=f=1200:width_type=o:width=1.4:g={mid:.2f}")
    if abs(high) > 0.01:
        parts.append(f"equalizer=f=7500:width_type=o:width=1.6:g={high:.2f}")
    return parts


def _build_a_voice(p: dict, ctx: EffectContext) -> list[str]:
    """Broadcast-style speech cleanup: rumble filter, presence, gentle comp."""
    strength = max(0.0, min(1.0, _num(p, "strength", 0.7)))
    return [
        "highpass=f=85",
        f"equalizer=f=2800:width_type=o:width=1.2:g={2.5 * strength:.2f}",
        f"equalizer=f=250:width_type=o:width=1.0:g={-2.0 * strength:.2f}",
        f"acompressor=threshold=-18dB:ratio={2 + 2 * strength:.1f}:attack=8:release=180:makeup=2",
        "alimiter=limit=0.95",
    ]


def _build_a_denoise(p: dict, ctx: EffectContext) -> list[str]:
    strength = max(0.01, min(0.97, _num(p, "strength", 0.5)))
    return [f"afftdn=nr={12 + strength * 24:.1f}:nf=-28:tn=1"]


def _build_a_normalize(p: dict, ctx: EffectContext) -> list[str]:
    target = _num(p, "lufs", -14.0)
    return [f"loudnorm=I={target:.1f}:TP=-1.5:LRA=11"]


def _build_a_compress(p: dict, ctx: EffectContext) -> list[str]:
    return [
        f"acompressor=threshold={_num(p, 'threshold_db', -20):.1f}dB"
        f":ratio={_num(p, 'ratio', 3.0):.2f}"
        f":attack={_num(p, 'attack_ms', 10):.1f}"
        f":release={_num(p, 'release_ms', 180):.1f}"
    ]


# --- registration ---------------------------------------------------------

for _spec in [
    EffectSpec("exposure", "Exposure", "color", _build_exposure,
               {"stops": 0.0}, {"brightness"}),
    EffectSpec("color", "Color", "color", _build_color,
               {"contrast": 1.0, "brightness": 0.0, "saturation": 1.0, "gamma": 1.0},
               {"contrast", "brightness", "saturation", "gamma"}),
    EffectSpec("temperature", "Temperature", "color", _build_temperature,
               {"kelvin": 6500, "mix": 1.0}),
    EffectSpec("color_balance", "Color Wheels", "color", _build_balance),
    EffectSpec("curves", "Curves", "color", _build_curves),
    EffectSpec("lut", "LUT", "color", _build_lut,
               {"asset_id": "", "interp": "tetrahedral"}),
    EffectSpec("hue", "Hue / Saturation", "color", _build_hue,
               {"hue": 0.0, "saturation": 1.0}),
    EffectSpec("color_grade", "Grade", "look", _build_grade,
               {"preset": "cinematic_dark", "intensity": 1.0}),
    EffectSpec("film_grain", "Film Grain", "look", _build_grain, {"intensity": 0.18}),
    EffectSpec("vignette", "Vignette", "look", _build_vignette, {"intensity": 0.4}),
    EffectSpec("glow", "Glow", "look", _build_glow,
               {"intensity": 0.35, "sigma": 14}, timeline_gated=False),
    EffectSpec("letterbox", "Letterbox", "look", _build_letterbox,
               {"aspect_ratio": "2.39:1"}),
    EffectSpec("blur", "Blur", "image", _build_blur, {"sigma": 8.0}),
    EffectSpec("sharpen", "Sharpen", "image", _build_sharpen, {"amount": 0.8}),
    EffectSpec("denoise", "Denoise", "image", _build_denoise, {"strength": 0.4}),
    EffectSpec("stabilize", "Stabilize", "image", _build_stabilize, {"range": 16},
               timeline_gated=False),
    EffectSpec("pixelate", "Pixelate", "image", _build_pixelate, {"size": 16}),
    EffectSpec("mirror", "Mirror", "image", _build_mirror, {"axis": "horizontal"}),
    EffectSpec("camera_shake", "Camera Shake", "motion", _build_shake,
               {"amount": 6.0, "frequency": 7.0}, timeline_gated=False),
    EffectSpec("zoom", "Push In", "motion", _build_zoom,
               {"start": 1.0, "end": 1.08}, timeline_gated=False),
    # audio
    EffectSpec("a_volume", "Volume", "audio", _build_a_volume, {"gain_db": 0.0},
               {"gain_db"}),
    EffectSpec("a_eq", "EQ", "audio", _build_a_eq),
    EffectSpec("a_voice", "Voice Enhance", "audio", _build_a_voice, {"strength": 0.7}),
    EffectSpec("a_denoise", "Noise Reduction", "audio", _build_a_denoise,
               {"strength": 0.5}),
    EffectSpec("a_normalize", "Loudness", "audio", _build_a_normalize, {"lufs": -14.0}),
    EffectSpec("a_compress", "Compressor", "audio", _build_a_compress),
]:
    register(_spec)


AUDIO_EFFECTS = {t for t, s in REGISTRY.items() if s.category == "audio"}
VIDEO_EFFECTS = {t for t, s in REGISTRY.items() if s.category != "audio"}


def build_effect(effect: Effect, ctx: EffectContext) -> list[str]:
    """Compile one effect into filter expressions (empty if it is a no-op)."""
    spec = REGISTRY.get(effect.type)
    if spec is None:
        ctx.warnings.append(f"Unknown effect '{effect.type}' was skipped.")
        return []
    if not effect.enabled:
        return []

    params = {**spec.defaults, **effect.parameters}
    ctx.expressions = {}
    for name, kfs in (effect.keyframes or {}).items():
        if not kfs:
            continue
        if name in spec.animatable:
            ctx.expressions[name] = keyframe_expression(kfs, ctx.clip_start)
        else:
            params[name] = evaluate_keyframes(kfs, ctx.clip_duration / 2)
            ctx.warnings.append(
                f"'{name}' on {spec.label} is keyframed but that filter takes a "
                "constant; the render used the clip-midpoint value."
            )
    out = spec.build(params, ctx)
    ctx.expressions = {}
    return out


def describe_registry() -> list[dict]:
    """Effect catalogue for the UI's effects browser."""
    return [
        {
            "type": s.type,
            "label": s.label,
            "category": s.category,
            "defaults": s.defaults,
            "animatable": sorted(s.animatable),
        }
        for s in sorted(REGISTRY.values(), key=lambda x: (x.category, x.label))
    ]
