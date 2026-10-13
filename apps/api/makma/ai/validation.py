"""Plan validation.

Everything a language model produces passes through here before it can touch
a project. The model is not trusted: not to name a real clip, not to stay in
range, not to return valid JSON, not to avoid a filesystem path where a number
belongs. Validation is the boundary that makes "the model plans, engines
execute" true rather than aspirational.

The pipeline, in order, because each stage assumes the previous one passed:

    raw text
      → JSON parse
      → operation shape
      → capability check      is this operation implemented?
      → reference validation  do these ids exist in this project?
      → range validation      are the numbers finite and in bounds?
      → constraint check      does the result make sense as an edit?

A failure is never an exception thrown at the user. It is a list of specific,
correctable problems, which is what makes an automatic repair round possible.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from ..render.effects import REGISTRY
from ..schema.core import ANIMATABLE_TRANSFORM, ProjectDocument, TransitionType
from ..schema.operations import (
    IMPLEMENTED_OPS,
    EditOperation,
    ExportRequest,
    OpType,
    Scope,
)

#: Numbers beyond this are always a mistake, whatever the field.
MAX_MAGNITUDE = 1e6

#: Parameters that carry free text the user will see. Everything else that
#: arrives as a string must match a known vocabulary.
FREE_TEXT_PARAMS = {"text", "name", "label", "font_family", "color",
                    "stroke_color", "background", "description"}

#: Shapes that mean a parameter is trying to be something other than a value:
#: a path, a shell command, or a filtergraph fragment.
_INJECTION = re.compile(
    r"(^|[^A-Za-z0-9])(\.\./|\.\.\\)"          # traversal
    r"|^[A-Za-z]:[\\/]"                        # windows absolute path
    r"|^/(etc|proc|sys|root|home|var|usr)\b"   # unix system path
    r"|^\\\\"                                  # UNC path
    r"|[;&|`$]"                                # shell metacharacters
    r"|\b(?:lut3d|drawtext|geq|movie|amovie|concat)\s*=",  # filtergraph
    re.IGNORECASE,
)

#: Per-parameter numeric bounds. Anything not listed only has to be finite and
#: under MAX_MAGNITUDE; these are the ones where a wrong value is destructive.
BOUNDS: dict[str, tuple[float, float]] = {
    "speed": (0.01, 100.0),
    "opacity": (0.0, 1.0),
    "volume": (0.0, 8.0),
    "gain_db": (-90.0, 30.0),
    "scale": (0.001, 100.0),
    "rotation": (-3600.0, 3600.0),
    "duration": (0.001, 24 * 3600.0),
    "start": (0.0, 24 * 3600.0),
    "end": (0.0, 24 * 3600.0),
    "time": (0.0, 24 * 3600.0),
    "in_point": (0.0, 24 * 3600.0),
    "fps": (1.0, 480.0),
    "width": (16, 16384),
    "height": (16, 16384),
    "font_size": (1, 2000),
    "crop_left": (0.0, 0.99),
    "crop_right": (0.0, 0.99),
    "crop_top": (0.0, 0.99),
    "crop_bottom": (0.0, 0.99),
    "intensity": (0.0, 1.0),
    "track_index": (0, 64),
}


@dataclass
class Problem:
    """One specific, correctable thing wrong with a plan."""
    code: str
    message: str
    operation_index: int | None = None
    operation_type: str | None = None

    def __str__(self) -> str:
        where = (f"operation {self.operation_index + 1}"
                 if self.operation_index is not None else "plan")
        return f"{where}: {self.message}"


@dataclass
class ValidationResult:
    operations: list[EditOperation] = field(default_factory=list)
    exports: list[ExportRequest] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    #: Operations the model asked for that this build cannot perform. Not a
    #: failure -- reported to the user rather than repaired.
    unsupported: list[str] = field(default_factory=list)
    summary: str = ""
    rationale: str = ""
    questions: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def repair_brief(self) -> str:
        """What to tell the model so it can correct itself."""
        lines = [str(p) for p in self.problems]
        return "\n".join(f"- {line}" for line in lines)


@dataclass
class ProjectFacts:
    """The ids and limits a plan is allowed to reference."""
    clip_ids: set[str] = field(default_factory=set)
    track_ids: set[str] = field(default_factory=set)
    sequence_ids: set[str] = field(default_factory=set)
    asset_ids: set[str] = field(default_factory=set)
    duration: float = 0.0

    @classmethod
    def from_document(cls, document: ProjectDocument,
                      asset_ids: set[str] | None = None) -> "ProjectFacts":
        facts = cls(asset_ids=set(asset_ids or ()))
        for sequence in document.sequences:
            facts.sequence_ids.add(sequence.id)
            facts.duration = max(facts.duration, sequence.duration)
            for track in sequence.tracks:
                facts.track_ids.add(track.id)
                for clip in track.clips:
                    facts.clip_ids.add(clip.id)
        return facts


# --------------------------------------------------------------------------
# Stage 1 — parse
# --------------------------------------------------------------------------

def parse_payload(raw: Any) -> tuple[dict | None, Problem | None]:
    """Accept a dict, or JSON text, including text wrapped in a code fence."""
    if isinstance(raw, dict):
        return raw, None
    if not isinstance(raw, str) or not raw.strip():
        return None, Problem(
            "empty_response",
            "No plan was returned. Reply by calling the tool with a plan.",
        )

    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    # A model sometimes wraps the object in prose; take the outermost braces.
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, Problem(
            "invalid_json",
            f"The response was not valid JSON ({exc.msg} at position "
            f"{exc.pos}). Return a single JSON object and nothing else.",
        )
    if not isinstance(parsed, dict):
        return None, Problem(
            "not_an_object",
            f"Expected a JSON object, got {type(parsed).__name__}.",
        )
    return parsed, None


# --------------------------------------------------------------------------
# Stage 2-5 — per-operation validation
# --------------------------------------------------------------------------

def _finite(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return math.isfinite(value) and abs(value) <= MAX_MAGNITUDE
    return True


def _check_value(name: str, value: Any, index: int, op_type: str,
                 problems: list[Problem]) -> None:
    """Recursively check one parameter value for finiteness, bounds and shape."""
    if isinstance(value, dict):
        for key, inner in value.items():
            _check_value(key, inner, index, op_type, problems)
        return
    if isinstance(value, list):
        if len(value) > 5000:
            problems.append(Problem(
                "list_too_long",
                f"'{name}' has {len(value)} entries; that is not a plausible "
                "edit. Keep lists under 5000 items.",
                index, op_type,
            ))
            return
        for inner in value:
            _check_value(name, inner, index, op_type, problems)
        return

    if isinstance(value, float) and not math.isfinite(value):
        problems.append(Problem(
            "not_finite",
            f"'{name}' is {value}. Every number must be finite.",
            index, op_type,
        ))
        return

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if abs(value) > MAX_MAGNITUDE:
            problems.append(Problem(
                "out_of_range",
                f"'{name}' is {value}, which is far outside any sensible "
                f"value. Keep it under {MAX_MAGNITUDE:g}.",
                index, op_type,
            ))
            return
        bounds = BOUNDS.get(name)
        if bounds and not (bounds[0] <= value <= bounds[1]):
            problems.append(Problem(
                "out_of_range",
                f"'{name}' is {value}; it must be between {bounds[0]:g} and "
                f"{bounds[1]:g}.",
                index, op_type,
            ))
        return

    if isinstance(value, str):
        if name in FREE_TEXT_PARAMS:
            if len(value) > 5000:
                problems.append(Problem(
                    "string_too_long",
                    f"'{name}' is {len(value)} characters. Keep it under 5000.",
                    index, op_type,
                ))
            return
        if len(value) > 500:
            problems.append(Problem(
                "string_too_long",
                f"'{name}' is {len(value)} characters, which is not a value "
                "this parameter takes.",
                index, op_type,
            ))
            return
        if _INJECTION.search(value):
            # This is the line between "a wrong value" and "an attempt to be
            # something other than a value".
            problems.append(Problem(
                "unsafe_value",
                f"'{name}' looks like a file path, shell command or filter "
                "expression rather than a value. Parameters name project "
                "objects and settings, never files or commands.",
                index, op_type,
            ))


def validate_operation(raw: Any, index: int, facts: ProjectFacts,
                       problems: list[Problem]) -> EditOperation | None:
    """One operation through capability, reference and range checks."""
    if not isinstance(raw, dict):
        problems.append(Problem(
            "malformed_operation",
            f"Expected an object, got {type(raw).__name__}.", index,
        ))
        return None

    before = len(problems)
    raw_type = raw.get("type")
    if not isinstance(raw_type, str) or not raw_type:
        problems.append(Problem(
            "missing_type", "No 'type'. Every operation needs one.", index,
        ))
        return None

    # --- capability ---------------------------------------------------
    try:
        op_type = OpType(raw_type)
    except ValueError:
        problems.append(Problem(
            "unknown_operation",
            f"'{raw_type}' is not an operation. Use one of: "
            + ", ".join(sorted(o.value for o in IMPLEMENTED_OPS)),
            index, raw_type,
        ))
        return None

    parameters = raw.get("parameters") or {}
    if not isinstance(parameters, dict):
        problems.append(Problem(
            "malformed_parameters",
            f"'parameters' must be an object, got {type(parameters).__name__}.",
            index, raw_type,
        ))
        return None

    # --- shape --------------------------------------------------------
    scope_raw = raw.get("scope") or {}
    if not isinstance(scope_raw, dict):
        problems.append(Problem(
            "malformed_scope",
            f"'scope' must be an object, got {type(scope_raw).__name__}.",
            index, raw_type,
        ))
        return None
    try:
        scope = Scope(**scope_raw)
    except ValidationError as exc:
        problems.append(Problem(
            "malformed_scope", f"'scope' is invalid: {exc.errors()[0]['msg']}.",
            index, raw_type,
        ))
        return None

    # --- references ---------------------------------------------------
    for clip_id in scope.clip_ids:
        if clip_id not in facts.clip_ids:
            problems.append(Problem(
                "unknown_clip",
                f"clip '{clip_id}' is not in this project. Use a clip id from "
                "the project context.",
                index, raw_type,
            ))
    for track_id in scope.track_ids:
        if track_id not in facts.track_ids:
            problems.append(Problem(
                "unknown_track",
                f"track '{track_id}' is not in this project.", index, raw_type,
            ))

    asset_id = parameters.get("asset_id")
    if asset_id is not None:
        if not isinstance(asset_id, str) or asset_id not in facts.asset_ids:
            problems.append(Problem(
                "unknown_asset",
                f"asset '{asset_id}' is not in this project's media. Use an "
                "asset id from the context.",
                index, raw_type,
            ))

    # --- ranges -------------------------------------------------------
    for name, value in parameters.items():
        _check_value(name, value, index, raw_type, problems)
    for name in ("start", "end"):
        value = getattr(scope, name)
        if value is not None:
            _check_value(name, value, index, raw_type, problems)

    # --- per-operation constraints -------------------------------------
    _check_constraints(op_type, parameters, scope, index, facts, problems)

    if len(problems) > before:
        # An operation with any problem is dropped whole. Applying the safe
        # half of a bad instruction is worse than applying none of it.
        return None

    try:
        return EditOperation(
            type=op_type, scope=scope, parameters=parameters,
            description=str(raw.get("description", ""))[:200],
        )
    except ValidationError as exc:
        problems.append(Problem(
            "malformed_operation", f"{exc.errors()[0]['msg']}.", index, raw_type,
        ))
        return None


def _check_constraints(op_type: OpType, parameters: dict, scope: Scope,
                       index: int, facts: ProjectFacts,
                       problems: list[Problem]) -> None:
    """Things that are individually in range but wrong together."""
    add = lambda code, msg: problems.append(  # noqa: E731
        Problem(code, msg, index, op_type.value)
    )

    start, end = parameters.get("start"), parameters.get("end")
    if isinstance(start, (int, float)) and isinstance(end, (int, float)):
        if end <= start:
            add("backwards_range",
                f"end ({end}) must be after start ({start}).")

    if scope.start is not None and scope.end is not None:
        if scope.end <= scope.start:
            add("backwards_range",
                f"scope end ({scope.end}) must be after start ({scope.start}).")

    if op_type == OpType.REMOVE_RANGE:
        if start is None or end is None:
            add("missing_range", "remove_range needs both 'start' and 'end'.")

    if op_type == OpType.ADD_CLIP and not parameters.get("asset_id"):
        add("missing_asset", "add_clip needs an 'asset_id'.")

    if op_type == OpType.ADD_TEXT:
        text = parameters.get("text")
        if not isinstance(text, str) or not text.strip():
            add("missing_text", "add_text needs non-empty 'text'.")

    if op_type in (OpType.ADD_EFFECT, OpType.UPDATE_EFFECT):
        effect = parameters.get("type") or parameters.get("effect")
        if op_type == OpType.ADD_EFFECT and not effect:
            add("missing_effect_type", "add_effect needs a 'type'.")
        elif effect is not None and effect not in REGISTRY:
            add("unknown_effect",
                f"'{effect}' is not an effect. Available: "
                + ", ".join(sorted(REGISTRY)))
        if effect == "lut":
            inner = parameters.get("parameters") or {}
            if isinstance(inner, dict) and "path" in inner:
                add("unsafe_value",
                    "a LUT is named by 'asset_id', never by a file path.")

    if op_type == OpType.SET_TRANSITION:
        name = parameters.get("transition", parameters.get("type"))
        if name is not None:
            try:
                TransitionType(str(name))
            except ValueError:
                add("unknown_transition",
                    f"'{name}' is not a transition. Options: "
                    + ", ".join(t.value for t in TransitionType))

    if op_type == OpType.SET_KEYFRAMES:
        prop = parameters.get("property") or parameters.get("name")
        target = parameters.get("target", "transform")
        keyframes = parameters.get("keyframes")
        if not prop:
            add("missing_property", "set_keyframes needs a 'property'.")
        elif target == "transform" and keyframes and prop not in ANIMATABLE_TRANSFORM:
            add("not_animatable",
                f"'{prop}' cannot be animated. Animatable transform "
                f"properties: {', '.join(sorted(ANIMATABLE_TRANSFORM))}. "
                "For a moving zoom use add_effect type=\"zoom\".")
        if keyframes is not None and not isinstance(keyframes, list):
            add("malformed_keyframes", "'keyframes' must be a list.")


# --------------------------------------------------------------------------
# Whole-plan validation
# --------------------------------------------------------------------------

def validate_plan(raw: Any, facts: ProjectFacts) -> ValidationResult:
    """Run a model's response through the whole pipeline."""
    result = ValidationResult()

    payload, problem = parse_payload(raw)
    if problem is not None:
        result.problems.append(problem)
        return result
    assert payload is not None

    result.summary = str(payload.get("summary", ""))[:600]
    result.rationale = str(payload.get("rationale", ""))[:900]
    questions = payload.get("questions")
    if isinstance(questions, list):
        result.questions = [str(q)[:300] for q in questions[:3]]

    raw_operations = payload.get("operations")
    if raw_operations is None:
        result.problems.append(Problem(
            "missing_operations",
            "No 'operations' array. Return one, empty if there is nothing to do.",
        ))
        return result
    if not isinstance(raw_operations, list):
        result.problems.append(Problem(
            "missing_operations",
            f"'operations' must be an array, got {type(raw_operations).__name__}.",
        ))
        return result
    if len(raw_operations) > 500:
        result.problems.append(Problem(
            "too_many_operations",
            f"{len(raw_operations)} operations is not a plausible single edit. "
            "Keep a plan under 500.",
        ))
        return result

    for index, raw_op in enumerate(raw_operations):
        # An operation this build cannot perform is reported, not repaired:
        # asking the model again will not make segmentation exist.
        if isinstance(raw_op, dict) and isinstance(raw_op.get("type"), str):
            try:
                declared = OpType(raw_op["type"])
            except ValueError:
                declared = None
            if declared is not None and declared not in IMPLEMENTED_OPS:
                result.unsupported.append(declared.value)
                continue

        operation = validate_operation(raw_op, index, facts, result.problems)
        if operation is not None:
            result.operations.append(operation)

    for raw_export in (payload.get("exports") or [])[:4]:
        if not isinstance(raw_export, dict):
            continue
        try:
            result.exports.append(ExportRequest(**raw_export))
        except ValidationError as exc:
            result.problems.append(Problem(
                "malformed_export",
                f"export request is invalid: {exc.errors()[0]['msg']}.",
            ))

    result.unsupported = sorted(set(result.unsupported))
    return result


# --------------------------------------------------------------------------
# Safety-only check for client-supplied operations
# --------------------------------------------------------------------------

def safety_problems(operations: list[EditOperation]) -> list[Problem]:
    """The subset of validation that applies to a trusted-but-not-trusted client.

    The editor is signed in and owns the project, so it is allowed to make
    edits the planner would never propose. It is still not allowed to send a
    NaN, an absurd number, or a value shaped like a file path or a shell
    command -- those are never a legitimate edit, and they are exactly what an
    attacker would try if they could reach this endpoint.
    """
    problems: list[Problem] = []
    for index, operation in enumerate(operations):
        for name, value in (operation.parameters or {}).items():
            _check_value(name, value, index, operation.type.value, problems)
        if operation.type == OpType.ADD_EFFECT:
            inner = (operation.parameters or {}).get("parameters")
            if (operation.parameters or {}).get("type") == "lut" and \
                    isinstance(inner, dict) and "path" in inner:
                problems.append(Problem(
                    "unsafe_value",
                    "a LUT is named by 'asset_id', never by a file path.",
                    index, operation.type.value,
                ))
    return problems
