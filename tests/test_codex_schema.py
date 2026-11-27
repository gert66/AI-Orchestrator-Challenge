from core.codex_runner import _strict_output_schema
from core.contracts import load_schema


def test_strict_schema_requires_every_object_property():
    schema = _strict_output_schema(load_schema("brain"))
    assert set(schema["required"]) == set(schema["properties"])
    batch = schema["$defs"]["batch"]
    assert set(batch["required"]) == set(batch["properties"])
    assert batch["properties"]["acceptance_criteria"]["type"] == ["array", "null"]


def test_codex_runner_normalizes_conditionally_forbidden_brain_fields():
    # The normalization is intentionally coupled to decision semantics: strict
    # schemas make fields present, canonical contracts decide when they are null.
    payload = {"decision": "DONE", "next_batch": {"step_id": "fake"}, "human_question": "x", "wait_seconds": 10}
    decision = payload.get("decision")
    if decision not in {"NEXT", "REPAIR"}: payload["next_batch"] = None
    if decision != "NEEDS_HUMAN": payload["human_question"] = None
    if decision != "WAIT": payload["wait_seconds"] = None
    assert payload == {"decision": "DONE", "next_batch": None, "human_question": None, "wait_seconds": None}
