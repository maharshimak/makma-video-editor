"""The edit engine: structured operations applied to a ProjectDocument.

This is the only code path that mutates a timeline. Dragging a clip in the UI
and typing "cut the first three seconds" both land here as the same
`EditOperation`, which is what makes AI edits and manual edits equally
inspectable, testable and undoable.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable

from .schema.core import (
    ANIMATABLE_TRANSFORM,
    Clip,
    ClipKind,
    Easing,
    Effect,
    Keyframe,
    Marker,
    ProjectDocument,
    Sequence,
    TextContent,
    TextStyle,
    Track,
    TrackKind,
    Transform,
    Transition,
    TransitionType,
    default_sequence,
)
from .schema.operations import (
    ApplyResult,
    EditOperation,
    EditPlan,
    OperationResult,
    OpType,
    Scope,
)


class OperationError(ValueError):
    """A operation that cannot be carried out as specified."""


@dataclass
class AssetInfo:
    id: str
    name: str = ""
    kind: str = "video"
    duration: float = 5.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_audio: bool = False


@dataclass
class EditContext:
    """Everything an operation may need beyond the document itself."""
    assets: dict[str, AssetInfo] = field(default_factory=dict)
    selection: list[str] = field(default_factory=list)
    playhead: float = 0.0
    sequence_id: str | None = None

    def asset(self, asset_id: str) -> AssetInfo:
        info = self.assets.get(asset_id)
        if info is None:
            raise OperationError(f"Unknown asset '{asset_id}'.")
        return info


# --------------------------------------------------------------------------
# Scope resolution
# --------------------------------------------------------------------------

def resolve_scope(seq: Sequence, scope: Scope, ctx: EditContext) -> list[tuple[Track, Clip]]:
    """Turn a scope into the concrete clips it refers to."""
    pairs: list[tuple[Track, Clip]] = []

    if scope.type == "clip" or scope.clip_ids:
        for cid in scope.clip_ids:
            hit = seq.find_clip(cid)
            if hit is None:
                raise OperationError(f"Clip '{cid}' is not in this sequence.")
            pairs.append(hit)
        if pairs:
            return _filter_kinds(pairs, scope)

    candidates: list[tuple[Track, Clip]] = []
    if scope.type == "selection":
        for cid in ctx.selection:
            hit = seq.find_clip(cid)
            if hit:
                candidates.append(hit)
        if not candidates:
            # An empty selection means "the whole sequence"; a command like
            # "make it cinematic" with nothing selected should still work.
            candidates = [(t, c) for t in seq.tracks for c in t.clips]
    elif scope.type == "track":
        ids = set(scope.track_ids)
        candidates = [(t, c) for t in seq.tracks for c in t.clips if t.id in ids]
    else:  # range | all | sequence
        candidates = [(t, c) for t in seq.tracks for c in t.clips]

    if scope.start is not None or scope.end is not None:
        lo = scope.start if scope.start is not None else float("-inf")
        hi = scope.end if scope.end is not None else float("inf")
        candidates = [(t, c) for t, c in candidates if c.start < hi and c.end > lo]

    if scope.track_ids and scope.type != "track":
        ids = set(scope.track_ids)
        candidates = [(t, c) for t, c in candidates if t.id in ids]

    return _filter_kinds(candidates, scope)


def _filter_kinds(pairs: list[tuple[Track, Clip]], scope: Scope) -> list[tuple[Track, Clip]]:
    if not scope.kinds:
        return pairs
    kinds = {k.lower() for k in scope.kinds}
    return [(t, c) for t, c in pairs if c.kind.value in kinds]


def _num(params: dict, key: str, default: float | None = None) -> float:
    val = params.get(key, default)
    if val is None:
        raise OperationError(f"Missing required parameter '{key}'.")
    try:
        return float(val)
    except (TypeError, ValueError):
        raise OperationError(f"Parameter '{key}' must be a number, got {val!r}.")


def _pick_track(seq: Sequence, params: dict, kind: TrackKind) -> Track:
    track_id = params.get("track_id")
    if track_id:
        track = seq.find_track(track_id)
        if track is None:
            raise OperationError(f"Track '{track_id}' does not exist.")
        return track
    index = params.get("track_index")
    pool = seq.video_tracks() if kind == TrackKind.VIDEO else seq.audio_tracks()
    if not pool:
        track = Track(kind=kind, index=0, name="V1" if kind == TrackKind.VIDEO else "A1")
        seq.tracks.append(track)
        return track
    if index is not None:
        for t in pool:
            if t.index == int(index):
                return t
        track = Track(
            kind=kind, index=int(index),
            name=f"{'V' if kind == TrackKind.VIDEO else 'A'}{int(index) + 1}",
        )
        seq.tracks.append(track)
        return track
    return pool[0]


def _free_start(track: Track) -> float:
    return max((c.end for c in track.clips), default=0.0)


# --------------------------------------------------------------------------
# Operation handlers
# --------------------------------------------------------------------------

Handler = Callable[[Sequence, EditOperation, EditContext, ProjectDocument], list[str]]
HANDLERS: dict[OpType, Handler] = {}


def handler(op_type: OpType):
    def wrap(fn: Handler) -> Handler:
        HANDLERS[op_type] = fn
        return fn
    return wrap


@handler(OpType.ADD_CLIP)
def _add_clip(seq: Sequence, op: EditOperation, ctx: EditContext,
              doc: ProjectDocument) -> list[str]:
    p = op.parameters
    asset = ctx.asset(str(p.get("asset_id", "")))

    kind = {
        "video": ClipKind.VIDEO, "audio": ClipKind.AUDIO, "image": ClipKind.IMAGE,
    }.get(asset.kind, ClipKind.VIDEO)
    track_kind = TrackKind.AUDIO if kind == ClipKind.AUDIO else TrackKind.VIDEO
    track = _pick_track(seq, p, track_kind)

    in_point = max(0.0, float(p.get("in_point", 0.0)))
    default_dur = 5.0 if kind == ClipKind.IMAGE else max(0.1, asset.duration - in_point)
    duration = max(0.05, float(p.get("duration", default_dur)))

    start = p.get("start")
    start = _free_start(track) if start in (None, "append") else max(0.0, float(start))

    clip = Clip(
        kind=kind,
        name=p.get("name") or asset.name or asset.id,
        asset_id=asset.id,
        start=start,
        duration=duration,
        in_point=in_point,
        speed=float(p.get("speed", 1.0)),
        volume=float(p.get("volume", 1.0)),
    )
    if p.get("fit"):
        clip.transform.fit = p["fit"]
    track.clips.append(clip)
    return [clip.id]


@handler(OpType.DELETE_CLIP)
def _delete_clip(seq: Sequence, op: EditOperation, ctx: EditContext,
                 doc: ProjectDocument) -> list[str]:
    pairs = resolve_scope(seq, op.scope, ctx)
    if not pairs:
        raise OperationError("Nothing matched the scope, so nothing was deleted.")
    ripple = bool(op.parameters.get("ripple", False))
    changed = []
    for track, clip in pairs:
        track.clips = [c for c in track.clips if c.id != clip.id]
        changed.append(clip.id)
        if ripple:
            for other in track.clips:
                if other.start >= clip.end:
                    other.start = max(0.0, other.start - clip.duration)
    return changed


@handler(OpType.MOVE_CLIP)
def _move_clip(seq: Sequence, op: EditOperation, ctx: EditContext,
               doc: ProjectDocument) -> list[str]:
    pairs = resolve_scope(seq, op.scope, ctx)
    p = op.parameters
    changed = []
    target_track_id = p.get("track_id")
    for track, clip in pairs:
        if "start" in p:
            clip.start = max(0.0, _num(p, "start"))
        elif "delta" in p:
            clip.start = max(0.0, clip.start + _num(p, "delta"))
        if target_track_id and target_track_id != track.id:
            dest = seq.find_track(target_track_id)
            if dest is None:
                raise OperationError(f"Track '{target_track_id}' does not exist.")
            if dest.kind != track.kind:
                raise OperationError(
                    f"Cannot move a {track.kind.value} clip onto a "
                    f"{dest.kind.value} track."
                )
            track.clips = [c for c in track.clips if c.id != clip.id]
            dest.clips.append(clip)
        changed.append(clip.id)
    return changed


@handler(OpType.TRIM_CLIP)
def _trim_clip(seq: Sequence, op: EditOperation, ctx: EditContext,
               doc: ProjectDocument) -> list[str]:
    """Adjust a clip's in/out points on the timeline.

    `start`/`end` are absolute timeline positions. Trimming the head also
    advances the source in-point so the picture does not jump.
    """
    p = op.parameters
    pairs = resolve_scope(seq, op.scope, ctx)
    if not pairs:
        raise OperationError("Nothing matched the scope, so nothing was trimmed.")
    changed = []
    for track, clip in pairs:
        old_start, old_end = clip.start, clip.end
        new_start = float(p["start"]) if "start" in p else old_start
        new_end = float(p["end"]) if "end" in p else old_end
        if "trim_head" in p:
            new_start = old_start + _num(p, "trim_head")
        if "trim_tail" in p:
            new_end = old_end - _num(p, "trim_tail")

        new_start = max(0.0, new_start)
        if new_end - new_start < 0.02:
            raise OperationError(
                f"Trim would leave clip '{clip.name or clip.id}' shorter than "
                "one frame."
            )
        head = new_start - old_start
        if head > 0:
            clip.in_point = max(0.0, clip.in_point + head * clip.speed)
        elif head < 0:
            room = clip.in_point / max(clip.speed, 1e-6)
            if -head > room + 1e-6:
                new_start = old_start - room
                clip.in_point = 0.0
            else:
                clip.in_point = max(0.0, clip.in_point + head * clip.speed)
        clip.start = new_start
        clip.duration = new_end - new_start
        changed.append(clip.id)

        if p.get("ripple"):
            shift = (new_start - old_start) + (new_end - old_end) - (new_start - old_start)
            for other in track.clips:
                if other.id != clip.id and other.start >= old_end:
                    other.start = max(0.0, other.start + shift)
    return changed


@handler(OpType.SPLIT_CLIP)
def _split_clip(seq: Sequence, op: EditOperation, ctx: EditContext,
                doc: ProjectDocument) -> list[str]:
    at = float(op.parameters.get("time", ctx.playhead))
    pairs = resolve_scope(seq, op.scope, ctx)
    pairs = [(t, c) for t, c in pairs if c.start + 0.01 < at < c.end - 0.01]
    if not pairs:
        raise OperationError(
            f"No clip spans {at:.2f}s, so there was nothing to split there."
        )
    changed = []
    for track, clip in pairs:
        offset = at - clip.start
        right = clip.model_copy(deep=True)
        right.id = Clip(kind=clip.kind).id
        right.start = at
        right.duration = clip.end - at
        right.in_point = clip.in_point + offset * clip.speed
        right.transition_in = Transition()
        clip.duration = offset
        clip.transition_out = Transition()
        track.clips.append(right)
        changed += [clip.id, right.id]
    return changed


@handler(OpType.DUPLICATE_CLIP)
def _duplicate_clip(seq: Sequence, op: EditOperation, ctx: EditContext,
                    doc: ProjectDocument) -> list[str]:
    pairs = resolve_scope(seq, op.scope, ctx)
    changed = []
    for track, clip in pairs:
        copy_ = clip.model_copy(deep=True)
        copy_.id = Clip(kind=clip.kind).id
        copy_.start = float(op.parameters.get("start", clip.end))
        for fx in copy_.effects:
            fx.id = Effect(type=fx.type).id
        track.clips.append(copy_)
        changed.append(copy_.id)
    return changed


@handler(OpType.REMOVE_RANGE)
def _remove_range(seq: Sequence, op: EditOperation, ctx: EditContext,
                  doc: ProjectDocument) -> list[str]:
    """Ripple-delete a time range across the whole sequence."""
    p = op.parameters
    start = _num(p, "start", op.scope.start if op.scope.start is not None else 0.0)
    end = _num(p, "end", op.scope.end if op.scope.end is not None else 0.0)
    if end <= start:
        raise OperationError(f"Range end ({end}) must be after start ({start}).")
    span = end - start
    ripple = bool(p.get("ripple", True))
    track_ids = set(op.scope.track_ids)
    changed: list[str] = []

    for track in seq.tracks:
        if track_ids and track.id not in track_ids:
            continue
        if track.locked:
            continue
        kept: list[Clip] = []
        for clip in track.sorted_clips():
            if clip.end <= start:
                kept.append(clip)
            elif clip.start >= end:
                if ripple:
                    clip.start -= span
                kept.append(clip)
            elif clip.start >= start and clip.end <= end:
                changed.append(clip.id)          # fully inside: removed
            elif clip.start < start and clip.end > end:
                # Range sits inside the clip: split and close the gap.
                right = clip.model_copy(deep=True)
                right.id = Clip(kind=clip.kind).id
                right.in_point = clip.in_point + (end - clip.start) * clip.speed
                right.duration = clip.end - end
                right.start = start if ripple else end
                clip.duration = start - clip.start
                kept += [clip, right]
                changed += [clip.id, right.id]
            elif clip.start < start < clip.end:
                clip.duration = start - clip.start
                kept.append(clip)
                changed.append(clip.id)
            else:  # clip.start < end < clip.end
                cut = end - clip.start
                clip.in_point += cut * clip.speed
                clip.duration = clip.end - end
                clip.start = start if ripple else end
                kept.append(clip)
                changed.append(clip.id)
        track.clips = [c for c in kept if c.duration > 0.02]
    return changed


@handler(OpType.REORDER)
def _reorder(seq: Sequence, op: EditOperation, ctx: EditContext,
             doc: ProjectDocument) -> list[str]:
    """Lay the given clips out back-to-back in the order supplied."""
    order = op.parameters.get("clip_ids") or op.scope.clip_ids
    if not order:
        raise OperationError("Reorder needs an explicit clip_ids order.")
    gap = float(op.parameters.get("gap", 0.0))
    cursor = float(op.parameters.get("start", 0.0))
    changed = []
    for cid in order:
        hit = seq.find_clip(cid)
        if hit is None:
            raise OperationError(f"Clip '{cid}' is not in this sequence.")
        _, clip = hit
        clip.start = cursor
        cursor += clip.duration + gap
        changed.append(clip.id)
    return changed


@handler(OpType.SET_TRACK)
def _set_track(seq: Sequence, op: EditOperation, ctx: EditContext,
               doc: ProjectDocument) -> list[str]:
    ids = op.scope.track_ids or [t.id for t in seq.tracks]
    p = op.parameters
    for tid in ids:
        track = seq.find_track(tid)
        if track is None:
            raise OperationError(f"Track '{tid}' does not exist.")
        for field_ in ("muted", "hidden", "locked", "solo", "is_adjustment"):
            if field_ in p:
                setattr(track, field_, bool(p[field_]))
        if "volume" in p:
            track.volume = max(0.0, _num(p, "volume"))
        if "name" in p:
            track.name = str(p["name"])
    return []


@handler(OpType.SET_KEYFRAMES)
def _set_keyframes(seq: Sequence, op: EditOperation, ctx: EditContext,
                   doc: ProjectDocument) -> list[str]:
    """Animate a property, or stop animating it.

    This is the one path that creates keyframes, so a curve dragged in the
    inspector and a curve the director asked for are the same data. Times are
    clip-local seconds, matching how the render reads them.

    Parameters:
      target     "transform" (default) or "effect"
      property   which property to animate
      keyframes  [{time, value, easing?}, ...]; an empty list clears the
                 animation and the property falls back to its static value
      effect_id / effect_type   which effect, when target is "effect"
      relative   times given as a 0..1 fraction of the clip instead of seconds
    """
    p = op.parameters
    target = str(p.get("target", "transform"))
    name = str(p.get("property", "") or p.get("name", ""))
    if not name:
        raise OperationError("Animating something needs a property name.")

    raw = p.get("keyframes")
    if raw is None:
        # Easing on its own retimes a curve that already exists, which is what
        # "make that movement smoother" means.
        if p.get("easing"):
            return _restyle_easing(seq, op, ctx, target, name, str(p["easing"]))
        raise OperationError(
            "Animating a property needs a list of keyframes; pass an empty "
            "list to remove the animation."
        )
    if not isinstance(raw, list):
        raise OperationError("'keyframes' must be a list of {time, value}.")

    if target == "transform" and raw and name not in ANIMATABLE_TRANSFORM:
        raise OperationError(
            f"'{name}' cannot be animated in a render. Animatable transform "
            f"properties are: {', '.join(sorted(ANIMATABLE_TRANSFORM))}. "
            "For an animated push-in, add the 'zoom' effect instead."
        )

    relative = bool(p.get("relative", False))
    pairs = resolve_scope(seq, op.scope, ctx)
    if not pairs:
        raise OperationError("Nothing matched the scope, so nothing was animated.")

    changed: list[str] = []
    for _track, clip in pairs:
        curve: list[Keyframe] = []
        for entry in raw:
            if not isinstance(entry, dict):
                raise OperationError("Each keyframe must be {time, value}.")
            try:
                time = float(entry["time"])
                value = float(entry["value"])
            except (KeyError, TypeError, ValueError):
                raise OperationError(
                    f"Keyframe {entry!r} needs a numeric 'time' and 'value'."
                )
            if relative:
                time *= clip.duration
            easing = entry.get("easing", Easing.EASE_IN_OUT)
            try:
                curve.append(Keyframe(time=max(0.0, time), value=value,
                                      easing=Easing(easing)))
            except ValueError:
                raise OperationError(
                    f"'{easing}' is not a known easing. Options: "
                    f"{', '.join(e.value for e in Easing)}"
                )
        curve.sort(key=lambda k: k.time)

        if target == "transform":
            if curve:
                clip.transform.keyframes[name] = curve
            else:
                clip.transform.keyframes.pop(name, None)
        else:
            effect = _find_effect(clip, p)
            if effect is None:
                raise OperationError(
                    "No matching effect on this clip to animate."
                )
            if curve:
                effect.keyframes[name] = curve
            else:
                effect.keyframes.pop(name, None)
        changed.append(clip.id)
    return changed


def _restyle_easing(seq: Sequence, op: EditOperation, ctx: EditContext,
                    target: str, name: str, easing: str) -> list[str]:
    """Change how an existing curve moves, without touching its values."""
    try:
        eased = Easing(easing)
    except ValueError:
        raise OperationError(
            f"'{easing}' is not a known easing. Options: "
            f"{', '.join(e.value for e in Easing)}"
        )

    changed: list[str] = []
    for _track, clip in resolve_scope(seq, op.scope, ctx):
        if target == "transform":
            curve = clip.transform.keyframes.get(name)
        else:
            effect = _find_effect(clip, op.parameters)
            curve = effect.keyframes.get(name) if effect else None
        if not curve:
            continue
        for keyframe in curve:
            keyframe.easing = eased
        changed.append(clip.id)

    if not changed:
        raise OperationError(
            f"Nothing selected has an animated '{name}' to re-time. Animate it "
            "first, then change how it moves."
        )
    return changed


def _find_effect(clip: Clip, params: dict) -> Effect | None:
    effect_id = params.get("effect_id")
    effect_type = params.get("effect_type") or params.get("type")
    for effect in clip.effects:
        if effect_id and effect.id == effect_id:
            return effect
        if effect_type and effect.type == effect_type:
            return effect
    return None


@handler(OpType.SET_DURATION)
def _set_duration(seq: Sequence, op: EditOperation, ctx: EditContext,
                  doc: ProjectDocument) -> list[str]:
    """Set how long a clip occupies the timeline.

    Stills and generated layers can be any length. Footage cannot outlast its
    source, so a request beyond what the media holds is capped and reported
    rather than silently producing a frozen tail.
    """
    p = op.parameters
    duration = _num(p, "duration")
    if duration <= 0:
        raise OperationError("Duration must be greater than zero.")
    ripple = bool(p.get("ripple", False))

    pairs = resolve_scope(seq, op.scope, ctx)
    if not pairs:
        raise OperationError("Nothing matched the scope, so no duration changed.")

    changed: list[str] = []
    capped: list[str] = []
    for track, clip in pairs:
        old_end = clip.end
        wanted = duration
        if clip.kind in (ClipKind.VIDEO, ClipKind.AUDIO) and clip.asset_id:
            asset = ctx.assets.get(clip.asset_id)
            if asset is not None and asset.duration > 0:
                available = (asset.duration - clip.in_point) / max(clip.speed, 1e-6)
                if wanted > available + 1e-6:
                    wanted = available
                    capped.append(clip.name or clip.id)
        clip.duration = max(0.02, wanted)
        changed.append(clip.id)
        if ripple:
            shift = clip.end - old_end
            for other in track.clips:
                if other.id != clip.id and other.start >= old_end:
                    other.start = max(0.0, other.start + shift)

    if capped:
        raise OperationError(
            f"{', '.join(capped[:3])} only has "
            f"{clip.duration:.2f}s of media left, so it cannot run for "
            f"{duration:.2f}s. Slow the clip down or hold a still instead."
        )
    return changed


@handler(OpType.SET_SPEED)
def _set_speed(seq: Sequence, op: EditOperation, ctx: EditContext,
               doc: ProjectDocument) -> list[str]:
    p = op.parameters
    speed = _num(p, "speed")
    if speed <= 0:
        raise OperationError("Speed must be greater than zero.")
    # Preserving the source window means a 0.5x clip occupies twice the
    # timeline; that is what "slow this down" means to an editor.
    preserve = bool(p.get("preserve_source", True))
    ripple = bool(p.get("ripple", False))
    pairs = resolve_scope(seq, op.scope, ctx)
    if not pairs:
        raise OperationError("Nothing matched the scope, so no speed was changed.")
    changed = []
    for track, clip in pairs:
        old_end = clip.end
        if preserve:
            source_span = clip.duration * clip.speed
            clip.duration = source_span / speed
        clip.speed = speed
        if bool(p.get("reverse")):
            clip.reverse = True
        changed.append(clip.id)
        if ripple:
            shift = clip.end - old_end
            for other in track.clips:
                if other.id != clip.id and other.start >= old_end:
                    other.start = max(0.0, other.start + shift)
    return changed


@handler(OpType.SET_VOLUME)
def _set_volume(seq: Sequence, op: EditOperation, ctx: EditContext,
                doc: ProjectDocument) -> list[str]:
    p = op.parameters
    pairs = resolve_scope(seq, op.scope, ctx)
    changed = []
    for _t, clip in pairs:
        if "volume" in p:
            clip.volume = max(0.0, _num(p, "volume"))
        if "gain_db" in p:
            clip.volume = max(0.0, clip.volume * (10 ** (_num(p, "gain_db") / 20)))
        if "muted" in p:
            clip.muted = bool(p["muted"])
        changed.append(clip.id)
    return changed


@handler(OpType.SET_TRANSFORM)
def _set_transform(seq: Sequence, op: EditOperation, ctx: EditContext,
                   doc: ProjectDocument) -> list[str]:
    p = op.parameters
    pairs = resolve_scope(seq, op.scope, ctx)
    changed = []
    for _t, clip in pairs:
        data = clip.transform.model_dump()
        for key in Transform.model_fields:
            if key in p:
                data[key] = p[key]
        clip.transform = Transform(**data)
        changed.append(clip.id)
    return changed


@handler(OpType.SET_ENABLED)
def _set_enabled(seq: Sequence, op: EditOperation, ctx: EditContext,
                 doc: ProjectDocument) -> list[str]:
    enabled = bool(op.parameters.get("enabled", True))
    pairs = resolve_scope(seq, op.scope, ctx)
    for _t, clip in pairs:
        clip.enabled = enabled
    return [c.id for _t, c in pairs]


@handler(OpType.ADD_EFFECT)
def _add_effect(seq: Sequence, op: EditOperation, ctx: EditContext,
                doc: ProjectDocument) -> list[str]:
    from .render.effects import REGISTRY

    p = dict(op.parameters)
    fx_type = str(p.pop("type", "") or p.pop("effect", ""))
    if fx_type not in REGISTRY:
        raise OperationError(
            f"'{fx_type}' is not a known effect. Available: "
            f"{', '.join(sorted(REGISTRY))}"
        )
    params = p.pop("parameters", None) or p
    replace = bool(op.parameters.get("replace", True))

    pairs = resolve_scope(seq, op.scope, ctx)
    if not pairs:
        raise OperationError("Nothing matched the scope, so no effect was added.")
    changed = []
    for _t, clip in pairs:
        if replace:
            # One grade per clip, not five stacked ones -- re-running
            # "make it cinematic" should refine, not compound.
            clip.effects = [e for e in clip.effects if e.type != fx_type]
        clip.effects.append(
            Effect(type=fx_type, parameters=dict(params), created_by=op.id)
        )
        changed.append(clip.id)
    return changed


@handler(OpType.UPDATE_EFFECT)
def _update_effect(seq: Sequence, op: EditOperation, ctx: EditContext,
                   doc: ProjectDocument) -> list[str]:
    p = op.parameters
    effect_id = p.get("effect_id")
    fx_type = p.get("type")
    updates = p.get("parameters") or {}
    changed = []
    for _t, clip in resolve_scope(seq, op.scope, ctx):
        for fx in clip.effects:
            if (effect_id and fx.id == effect_id) or (fx_type and fx.type == fx_type):
                fx.parameters.update(updates)
                if "enabled" in p:
                    fx.enabled = bool(p["enabled"])
                if "keyframes" in p:
                    fx.keyframes = p["keyframes"]
                changed.append(clip.id)
    if not changed:
        raise OperationError("No matching effect was found to update.")
    return changed


@handler(OpType.REMOVE_EFFECT)
def _remove_effect(seq: Sequence, op: EditOperation, ctx: EditContext,
                   doc: ProjectDocument) -> list[str]:
    p = op.parameters
    effect_id = p.get("effect_id")
    fx_type = p.get("type")
    created_by = p.get("created_by")
    changed = []
    for _t, clip in resolve_scope(seq, op.scope, ctx):
        before = len(clip.effects)
        clip.effects = [
            f for f in clip.effects
            if not ((effect_id and f.id == effect_id)
                    or (fx_type and f.type == fx_type)
                    or (created_by and f.created_by == created_by))
        ]
        if len(clip.effects) != before:
            changed.append(clip.id)
    return changed


@handler(OpType.SET_TRANSITION)
def _set_transition(seq: Sequence, op: EditOperation, ctx: EditContext,
                    doc: ProjectDocument) -> list[str]:
    p = op.parameters
    try:
        ttype = TransitionType(str(p.get("transition", p.get("type", "dissolve"))))
    except ValueError:
        raise OperationError(
            f"Unknown transition '{p.get('transition')}'. Options: "
            f"{', '.join(t.value for t in TransitionType)}"
        )
    duration = max(0.0, float(p.get("duration", 0.5)))
    where = str(p.get("where", "both"))
    pairs = resolve_scope(seq, op.scope, ctx)
    if not pairs:
        raise OperationError("Nothing matched the scope, so no transition was set.")
    changed = []
    for _t, clip in pairs:
        d = min(duration, clip.duration / 2)
        if where in ("in", "both"):
            clip.transition_in = Transition(type=ttype, duration=d)
        if where in ("out", "both"):
            clip.transition_out = Transition(type=ttype, duration=d)
        changed.append(clip.id)
    return changed


@handler(OpType.ADD_TEXT)
def _add_text(seq: Sequence, op: EditOperation, ctx: EditContext,
              doc: ProjectDocument) -> list[str]:
    p = op.parameters
    text = str(p.get("text", "")).strip()
    if not text:
        raise OperationError("A text layer needs some text.")

    style_fields = {k: v for k, v in p.items() if k in TextStyle.model_fields}
    content = TextContent(
        text=text,
        style=TextStyle(**style_fields),
        anchor_x=float(p.get("anchor_x", 0.5)),
        anchor_y=float(p.get("anchor_y", 0.42)),
        animation=str(p.get("animation", "fade")),
    )
    position = str(p.get("position", "")).lower()
    if position in ("top", "upper"):
        content.anchor_y = 0.15
    elif position in ("bottom", "lower"):
        content.anchor_y = 0.82
    elif position == "center":
        content.anchor_y = 0.5

    track = _pick_track(seq, p, TrackKind.VIDEO)
    if "track_id" not in p and "track_index" not in p:
        # Text belongs above the picture; use the top video track.
        track = seq.video_tracks()[-1]

    start = float(p.get("start", ctx.playhead))
    duration = max(0.2, float(p.get("duration", 3.0)))
    if "end" in p:
        duration = max(0.2, float(p["end"]) - start)

    clip = Clip(
        kind=ClipKind.TEXT, name=text[:40], start=max(0.0, start),
        duration=duration, text=content,
    )
    track.clips.append(clip)
    return [clip.id]


@handler(OpType.ADD_MARKER)
def _add_marker(seq: Sequence, op: EditOperation, ctx: EditContext,
                doc: ProjectDocument) -> list[str]:
    p = op.parameters
    times = p.get("times")
    if times is None:
        times = [float(p.get("time", ctx.playhead))]
    for t in times:
        seq.markers.append(
            Marker(time=float(t), name=str(p.get("name", "")),
                   kind=str(p.get("kind", "generic")),
                   color=str(p.get("color", "#E4B33B")))
        )
    return []


@handler(OpType.SET_FORMAT)
def _set_format(seq: Sequence, op: EditOperation, ctx: EditContext,
                doc: ProjectDocument) -> list[str]:
    p = op.parameters
    if "aspect_ratio" in p:
        ratio = str(p["aspect_ratio"]).replace("/", ":")
        presets = {
            "16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080),
            "4:5": (1080, 1350), "4:3": (1440, 1080), "2.39:1": (1920, 804),
            "21:9": (2560, 1080),
        }
        if ratio not in presets:
            raise OperationError(
                f"Unsupported aspect ratio '{ratio}'. Options: "
                f"{', '.join(presets)}"
            )
        seq.width, seq.height = presets[ratio]
    if "width" in p:
        seq.width = max(16, int(p["width"]))
    if "height" in p:
        seq.height = max(16, int(p["height"]))
    if "fps" in p:
        seq.fps = max(1.0, float(p["fps"]))
    if "background" in p:
        seq.background = str(p["background"])
    return []


@handler(OpType.CREATE_SEQUENCE)
def _create_sequence(seq: Sequence, op: EditOperation, ctx: EditContext,
                     doc: ProjectDocument) -> list[str]:
    p = op.parameters
    new = default_sequence(
        name=str(p.get("name", "Sequence")),
        width=int(p.get("width", 1080)),
        height=int(p.get("height", 1920)),
        fps=float(p.get("fps", seq.fps)),
    )
    if p.get("copy_from_active", False):
        source = doc.active()
        for src_track, dst_track in zip(source.tracks, new.tracks):
            for clip in src_track.clips:
                c = clip.model_copy(deep=True)
                c.id = Clip(kind=clip.kind).id
                c.transform.fit = "cover"
                dst_track.clips.append(c)
    doc.sequences.append(new)
    if p.get("activate", True):
        doc.active_sequence_id = new.id
    return []


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def apply_operation(
    doc: ProjectDocument,
    op: EditOperation,
    ctx: EditContext | None = None,
) -> OperationResult:
    ctx = ctx or EditContext()
    handler_fn = HANDLERS.get(op.type)
    if handler_fn is None:
        return OperationResult(
            operation_id=op.id, ok=False,
            message=f"'{op.type.value}' is not implemented in this build yet.",
        )
    seq = doc.sequence(ctx.sequence_id)
    try:
        changed = handler_fn(seq, op, ctx, doc)
    except OperationError as exc:
        return OperationResult(operation_id=op.id, ok=False, message=str(exc))
    except (KeyError, TypeError, ValueError) as exc:
        return OperationResult(
            operation_id=op.id, ok=False,
            message=f"{op.type.value} failed: {exc}",
        )
    return OperationResult(operation_id=op.id, ok=True, changed_clip_ids=changed)


def apply_operations(
    doc: ProjectDocument,
    operations: list[EditOperation],
    ctx: EditContext | None = None,
    atomic: bool = True,
) -> tuple[ProjectDocument, ApplyResult]:
    """Apply a list of operations to a *copy* of the document.

    With `atomic=True` a failure anywhere discards every change, so a partly
    applied AI plan can never leave the timeline in a state the user did not
    ask for.
    """
    ctx = ctx or EditContext()
    working = doc.model_copy(deep=True)
    result = ApplyResult()

    for op in operations:
        res = apply_operation(working, op, ctx)
        result.results.append(res)
        if not res.ok and atomic:
            return doc, result
    return working, result


def apply_plan(
    doc: ProjectDocument,
    plan: EditPlan,
    ctx: EditContext | None = None,
    atomic: bool = True,
) -> tuple[ProjectDocument, ApplyResult]:
    working, result = apply_operations(doc, plan.operations, ctx, atomic)
    result.plan_id = plan.id
    return working, result
