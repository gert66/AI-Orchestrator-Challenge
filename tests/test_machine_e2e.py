"""Offline end-to-end run of core.machine.Machine: no network, no provider CLI.

A scripted runner stands in for Brain/Worker/Reviewer and returns the same
ClaudeResult shape a real `--json-schema` call produces (core.machine.fake_result).
Everything else is real: the shipped prompt templates, the repo policy loader,
gitsafe preflight + branch freshness gate against a local bare origin, evidence
collection (a real test command), scope check and the git commit.
"""
import json
import sys

import pytest

from core import gitsafe, machine, repo_policy
from core.machine import Machine, MachineError, create_job, fake_result
from core.state import load_json, read_events, save_json

REPO_KEY = "example/e2e-fixture"
TEST_COMMAND = [sys.executable, "-c", "print('ok')"]


class ScriptedRunner:
    """Answers each role call from a queue; the Worker callback edits the checkout like a real Worker would."""

    def __init__(self, script):
        self.script = list(script)   # (role, payload-or-callable) in call order
        self.calls = []              # (role, prompt, kwargs) actually received from the Machine

    @property
    def roles(self):
        return [role for role, _, _ in self.calls]

    def __call__(self, role, prompt, **kwargs):
        self.calls.append((role, prompt, kwargs))
        assert self.script, f"unexpected extra {role} call"
        expected_role, answer = self.script.pop(0)
        assert role == expected_role, f"Machine called {role}, script expected {expected_role}"
        payload = answer(prompt, kwargs) if callable(answer) else answer
        return fake_result(payload, prompt, kwargs.get("cwd", "."))


def _brain(decision, reason, step_id=None, **extra):
    payload = {"schema_version": 1, "decision": decision, "reason": reason}
    if step_id:
        payload["next_batch"] = {
            "step_id": step_id, "goal": f"{decision}: add hello.py",
            "instructions": "Create hello.py printing hello.", "acceptance_criteria": ["hello.py exists"],
        }
    payload.update(extra)
    return payload


def _worker(step_id, checkout, filename, content):
    def run(prompt, kwargs):
        assert kwargs["cwd"] == str(checkout.work)
        assert kwargs["permission_mode"] == machine.WRITE_PERMISSION
        assert kwargs["worker_mode"] == "write"
        (checkout.work / filename).write_text(content, encoding="utf-8")
        return {"schema_version": 1, "step_id": step_id, "status": "COMPLETED",
                "summary": f"wrote {filename}", "files_changed": [filename], "tests_run": [], "tests_passed": None}
    return run


def _reviewer(step_id, verdict, findings=()):
    return {"schema_version": 1, "step_id": step_id, "verdict": verdict, "scope_ok": True,
            "tests_ok": True, "summary": f"reviewer says {verdict}", "findings": list(findings)}


def _write_policy(path, checkout, **extra):
    """Write a tmp repo policy for the fixture repo; extra keys (e.g. push_job_branch) go into its entry."""
    entry = {"expected_origin": str(checkout.origin), "canonical_base_branch": checkout.BASE_BRANCH, **extra}
    path.write_text(json.dumps({"schema_version": 1, "repos": {REPO_KEY: entry}}), encoding="utf-8")


@pytest.fixture
def job(tmp_path, checkout, monkeypatch):
    """A QUEUED write job pointed at the checkout, with the repo policy and kit side effects sandboxed."""
    policy = tmp_path / "repo_policy.json"
    _write_policy(policy, checkout)  # push_job_branch absent: the default (no push)
    # Machine imports load_repo_policy by name and relies on its default path; route it to the tmp policy.
    monkeypatch.setattr(machine, "load_repo_policy", lambda repo: repo_policy.load_repo_policy(repo, policy))
    monkeypatch.setattr(machine, "REPO_LOCKS_DIR", tmp_path / "locks")            # never write into the kit
    monkeypatch.setattr(machine, "HERMES_ADVISORY_FLAG", tmp_path / "no-hermes")  # advisory stays off
    monkeypatch.setattr(machine, "GEMINI_LEDGER_FILE", tmp_path / "gemini.jsonl")
    job_dir = tmp_path / "jobs" / "e2e-job"
    state = create_job(job_dir, {
        "schema_version": 1, "job_id": "e2e-job", "repo": REPO_KEY, "branch": checkout.JOB_BRANCH,
        "mode": "write", "goal": "Add hello.py to the fixture repository.", "created_at": "2026-01-01T00:00:00Z",
        "max_steps": 2, "max_repairs_per_step": 1,
    }, {"repo_path": str(checkout.work), "origin": str(checkout.origin), "worker_provider": "claude",
        "test_commands": [TEST_COMMAND]})
    assert state["phase"] == "QUEUED"
    return job_dir


def _phases(job_dir):
    return [(e["from"], e["to"]) for e in read_events(job_dir / "events.jsonl")]


def _assert_committed_once(checkout, job_dir, filename, content):
    assert checkout.commits_ahead_of_base() == 1, "exactly one new commit on the job branch"
    assert filename in checkout.head_files()
    assert (checkout.work / filename).read_text(encoding="utf-8") == content
    assert checkout.is_clean()
    assert checkout.rev_parse(checkout.BASE_BRANCH) == checkout.base_sha, "main is untouched"
    # Nothing was pushed: the job branch does not exist on origin and origin/main did not move.
    assert not checkout.origin_has_branch(checkout.JOB_BRANCH)
    assert checkout.origin_sha(checkout.BASE_BRANCH) == checkout.base_sha
    state = load_json(job_dir / "state.json")
    assert state["phase"] == "DONE" and state["step_id"] is None
    history = state["runtime"]["history"]
    assert len(history) == 1
    assert history[0]["outcome"] == "committed" and history[0]["verdict"] == "PASS"
    assert history[0]["paths"] == [filename] and history[0]["commit"] == checkout.rev_parse("HEAD")
    # Push disabled: the history entry carries no push bookkeeping at all (unchanged shape).
    assert not {"pushed", "pushed_to", "push_error"} & set(history[0])
    assert state["runtime"]["batch"] is None and state["runtime"]["inflight"] is None
    return state


def _one_batch_script(step, checkout):
    return [
        ("brain", _brain("NEXT", "one batch is enough", step)),
        ("worker", _worker(step, checkout, "hello.py", "print('hello')\n")),
        ("reviewer", _reviewer(step, "PASS")),
        ("brain", _brain("DONE", "hello.py is committed")),
    ]


def test_machine_runs_queued_to_done_and_commits_without_pushing(job, checkout):
    step = "e2e-01"
    runner = ScriptedRunner([
        ("brain", _brain("NEXT", "one batch is enough", step)),
        ("worker", _worker(step, checkout, "hello.py", "print('hello')\n")),
        ("reviewer", _reviewer(step, "PASS")),
        ("brain", _brain("DONE", "hello.py is committed")),
    ])
    assert Machine(job, runner).run() == "DONE"
    assert runner.script == [], "every scripted call was consumed"
    assert runner.roles == ["brain", "worker", "reviewer", "brain"]

    state = _assert_committed_once(checkout, job, "hello.py", "print('hello')\n")
    assert state["runtime"]["counters"] == {"brain": 2, "batches": 1, "claude_calls": 4}
    assert state["runtime"]["history"][0]["repairs"] == 0
    assert _phases(job) == [
        (None, "QUEUED"), ("QUEUED", "PLANNING"), ("PLANNING", "WORKING"), ("WORKING", "EVIDENCE"),
        ("EVIDENCE", "REVIEWING"), ("REVIEWING", "COMMITTING"), ("COMMITTING", "PLANNING"), ("PLANNING", "DONE"),
    ]
    # Fail-closed write gate ran (and passed) at job start and before the first Worker write.
    freshness = state["runtime"]["branch_freshness_history"]
    assert [f["stage"] for f in freshness] == ["job start", "first Worker write"]
    assert all(f["ok"] and f["behind_by"] == 0 for f in freshness)

    # Every provider output is persisted under steps/ keyed by call id.
    assert sorted(p.name for p in (job / "steps").glob("*.json")) == [
        "brain-001.json", "brain-002.json", f"{step}-reviewer-1.json", f"{step}-worker-1.json",
    ]
    assert load_json(job / "steps" / f"{step}-worker-1.json")["payload"]["files_changed"] == ["hello.py"]

    # Evidence holds the orchestrator's own test run, not the Worker's claim.
    evidence_files = sorted((job / "evidence").glob("*.json"))
    assert [p.name for p in evidence_files] == [f"{step}-evidence-1.json"]
    evidence = load_json(evidence_files[0])
    assert evidence["tests_summary"] == "PASS" and evidence["tests_passed"] is True
    assert [t["argv"] for t in evidence["tests"]] == [TEST_COMMAND]
    assert evidence["tests"][0]["status"] == "PASS" and evidence["tests"][0]["exit_code"] == 0
    assert evidence["tests"][0]["stdout"].strip() == "ok"
    assert [c["path"] for c in evidence["changed_paths"]] == ["hello.py"]
    assert "print('hello')" in evidence["diff"]
    assert evidence["branch"] == checkout.JOB_BRANCH

    # The commit message ties the commit back to the batch and the job.
    message = checkout.head_message()
    assert message.startswith(f"{step}: ") and "Orchestrator job e2e-job" in message

    # The shipped prompt templates were rendered (no monkeypatched prompts dir).
    prompts = sorted(p.name for p in (job / "system_prompts").glob("*.md"))
    assert any(n.startswith("brain-job-") for n in prompts)
    assert any(n.startswith(f"worker-{step}-") for n in prompts)
    assert any(n.startswith(f"reviewer-{step}-") for n in prompts)


def test_reviewer_fail_triggers_repair_then_commit(job, checkout):
    step = "e2e-repair-01"
    finding = {"severity": "MAJOR", "message": "hello.py prints the wrong text", "file": "hello.py"}
    runner = ScriptedRunner([
        ("brain", _brain("NEXT", "first attempt", step)),
        ("worker", _worker(step, checkout, "hello.py", "print('helo')\n")),
        ("reviewer", _reviewer(step, "FAIL", [finding])),
        ("brain", _brain("REPAIR", "fix the typo", step)),        # same step_id: a repair, not a new batch
        ("worker", _worker(step, checkout, "hello.py", "print('hello')\n")),
        ("reviewer", _reviewer(step, "PASS")),
        ("brain", _brain("DONE", "repaired and committed")),
    ])
    assert Machine(job, runner).run() == "DONE"
    assert runner.script == []
    assert runner.roles == ["brain", "worker", "reviewer", "brain", "worker", "reviewer", "brain"]

    state = _assert_committed_once(checkout, job, "hello.py", "print('hello')\n")
    history = state["runtime"]["history"][0]
    assert history["repairs"] == 1 and history["reviews"] == 2 and history["step_id"] == step
    assert state["runtime"]["counters"] == {"brain": 3, "batches": 1, "claude_calls": 7}
    assert state["runtime"]["used_step_ids"] == [step], "a repair reuses the step id"
    assert _phases(job) == [
        (None, "QUEUED"), ("QUEUED", "PLANNING"), ("PLANNING", "WORKING"), ("WORKING", "EVIDENCE"),
        ("EVIDENCE", "REVIEWING"), ("REVIEWING", "PLANNING"),                      # reviewer FAIL
        ("PLANNING", "WORKING"), ("WORKING", "EVIDENCE"), ("EVIDENCE", "REVIEWING"),  # brain REPAIR #1
        ("REVIEWING", "COMMITTING"), ("COMMITTING", "PLANNING"), ("PLANNING", "DONE"),
    ]
    reasons = [e["reason"] for e in read_events(job / "events.jsonl")]
    assert any(r.startswith("brain REPAIR #1") for r in reasons)
    assert any(r.startswith("reviewer FAIL") for r in reasons)

    # The repaired Worker run received the reviewer's findings and was flagged as escalated.
    _, repair_prompt, repair_kwargs = runner.calls[4]
    assert repair_kwargs["worker_escalated"] is True
    assert "## Repair feedback" in repair_prompt and finding["message"] in repair_prompt
    _, first_prompt, first_kwargs = runner.calls[1]
    assert first_kwargs["worker_escalated"] is False and "(first run of this batch)" in first_prompt
    assert sorted(p.name for p in (job / "steps").glob("*.json")) == [
        "brain-001.json", "brain-002.json", "brain-003.json",
        f"{step}-reviewer-1.json", f"{step}-reviewer-2.json", f"{step}-worker-1.json", f"{step}-worker-2.json",
    ]
    assert sorted(p.name for p in (job / "evidence").glob("*.json")) == [
        f"{step}-evidence-1.json", f"{step}-evidence-2.json",
    ]
    second = load_json(job / "evidence" / f"{step}-evidence-2.json")
    assert second["tests_summary"] == "PASS" and "print('hello')" in second["diff"]
    assert load_json(job / "steps" / f"{step}-reviewer-1.json")["payload"]["findings"] == [finding]


# -- policy-gated push -------------------------------------------------------

def test_push_enabled_pushes_job_branch(job, checkout, tmp_path):
    _write_policy(tmp_path / "repo_policy.json", checkout, push_job_branch=True)
    step = "e2e-push-01"
    runner = ScriptedRunner(_one_batch_script(step, checkout))
    assert Machine(job, runner).run() == "DONE"
    assert runner.script == []

    head = checkout.rev_parse("HEAD")
    assert checkout.commits_ahead_of_base() == 1 and checkout.is_clean()
    # Only the job branch reached origin, at exactly the local head; origin/main never moved.
    assert checkout.origin_has_branch(checkout.JOB_BRANCH)
    assert checkout.origin_sha(checkout.JOB_BRANCH) == head
    assert checkout.origin_sha(checkout.BASE_BRANCH) == checkout.base_sha
    assert checkout.rev_parse(checkout.BASE_BRANCH) == checkout.base_sha

    state = load_json(job / "state.json")
    assert state["phase"] == "DONE" and state["step_id"] is None
    history = state["runtime"]["history"]
    assert len(history) == 1
    entry = history[0]
    assert entry["outcome"] == "committed" and entry["commit"] == head
    assert entry["pushed"] is True and entry["pushed_to"] == f"origin/{checkout.JOB_BRANCH}"
    assert "push_error" not in entry
    assert _phases(job) == [
        (None, "QUEUED"), ("QUEUED", "PLANNING"), ("PLANNING", "WORKING"), ("WORKING", "EVIDENCE"),
        ("EVIDENCE", "REVIEWING"), ("REVIEWING", "COMMITTING"), ("COMMITTING", "PLANNING"), ("PLANNING", "DONE"),
    ]
    reasons = [e["reason"] for e in read_events(job / "events.jsonl")]
    assert any(f"pushed to origin/{checkout.JOB_BRANCH}" in r for r in reasons)


def test_push_failure_pauses_job_and_keeps_commit(job, checkout, tmp_path, monkeypatch):
    _write_policy(tmp_path / "repo_policy.json", checkout, push_job_branch=True)
    step = "e2e-push-fail-01"
    real_push = gitsafe.push_branch
    seen = []

    def failing_push(repo, branch, remote="origin"):
        # Deterministic remote failure after preflight passed: the real helper
        # runs, but origin now points at a directory that does not exist.
        seen.append((str(repo), branch, remote))
        broken = gitsafe.run_git(checkout.work, ["remote", "set-url", "origin", str(tmp_path / "missing-remote.git")])
        assert broken.ok, broken.stderr
        return real_push(repo, branch, remote)

    monkeypatch.setattr(gitsafe, "push_branch", failing_push)
    runner = ScriptedRunner(_one_batch_script(step, checkout))
    assert Machine(job, runner).run() == "NEEDS_HUMAN"
    assert seen == [(str(checkout.work), checkout.JOB_BRANCH, "origin")]
    assert [r for r, _, _ in runner.calls] == ["brain", "worker", "reviewer"], "the machine paused before the next Brain call"

    # The local commit exists and is intact; nothing reached the (original) origin.
    head = checkout.rev_parse("HEAD")
    assert checkout.commits_ahead_of_base() == 1 and "hello.py" in checkout.head_files() and checkout.is_clean()
    assert not checkout.origin_has_branch(checkout.JOB_BRANCH)
    assert checkout.origin_sha(checkout.BASE_BRANCH) == checkout.base_sha

    state = load_json(job / "state.json")
    assert state["phase"] == "NEEDS_HUMAN" and state["step_id"] is None
    assert "push" in state["human_question"].lower() and step in state["human_question"]
    assert state["runtime"]["retry_phase"] == "PLANNING" and state["runtime"]["batch"] is None
    history = state["runtime"]["history"]
    assert len(history) == 1
    entry = history[0]
    assert entry["outcome"] == "committed" and entry["commit"] == head and entry["paths"] == ["hello.py"]
    assert entry["pushed"] is False
    assert isinstance(entry["push_error"], str) and entry["push_error"] and len(entry["push_error"]) <= 500
    assert "pushed_to" not in entry
    assert _phases(job)[-1] == ("COMMITTING", "NEEDS_HUMAN")


def test_done_is_terminal_against_stale_recovery_transition(job, checkout):
    step = "terminal-01"
    runner = ScriptedRunner([
        ("brain", _brain("NEXT", "one batch", step)),
        ("worker", _worker(step, checkout, "hello.py", "print('terminal')\n")),
        ("reviewer", _reviewer(step, "PASS")),
        ("brain", _brain("DONE", "complete")),
    ])
    machine_instance = Machine(job, runner)
    assert machine_instance.run() == "DONE"
    before = load_json(job / "state.json")
    with pytest.raises(MachineError, match="DONE is terminal"):
        machine_instance._transition("REVIEWING", "stale recovery action")
    after = load_json(job / "state.json")
    assert after["phase"] == "DONE"
    assert after["updated_at"] == before["updated_at"]


def test_audited_terminal_recovery_repairs_only_proven_completed_job(job, checkout):
    step = "recover-done-01"
    runner = ScriptedRunner([
        ("brain", _brain("NEXT", "one batch", step)),
        ("worker", _worker(step, checkout, "hello.py", "print('recover')\n")),
        ("reviewer", _reviewer(step, "PASS")),
        ("brain", _brain("DONE", "complete")),
    ])
    m = Machine(job, runner)
    assert m.run() == "DONE"
    state = load_json(job / "state.json")
    state["phase"] = "ERROR"
    state["last_error"] = "stale administrative transition"
    save_json(job / "state.json", state)
    assert Machine(job, runner).recover_completed_state("test audited recovery") == "DONE"
    recovered = load_json(job / "state.json")
    assert recovered["phase"] == "DONE" and recovered["last_error"] is None
    assert read_events(job / "events.jsonl")[-1]["reason"] == "test audited recovery"


def test_audited_terminal_recovery_fails_closed_when_head_no_longer_matches(job, checkout):
    step = "recover-refuse-01"
    runner = ScriptedRunner([
        ("brain", _brain("NEXT", "one batch", step)),
        ("worker", _worker(step, checkout, "hello.py", "print('recover')\n")),
        ("reviewer", _reviewer(step, "PASS")),
        ("brain", _brain("DONE", "complete")),
    ])
    m = Machine(job, runner)
    assert m.run() == "DONE"
    state = load_json(job / "state.json")
    state["phase"] = "ERROR"
    save_json(job / "state.json", state)
    gitsafe.run_git(checkout.work, ["commit", "--allow-empty", "--quiet", "-m", "foreign commit"])
    with pytest.raises(MachineError, match="recorded commit"):
        Machine(job, runner).recover_completed_state()
    assert load_json(job / "state.json")["phase"] == "ERROR"
