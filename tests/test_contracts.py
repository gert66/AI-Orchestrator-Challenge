"""One valid and one invalid example per contract in core.contracts, plus the repo policy loader."""
import json

import pytest

from core import contracts
from core.contracts import ContractError

TS = "2026-09-19T00:00:00Z"

VALID = {
    "brain": {
        "schema_version": 1,
        "decision": "NEXT",
        "reason": "start",
        "next_batch": {"step_id": "01-first", "goal": "g", "instructions": "i"},
    },
    "worker": {
        "schema_version": 1,
        "step_id": "01-first",
        "status": "COMPLETED",
        "summary": "done",
        "files_changed": ["a.py"],
        "tests_run": ["pytest"],
        "tests_passed": True,
    },
    "reviewer": {
        "schema_version": 1,
        "step_id": "01-first",
        "verdict": "PASS",
        "scope_ok": True,
        "tests_ok": True,
        "summary": "ok",
        "findings": [],
    },
    "job": {
        "schema_version": 1,
        "job_id": "job-1",
        "repo": "/tmp/repo",
        "branch": "feature/x",
        "mode": "write",
        "goal": "build",
        "created_at": TS,
    },
    "state": {
        "schema_version": 1,
        "job_id": "job-1",
        "phase": "QUEUED",
        "step_id": None,
        "attempt": 0,
        "created_at": TS,
        "updated_at": TS,
        "last_error": None,
    },
}

# (contract, mutation applied to a copy of the valid example)
INVALID = [
    ("brain", lambda d: d.update(next_batch=None)),          # NEXT requires next_batch
    ("worker", lambda d: d.update(status="MAYBE")),          # not in enum
    ("reviewer", lambda d: d.pop("findings")),               # required key missing
    ("job", lambda d: d.update(mode="WRITE")),               # modes are lowercase
    ("state", lambda d: d.update(phase="SLEEPING")),         # not a phase
]


@pytest.mark.parametrize("name", sorted(VALID))
def test_valid_examples_pass(name):
    out = contracts.validate(name, VALID[name])
    assert out is not VALID[name]  # returns a copy
    assert out["schema_version"] == contracts.SCHEMA_VERSION


@pytest.mark.parametrize("name,mutate", INVALID, ids=[n for n, _ in INVALID])
def test_invalid_examples_fail(name, mutate):
    bad = dict(VALID[name])
    mutate(bad)
    with pytest.raises(ContractError):
        contracts.validate(name, bad)


@pytest.mark.parametrize("name", sorted(VALID))
def test_unknown_key_rejected(name):
    bad = dict(VALID[name], unexpected=1)
    with pytest.raises(ContractError):
        contracts.validate(name, bad)


def test_reviewer_pass_is_degraded_on_blocker():
    obj = dict(VALID["reviewer"], findings=[{"severity": "BLOCKER", "message": "nope"}])
    out = contracts.validate_reviewer(obj)
    assert out["verdict"] == "FAIL"
    assert out["verdict_degraded"] is True
    assert out["degrade_reasons"]


def test_unknown_contract_name():
    with pytest.raises(ContractError):
        contracts.validate("nope", {})


@pytest.mark.parametrize("raw,expected", [
    ({}, False),                          # key absent: today's no-push behaviour
    ({"push_job_branch": False}, False),
    ({"push_job_branch": True}, True),
], ids=["absent", "false", "true"])
def test_repo_policy_push_job_branch(tmp_path, raw, expected):
    from core.repo_policy import load_repo_policy
    entry = {"expected_origin": "https://github.com/owner/name.git", "canonical_base_branch": "main", **raw}
    path = tmp_path / "repo_policy.json"
    path.write_text(json.dumps({"schema_version": 1, "repos": {"owner/name": entry}}), encoding="utf-8")
    policy = load_repo_policy("owner/name", path)
    assert policy["push_job_branch"] is expected
    assert policy["allow_write_on_canonical_base"] is False
