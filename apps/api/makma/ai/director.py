"""The AI Director.

Turns an instruction plus project context into a structured `EditPlan`. The
model is the planner, never the pixel pusher: it selects operations from a
fixed vocabulary and the deterministic engines carry them out. Anything the
model proposes that this build cannot execute is reported as unsupported
rather than quietly dropped.
"""
from __future__ import annotations

import json
import logging

from ..config import settings
from ..render.effects import GRADE_PRESETS, REGISTRY
from ..schema.core import ProjectDocument, TransitionType
from ..schema.operations import (
    IMPLEMENTED_OPS,
    CostEstimate,
    EditPlan,
    OpType,
)
from . import heuristic
from .context import AssetBrief, build_context
from .heuristic import MediaRef, PlanContext
from .router import Capability, NotConfigured, get_router
from .validation import ProjectFacts, ValidationResult, validate_plan

log = logging.getLogger("makma.ai.director")


# --------------------------------------------------------------------------
# Tool definition
# --------------------------------------------------------------------------

def operation_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": sorted(o.value for o in OpType),
                "description": "The operation to perform.",
            },
            "description": {
                "type": "string",
                "description": "One short line shown to the user in the plan "
                               "preview, e.g. 'Remove the first 3.2 seconds'.",
            },
            "scope": {
                "type": "object",
                "description": "What the operation applies to.",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["clip", "track", "range", "selection", "all",
                                 "sequence"],
                    },
                    "clip_ids": {"type": "array", "items": {"type": "string"}},
                    "track_ids": {"type": "array", "items": {"type": "string"}},
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "kinds": {
                        "type": "array",
                        "items": {"type": "string",
                                  "enum": ["video", "audio", "image", "text",
                                           "solid", "adjustment"]},
                    },
                },
            },
            "parameters": {
                "type": "object",
                "description": "Operation parameters. See the system prompt for "
                               "the parameters each operation takes.",
            },
        },
        "required": ["type", "description"],
    }


def plan_tool() -> dict:
    return {
        "name": "propose_edit",
        "description": (
            "Propose a structured edit plan for the user's instruction. Every "
            "change to the timeline must go through this tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "One sentence describing the edit, in the "
                                   "voice of an editor talking to a client.",
                },
                "rationale": {
                    "type": "string",
                    "description": "Why these choices, referencing what you saw "
                                   "in the project context. Two sentences max.",
                },
                "operations": {
                    "type": "array",
                    "items": operation_schema(),
                },
                "exports": {
                    "type": "array",
                    "description": "Renders the user asked for. These are queued "
                                   "after the edits apply; they are not timeline "
                                   "operations.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "preset": {
                                "type": "string",
                                "enum": ["master_1080p", "master_4k",
                                         "youtube_1080p", "tiktok", "reel",
                                         "instagram_feed", "linkedin", "x_720p",
                                         "draft_540p"],
                            },
                            "label": {"type": "string"},
                        },
                        "required": ["preset"],
                    },
                },
                "questions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ask only if the instruction cannot be acted "
                                   "on at all without an answer. Prefer acting.",
                },
            },
            "required": ["summary", "operations"],
        },
    }


# --------------------------------------------------------------------------
# System prompt
# --------------------------------------------------------------------------

OPERATION_REFERENCE = """
TIMELINE STRUCTURE
  add_clip        {asset_id, track_id?, track_index?, start|"append", duration?,
                   in_point?, speed?, volume?}
  delete_clip     scope=clip; {ripple?: bool}
  move_clip       scope=clip; {start?} or {delta?}, {track_id?}
  trim_clip       scope=clip; {start?, end?} absolute timeline seconds,
                  or {trim_head?, trim_tail?} in seconds
  split_clip      scope=clip|all; {time}
  duplicate_clip  scope=clip; {start?}
  remove_range    scope=all|track; {start, end, ripple?: bool=true}
                  Ripple-deletes across the sequence and closes the gap.
  reorder         {clip_ids: [...], start?, gap?}
  set_track       scope=track; {muted?, hidden?, locked?, volume?, is_adjustment?}

CLIP PROPERTIES
  set_keyframes   {property, keyframes:[{time,value,easing?}], relative?,
                   target?: transform|effect, effect_id?|effect_type?}
                  Animates a property. Times are clip-local seconds, or a
                  0..1 fraction of the clip when relative is true. An empty
                  keyframes list removes the animation; passing only `easing`
                  re-times a curve that already exists.
                  Animatable transform properties: x, y, opacity. Scale and
                  rotation cannot be animated in a render -- for a moving
                  zoom use add_effect type="zoom" instead.
  set_duration    {duration, ripple?}  How long the clip occupies the timeline.
                  Use this for stills ("hold this photo for 3 seconds");
                  footage cannot outlast its source.
  set_speed       {speed, preserve_source?: bool=true, reverse?, ripple?}
                  speed<1 is slow motion; 0.5 makes the clip twice as long.
  set_volume      {volume?} linear, or {gain_db?}, or {muted?}
  set_transform   {x?, y?, scale?, rotation?, opacity?, fit?: contain|cover|
                   stretch|none, crop_left?, crop_right?, crop_top?, crop_bottom?}
  set_enabled     {enabled}

LOOKS
  add_effect      {type, parameters:{...}}  Replaces an existing effect of the
                  same type on that clip, so re-running refines rather than stacks.
  update_effect   {effect_id?|type, parameters:{...}, enabled?}
  remove_effect   {effect_id?|type}
  set_transition  {transition, duration?, where?: in|out|both}

CONTENT
  add_text        {text, start?, duration?|end?, position?: top|center|bottom,
                   font_size?, color?, uppercase?, font_family?, stroke_color?,
                   stroke_width?, background?, animation?: fade|none,
                   anchor_x?, anchor_y?}
  add_marker      {time?|times?: [...], name?, kind?}

SEQUENCE
  set_format      {aspect_ratio?: 16:9|9:16|1:1|4:5|4:3|2.39:1|21:9,
                   width?, height?, fps?, background?}
  create_sequence {name, width, height, fps?, copy_from_active?: bool,
                   activate?: bool}
"""


def system_prompt() -> str:
    effects = {}
    for spec in REGISTRY.values():
        effects.setdefault(spec.category, []).append(
            f"{spec.type}({', '.join(f'{k}={v}' for k, v in spec.defaults.items()) or 'no params'})"
        )
    effect_lines = "\n".join(
        f"  {cat}: " + ", ".join(sorted(items)) for cat, items in sorted(effects.items())
    )
    return f"""You are the AI Director inside Mak'ma Editz, a professional
non-linear video editor. You are an experienced editor and colourist, not a
chatbot: you translate what a creator asks for into concrete timeline
operations.

HOW YOU WORK
- You never touch pixels. You emit operations; deterministic engines (FFmpeg,
  the timeline engine) carry them out. Always call the `propose_edit` tool.
- You are given a compact project index built during ingestion: shot
  boundaries, exposure and energy scores per shot, ranked highlights, detected
  silences. Reason over that index. You have not watched the footage; do not
  claim you have, and do not invent content you cannot see.
- Reference real ids from the context. Never invent a clip_id or asset_id.
- Prefer acting over asking. Make the sensible editorial call and say what you
  chose in `summary`. Only use `questions` when the instruction is impossible
  to act on at all.
- Scope carefully. If the user has a selection, default to it. With no
  selection, "make this cinematic" means the whole sequence.
- Time is in seconds on the sequence timeline unless stated otherwise.

EDITORIAL JUDGEMENT
- "Cinematic" is a package, not a LUT: grade, 2.39:1 letterbox, restrained
  grain, a soft vignette, and slower cuts. Apply the package.
- Slow motion means speed < 1 with preserve_source true, so the shot occupies
  more timeline.
- When asked to shorten or find the best material, use the `highlights` and
  `quality`/`energy` scores in the context rather than cutting arbitrarily.
- When reframing to a different aspect ratio, also set fit="cover" on the
  visual clips, otherwise the result is just letterboxed.
- Keep grain, vignette and shake subtle unless asked for the opposite.
- "Slowly zoom in" is a camera move, not a speed change: use the zoom effect.
- Movement language ("drift across", "float", "settle") means keyframes. Give
  them easing rather than linear motion; linear movement reads as mechanical.

OPERATIONS
{OPERATION_REFERENCE}

EFFECT TYPES
{effect_lines}

GRADE PRESETS for add_effect type="color_grade": {', '.join(sorted(GRADE_PRESETS))}

TRANSITIONS: {', '.join(t.value for t in TransitionType)}
  dissolve reveals the layers beneath; fade goes to black. slide_* animate
  position and are cheap. wipe_* are drawn per pixel and are slower.

NOT YET IMPLEMENTED in this build: object segmentation and tracking, object
removal, background replacement, generative video/B-roll, relighting,
automatic captions, beat detection. If the user asks for one of these, include
the matching operation type anyway so it is recorded, but say plainly in
`summary` that it is not available yet and deliver whatever part of the
request you *can* do with the operations above."""


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------

def repair_prompt(result: ValidationResult) -> str:
    """Tell the model precisely what was wrong, so a retry can be different.

    A vague "that was invalid, try again" produces the same plan a second
    time. Naming the field and the rule is what makes the retry worth paying
    for.
    """
    return (
        "That plan was rejected by validation. Fix these problems and call "
        "`propose_edit` again with the corrected plan:\n\n"
        f"{result.repair_brief()}\n\n"
        "Keep everything that was already correct. Use only ids that appear "
        "in the project context, keep every number finite and in range, and "
        "do not put file paths, commands or filter expressions in parameters."
    )


def _plan_from_result(result: ValidationResult, command: str,
                      warnings: list[str] | None = None) -> EditPlan:
    """Turn a validated result into an EditPlan.

    Only operations that survived every validation stage are here. Nothing in
    this function repairs, coerces or guesses: by this point a value is either
    trustworthy or gone.
    """
    executable = [o for o in result.operations if o.type in IMPLEMENTED_OPS]

    plan = EditPlan(
        command=command,
        exports=result.exports,
        summary=result.summary,
        rationale=result.rationale,
        operations=executable,
        unsupported=result.unsupported,
        planner="llm",
        warnings=list(warnings or []),
    )
    plan.warnings += [f"Question: {q}" for q in result.questions]

    heavy = sum(
        1 for o in executable
        if o.type == OpType.ADD_EFFECT
        and o.parameters.get("type") in {"stabilize", "denoise", "glow"}
    )
    plan.estimate = CostEstimate(
        tier="heavy" if heavy or len(executable) > 14 else
             "moderate" if len(executable) > 5 else "light",
        seconds=round(len(executable) * 1.5 + heavy * 8, 1),
    )
    plan.requires_confirmation = (
        len(executable) > 3
        or any(o.type in (OpType.REMOVE_RANGE, OpType.DELETE_CLIP) for o in executable)
    )
    return plan


def build_plan_context(
    document: ProjectDocument,
    assets: list[AssetBrief],
    selection: list[str],
    playhead: float,
    sequence_id: str | None,
    command: str = "",
) -> PlanContext:
    return PlanContext(
        document=document,
        sequence=document.sequence(sequence_id),
        selection=selection,
        playhead=playhead,
        analyses={a.id: (a.analysis or {}) for a in assets},
        asset_names={a.id: a.name for a in assets},
        media=[
            MediaRef(id=a.id, name=a.name, kind=a.kind, duration=a.duration)
            for a in assets
        ],
        raw_command=command,
    )


def plan_edit(
    command: str,
    document: ProjectDocument,
    assets: list[AssetBrief],
    selection: list[str] | None = None,
    playhead: float = 0.0,
    sequence_id: str | None = None,
    history: list[dict] | None = None,
    memory: dict | None = None,
    project_id: str = "",
    mode: str | None = None,
) -> EditPlan:
    """Produce an edit plan, preferring the LLM planner and falling back cleanly."""
    selection = selection or []
    mode = mode or settings.planner_mode
    ctx = build_plan_context(document, assets, selection, playhead,
                             sequence_id, command=command)

    if mode == "heuristic":
        return heuristic.plan(command, ctx)

    router = get_router()
    if not router.has(Capability.REASON):
        fallback = heuristic.plan(command, ctx)
        if not fallback.operations:
            fallback.warnings.append(
                "No language model is configured, so only the offline rule "
                "planner ran and it did not recognise this instruction. Set "
                "MAKMA_ANTHROPIC_API_KEY to enable full natural-language editing."
            )
        else:
            fallback.warnings.append(
                "Planned offline; no language model is configured."
            )
        return fallback

    context = build_context(document, assets, selection, playhead, sequence_id, memory)
    messages: list[dict] = []
    for turn in (history or [])[-8:]:
        if turn.get("role") in ("user", "assistant") and turn.get("content"):
            messages.append({"role": turn["role"], "content": turn["content"][:2000]})
    messages.append({
        "role": "user",
        "content": (
            f"PROJECT CONTEXT\n{json.dumps(context, separators=(',', ':'))}\n\n"
            f"INSTRUCTION\n{command}"
        ),
    })

    try:
        response = router.reason(
            system=system_prompt(), messages=messages, tools=[plan_tool()],
            max_tokens=4096, temperature=0.2, project_id=project_id,
        )
    except NotConfigured as exc:
        fallback = heuristic.plan(command, ctx)
        fallback.warnings.append(str(exc))
        return fallback

    if not response.ok or not isinstance(response.data, dict):
        fallback = heuristic.plan(command, ctx)
        detail = response.error or "the model did not return a plan"
        fallback.warnings.append(
            f"The language model planner failed ({detail}); fell back to the "
            "offline rule planner."
        )
        return fallback

    facts = ProjectFacts.from_document(document, {a.id for a in assets})
    payload = response.data.get("input") or {}
    result = validate_plan(payload, facts)
    cost = response.usage.cost_usd
    warnings: list[str] = []

    # --- repair loop --------------------------------------------------
    # An invalid plan is a correctable mistake, not a crash. The model is told
    # exactly what was wrong and asked to try again; nothing invalid is ever
    # carried forward between rounds.
    attempts = 0
    while not result.ok and attempts < max(0, settings.plan_repair_attempts):
        attempts += 1
        log.info("repairing plan (attempt %d): %s", attempts,
                 "; ".join(p.code for p in result.problems))
        repair_messages = messages + [
            {"role": "assistant",
             "content": json.dumps(payload, separators=(",", ":"))[:6000]},
            {"role": "user", "content": repair_prompt(result)},
        ]
        try:
            retry = router.reason(
                system=system_prompt(), messages=repair_messages,
                tools=[plan_tool()], max_tokens=4096, temperature=0.0,
                project_id=project_id,
            )
        except NotConfigured:
            break
        cost += retry.usage.cost_usd
        if not retry.ok or not isinstance(retry.data, dict):
            break
        payload = retry.data.get("input") or {}
        result = validate_plan(payload, facts)

    if attempts and result.ok:
        warnings.append(
            f"The first plan did not validate; the model corrected it after "
            f"{attempts} {'try' if attempts == 1 else 'tries'}."
        )
    elif not result.ok:
        # Whatever survived validation is still safe to run. What did not is
        # named rather than silently dropped.
        detail = "; ".join(str(p) for p in result.problems[:4])
        if result.operations:
            warnings.append(
                f"Part of the plan was invalid and was dropped ({detail}). "
                "Review the remaining steps before applying them."
            )
        else:
            fallback = heuristic.plan(command, ctx)
            fallback.warnings.append(
                f"The model's plan failed validation ({detail}); used the "
                "offline rule planner instead."
            )
            fallback.estimate.usd = round(cost, 5)
            return fallback

    plan = _plan_from_result(result, command, warnings)
    plan.estimate.usd = round(cost, 5)
    if not result.ok:
        plan.requires_confirmation = True

    if not plan.operations and not plan.unsupported and not plan.exports:
        fallback = heuristic.plan(command, ctx)
        if fallback.operations:
            fallback.warnings.append(
                "The model proposed no executable operations; used the offline "
                "rule planner instead."
            )
            fallback.summary = plan.summary or fallback.summary
            fallback.estimate.usd = round(cost, 5)
            return fallback
    return plan
