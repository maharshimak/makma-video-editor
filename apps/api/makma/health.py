"""Project health and pre-flight checks.

Every problem here is one that renders *something* rather than failing, which
is exactly why it needs finding before export. A clip whose media is gone
still occupies its place in the sequence and simply draws black; an image
stretched past its resolution still encodes. The render would succeed and the
result would be wrong.

Each issue carries a time, so the UI can put the user in front of it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .schema.core import ClipKind, ProjectDocument, Sequence

Severity = Literal["error", "warning", "info"]


@dataclass
class Issue:
    severity: Severity
    code: str
    message: str
    time: float | None = None
    clip_id: str | None = None
    sequence_id: str | None = None
    fix: str = ""

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "time": round(self.time, 3) if self.time is not None else None,
            "clip_id": self.clip_id,
            "sequence_id": self.sequence_id,
            "fix": self.fix,
        }


@dataclass
class HealthReport:
    issues: list[Issue] = field(default_factory=list)
    duration: float = 0.0
    clip_count: int = 0

    @property
    def ready_to_export(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)

    def to_dict(self) -> dict:
        counts = {"error": 0, "warning": 0, "info": 0}
        for issue in self.issues:
            counts[issue.severity] += 1
        return {
            "ready_to_export": self.ready_to_export,
            "duration": round(self.duration, 3),
            "clip_count": self.clip_count,
            "counts": counts,
            "issues": [i.to_dict() for i in self.issues],
        }


@dataclass
class AssetFacts:
    """What health checking needs to know about a file."""
    id: str
    name: str
    status: str = "ready"
    width: int = 0
    height: int = 0
    duration: float = 0.0
    kind: str = "video"
    has_audio: bool = False
    peak_db: float | None = None


def check_sequence(sequence: Sequence, assets: dict[str, AssetFacts]) -> list[Issue]:
    issues: list[Issue] = []
    clips = [c for c in sequence.all_clips() if c.enabled]

    if not clips:
        issues.append(Issue(
            "error", "empty_sequence",
            f"'{sequence.name}' has no clips.",
            sequence_id=sequence.id,
            fix="Add media to the timeline before exporting.",
        ))
        return issues

    for clip in sorted(clips, key=lambda c: c.start):
        if clip.asset_id:
            asset = assets.get(clip.asset_id)

            if asset is None:
                issues.append(Issue(
                    "error", "offline_media",
                    f"'{clip.name or clip.kind}' points at media that is no "
                    "longer in the project.",
                    time=clip.start, clip_id=clip.id, sequence_id=sequence.id,
                    fix="Remove the clip, or re-import the file.",
                ))
                continue

            if asset.status == "failed":
                issues.append(Issue(
                    "error", "unreadable_media",
                    f"'{asset.name}' could not be decoded, so this clip will "
                    "render as nothing.",
                    time=clip.start, clip_id=clip.id, sequence_id=sequence.id,
                    fix="Replace the file with one this build can read.",
                ))
                continue

            if asset.status != "ready":
                issues.append(Issue(
                    "warning", "still_ingesting",
                    f"'{asset.name}' is still being processed.",
                    time=clip.start, clip_id=clip.id, sequence_id=sequence.id,
                    fix="Wait for ingestion to finish before exporting.",
                ))

            # Upscaling past about 1.2x is where softness becomes obvious.
            if asset.width and clip.kind in (ClipKind.VIDEO, ClipKind.IMAGE):
                effective = asset.width * max(clip.transform.scale, 1.0)
                if effective * 1.2 < sequence.width:
                    issues.append(Issue(
                        "warning", "low_resolution",
                        f"'{asset.name}' is {asset.width}px wide in a "
                        f"{sequence.width}px sequence, so it will look soft.",
                        time=clip.start, clip_id=clip.id,
                        sequence_id=sequence.id,
                        fix="Use a higher-resolution source, or scale the clip down.",
                    ))

            # Asking for more material than the file holds freezes the tail.
            if asset.duration > 0 and clip.kind == ClipKind.VIDEO:
                needed = clip.in_point + clip.duration * clip.speed
                if needed > asset.duration + 0.05:
                    issues.append(Issue(
                        "warning", "past_end_of_media",
                        f"'{clip.name or asset.name}' runs "
                        f"{needed - asset.duration:.2f}s past the end of its "
                        "source, so the last frame is held.",
                        time=clip.start + clip.duration, clip_id=clip.id,
                        sequence_id=sequence.id,
                        fix="Shorten the clip or slow it down.",
                    ))

        if clip.kind == ClipKind.TEXT and clip.text:
            # Text outside the title-safe area gets cropped on some players.
            for axis, value in (("horizontally", clip.text.anchor_x),
                                ("vertically", clip.text.anchor_y)):
                if value < 0.05 or value > 0.95:
                    issues.append(Issue(
                        "warning", "text_outside_safe_area",
                        f"'{clip.text.text[:30]}' sits {axis} outside the "
                        "title-safe area and may be cropped.",
                        time=clip.start, clip_id=clip.id,
                        sequence_id=sequence.id,
                        fix="Move the text further inside the frame.",
                    ))
                    break

        if clip.duration < 1 / max(sequence.fps, 1):
            issues.append(Issue(
                "warning", "shorter_than_a_frame",
                f"'{clip.name or clip.kind}' is shorter than one frame and "
                "may not appear at all.",
                time=clip.start, clip_id=clip.id, sequence_id=sequence.id,
                fix="Lengthen or remove the clip.",
            ))

    # Gaps on the bottom video track render as black, which is sometimes
    # deliberate and sometimes a clip that got dragged.
    bottom = sequence.video_tracks()[0] if sequence.video_tracks() else None
    if bottom:
        cursor = 0.0
        for clip in sorted((c for c in bottom.clips if c.enabled),
                           key=lambda c: c.start):
            if clip.start - cursor > 0.25:
                issues.append(Issue(
                    "info", "gap",
                    f"{clip.start - cursor:.1f}s of black on {bottom.name} "
                    f"at {cursor:.1f}s.",
                    time=cursor, sequence_id=sequence.id,
                    fix="Close the gap, or leave it if the black is intended.",
                ))
            cursor = max(cursor, clip.end)

    if not any(
        c.enabled and not c.muted and c.asset_id
        and (assets.get(c.asset_id).has_audio if assets.get(c.asset_id) else False)
        for c in clips
    ):
        issues.append(Issue(
            "info", "no_audio",
            "Nothing on this timeline has audible sound.",
            sequence_id=sequence.id,
            fix="Add music or unmute a clip, if the silence is not intended.",
        ))

    for asset in assets.values():
        if asset.peak_db is not None and asset.peak_db > -0.1:
            issues.append(Issue(
                "warning", "audio_clipping",
                f"'{asset.name}' peaks at {asset.peak_db:.1f} dBFS and is "
                "already clipping before the mix.",
                fix="Lower the clip's level, or add loudness normalisation.",
            ))

    return issues


def check_project(document: ProjectDocument, assets: dict[str, AssetFacts],
                  sequence_id: str | None = None) -> HealthReport:
    sequence = document.sequence(sequence_id)
    report = HealthReport(
        duration=sequence.duration,
        clip_count=len([c for c in sequence.all_clips() if c.enabled]),
    )
    report.issues = check_sequence(sequence, assets)
    # Errors first, then by where they happen, so the list reads in the order
    # the user would fix things.
    order = {"error": 0, "warning": 1, "info": 2}
    report.issues.sort(key=lambda i: (order[i.severity], i.time if i.time is not None else -1))
    return report
