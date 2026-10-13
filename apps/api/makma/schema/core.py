"""Core project schema.

This module is the single source of truth for what a Mak'ma project *is*.
Everything else in the system -- the timeline UI, the AI edit planner, the
render graph -- operates on these structures. The schema is normalized and
every entity carries a stable id so operations can reference things over time.
"""
from __future__ import annotations

import uuid
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------

class TrackKind(str, Enum):
    VIDEO = "video"
    AUDIO = "audio"


class ClipKind(str, Enum):
    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"
    TEXT = "text"
    SOLID = "solid"
    ADJUSTMENT = "adjustment"


class Easing(str, Enum):
    LINEAR = "linear"
    EASE_IN = "ease_in"
    EASE_OUT = "ease_out"
    EASE_IN_OUT = "ease_in_out"
    HOLD = "hold"


class TransitionType(str, Enum):
    NONE = "none"
    FADE = "fade"          # fade from/to black
    DISSOLVE = "dissolve"  # alpha cross-dissolve against layers beneath
    WIPE_LEFT = "wipe_left"      # reveal, growing from the left edge
    WIPE_RIGHT = "wipe_right"    # reveal, growing from the right edge
    SLIDE_LEFT = "slide_left"    # push in from the right, travelling left
    SLIDE_RIGHT = "slide_right"  # push in from the left, travelling right
    SLIDE_UP = "slide_up"
    SLIDE_DOWN = "slide_down"


class FitMode(str, Enum):
    CONTAIN = "contain"
    COVER = "cover"
    STRETCH = "stretch"
    NONE = "none"


# --------------------------------------------------------------------------
# Primitives
# --------------------------------------------------------------------------

class TimeRange(BaseModel):
    start: float = 0.0
    end: float = 0.0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


class Keyframe(BaseModel):
    """A single animated value at a point in *clip-local* time (seconds)."""
    id: str = Field(default_factory=lambda: new_id("kf"))
    time: float = 0.0
    value: float = 0.0
    easing: Easing = Easing.EASE_IN_OUT


#: Transform properties and their neutral values.
TRANSFORM_DEFAULTS: dict[str, float] = {
    "x": 0.0, "y": 0.0, "scale": 1.0, "rotation": 0.0, "opacity": 1.0,
}

#: Transform properties that animate all the way through to the render.
#:
#: `x` and `y` ride on `overlay`, whose position expressions are evaluated per
#: frame, and `opacity` is drawn onto the alpha plane. `scale` and `rotation`
#: are absent on purpose: FFmpeg's `scale` fixes its output size when the
#: filter starts, and `rotate` reads its angle once despite advertising a
#: runtime flag. Animating them would look right in the preview and come out
#: static in the render, so the editor does not offer it. An animated push-in
#: is available as the Push In effect, which uses `zoompan` and does work.
ANIMATABLE_TRANSFORM: dict[str, float] = {
    "x": 0.0, "y": 0.0, "opacity": 1.0,
}


class Transform(BaseModel):
    x: float = 0.0            # offset in canvas pixels from centre
    y: float = 0.0
    scale: float = 1.0
    rotation: float = 0.0     # degrees
    opacity: float = 1.0
    fit: FitMode = FitMode.CONTAIN
    crop_left: float = 0.0    # normalised 0..1
    crop_right: float = 0.0
    crop_top: float = 0.0
    crop_bottom: float = 0.0

    #: property name -> curve, in clip-local seconds. A property with
    #: keyframes ignores its static value; the static value is what the
    #: property reverts to when its animation is removed.
    keyframes: dict[str, list[Keyframe]] = Field(default_factory=dict)

    def animated(self, name: str) -> bool:
        return bool(self.keyframes.get(name))

    def value_at(self, name: str, local_time: float) -> float:
        """Sample a transform property at a point in clip-local time."""
        curve = self.keyframes.get(name)
        if not curve:
            return float(getattr(self, name, TRANSFORM_DEFAULTS.get(name, 0.0)))
        ordered = sorted(curve, key=lambda k: k.time)
        if local_time <= ordered[0].time:
            return ordered[0].value
        if local_time >= ordered[-1].time:
            return ordered[-1].value
        for a, b in zip(ordered, ordered[1:]):
            if a.time <= local_time <= b.time:
                span = b.time - a.time
                if span <= 0:
                    return b.value
                return a.value + (b.value - a.value) * _ease(
                    a.easing, (local_time - a.time) / span
                )
        return ordered[-1].value


def _ease(easing: "Easing", p: float) -> float:
    if easing == Easing.LINEAR:
        return p
    if easing == Easing.EASE_IN:
        return p * p
    if easing == Easing.EASE_OUT:
        return 1 - (1 - p) * (1 - p)
    if easing == Easing.HOLD:
        return 0.0
    return 2 * p * p if p < 0.5 else 1 - 2 * (1 - p) * (1 - p)


class Effect(BaseModel):
    """A non-destructive operation applied to a clip or adjustment layer.

    `type` selects the implementation in the render engine registry; the
    engine validates parameters, not this schema, so new effects can ship
    without a schema migration.
    """
    id: str = Field(default_factory=lambda: new_id("fx"))
    type: str
    enabled: bool = True
    parameters: dict[str, Any] = Field(default_factory=dict)
    keyframes: dict[str, list[Keyframe]] = Field(default_factory=dict)
    label: str | None = None
    created_by: str | None = None  # provenance: AI operation id, if any


class Transition(BaseModel):
    type: TransitionType = TransitionType.NONE
    duration: float = 0.5


class TextStyle(BaseModel):
    font_family: str = "Inter"
    font_size: int = 72
    color: str = "#FFFFFF"
    bold: bool = True
    italic: bool = False
    align: Literal["left", "center", "right"] = "center"
    letter_spacing: float = 0.0
    line_height: float = 1.2
    uppercase: bool = False
    stroke_color: str | None = None
    stroke_width: float = 0.0
    shadow: bool = True
    background: str | None = None
    background_padding: int = 16


class TextContent(BaseModel):
    text: str = "TEXT"
    style: TextStyle = Field(default_factory=TextStyle)
    # position expressed 0..1 relative to canvas; (0.5, 0.5) is centre
    anchor_x: float = 0.5
    anchor_y: float = 0.5
    animation: str = "fade"  # fade | slide_up | scale_in | none


# --------------------------------------------------------------------------
# Timeline entities
# --------------------------------------------------------------------------

class Clip(BaseModel):
    id: str = Field(default_factory=lambda: new_id("clip"))
    kind: ClipKind
    name: str = ""
    asset_id: str | None = None

    # Placement on the sequence timeline (seconds).
    start: float = 0.0
    duration: float = 1.0

    # Source media window (seconds, pre-speed). Ignored for text/solid.
    in_point: float = 0.0

    speed: float = 1.0
    reverse: bool = False

    volume: float = 1.0
    muted: bool = False

    transform: Transform = Field(default_factory=Transform)
    effects: list[Effect] = Field(default_factory=list)

    transition_in: Transition = Field(default_factory=Transition)
    transition_out: Transition = Field(default_factory=Transition)

    text: TextContent | None = None
    color: str | None = None  # for SOLID clips

    enabled: bool = True
    locked: bool = False
    label_color: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)

    @property
    def end(self) -> float:
        return self.start + self.duration

    @property
    def source_duration(self) -> float:
        """How much source material this clip consumes, accounting for speed."""
        return self.duration * self.speed

    @model_validator(mode="after")
    def _validate(self) -> "Clip":
        self.duration = max(0.001, self.duration)
        self.speed = min(100.0, max(0.01, self.speed))
        self.start = max(0.0, self.start)
        if self.kind == ClipKind.TEXT and self.text is None:
            self.text = TextContent()
        return self


class Track(BaseModel):
    id: str = Field(default_factory=lambda: new_id("track"))
    kind: TrackKind
    name: str = ""
    index: int = 0    # 0 is bottom-most (composited first)
    clips: list[Clip] = Field(default_factory=list)
    muted: bool = False
    hidden: bool = False
    locked: bool = False
    solo: bool = False
    volume: float = 1.0
    is_adjustment: bool = False

    def sorted_clips(self) -> list[Clip]:
        return sorted(self.clips, key=lambda c: c.start)


class Marker(BaseModel):
    id: str = Field(default_factory=lambda: new_id("mk"))
    time: float
    name: str = ""
    color: str = "#E4B33B"
    kind: str = "generic"  # generic | beat | scene | note | ai


class Sequence(BaseModel):
    id: str = Field(default_factory=lambda: new_id("seq"))
    name: str = "Main"
    width: int = 1920
    height: int = 1080
    fps: float = 30.0
    sample_rate: int = 48000
    background: str = "#000000"
    tracks: list[Track] = Field(default_factory=list)
    markers: list[Marker] = Field(default_factory=list)

    @property
    def duration(self) -> float:
        end = 0.0
        for track in self.tracks:
            for clip in track.clips:
                if clip.enabled:
                    end = max(end, clip.end)
        return end

    def video_tracks(self) -> list[Track]:
        return sorted(
            [t for t in self.tracks if t.kind == TrackKind.VIDEO],
            key=lambda t: t.index,
        )

    def audio_tracks(self) -> list[Track]:
        return sorted(
            [t for t in self.tracks if t.kind == TrackKind.AUDIO],
            key=lambda t: t.index,
        )

    def find_clip(self, clip_id: str) -> tuple[Track, Clip] | None:
        for track in self.tracks:
            for clip in track.clips:
                if clip.id == clip_id:
                    return track, clip
        return None

    def find_track(self, track_id: str) -> Track | None:
        for track in self.tracks:
            if track.id == track_id:
                return track
        return None

    def all_clips(self) -> list[Clip]:
        return [c for t in self.tracks for c in t.clips]


class StyleProfile(BaseModel):
    """'Style DNA' -- abstract creative characteristics, not a copy of a work."""
    id: str = Field(default_factory=lambda: new_id("style"))
    name: str = "Untitled Style"
    palette: dict[str, Any] = Field(default_factory=dict)
    editing: dict[str, Any] = Field(default_factory=dict)
    camera: dict[str, Any] = Field(default_factory=dict)
    typography: dict[str, Any] = Field(default_factory=dict)
    sound: dict[str, Any] = Field(default_factory=dict)
    source: str | None = None
    notes: str = ""


class ProjectDocument(BaseModel):
    """The full, self-contained state of a project's timeline."""
    schema_version: int = 1
    id: str = Field(default_factory=lambda: new_id("proj"))
    name: str = "Untitled Project"
    sequences: list[Sequence] = Field(default_factory=list)
    active_sequence_id: str | None = None
    styles: list[StyleProfile] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _ensure_sequence(self) -> "ProjectDocument":
        if not self.sequences:
            self.sequences = [default_sequence()]
        if not self.active_sequence_id or not any(
            s.id == self.active_sequence_id for s in self.sequences
        ):
            self.active_sequence_id = self.sequences[0].id
        return self

    def active(self) -> Sequence:
        for s in self.sequences:
            if s.id == self.active_sequence_id:
                return s
        return self.sequences[0]

    def sequence(self, sequence_id: str | None) -> Sequence:
        if sequence_id is None:
            return self.active()
        for s in self.sequences:
            if s.id == sequence_id:
                return s
        raise KeyError(f"No such sequence: {sequence_id}")


def default_sequence(
    name: str = "Main",
    width: int = 1920,
    height: int = 1080,
    fps: float = 30.0,
) -> Sequence:
    """A sequence with the standard 3 video + 2 audio track layout."""
    return Sequence(
        name=name,
        width=width,
        height=height,
        fps=fps,
        tracks=[
            Track(kind=TrackKind.VIDEO, index=0, name="V1"),
            Track(kind=TrackKind.VIDEO, index=1, name="V2"),
            Track(kind=TrackKind.VIDEO, index=2, name="V3"),
            Track(kind=TrackKind.AUDIO, index=0, name="A1"),
            Track(kind=TrackKind.AUDIO, index=1, name="A2"),
        ],
    )
