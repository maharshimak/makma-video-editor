"""Deterministic natural-language planner.

This is not a stand-in for the LLM planner -- it is the floor the product
stands on. It runs with no API key, costs nothing, is instant, and is fully
testable, so a meaningful set of editing instructions works offline. The LLM
planner handles everything past its reach and uses the same operation
vocabulary, so both produce plans the edit engine executes identically.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from ..schema.core import ProjectDocument, Sequence
from ..schema.operations import (
    CostEstimate,
    EditOperation,
    EditPlan,
    ExportRequest,
    OpType,
    Scope,
)

# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------

_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "fifteen": 15,
    "twenty": 20, "thirty": 30, "forty": 40, "forty-five": 45, "sixty": 60,
    "ninety": 90, "half": 0.5,
}

_TIME_TOKEN = r"(?:\d+:\d+(?:\.\d+)?|\d+(?:\.\d+)?\s*(?:seconds?|secs?|s\b)?)"


def parse_time(token: str) -> float | None:
    """Parse '12', '12.5', '1:23', '1:23.5', 'three seconds'."""
    if token is None:
        return None
    text = token.strip().lower()
    text = re.sub(r"\b(seconds?|secs?|s)\b", "", text).strip()
    if ":" in text:
        parts = text.split(":")
        try:
            values = [float(p) for p in parts]
        except ValueError:
            return None
        seconds = 0.0
        for value in values:
            seconds = seconds * 60 + value
        return seconds
    if text in _NUMBER_WORDS:
        return float(_NUMBER_WORDS[text])
    try:
        return float(text)
    except ValueError:
        return None


def find_duration(text: str, default: float | None = None) -> float | None:
    """First duration-ish quantity in a phrase."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:seconds?|secs?|s)\b", text)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+:\d+(?:\.\d+)?)", text)
    if m:
        return parse_time(m.group(1))
    for word, value in _NUMBER_WORDS.items():
        if re.search(rf"\b{re.escape(word)}\b\s+(?:seconds?|secs?)", text):
            return float(value)
    return default


def find_range(text: str) -> tuple[float, float] | None:
    """'between 3:42 and 4:07', 'from 5 to 9', '5s-9s'."""
    patterns = [
        rf"between\s+({_TIME_TOKEN})\s+(?:and|to|-)\s+({_TIME_TOKEN})",
        rf"from\s+({_TIME_TOKEN})\s+(?:to|until|-)\s+({_TIME_TOKEN})",
        rf"({_TIME_TOKEN})\s*(?:-|–|to)\s*({_TIME_TOKEN})\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            a, b = parse_time(m.group(1)), parse_time(m.group(2))
            if a is not None and b is not None and b > a:
                return a, b
    return None


def find_multiplier(text: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*x\b", text)
    if m:
        return float(m.group(1))
    m = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:times|percent|%)", text)
    if m:
        value = float(m.group(1))
        return value / 100 if "%" in m.group(0) or "percent" in m.group(0) else value
    return None


#: Phrases where "and"/"to" joins two halves of one value rather than two
#: clauses. Splitting inside these would destroy the range they express.
_PROTECTED = re.compile(
    rf"\bbetween\s+{_TIME_TOKEN}\s+and\s+{_TIME_TOKEN}"
    rf"|\bfrom\s+{_TIME_TOKEN}\s+to\s+{_TIME_TOKEN}"
    # Terms of art that happen to contain a joining word.
    r"|\bblack and white\b"
    r"|\bteal\s*(?:and|&|/)\s*orange\b"
    r"|\bhue and saturation\b"
    r"|\blift,?\s*gamma,?\s*(?:and\s+)?gain\b"
    r"|\bwear and tear\b"
    # An easing description, not two separate speed requests.
    r"|\bstarts?\s+(?:off\s+)?fast(?:er)?\s+(?:and|then)\s+slows?\s+down\b"
    r"|\bstarts?\s+(?:off\s+)?slow(?:ly|er)?\s+(?:and|then)\s+speeds?\s+up\b",
    re.IGNORECASE,
)

_CLAUSE_SPLIT = re.compile(r",|\band then\b|\bthen\b|\band\b|;|\.\s|\.$", re.IGNORECASE)


def split_clauses(command: str) -> list[str]:
    """Break a compound instruction into the separate things it asks for.

    Ranges such as "between 3:42 and 4:07" are protected first, so the "and"
    inside a value is never mistaken for a clause boundary.
    """
    placeholders: list[str] = []

    def _protect(m: re.Match[str]) -> str:
        placeholders.append(m.group(0))
        # A token with no split characters and no letters that a rule matches.
        return f"\x00{len(placeholders) - 1}\x00"

    protected = _PROTECTED.sub(_protect, command)

    clauses: list[str] = []
    for piece in _CLAUSE_SPLIT.split(protected):
        text = (piece or "").strip()
        if not text:
            continue
        for index, original in enumerate(placeholders):
            text = text.replace(f"\x00{index}\x00", original)
        clauses.append(text)
    return clauses or [command.strip()]


_QUOTED = re.compile(r'"[^"]{1,200}"|“[^”]{1,200}”')


def strip_quoted(text: str) -> str:
    """Blank out quoted spans, keeping offsets stable for other patterns."""
    return _QUOTED.sub(lambda m: " " * len(m.group(0)), text)


def find_quoted(text: str) -> str | None:
    for pattern in (r'"([^"]{1,200})"', r"'([^']{1,200})'", r"[“”]([^“”]{1,200})[“”]"):
        m = re.search(pattern, text)
        if m:
            return m.group(1).strip()
    return None


_TITLE_PHRASE = re.compile(
    r"\b(?:title|text|headline|caption)\s+"
    r"(?:that says\s+|saying\s+|reading\s+|of\s+)?"
    r"([A-Za-z0-9][A-Za-z0-9 '&!?.,-]{1,60})",
    re.IGNORECASE,
)


#: Two or more shouted words after a placement verb, e.g. "add NIGHT DRIVE".
_SHOUTED_TITLE = re.compile(
    r"\b(?:add|put|show|insert|display|overlay|write)\s+"
    r"((?:[A-Z0-9][A-Z0-9'&!?.-]*)(?:\s+[A-Z0-9][A-Z0-9'&!?.-]*)+)"
)

#: Shouted phrases that are instructions, not titles.
_NOT_TITLES = {"ai", "hd", "4k", "fps", "lut", "mp4", "bw", "vfx", "sfx"}


def find_title(text: str) -> tuple[str, int, int] | None:
    """The title a command asks for, plus where it sits in the string.

    Returning the span lets the planner blank it out before the other rules
    run, so a title reading NIGHT DRIVE cannot be mistaken for a request to
    grade the shot for night.
    """
    for pattern in (r'"([^"]{1,200})"', r"[“]([^”]{1,200})[”]"):
        m = re.search(pattern, text)
        if m:
            return m.group(1).strip(), m.start(), m.end()
    m = _TITLE_PHRASE.search(text)
    if m:
        content = m.group(1).strip(" .,")
        if content:
            return content, m.start(1), m.start(1) + len(content)

    # "add NIGHT DRIVE" -- no keyword, but a run of shouted words after a
    # placement verb is a title. Capitalisation is the signal, so this only
    # ever runs against the instruction as typed.
    m = _SHOUTED_TITLE.search(text)
    if m:
        content = m.group(1).strip(" .,")
        if content and content.lower() not in _NOT_TITLES:
            return content, m.start(1), m.start(1) + len(content)
    return None


# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

GRADE_WORDS: list[tuple[str, str]] = [
    (r"teal\s*(?:and|&|/)?\s*orange", "teal_orange"),
    (r"\bcyberpunk|neon future|blade ?runner\b", "cyberpunk"),
    (r"\bluxur(?:y|ious)|premium|expensive|high[- ]end\b", "luxury"),
    (r"\bnight\b|night ?time|after dark|moonlit", "night"),
    (r"\bgolden hour|sunset|sunrise|warm sunlight\b", "golden_hour"),
    (r"\bvintage|retro|old film|70s|80s\b", "vintage"),
    (r"\bblack and white|b\s*&\s*w|monochrome|greyscale|grayscale\b", "bw"),
    (r"\bhigh[- ]contrast|punchy|contrasty\b", "high_contrast"),
    (r"\bflat|log|ungraded|neutral grade\b", "flat"),
    (r"\bwarm(?:er)?\b", "warm"),
    (r"\bcool(?:er)?|colder|blue tone\b", "cool"),
    (r"\bcinematic|filmic|movie[- ]?like|film look|dark cinematic\b", "cinematic_dark"),
]

ASPECTS: list[tuple[str, str]] = [
    (r"\b9:16|vertical|portrait|tiktok|reel|shorts?\b", "9:16"),
    (r"\b1:1|square|instagram feed\b", "1:1"),
    (r"\b4:5\b", "4:5"),
    (r"\b2\.39|2\.35|anamorphic|scope\b", "2.39:1"),
    (r"\b21:9|ultrawide\b", "21:9"),
    (r"\b16:9|horizontal|landscape|widescreen|youtube\b", "16:9"),
]


@dataclass
class MediaRef:
    """The minimum the planner needs to talk about a file by name."""
    id: str
    name: str = ""
    kind: str = "video"       # video | audio | image | lut | font | other
    duration: float = 0.0


#: Words a creator uses for each kind of media.
KIND_WORDS: dict[str, str] = {
    "photo": "image", "photos": "image", "picture": "image", "pictures": "image",
    "image": "image", "images": "image", "still": "image", "stills": "image",
    "music": "audio", "song": "audio", "track": "audio", "soundtrack": "audio",
    "audio": "audio", "voiceover": "audio", "voice over": "audio",
    "clip": "video", "footage": "video", "video": "video", "shot": "video",
}


@dataclass
class PlanContext:
    """What the planner knows about the project it is editing."""
    document: ProjectDocument
    sequence: Sequence
    selection: list[str] = field(default_factory=list)
    playhead: float = 0.0
    #: asset_id -> analysis dict (scenes, silences, highlights)
    analyses: dict[str, dict] = field(default_factory=dict)
    asset_names: dict[str, str] = field(default_factory=dict)
    #: Everything in the media pool, used to resolve "Photo 4" or "this music".
    media: list[MediaRef] = field(default_factory=list)
    #: The instruction exactly as typed. Rules match against a lowercased
    #: copy, but anything the user will *see* -- a title, a caption -- has to
    #: come from here so their capitalisation survives.
    raw_command: str = ""

    def scope(self) -> Scope:
        """Default scope: the selection, or everything when nothing is selected."""
        if self.selection:
            return Scope(type="clip", clip_ids=list(self.selection))
        return Scope(type="all")

    def visual_scope(self) -> Scope:
        s = self.scope()
        s.kinds = ["video", "image"]
        return s

    def audio_scope(self) -> Scope:
        s = self.scope()
        s.kinds = ["video", "audio"]
        return s

    def animated_property(self) -> str | None:
        """A transform property already animated on the clips in scope.

        Re-timing language means easing only when there is a curve to re-time;
        with nothing animated the same words are about playback speed.
        """
        clips = []
        for clip_id in self.selection:
            hit = self.sequence.find_clip(clip_id)
            if hit:
                clips.append(hit[1])
        if not clips:
            clips = self.sequence.all_clips()

        for clip in clips:
            for name, curve in (clip.transform.keyframes or {}).items():
                if curve:
                    return name
        return None

    def beat_grid(self) -> dict | None:
        """The beat grid of the music on this timeline, in timeline seconds.

        Beats are detected against the source file, so they are shifted by
        wherever that music actually sits in the sequence. Without that a
        track starting at 4s would put every beat four seconds early.
        """
        best: dict | None = None
        for track in self.sequence.tracks:
            for clip in track.clips:
                analysis = self.analyses.get(clip.asset_id or "")
                grid = (analysis or {}).get("beats")
                if not grid or not grid.get("beats"):
                    continue
                if float(grid.get("confidence", 0)) < 0.35:
                    continue
                shifted = [
                    clip.start + (beat - clip.in_point) / max(clip.speed, 1e-6)
                    for beat in grid["beats"]
                ]
                inside = [b for b in shifted if clip.start - 0.01 <= b <= clip.end]
                if len(inside) < 4:
                    continue
                candidate = {**grid, "beats": inside, "clip_id": clip.id}
                # Prefer the most confident grid when several tracks have one.
                if best is None or candidate["confidence"] > best["confidence"]:
                    best = candidate
        return best

    def media_of(self, kind: str) -> list[MediaRef]:
        return [m for m in self.media if m.kind == kind]

    def resolve_media(self, text: str, kind: str | None = None) -> MediaRef | None:
        """Find the asset an instruction is pointing at.

        Tries, in order: an explicit name from the media pool ("drive.mp4"),
        a name plus index ("Photo 4"), and finally the only asset of the kind
        being talked about ("this music"). Returns None rather than guessing
        between several equally plausible files.
        """
        pool = [m for m in self.media if kind is None or m.kind == kind]
        if not pool:
            return None

        # 1. Longest matching filename or stem wins, so "car_video.mp4" beats
        #    a stray match on "car".
        best: MediaRef | None = None
        best_len = 0
        for ref in pool:
            for candidate in {ref.name.lower(), ref.name.lower().rsplit(".", 1)[0]}:
                if len(candidate) >= 3 and candidate in text and len(candidate) > best_len:
                    best, best_len = ref, len(candidate)
        if best is not None:
            return best

        # 2. "Photo 4", "clip 2" -- an ordinal within that kind.
        for word, word_kind in KIND_WORDS.items():
            m = re.search(rf"\b{re.escape(word)}\s*#?(\d+)\b", text)
            if m and (kind is None or word_kind == kind):
                same = [r for r in pool if r.kind == word_kind]
                index = int(m.group(1)) - 1
                if 0 <= index < len(same):
                    return same[index]

        # 3. "this photo", "the music" -- unambiguous only if there is one.
        if kind is not None:
            same = [r for r in pool if r.kind == kind]
            if len(same) == 1:
                return same[0]
        return None

    def referenced_kind(self, text: str) -> str | None:
        """Which kind of media an instruction is talking about, if any."""
        for word, kind in KIND_WORDS.items():
            if re.search(rf"\b{re.escape(word)}\b", text):
                return kind
        return None


Rule = Callable[[str, PlanContext], list[EditOperation]]
RULES: list[tuple[str, Rule]] = []


def rule(name: str):
    def wrap(fn: Rule) -> Rule:
        RULES.append((name, fn))
        return fn
    return wrap


def op(op_type: OpType, description: str, scope: Scope | None = None,
       **parameters) -> EditOperation:
    return EditOperation(
        type=op_type, scope=scope or Scope(), parameters=parameters,
        description=description,
    )


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------

@rule("grade")
def _grade(text: str, ctx: PlanContext) -> list[EditOperation]:
    for pattern, preset in GRADE_WORDS:
        if re.search(pattern, text):
            intensity = 1.0
            if re.search(r"\bsubtl[ey]|slight(?:ly)?|gentl[ey]|barely|"
                         r"a (?:little|bit|touch)\b", text):
                intensity = 0.5
            elif re.search(r"\bheav(?:y|ily)|strong(?:ly)?|extreme(?:ly)?|"
                           r"much more|way more|really\b", text):
                intensity = 1.0
            label = preset.replace("_", " ")
            return [
                op(OpType.ADD_EFFECT, f"Apply the {label} grade", ctx.visual_scope(),
                   type="color_grade", parameters={"preset": preset,
                                                   "intensity": intensity})
            ]
    return []


@rule("cinematic_package")
def _cinematic_package(text: str, ctx: PlanContext) -> list[EditOperation]:
    """'Make it cinematic' means more than a LUT: bars, grain, a gentle push."""
    if not re.search(r"\bcinematic|filmic|film look|like a (?:movie|film)\b", text):
        return []
    ops = [
        op(OpType.ADD_EFFECT, "Letterbox to 2.39:1", ctx.visual_scope(),
           type="letterbox", parameters={"aspect_ratio": "2.39:1"}),
        op(OpType.ADD_EFFECT, "Add subtle film grain", ctx.visual_scope(),
           type="film_grain", parameters={"intensity": 0.16}),
        op(OpType.ADD_EFFECT, "Add a soft vignette", ctx.visual_scope(),
           type="vignette", parameters={"intensity": 0.32}),
    ]
    return ops


@rule("grain")
def _grain(text: str, ctx: PlanContext) -> list[EditOperation]:
    if not re.search(r"\b(?:film )?grain|noise texture|16 ?mm|35 ?mm\b", text):
        return []
    if re.search(r"\bcinematic\b", text):
        return []  # already covered by the cinematic package
    intensity = 0.3 if re.search(r"\bheav|strong|lots\b", text) else 0.16
    return [op(OpType.ADD_EFFECT, "Add film grain", ctx.visual_scope(),
               type="film_grain", parameters={"intensity": intensity})]


@rule("vignette")
def _vignette(text: str, ctx: PlanContext) -> list[EditOperation]:
    if not re.search(r"\bvignette|darken(?:ed)? (?:the )?(?:edges|corners)\b", text):
        return []
    return [op(OpType.ADD_EFFECT, "Add a vignette", ctx.visual_scope(),
               type="vignette", parameters={"intensity": 0.45})]


@rule("letterbox")
def _letterbox(text: str, ctx: PlanContext) -> list[EditOperation]:
    if not re.search(r"\bletterbox|black bars|cinemascope|anamorphic bars\b", text):
        return []
    ratio = "2.39:1"
    for pattern, value in ASPECTS:
        if re.search(pattern, text) and value in ("2.39:1", "21:9"):
            ratio = value
    return [op(OpType.ADD_EFFECT, f"Letterbox to {ratio}", ctx.visual_scope(),
               type="letterbox", parameters={"aspect_ratio": ratio})]


@rule("exposure")
def _exposure(text: str, ctx: PlanContext) -> list[EditOperation]:
    if re.search(r"\b(?:make it |too )?(?:brighter|brighten|lighter)\b", text):
        return [op(OpType.ADD_EFFECT, "Raise exposure", ctx.visual_scope(),
                   type="exposure", parameters={"stops": 0.5})]
    if re.search(r"\b(?:make it |too )?(?:darker|darken)\b", text) and \
            not re.search(r"\bedges|corners|vignette\b", text):
        return [op(OpType.ADD_EFFECT, "Lower exposure", ctx.visual_scope(),
                   type="exposure", parameters={"stops": -0.5})]
    if re.search(r"\b(?:increase|raise|more|add|boost|up|higher) (?:the )?contrast\b"
                 r"|\bmore contrasty\b", text):
        return [op(OpType.ADD_EFFECT, "Increase contrast", ctx.visual_scope(),
                   type="color", parameters={"contrast": 1.25})]
    if re.search(r"\b(?:reduce|lower|less|decrease|drop) (?:the )?contrast\b"
                 r"|\bflatter\b", text):
        return [op(OpType.ADD_EFFECT, "Reduce contrast", ctx.visual_scope(),
                   type="color", parameters={"contrast": 0.82})]
    if re.search(r"\bmore saturat|more colou?rful|punchier colou?r\b", text):
        return [op(OpType.ADD_EFFECT, "Increase saturation", ctx.visual_scope(),
                   type="color", parameters={"saturation": 1.25})]
    if re.search(r"\bdesaturat|less colou?rful|muted colou?rs?\b", text):
        return [op(OpType.ADD_EFFECT, "Reduce saturation", ctx.visual_scope(),
                   type="color", parameters={"saturation": 0.7})]
    return []


@rule("image_ops")
def _image_ops(text: str, ctx: PlanContext) -> list[EditOperation]:
    ops = []
    if re.search(r"\bblur(?:ry|red)?\b", text) and not re.search(r"\bbackground\b", text):
        ops.append(op(OpType.ADD_EFFECT, "Blur the picture", ctx.visual_scope(),
                      type="blur", parameters={"sigma": 8.0}))
    if re.search(r"\bsharpen|sharper|crisper\b", text):
        ops.append(op(OpType.ADD_EFFECT, "Sharpen", ctx.visual_scope(),
                      type="sharpen", parameters={"amount": 1.0}))
    if re.search(r"\bstabili[sz]e|smoother camera|shaky\b", text):
        ops.append(op(OpType.ADD_EFFECT, "Stabilise the shot", ctx.visual_scope(),
                      type="stabilize", parameters={"range": 20}))
    if re.search(r"\bdenoise|remove (?:video |image )?noise|grainy footage\b", text):
        ops.append(op(OpType.ADD_EFFECT, "Denoise", ctx.visual_scope(),
                      type="denoise", parameters={"strength": 0.5}))
    if re.search(r"\bcamera ?shake|handheld feel|shaky ?cam\b", text):
        amount = 12.0 if re.search(r"\bheav|strong|aggressive\b", text) else 6.0
        ops.append(op(OpType.ADD_EFFECT, "Add camera shake", ctx.visual_scope(),
                      type="camera_shake", parameters={"amount": amount}))
    if re.search(r"\bpush(?:es|ing)? ?in\b"
                 r"|\b(?:zoom|push)(?:es|ing)?\s+(?:slowly\s+|gently\s+|slow\s+)?"
                 r"(?:in|into|towards?|onto|on|at)\b"
                 r"|\bslow zoom\b|\bken burns\b", text):
        ops.append(op(OpType.ADD_EFFECT, "Slow push in", ctx.visual_scope(),
                      type="zoom", parameters={"start": 1.0, "end": 1.12}))
    if re.search(r"\bglow|bloom|dreamy light\b", text):
        ops.append(op(OpType.ADD_EFFECT, "Add glow", ctx.visual_scope(),
                      type="glow", parameters={"intensity": 0.35}))
    if re.search(r"\bmirror|flip (?:it )?horizontal", text):
        ops.append(op(OpType.ADD_EFFECT, "Mirror horizontally", ctx.visual_scope(),
                      type="mirror", parameters={"axis": "horizontal"}))
    return ops


@rule("speed")
def _speed(text: str, ctx: PlanContext) -> list[EditOperation]:
    scope = ctx.scope()
    rng = find_range(text)
    if rng:
        scope = Scope(type="range", start=rng[0], end=rng[1])

    if re.search(r"\bzoom|push ?in|ken burns\b", text):
        # "zoom in slowly" describes a camera move, not the playback rate.
        return []

    mult = find_multiplier(text)
    if re.search(r"\bslow(?:ed|er)?(?: (?:it|this|that|them) )?(?: down)?|slow[- ]?mo(?:tion)?\b", text):
        speed = 1 / mult if (mult and mult > 1) else (mult or 0.5)
        speed = min(1.0, max(0.05, speed))
        return [op(OpType.SET_SPEED, f"Slow to {speed:.2f}x", scope, speed=speed)]
    if re.search(r"\bspeed (?:it |this |that )?up|faster|time ?lapse|hyperlapse\b", text):
        speed = mult if (mult and mult > 1) else 2.0
        return [op(OpType.SET_SPEED, f"Speed up to {speed:.2f}x", scope, speed=speed)]
    if re.search(r"\breverse[sd]?\b|\bbackwards?\b", text):
        return [op(OpType.SET_SPEED, "Reverse", scope, speed=1.0, reverse=True)]
    if mult and re.search(r"\bspeed\b", text):
        return [op(OpType.SET_SPEED, f"Set speed to {mult:.2f}x", scope, speed=mult)]
    return []


@rule("cut_head_tail")
def _cut_head_tail(text: str, ctx: PlanContext) -> list[EditOperation]:
    ops: list[EditOperation] = []
    m = re.search(
        r"(?:cut|trim|remove|drop|delete|lose)\s+(?:the\s+)?(?:boring\s+|first\s+)"
        rf"(?:{_TIME_TOKEN})?", text
    )
    if re.search(r"(?:cut|trim|remove|drop|delete|lose)[^.]*\bfirst\b", text):
        seconds = find_duration(text, 3.0)
        if seconds:
            ops.append(
                op(OpType.REMOVE_RANGE, f"Remove the first {seconds:g}s",
                   Scope(type="all"), start=0.0, end=seconds, ripple=True)
            )
    if re.search(r"(?:cut|trim|remove|drop|delete|lose)[^.]*\b(?:last|end|ending)\b", text):
        seconds = find_duration(text, 3.0)
        if seconds:
            total = ctx.sequence.duration
            ops.append(
                op(OpType.REMOVE_RANGE, f"Remove the last {seconds:g}s",
                   Scope(type="all"), start=max(0.0, total - seconds), end=total,
                   ripple=False)
            )
    return ops


@rule("remove_range")
def _remove_range(text: str, ctx: PlanContext) -> list[EditOperation]:
    if not re.search(r"\b(?:cut|remove|delete|drop)\b", text):
        return []
    if re.search(r"\bfirst|last\b", text):
        return []   # handled by cut_head_tail
    rng = find_range(text)
    if not rng:
        return []
    return [op(OpType.REMOVE_RANGE, f"Ripple-delete {rng[0]:g}s to {rng[1]:g}s",
               Scope(type="all"), start=rng[0], end=rng[1], ripple=True)]


@rule("remove_silence")
def _remove_silence(text: str, ctx: PlanContext) -> list[EditOperation]:
    """'Remove the pauses' -- uses the ingest silence index, not guesswork."""
    if not re.search(
        r"\b(?:remove|cut|kill|delete|trim)\b[^.]*\b(?:pauses?|silences?|dead air|"
        r"gaps?|boring (?:parts?|bits?|sections?))\b", text
    ):
        return []

    silences: list[tuple[float, float]] = []
    for track in ctx.sequence.tracks:
        for clip in track.clips:
            analysis = ctx.analyses.get(clip.asset_id or "")
            if not analysis:
                continue
            for entry in analysis.get("silences", []):
                # Map source time onto timeline time for this clip.
                s = clip.start + (entry["start"] - clip.in_point) / max(clip.speed, 1e-6)
                e = clip.start + (entry["end"] - clip.in_point) / max(clip.speed, 1e-6)
                s, e = max(s, clip.start), min(e, clip.end)
                if e - s >= 0.5:
                    silences.append((s, e))

    if not silences:
        return []
    # Remove late-to-early so earlier positions stay valid as the ripple closes.
    silences.sort(reverse=True)
    ops = []
    for s, e in silences[:60]:
        ops.append(
            op(OpType.REMOVE_RANGE, f"Remove silence {s:.1f}s-{e:.1f}s",
               Scope(type="all"), start=round(s, 3), end=round(e, 3), ripple=True)
        )
    return ops


@rule("split")
def _split(text: str, ctx: PlanContext) -> list[EditOperation]:
    if not re.search(r"\b(?:split|cut|slice)\b[^.]*\b(?:here|at|playhead)\b", text):
        return []
    at = ctx.playhead
    m = re.search(rf"\bat\s+({_TIME_TOKEN})", text)
    if m:
        parsed = parse_time(m.group(1))
        if parsed is not None:
            at = parsed
    return [op(OpType.SPLIT_CLIP, f"Split at {at:.2f}s", ctx.scope(), time=at)]


@rule("transitions")
def _transitions(text: str, ctx: PlanContext) -> list[EditOperation]:
    ops: list[EditOperation] = []
    duration = find_duration(text, None)
    if re.search(r"\bfades? (?:it |the \w+ )?in\b|\bfades? up\b", text):
        ops.append(op(OpType.SET_TRANSITION, "Fade in",
                      _kind_scope(ctx, text, ctx.scope()),
                      transition="fade", duration=duration or 1.0, where="in"))
    if re.search(r"\bfades? (?:it |the \w+ )?out\b|\bfade to black\b", text):
        ops.append(op(OpType.SET_TRANSITION, "Fade out",
                      _kind_scope(ctx, text, ctx.scope()),
                      transition="fade", duration=duration or 1.5, where="out"))
    if re.search(r"\b(?:cross ?fades?|dissolves?|smooth transitions?|"
                 r"blend the cuts)\b", text):
        ops.append(op(OpType.SET_TRANSITION, "Cross-dissolve between clips",
                      ctx.visual_scope(), transition="dissolve",
                      duration=duration or 0.5, where="both"))
    if re.search(r"\bslide transitions?|push transitions?|slide in\b", text):
        ops.append(op(OpType.SET_TRANSITION, "Slide transition", ctx.visual_scope(),
                      transition="slide_left", duration=duration or 0.4, where="in"))
    if re.search(r"\bwipes?\b", text):
        ops.append(op(OpType.SET_TRANSITION, "Wipe transition", ctx.visual_scope(),
                      transition="wipe_left", duration=duration or 0.4, where="in"))
    if re.search(r"\bhard cuts?|no transitions?|remove transitions?\b", text):
        ops.append(op(OpType.SET_TRANSITION, "Remove transitions", ctx.scope(),
                      transition="none", duration=0.0, where="both"))
    return ops


@rule("text")
def _text(text: str, ctx: PlanContext) -> list[EditOperation]:
    source = ctx.raw_command or text
    found = find_title(source)
    if not found:
        return []

    # Either the clause names what it is adding ("add the title …"), or the
    # title announced itself by being shouted or quoted ("add NIGHT DRIVE").
    named = re.search(
        r"\b(?:add|put|overlay|show|insert|display|write)\b[^.]*"
        r"\b(?:title|text|caption|headline|words?|lower third)\b", text
    )
    if not named and not re.search(r"\b(?:add|put|overlay|show|insert|display|write)\b",
                                   text):
        return []
    content = found[0]

    start = ctx.playhead
    m = re.search(rf"\bat\s+({_TIME_TOKEN})", text)
    if m:
        parsed = parse_time(m.group(1))
        if parsed is not None:
            start = parsed
    duration = find_duration(text, 3.0) or 3.0

    position = "center"
    if re.search(r"\bbottom|lower third|below\b", text):
        position = "bottom"
    elif re.search(r"\btop|above|upper\b", text):
        position = "top"

    return [
        op(OpType.ADD_TEXT, f'Add the title "{content}"', Scope(type="sequence"),
           text=content, start=round(start, 3), duration=duration,
           position=position, uppercase=bool(re.search(r"\bupper ?case|all caps\b", text)),
           font_size=84, animation="fade")
    ]


def _kind_scope(ctx: PlanContext, text: str, fallback: Scope) -> Scope:
    """Narrow a scope to the kind of media the sentence names.

    "Fade the music out" must not fade the picture as well, so naming a kind
    restricts the operation to clips of that kind.
    """
    kind = ctx.referenced_kind(text)
    if kind is None:
        return fallback
    kinds = ["audio", "video"] if kind == "audio" else [kind]
    if kind == "audio" and re.search(r"\b(?:music|song|soundtrack)\b", text):
        # Music sits on its own clips; leave the dialogue on the footage alone.
        kinds = ["audio"]
    return Scope(type=fallback.type, clip_ids=fallback.clip_ids, kinds=kinds)


@rule("audio")
def _audio(text: str, ctx: PlanContext) -> list[EditOperation]:
    ops: list[EditOperation] = []
    scope = ctx.audio_scope()

    if re.search(r"\bmute\b", text):
        ops.append(op(OpType.SET_VOLUME, "Mute", scope, muted=True))
    elif re.search(r"\bunmute\b", text):
        ops.append(op(OpType.SET_VOLUME, "Unmute", scope, muted=False))

    # "lower the music to 20%" is an absolute level, not a relative nudge.
    level_match = re.search(
        r"\b(?:lower|reduce|set|turn|bring|drop|put)\s+(?:the\s+)?"
        r"(?:music|audio|sound|volume|track)?\s*(?:down\s+)?(?:to|at)\s+"
        r"(\d+(?:\.\d+)?)\s*(?:%|percent)", text
    )
    if level_match:
        level = max(0.0, min(4.0, float(level_match.group(1)) / 100))
        ops.append(op(OpType.SET_VOLUME, f"Set the level to {level_match.group(1)}%",
                      _kind_scope(ctx, text, scope), volume=level))
    elif re.search(r"\b(?:turn|make|bring) (?:it |the (?:music|audio|sound) )?"
                   r"(?:down|quieter|softer)|lower the (?:music|audio|volume)\b", text):
        ops.append(op(OpType.SET_VOLUME, "Lower the level",
                      _kind_scope(ctx, text, scope), gain_db=-6.0))
    elif re.search(r"\b(?:turn|make) (?:it |the (?:music|audio|sound) )?"
                   r"(?:up|louder)|raise the (?:volume|music)\b", text):
        ops.append(op(OpType.SET_VOLUME, "Raise the level",
                      _kind_scope(ctx, text, scope), gain_db=4.0))

    if re.search(r"\bclean (?:up )?(?:my |the )?(?:voice|audio|vocals?)|"
                 r"enhance (?:my )?voice|voice sounds? bad\b", text):
        ops.append(op(OpType.ADD_EFFECT, "Enhance the voice", scope,
                      type="a_voice", parameters={"strength": 0.75}))
    if re.search(r"\b(?:remove|reduce|kill) (?:the )?(?:background )?"
                 r"(?:noise|hiss|hum|静)\b", text):
        ops.append(op(OpType.ADD_EFFECT, "Reduce noise", scope,
                      type="a_denoise", parameters={"strength": 0.55}))
    if re.search(r"\bnormali[sz]e|consistent (?:volume|loudness)|match the levels\b", text):
        ops.append(op(OpType.ADD_EFFECT, "Normalise loudness", scope,
                      type="a_normalize", parameters={"lufs": -14.0}))
    if re.search(r"\bcompress(?:or|ion)?\b", text) and re.search(r"\baudio|voice|sound\b", text):
        ops.append(op(OpType.ADD_EFFECT, "Compress", scope, type="a_compress"))
    return ops


@rule("animate")
def _animate(text: str, ctx: PlanContext) -> list[EditOperation]:
    """Turn movement language into real keyframes.

    Only properties that survive the render are offered: a drift across frame
    animates position, while a zoom goes through the Push In effect because
    FFmpeg cannot animate a layer's size.
    """
    ops: list[EditOperation] = []
    scope = ctx.visual_scope()

    # Re-timing language ("start fast and slow down") only means easing when
    # something is already animated. With no curve to re-time, the same words
    # are about playback speed, and the speed rule should have them.
    animated = ctx.animated_property()
    if animated is not None:
        if re.search(r"\b(?:make|keep)\b[^.]*\b(?:movement|motion|animation|move)\b"
                     r"[^.]*\bsmooth(?:er)?\b|\bsmooth(?:er)? (?:movement|motion)\b"
                     r"|\bease in and out\b", text):
            return [op(OpType.SET_KEYFRAMES, "Ease the existing movement", scope,
                       property=animated, easing="ease_in_out")]
        if re.search(r"\bstarts?\s+(?:off\s+)?fast(?:er)?[^.]*slows?\s+down\b"
                     r"|\bease out\b", text):
            return [op(OpType.SET_KEYFRAMES, "Start fast and settle", scope,
                       property=animated, easing="ease_out")]
        if re.search(r"\bstarts?\s+(?:off\s+)?slow(?:ly|er)?[^.]*speeds?\s+up\b"
                     r"|\bease in\b", text):
            return [op(OpType.SET_KEYFRAMES, "Start slow and accelerate", scope,
                       property=animated, easing="ease_in")]

    # "drift across the frame", "slide it to the left over 3 seconds"
    direction = None
    if re.search(r"\b(?:drift|glide|pan|slide|move|travel|float)\b", text):
        if re.search(r"\bleft\b", text):
            direction = -1
        elif re.search(r"\bright\b", text):
            direction = 1
        elif re.search(r"\bacross\b", text):
            direction = 1
    if direction is not None:
        distance = 160.0 * direction
        if re.search(r"\bsubtle|slight|gentle|barely\b", text):
            distance *= 0.4
        elif re.search(r"\bfar|big|large|dramatic|all the way\b", text):
            distance *= 2.5
        ops.append(
            op(OpType.SET_KEYFRAMES,
               f"Drift {'right' if direction > 0 else 'left'} across the shot",
               scope,
               property="x", relative=True,
               keyframes=[
                   {"time": 0.0, "value": -distance / 2, "easing": "ease_in_out"},
                   {"time": 1.0, "value": distance / 2, "easing": "ease_in_out"},
               ])
        )

    # "fade the logo in" on a layer above the picture is an opacity ramp; a
    # fade on the bottom layer is a transition, which the transitions rule
    # already handles.
    if re.search(r"\bfade (?:the )?(?:logo|graphic|overlay|title|image|photo) in\b",
                 text):
        ops.append(
            op(OpType.SET_KEYFRAMES, "Fade it up", scope,
               property="opacity", relative=True,
               keyframes=[
                   {"time": 0.0, "value": 0.0, "easing": "ease_out"},
                   {"time": 0.25, "value": 1.0, "easing": "linear"},
               ])
        )
    return ops


@rule("place_media")
def _place_media(text: str, ctx: PlanContext) -> list[EditOperation]:
    """'Put Photo 4 after this shot', 'add this music', 'use the logo here'."""
    if not re.search(r"\b(?:put|place|add|insert|drop|bring in|use)\b", text):
        return []
    if re.search(r"\b(?:title|text|caption|subtitle|effect|grade|vignette|"
                 r"grain|marker|transition|blur|glow)\b", text):
        return []   # those have their own rules

    kind = ctx.referenced_kind(text)
    ref = ctx.resolve_media(text, kind)
    if ref is None:
        return []

    # Where it lands: after the selection, at the playhead, or appended.
    start: float | str = "append"
    if re.search(r"\bafter (?:this|that|the) (?:shot|clip|one)\b", text):
        anchor = None
        for cid in ctx.selection:
            hit = ctx.sequence.find_clip(cid)
            if hit:
                anchor = max(anchor or 0.0, hit[1].end)
        start = anchor if anchor is not None else "append"
    elif re.search(r"\b(?:here|at the playhead|at this point|right here)\b", text):
        start = round(ctx.playhead, 3)
    elif re.search(r"\bat the (?:start|beginning)\b", text):
        start = 0.0

    duration = find_duration(text, None)
    if duration is None:
        duration = 5.0 if ref.kind == "image" else (ref.duration or 5.0)

    params: dict = {"asset_id": ref.id, "start": start, "duration": duration}
    if ref.kind == "image":
        # Stills belong above the footage they cut over, not under it.
        params["track_index"] = 1

    where = "at the end" if start == "append" else f"at {float(start):.2f}s"
    return [
        op(OpType.ADD_CLIP, f"Place {ref.name or ref.kind} {where}",
           Scope(type="sequence"), **params)
    ]


@rule("hold_duration")
def _hold_duration(text: str, ctx: PlanContext) -> list[EditOperation]:
    """'Keep this photo for 3 seconds', 'make the still 5 seconds long'."""
    if not re.search(
        r"\b(?:keep|hold|show|display|make|set|leave)\b[^.]*"
        r"\b(?:for|to|at)\b|\b(?:seconds?|secs?) long\b", text
    ):
        return []
    seconds = find_duration(text, None)
    if seconds is None or seconds <= 0:
        return []

    kind = ctx.referenced_kind(text)
    scope = ctx.scope()
    if kind:
        scope = Scope(type=scope.type, clip_ids=scope.clip_ids, kinds=[kind])

    noun = kind or "clip"
    return [op(OpType.SET_DURATION, f"Hold the {noun} for {seconds:g}s", scope,
               duration=seconds, ripple=True)]


#: Words a creator uses for an export target -> render preset key.
#: Exports are collected in `plan()` rather than by a rule, because they do
#: not produce timeline operations.
EXPORT_TARGETS: list[tuple[str, str]] = [
    (r"\b4k|2160p|uhd\b", "master_4k"),
    (r"\btiktok\b", "tiktok"),
    (r"\breels?\b|\binstagram reel\b", "reel"),
    (r"\binstagram (?:feed|post)\b|\bsquare\b", "instagram_feed"),
    (r"\blinkedin\b", "linkedin"),
    (r"\byoutube\b", "youtube_1080p"),
    (r"\bx\b|\btwitter\b|\b720p?\b", "x_720p"),
    (r"\bdraft|quick (?:render|look)|preview render\b", "draft_540p"),
    (r"\b1080p?\b|\bfull ?hd\b|\bmaster\b", "master_1080p"),
]


def find_exports(text: str) -> list[ExportRequest]:
    """Pick out render requests such as 'export 1080p' or 'render for TikTok'."""
    if not re.search(r"\b(?:export|render|download|save)\b", text):
        return []
    # "export the audio", "render this text" are not render requests.
    if re.search(r"\bexport (?:the )?(?:captions?|subtitles?|srt|xml|edl|stems?)\b",
                 text):
        return []

    requests: list[ExportRequest] = []
    for pattern, preset in EXPORT_TARGETS:
        if re.search(pattern, text):
            requests.append(
                ExportRequest(
                    preset=preset,
                    quality="draft" if preset == "draft_540p" else "final",
                    label=preset.replace("_", " "),
                )
            )
    if not requests:
        requests.append(
            ExportRequest(preset="master_1080p", label="master 1080p")
        )
    # De-duplicate while keeping the order the user asked in.
    seen: set[str] = set()
    unique: list[ExportRequest] = []
    for request in requests:
        if request.preset in seen:
            continue
        seen.add(request.preset or "")
        unique.append(request)
    return unique


@rule("format")
def _format(text: str, ctx: PlanContext) -> list[EditOperation]:
    if not re.search(r"\b(?:make|convert|turn|reframe|resize|crop) (?:it |this |the video )?"
                     r"(?:into |to |for )?|version for|aspect ratio\b", text):
        return []
    for pattern, ratio in ASPECTS:
        if re.search(pattern, text):
            ops = [op(OpType.SET_FORMAT, f"Set the canvas to {ratio}",
                      Scope(type="sequence"), aspect_ratio=ratio)]
            # A reframe that letterboxes everything is not a reframe; fill it.
            ops.append(op(OpType.SET_TRANSFORM, "Fill the new frame",
                          ctx.visual_scope(), fit="cover"))
            return ops
    return []


@rule("highlight_reel")
def _highlight_reel(text: str, ctx: PlanContext) -> list[EditOperation]:
    """Build a cut from the highest-scoring scenes found during ingestion."""
    if not re.search(
        r"\b(?:highlight|best (?:bits|parts|moments|takes)|trailer|montage|"
        r"supercut|sizzle)\b", text
    ):
        return []
    target = find_duration(text, 30.0) or 30.0

    candidates: list[tuple[float, str, float, float]] = []
    for track in ctx.sequence.tracks:
        for clip in track.clips:
            analysis = ctx.analyses.get(clip.asset_id or "")
            for hl in (analysis or {}).get("highlights", []):
                s = clip.start + (hl["start"] - clip.in_point) / max(clip.speed, 1e-6)
                e = clip.start + (hl["end"] - clip.in_point) / max(clip.speed, 1e-6)
                s, e = max(s, clip.start), min(e, clip.end)
                if e - s >= 0.8:
                    candidates.append((hl["score"], clip.id, s, e))
    if not candidates:
        return []

    candidates.sort(reverse=True)
    chosen: list[tuple[float, float]] = []
    total = 0.0
    for _score, _cid, s, e in candidates:
        span = min(e - s, 4.0)
        if total + span > target:
            continue
        if any(not (e <= cs or s >= ce) for cs, ce in chosen):
            continue      # overlaps something already picked
        chosen.append((s, s + span))
        total += span
        if total >= target * 0.95:
            break
    if not chosen:
        return []

    chosen.sort()
    # Keep only the chosen windows: ripple out everything between them.
    gaps: list[tuple[float, float]] = []
    cursor = 0.0
    for s, e in chosen:
        if s - cursor > 0.05:
            gaps.append((cursor, s))
        cursor = e
    if ctx.sequence.duration - cursor > 0.05:
        gaps.append((cursor, ctx.sequence.duration))

    ops = []
    for s, e in reversed(gaps):
        ops.append(
            op(OpType.REMOVE_RANGE, f"Drop {s:.1f}s-{e:.1f}s (low scoring)",
               Scope(type="all"), start=round(s, 3), end=round(e, 3), ripple=True)
        )
    ops.append(
        op(OpType.SET_TRANSITION,
           f"Cross-dissolve the {len(chosen)} selected moments",
           Scope(type="all", kinds=["video", "image"]),
           transition="dissolve", duration=0.3, where="both")
    )
    return ops


@rule("beats")
def _beats(text: str, ctx: PlanContext) -> list[EditOperation]:
    """Music-synced editing, driven by the beat grid found during ingestion."""
    wants_markers = bool(re.search(
        r"\b(?:show|add|mark)\b[^.]*\bbeats?\b|\bbeat (?:grid|markers?)\b", text
    ))
    wants_cut = bool(re.search(
        r"\bcut (?:to|on|with) the (?:beat|music|rhythm)\b"
        r"|\bsync(?:hronise|hronize|ed)?\b[^.]*\b(?:beat|music|rhythm|track)\b"
        r"|\bon the beat\b|\bto the music\b", text
    ))
    if not (wants_markers or wants_cut):
        return []

    grid = ctx.beat_grid()
    if grid is None:
        return []

    ops: list[EditOperation] = []
    beats = grid["beats"]

    if wants_markers:
        ops.append(
            op(OpType.ADD_MARKER,
               f"Mark {len(beats)} beats at {grid['bpm']:.0f} BPM",
               Scope(type="sequence"),
               times=[round(b, 3) for b in beats], kind="beat", name="Beat",
               color="#3f7fd4")
        )

    if wants_cut:
        # Every cut already on the timeline moves to the nearest beat. Nudging
        # existing edits is what "cut to the beat" means once a cut exists;
        # inventing new cuts would be a different instruction.
        moves: list[EditOperation] = []
        for track in ctx.sequence.video_tracks():
            for clip in sorted(track.clips, key=lambda c: c.start):
                if clip.start <= 0.001:
                    continue
                nearest = min(beats, key=lambda b: abs(b - clip.start))
                shift = nearest - clip.start
                # Only pull a cut that was clearly aiming at this beat. Past
                # about a third of a beat the two neighbouring beats have an
                # equal claim, and moving it would be a guess rather than a
                # correction.
                period = 60.0 / max(grid["bpm"], 1.0)
                if abs(shift) > period * 0.35 or abs(shift) < 0.008:
                    continue
                moves.append(
                    op(OpType.MOVE_CLIP,
                       f"Snap '{clip.name or clip.kind}' to the beat at "
                       f"{nearest:.2f}s",
                       Scope(type="clip", clip_ids=[clip.id]),
                       start=round(nearest, 4))
                )
        if not moves:
            ops.append(
                op(OpType.ADD_MARKER,
                   f"Mark the beat grid at {grid['bpm']:.0f} BPM "
                   "(no cuts were off-beat)",
                   Scope(type="sequence"),
                   times=[round(b, 3) for b in beats], kind="beat", name="Beat",
                   color="#3f7fd4")
            )
        ops.extend(moves)
    return ops


@rule("markers_from_scenes")
def _markers(text: str, ctx: PlanContext) -> list[EditOperation]:
    if not re.search(r"\bmark(?:er)?s? (?:the |every |each )?(?:scenes?|cuts?|shots?)|"
                     r"add markers\b", text):
        return []
    times = []
    for track in ctx.sequence.tracks:
        for clip in track.clips:
            for scene in (ctx.analyses.get(clip.asset_id or "") or {}).get("scenes", []):
                t = clip.start + (scene["start"] - clip.in_point) / max(clip.speed, 1e-6)
                if clip.start <= t <= clip.end:
                    times.append(round(t, 3))
    if not times:
        return []
    return [op(OpType.ADD_MARKER, f"Mark {len(times)} shot changes",
               Scope(type="sequence"), times=sorted(set(times)), kind="scene",
               name="Shot")]


@rule("vertical_version")
def _vertical_version(text: str, ctx: PlanContext) -> list[EditOperation]:
    if not re.search(r"\b(?:give me|make|create|generate) (?:a |an )?"
                     r"(?:vertical|tiktok|reel|shorts?|instagram) version\b", text):
        return []
    return [
        op(OpType.CREATE_SEQUENCE, "Create a 9:16 sequence from this edit",
           Scope(type="sequence"), name="Vertical", width=1080, height=1920,
           copy_from_active=True, activate=True)
    ]


# --------------------------------------------------------------------------
# Planner
# --------------------------------------------------------------------------

def _estimate(ops: list[EditOperation]) -> CostEstimate:
    heavy = {"stabilize", "denoise", "glow"}
    weight = 0.0
    for o in ops:
        if o.type == OpType.ADD_EFFECT:
            weight += 3.0 if o.parameters.get("type") in heavy else 1.0
        else:
            weight += 0.5
    tier = "instant" if weight <= 1 else "light" if weight <= 6 else \
        "moderate" if weight <= 16 else "heavy"
    return CostEstimate(tier=tier, seconds=round(weight * 1.5, 1), usd=0.0,
                        notes="Deterministic edit; no model spend.")


def plan(command: str, ctx: PlanContext) -> EditPlan:
    """Turn one instruction into a structured, inspectable edit plan.

    Real instructions are compound: "cut the first five seconds, slow this
    down, zoom toward me, and make it cinematic". Rules therefore run per
    clause rather than across the whole string. Matching the whole string
    lets unrelated clauses contaminate each other -- "cut ..." in one clause
    and "here" in another would combine into a split nobody asked for.
    """
    if not ctx.raw_command:
        ctx.raw_command = command

    original = ctx.raw_command
    operations: list[EditOperation] = []
    matched: list[str] = []
    exports: list[ExportRequest] = []

    for clause in split_clauses(command):
        # Title text is *content*, not intent: a title reading NIGHT DRIVE
        # must not trigger the night grade. Rules see the clause with the
        # title blanked out; the `text` rule reads the clause as typed.
        masked = strip_quoted(clause)
        title = find_title(clause)
        if title is not None:
            _content, start, end = title
            masked = masked[:start] + " " * (end - start) + masked[end:]
        text = masked.lower().strip()
        if not text:
            continue

        ctx.raw_command = clause
        for name, fn in RULES:
            try:
                produced = fn(text, ctx)
            except Exception as exc:  # noqa: BLE001 - one bad rule must not end the turn
                produced = []
                matched.append(f"{name}!error:{exc}")
            if produced:
                operations.extend(produced)
                if name not in matched:
                    matched.append(name)
        exports.extend(find_exports(text))
    ctx.raw_command = original

    seen: set[tuple] = set()
    deduped: list[EditOperation] = []
    for o in operations:
        key = (o.type, o.parameters.get("type"), o.parameters.get("start"),
               o.parameters.get("end"), o.parameters.get("text"),
               str(o.scope.model_dump()))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(o)

    seen_presets: set[str] = set()
    unique_exports: list[ExportRequest] = []
    for request in exports:
        if request.preset in seen_presets:
            continue
        seen_presets.add(request.preset or "")
        unique_exports.append(request)
    exports = unique_exports

    plan_ = EditPlan(
        command=command,
        operations=deduped,
        exports=exports,
        planner="heuristic",
        estimate=_estimate(deduped),
    )
    if exports and not deduped:
        plan_.summary = "Queue " + ", ".join(e.label for e in exports)
        plan_.rationale = "Matched an export request."
        return plan_
    if deduped:
        plan_.summary = "; ".join(o.description for o in deduped[:6])
        if len(deduped) > 6:
            plan_.summary += f"; +{len(deduped) - 6} more"
        plan_.rationale = (
            "Matched offline editing rules: " + ", ".join(matched) + "."
        )
        plan_.requires_confirmation = len(deduped) > 4 or any(
            o.type == OpType.REMOVE_RANGE for o in deduped
        )
        if exports:
            plan_.summary += "; queue " + ", ".join(e.label for e in exports)
    else:
        plan_.summary = ""
        plan_.rationale = (
            "The offline planner did not recognise this instruction."
        )
    return plan_


def can_plan(command: str, ctx: PlanContext) -> bool:
    return bool(plan(command, ctx).operations)
