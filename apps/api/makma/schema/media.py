"""Asset, probe and analysis schema."""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from .core import new_id


class AssetKind(str, Enum):
    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"
    LUT = "lut"
    FONT = "font"
    OTHER = "other"


class AssetStatus(str, Enum):
    UPLOADING = "uploading"
    INGESTING = "ingesting"
    READY = "ready"
    FAILED = "failed"


class VideoStreamInfo(BaseModel):
    index: int = 0
    codec: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    duration: float = 0.0
    bit_rate: int | None = None
    pix_fmt: str | None = None
    rotation: int = 0
    has_alpha: bool = False


class AudioStreamInfo(BaseModel):
    index: int = 0
    codec: str = ""
    sample_rate: int = 0
    channels: int = 0
    duration: float = 0.0
    bit_rate: int | None = None


class ProbeResult(BaseModel):
    """Normalized output of ffprobe."""
    format_name: str = ""
    duration: float = 0.0
    size: int = 0
    bit_rate: int | None = None
    video: list[VideoStreamInfo] = Field(default_factory=list)
    audio: list[AudioStreamInfo] = Field(default_factory=list)
    tags: dict[str, str] = Field(default_factory=dict)

    @property
    def primary_video(self) -> VideoStreamInfo | None:
        return self.video[0] if self.video else None

    @property
    def primary_audio(self) -> AudioStreamInfo | None:
        return self.audio[0] if self.audio else None


class Scene(BaseModel):
    """One detected shot. The AI Director reasons over these, not raw frames."""
    id: str = Field(default_factory=lambda: new_id("scene"))
    index: int = 0
    start: float = 0.0
    end: float = 0.0
    thumbnail: str | None = None    # storage key
    # Signal-derived metrics (V2 fills the semantic fields).
    motion_score: float = 0.0
    brightness: float = 0.0
    contrast: float = 0.0
    colorfulness: float = 0.0
    quality_score: float = 0.0
    energy_score: float = 0.0
    audio_rms: float = 0.0
    description: str | None = None
    shot_type: str | None = None
    subjects: list[str] = Field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


class BeatAnalysis(BaseModel):
    """Tempo and beat grid, so cuts can be placed against the music."""
    bpm: float = 0.0
    offset: float = 0.0
    confidence: float = 0.0
    beats_per_bar: int = 4
    beats: list[float] = Field(default_factory=list)
    downbeats: list[float] = Field(default_factory=list)
    onsets: list[float] = Field(default_factory=list)
    accents: list[float] = Field(default_factory=list)

    @property
    def usable(self) -> bool:
        """Whether the grid is trustworthy enough to cut against."""
        return self.bpm > 0 and self.confidence >= 0.35 and len(self.beats) > 3


class MediaAnalysis(BaseModel):
    scenes: list[Scene] = Field(default_factory=list)
    loudness_lufs: float | None = None
    peak_db: float | None = None
    silences: list[dict[str, float]] = Field(default_factory=list)
    highlights: list[dict[str, Any]] = Field(default_factory=list)
    beats: BeatAnalysis | None = None
    analyzed_at: str | None = None
    analyzer_version: int = 1


class AssetRef(BaseModel):
    """A pointer used inside media operations."""
    asset_id: str
    stream: int = 0
    use_proxy: bool = True
