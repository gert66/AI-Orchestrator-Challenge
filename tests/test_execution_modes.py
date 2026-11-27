"""Execution modes (production / standard / autonomous_sandbox), fully offline.

Proves, against the real Machine with a scripted runner and a throwaway git
checkout, that:

- the mode is validated, persisted in state.json, defaulted for old jobs and
  exposed by Machine.status(), the Brain/Worker prompts and the supervisor;
- soft gates (caps, ordinary Brain/Reviewer questions, a non-converging
  repair) pause in production/standard and are auto-resolved under
  autonomous_sandbox, with an audit trail in runtime.auto_resolved;
- hard gates (credential/spending/production/destructive/git-safety
  questions, branch freshness, git HEAD/commit safety, evidence-harness
  trust, AUTH/BUDGET provider failures) pause in every mode;
- autonomous_sandbox never replays the same automatic decision.
"""
import json
import sys

import pytest

from core import machine, repo_policy, submit_job
from core.claude_runner import ClaudeResult
from core.machine import (
    AUTONOMOUS_SANDBOX, DEFAULT_EXECUTION_MODE, EXECUTION_MODES, EXECUTION_POLICY, Machine, MachineError,
    create_job, fake_result, hard_gate_reason, new_runtime,
)
from core.state import load_json, read_events, save_json
from core.supervisor import supervise

REPO_KEY = "example/e2e-fixture"
TEST_COMMAND = [sys.executable, "-c", "print('ok')"]
JOB = {
    "schema_version": 1, "job_id": "mode-job", "repo": REPO_KEY, "mode": "write",
    "goal": "Add greeting files to the fixture repository.", "created_at": "2026-01-01T00:00:00Z",
}


# -- scripted roles ------------------------------------------------------------

class ScriptedRunner:
    """Answers each role call from a queue; an answer may be a payload, a callable or a raw ClaudeResult."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    @property
    def roles(self):
        return [role for role, _, _ in self.calls]

    def __call__(self, role, prompt, **kwargs):
        self.calls.append((role, prompt, kwargs))
        assert self.script, f"unexpected extra {role} call"
        expected, answer = self.script.pop(0)
        assert role == expected, f"Machine called {role}, script expected {expected}"
        if callable(answer):
            answer = answer(prompt, kwargs)
        if isinstance(answer, ClaudeResult):
            return answer
        return fake_result(answer, prompt, kwargs.get("cwd", "."))


def brain(decision, reason="scripted", step_id=None, **extra):
    payload = {"schema_version": 1, "decision": decision, "reason": reason}
    if step_id:
        payload["next_batch"] = {"step_id": step_id, "goal": f"{decision}: write {step_id}",
                                 "instructions": f"Create {step_id}.py.", "acceptance_criteria": [f"{step_id}.py exists"]}
    payload.update(extra)
    return payload


def worker(step_id, checkout, filename, content, after=None):
    def run(prompt, kwargs):
        (checkout.work / filename).write_text(content, encoding="utf-8")
        if after:
            after()
        return {"schema_version": 1, "step_id": step_id, "status": "COMPLETED", "summary": f"wrote {filename}",
                "files_changed": [filename], "tests_run": [], "tests_passed": None}
    return run


def reviewer(step_id, verdict="PASS", human_question=None):
    payload = {"schema_version": 1, "step_id": step_id, "verdict": verdict, "scope_ok": True, "tests_ok": True,
               "summary": f"reviewer says {verdict}", "findings": []}
    if human_question:
        payload["human_question"] = human_question
    return payload


def failed_call(stderr, subtype=None):
    """A ClaudeResult classify() turns into AUTH/BUDGET/... from its text or JSON subtype."""
    top = {"type": "result", "subtype": subtype or "error_during_execution", "is_error": True}
    return ClaudeResult(argv=["fake-claude"], cwd=".", exit_code=1, stdout=json.dumps(top), stderr=stderr,
                        duration_seconds=0.0, timed_out=False, structured=True, schema_requested=True,
                        parsed_json=top, prompt_chars=0)


def one_batch(step, checkout, filename="hello.py"):
    return [("brain", brain("NEXT", "start", step)), ("worker", worker(step, checkout, filename, f"# {step}\n")),
            ("reviewer", reviewer(step))]


# -- fixtures --------------------------------------------------------------------

@pytest.fixture
def sandbox(tmp_path, checkout, monkeypatch):
    """Route every kit side effect into tmp_path and return a job factory."""
    policy = tmp_path / "repo_policy.json"
    policy.write_text(json.dumps({"schema_version": 1, "repos": {REPO_KEY: {
        "expected_origin": str(checkout.origin), "canonical_base_branch": checkout.BASE_BRANCH}}}), encoding="utf-8")
    monkeypatch.setattr(machine, "load_repo_policy", lambda repo: repo_policy.load_repo_policy(repo, policy))
    monkeypatch.setattr(machine, "REPO_LOCKS_DIR", tmp_path / "locks")
    monkeypatch.setattr(machine, "HERMES_ADVISORY_FLAG", tmp_path / "no-hermes")
    monkeypatch.setattr(machine, "GEMINI_LEDGER_FILE", tmp_path / "gemini.jsonl")

    def make(execution_mode=None, job_id="mode-job", test_commands=None, **job_fields):
        job = dict(JOB, job_id=job_id, branch=checkout.JOB_BRANCH, **job_fields)
        config = {"repo_path": str(checkout.work), "origin": str(checkout.origin), "worker_provider": "claude",
                  "test_commands": [TEST_COMMAND] if test_commands is None else test_commands}
        if execution_mode is not None:
            config["execution_mode"] = execution_mode
        job_dir = tmp_path / "jobs" / job_id
        assert create_job(job_dir, job, config)["phase"] == "QUEUED"
        return job_dir

    make.policy = policy
    return make


def _state(job_dir):
    return load_json(job_dir / "state.json")


def _reasons(job_dir):
    return [e["reason"] for e in read_events(job_dir / "events.jsonl")]


def _config(job_dir):
    return _state(job_dir)["runtime"]["config"]


# -- validation, persistence, exposure -----------------------------------------------

def test_default_mode_is_standard_and_persisted(sandbox):
    job_dir = sandbox()
    assert _config(job_dir)["execution_mode"] == "standard" == DEFAULT_EXECUTION_MODE
    status = Machine(job_dir, ScriptedRunner([])).status()
    assert status["execution_mode"] == "standard"
    assert status["auto_resolved"] == [] and status["human"] is None


@pytest.mark.parametrize("mode", EXECUTION_MODES)
def test_every_mode_round_trips_through_state_json(sandbox, mode):
    job_dir = sandbox(mode, job_id=f"job-{mode}")
    assert _config(job_dir)["execution_mode"] == mode
    assert Machine(job_dir, ScriptedRunner([])).status()["execution_mode"] == mode


def test_unknown_mode_is_refused_at_creation_and_reload(sandbox):
    with pytest.raises(MachineError, match="execution_mode"):
        new_runtime(dict(JOB, branch="job/x"), {"repo_path": "/tmp/x", "execution_mode": "yolo"})
    job_dir = sandbox()
    state = _state(job_dir)
    state["runtime"]["config"]["execution_mode"] = "yolo"
    save_json(job_dir / "state.json", state)
    with pytest.raises(MachineError, match="unknown execution_mode"):
        Machine(job_dir, ScriptedRunner([]))


def test_job_created_before_execution_modes_reloads_as_standard(sandbox):
    job_dir = sandbox()
    state = _state(job_dir)
    del state["runtime"]["config"]["execution_mode"]
    del state["runtime"]["auto_resolved"]
    save_json(job_dir / "state.json", state)
    m = Machine(job_dir, ScriptedRunner([]))
    assert m.status()["execution_mode"] == "standard" and m.status()["auto_resolved"] == []


def test_submit_job_flag_persists_mode_and_defaults_to_standard(tmp_path, checkout, capsys):
    policy = tmp_path / "repo_policy.json"
    policy.write_text(json.dumps({"schema_version": 1, "repos": {REPO_KEY: {
        "expected_origin": str(checkout.origin), "canonical_base_branch": checkout.BASE_BRANCH}}}), encoding="utf-8")

    def argv(job_id, *extra):
        return ["--job-id", job_id, "--repo", REPO_KEY, "--repo-path", str(checkout.work), "--branch",
                checkout.JOB_BRANCH, "--mode", "write", "--goal", "g", "--jobs-dir", str(tmp_path / "jobs"),
                "--policy-file", str(policy), "--worker-provider", "claude", *extra]

    assert submit_job.main(argv("plain")) == 0
    assert _config(tmp_path / "jobs" / "plain")["execution_mode"] == "standard"
    assert submit_job.main(argv("auto", "--execution-mode", "autonomous_sandbox")) == 0
    assert _config(tmp_path / "jobs" / "auto")["execution_mode"] == "autonomous_sandbox"
    with pytest.raises(SystemExit) as exc:
        submit_job.main(argv("bad", "--execution-mode", "yolo"))
    assert exc.value.code == 2 and "invalid choice" in capsys.readouterr().err


def test_prompts_and_supervisor_expose_the_mode(sandbox, checkout):
    job_dir = sandbox(AUTONOMOUS_SANDBOX)
    runner = ScriptedRunner(one_batch("exp-01", checkout) + [("brain", brain("DONE"))])
    logs = []
    assert supervise(job_dir, runner=runner, sleep=lambda s: None, log=logs.append, max_cycles=3) == "DONE"
    assert logs[0] == "execution mode autonomous_sandbox; phase QUEUED"
    _, brain_prompt, _ = runner.calls[0]
    context = json.loads(brain_prompt.split("```json", 1)[1].split("```", 1)[0])
    assert context["execution_mode"] == AUTONOMOUS_SANDBOX
    assert context["execution_policy"] == EXECUTION_POLICY[AUTONOMOUS_SANDBOX]
    assert context["auto_resolved"] == []
    system_prompts = list((job_dir / "system_prompts").glob("worker-exp-01-*.md"))
    assert len(system_prompts) == 1
    worker_text = system_prompts[0].read_text(encoding="utf-8")
    assert "Execution mode `autonomous_sandbox`: " + EXECUTION_POLICY[AUTONOMOUS_SANDBOX] in worker_text
    assert "$execution_policy" not in worker_text


def test_policy_texts_differ_per_mode_and_name_the_hard_topics():
    assert set(EXECUTION_POLICY) == set(EXECUTION_MODES)
    assert EXECUTION_POLICY["production"].startswith("PRODUCTION: be conservative")
    assert EXECUTION_POLICY["standard"].startswith("STANDARD:")
    auto = EXECUTION_POLICY[AUTONOMOUS_SANDBOX]
    for topic in ("credentials/auth", "spends money", "contract", "production", "customer data", "irreversible",
                  "repository/branch safety"):
        assert topic in auto


# -- soft gates: caps -----------------------------------------------------------

@pytest.mark.parametrize("mode", ["production", "standard"])
def test_batch_cap_pauses_in_conservative_modes(sandbox, checkout, mode):
    job_dir = sandbox(mode, max_steps=1)
    runner = ScriptedRunner(one_batch("cap-01", checkout) + [("brain", brain("NEXT", "one more", "cap-02"))])
    assert Machine(job_dir, runner).run() == "NEEDS_HUMAN"
    assert runner.script == []
    state = _state(job_dir)
    assert state["runtime"]["cap_hit"] == "max_batches" and state["runtime"]["retry_phase"] == "PLANNING"
    assert state["runtime"]["config"]["caps"]["max_batches"] == 1, "no cap is lifted without a human"
    assert state["runtime"]["human"]["gate"] == "soft" and state["runtime"]["human"]["answer"] is None
    assert state["runtime"]["auto_resolved"] == []
    assert checkout.commits_ahead_of_base() == 1


def test_batch_cap_is_lifted_under_autonomous_sandbox(sandbox, checkout):
    job_dir = sandbox(AUTONOMOUS_SANDBOX, max_steps=1)
    runner = ScriptedRunner(one_batch("cap-01", checkout)
                            + [("brain", brain("NEXT", "one more", "cap-02"))]      # hits the cap: lifted, re-planned
                            + one_batch("cap-02", checkout, filename="second.py")
                            + [("brain", brain("DONE", "both committed"))])
    assert Machine(job_dir, runner).run() == "DONE"
    assert runner.script == [], "the second batch ran without a human"
    assert runner.roles == ["brain", "worker", "reviewer", "brain", "brain", "worker", "reviewer", "brain"]
    assert checkout.commits_ahead_of_base() == 2 and checkout.is_clean()

    state = _state(job_dir)
    assert state["runtime"]["config"]["caps"]["max_batches"] == 2
    assert state["runtime"]["cap_hit"] is None and state["human_question"] is None
    resolved = state["runtime"]["auto_resolved"]
    assert len(resolved) == 1
    assert resolved[0]["cap"] == "max_batches" and resolved[0]["lifted_to"] == 2
    assert resolved[0]["retry_phase"] == "PLANNING" and "Batch cap reached (1)" in resolved[0]["question"]
    human = state["runtime"]["human"]
    assert human["auto"] is True and human["answer"] == "auto-lifted max_batches to 2 under autonomous_sandbox"
    assert any(r.startswith("auto-lifted max_batches to 2 under autonomous_sandbox: Batch cap") for r in _reasons(job_dir))
    assert "NEEDS_HUMAN" not in {e["to"] for e in read_events(job_dir / "events.jsonl")}
    # The Brain call right after the lift saw the automatic decision in its context.
    _, later_prompt, _ = runner.calls[4]
    context = json.loads(later_prompt.split("```json", 1)[1].split("```", 1)[0])
    assert context["human"]["auto"] is True and context["auto_resolved"][0]["cap"] == "max_batches"
    assert Machine(job_dir, runner).status()["auto_resolved"] == resolved


def test_review_and_repair_caps_are_lifted_under_autonomous_sandbox(sandbox, checkout):
    step = "rep-01"
    job_dir = sandbox(AUTONOMOUS_SANDBOX, max_repairs_per_step=0)
    runner = ScriptedRunner([
        ("brain", brain("NEXT", "start", step)),
        ("worker", worker(step, checkout, "hello.py", "print('helo')\n")),
        ("reviewer", reviewer(step, "FAIL")),
        ("brain", brain("REPAIR", "fix typo", step)),          # repair cap 0 -> auto-lifted to 1, re-planned
        ("brain", brain("REPAIR", "fix typo", step)),
        ("worker", worker(step, checkout, "hello.py", "print('hello')\n")),
        ("reviewer", reviewer(step)),
        ("brain", brain("DONE")),
    ])
    assert Machine(job_dir, runner).run() == "DONE"
    assert runner.script == []
    state = _state(job_dir)
    assert state["runtime"]["config"]["caps"]["max_repairs_per_batch"] == 1
    assert [r["cap"] for r in state["runtime"]["auto_resolved"]] == ["max_repairs_per_batch"]
    assert state["runtime"]["history"][0]["repairs"] == 1 and state["runtime"]["history"][0]["outcome"] == "committed"


@pytest.mark.parametrize("mode,expected", [("standard", "NEEDS_HUMAN"), (AUTONOMOUS_SANDBOX, "DONE")])
def test_call_cap_pauses_in_standard_and_is_lifted_under_autonomous_sandbox(sandbox, checkout, mode, expected):
    job_dir = sandbox(mode)
    state = _state(job_dir)
    state["runtime"]["config"]["caps"]["max_claude_calls"] = 2
    save_json(job_dir / "state.json", state)
    runner = ScriptedRunner(one_batch("calls-01", checkout) + [("brain", brain("DONE"))])
    assert Machine(job_dir, runner).run() == expected
    rt = _state(job_dir)["runtime"]
    if mode == "standard":
        assert rt["cap_hit"] == "max_claude_calls" and rt["config"]["caps"]["max_claude_calls"] == 2
        assert len(runner.calls) == 2, "paused before the Reviewer call"
    else:
        assert rt["config"]["caps"]["max_claude_calls"] == 4 and len(runner.calls) == 4
        assert [(r["cap"], r["lifted_to"]) for r in rt["auto_resolved"]] == [("max_claude_calls", 4)]


@pytest.mark.parametrize("mode,expected_calls", [("standard", 3), (AUTONOMOUS_SANDBOX, 4)])
def test_max_attempts_is_lifted_once_per_call_then_pauses(sandbox, mode, expected_calls):
    # Consecutive invalid Brain outputs: standard pauses after 3 attempts,
    # autonomous_sandbox allows exactly one extra attempt of the same call.
    job_dir = sandbox(mode)
    invalid = {"schema_version": 1, "decision": "NEXT", "reason": "no next_batch given"}
    runner = ScriptedRunner([("brain", invalid)] * 6)
    assert Machine(job_dir, runner).run() == "NEEDS_HUMAN"
    assert len(runner.calls) == expected_calls
    rt = _state(job_dir)["runtime"]
    assert rt["cap_hit"] == "max_attempts"
    if mode == "standard":
        assert rt["auto_resolved"] == [] and rt["config"]["caps"]["max_attempts"] == 3
    else:
        assert rt["config"]["caps"]["max_attempts"] == 4
        assert [(r["cap"], r["lifted_to"], r["call_id"]) for r in rt["auto_resolved"]] == [("max_attempts", 4, "brain-001")]
        assert rt["human"]["gate"] == "soft" and rt["human"]["answer"] is None


# -- soft gates: Brain / Reviewer questions ---------------------------------------------

ORDINARY_QUESTION = "Should the greeting default to English or to the user's browser locale?"


def test_ordinary_brain_question_pauses_in_standard(sandbox):
    job_dir = sandbox("standard")
    runner = ScriptedRunner([("brain", brain("NEEDS_HUMAN", "ambiguous", human_question=ORDINARY_QUESTION))])
    assert Machine(job_dir, runner).run() == "NEEDS_HUMAN"
    state = _state(job_dir)
    assert state["human_question"] == ORDINARY_QUESTION
    assert state["runtime"]["human"] == {"question": ORDINARY_QUESTION, "asked_at": state["runtime"]["human"]["asked_at"],
                                         "answer": None, "answered_at": None, "gate": "soft", "hard_topic": None}
    assert state["runtime"]["retry_phase"] == "PLANNING"


def test_ordinary_brain_question_is_deferred_once_under_autonomous_sandbox(sandbox):
    job_dir = sandbox(AUTONOMOUS_SANDBOX)
    runner = ScriptedRunner([
        ("brain", brain("NEEDS_HUMAN", "ambiguous", human_question=ORDINARY_QUESTION)),
        ("brain", brain("DONE", "chose English and documented it")),
    ])
    assert Machine(job_dir, runner).run() == "DONE"
    assert runner.script == []
    state = _state(job_dir)
    resolved = state["runtime"]["auto_resolved"]
    assert len(resolved) == 1 and resolved[0]["cap"] is None and resolved[0]["question"] == ORDINARY_QUESTION
    assert state["runtime"]["human"]["answer"].startswith("auto-resolved under autonomous_sandbox")
    _, second_prompt, _ = runner.calls[1]
    context = json.loads(second_prompt.split("```json", 1)[1].split("```", 1)[0])
    assert context["human"]["question"] == ORDINARY_QUESTION and context["human"]["auto"] is True


def test_same_question_asked_twice_pauses_under_autonomous_sandbox(sandbox):
    job_dir = sandbox(AUTONOMOUS_SANDBOX)
    runner = ScriptedRunner([
        ("brain", brain("NEEDS_HUMAN", "ambiguous", human_question=ORDINARY_QUESTION)),
        ("brain", brain("NEEDS_HUMAN", "still ambiguous", human_question=ORDINARY_QUESTION)),
    ])
    assert Machine(job_dir, runner).run() == "NEEDS_HUMAN"
    assert runner.script == []
    state = _state(job_dir)
    assert len(state["runtime"]["auto_resolved"]) == 1
    assert state["human_question"] == ORDINARY_QUESTION and state["runtime"]["human"]["answer"] is None


HARD_QUESTIONS = {
    "credentials/auth": "Which API key should the Worker use for the mail provider?",
    "budget/spending/contract": "May I buy a paid plan for the image service?",
    "production/live-user/customer-data": "Should I run the backfill against real customer data?",
    "destructive/irreversible": "Can I drop the legacy_users table?",
    "repo-integrity/git-safety": "Should I rebase this branch onto main before continuing?",
}


@pytest.mark.parametrize("topic,question", sorted(HARD_QUESTIONS.items()))
def test_hard_topic_detector(topic, question):
    assert hard_gate_reason(question) == topic


@pytest.mark.parametrize("question", [
    ORDINARY_QUESTION,
    "Should the main entry point be app.py or cli.py?",
    "Do you want the Worker contract summary to include the file list?",
    "Is a 2-column or 3-column layout preferred for the dashboard?",
    "Should I delete the unused helper function in utils.py?",
    "Which release notes format do you prefer, keep-a-changelog or plain?",
    "", None,
])
def test_ordinary_questions_are_not_hard(question):
    assert hard_gate_reason(question) is None


@pytest.mark.parametrize("question", [
    "Please run vitest for the token tests and confirm they pass.",
    "Run the auth tests and typecheck this production code.",
    "Verify by inspection that no API secret is exposed to the browser.",
])
def test_verification_language_does_not_create_false_hard_gate(question):
    assert hard_gate_reason(question) is None


@pytest.mark.parametrize("topic,question", [
    ("credentials/auth", "Please provide the real API secret so the test can connect."),
    ("production/live-user/customer-data", "Please deploy to production to verify this."),
])
def test_verification_with_material_action_remains_hard(topic, question):
    assert hard_gate_reason(question) == topic


@pytest.mark.parametrize("topic,question", sorted(HARD_QUESTIONS.items()))
def test_hard_topic_question_pauses_even_under_autonomous_sandbox(sandbox, topic, question):
    job_dir = sandbox(AUTONOMOUS_SANDBOX, job_id="hard-" + topic.split("/")[0])
    runner = ScriptedRunner([("brain", brain("NEEDS_HUMAN", "must ask", human_question=question))])
    assert Machine(job_dir, runner).run() == "NEEDS_HUMAN"
    state = _state(job_dir)
    assert state["human_question"] == question and state["runtime"]["auto_resolved"] == []
    assert state["runtime"]["human"]["gate"] == "hard" and state["runtime"]["human"]["hard_topic"] == topic
    assert Machine(job_dir, runner).status()["human"]["hard_topic"] == topic


def test_reviewer_question_follows_the_same_rules(sandbox, checkout):
    step = "rev-01"
    soft = sandbox(AUTONOMOUS_SANDBOX, job_id="rev-soft")
    runner = ScriptedRunner([
        ("brain", brain("NEXT", "start", step)), ("worker", worker(step, checkout, "hello.py", "# a\n")),
        ("reviewer", reviewer(step, "NEEDS_HUMAN", human_question="Is the file name hello.py acceptable?")),
        ("brain", brain("DONE")),
    ])
    assert Machine(soft, runner).run() == "DONE" and runner.script == []
    state = _state(soft)
    assert [r["cap"] for r in state["runtime"]["auto_resolved"]] == [None]
    assert state["runtime"]["history"][0]["outcome"] == "committed"
    assert state["runtime"]["history"][0]["verdict"] == "PASS"

    hard = sandbox(AUTONOMOUS_SANDBOX, job_id="rev-hard")
    runner = ScriptedRunner([
        ("brain", brain("NEXT", "start", step)), ("worker", worker(step, checkout, "hello.py", "# b\n")),
        ("reviewer", reviewer(step, "NEEDS_HUMAN", human_question="This diff deletes the backups directory; intended?")),
    ])
    assert Machine(hard, runner).run() == "NEEDS_HUMAN"
    assert _state(hard)["runtime"]["human"]["hard_topic"] == "destructive/irreversible"


def test_reviewer_cannot_reopen_passing_orchestrator_test_evidence(sandbox, checkout):
    step = "evidence-wins-01"
    question = "Please run the token tests and confirm they pass."
    job_dir = sandbox(AUTONOMOUS_SANDBOX)
    runner = ScriptedRunner([
        ("brain", brain("NEXT", "start", step)),
        ("worker", worker(step, checkout, "hello.py", "# evidence wins\n")),
        ("reviewer", reviewer(step, "NEEDS_HUMAN", human_question=question)),
        ("brain", brain("DONE")),
    ])
    assert Machine(job_dir, runner).run() == "DONE"
    state = _state(job_dir)
    assert state["runtime"]["history"][0]["verdict"] == "PASS"
    assert state["runtime"]["history"][0]["outcome"] == "committed"
    assert any(r["question"] == question for r in state["runtime"]["auto_resolved"])


def test_non_converging_repair_is_deferred_once_then_pauses(sandbox, checkout):
    step = "same-01"
    job_dir = sandbox(AUTONOMOUS_SANDBOX)
    same = worker(step, checkout, "hello.py", "print('helo')\n")
    runner = ScriptedRunner([
        ("brain", brain("NEXT", "start", step)), ("worker", same), ("reviewer", reviewer(step, "FAIL")),
        ("brain", brain("REPAIR", "try again", step)), ("worker", same),        # identical diff: deferred to the Brain
        ("brain", brain("REPAIR", "and again", step)), ("worker", same),        # identical again: a human now
    ])
    assert Machine(job_dir, runner).run() == "NEEDS_HUMAN"
    assert runner.script == []
    state = _state(job_dir)
    assert "identical diff" in state["human_question"]
    assert len(state["runtime"]["auto_resolved"]) == 1 and "identical diff" in state["runtime"]["auto_resolved"][0]["question"]


# -- hard machine gates under autonomous_sandbox -----------------------------------------

def test_branch_freshness_gate_pauses_under_autonomous_sandbox(sandbox, checkout):
    from conftest import git
    git(checkout.work, "checkout", "--quiet", checkout.BASE_BRANCH)
    (checkout.work / "NEWS.md").write_text("upstream moved\n", encoding="utf-8")
    git(checkout.work, "add", "--", "NEWS.md")
    git(checkout.work, "commit", "--quiet", "-m", "upstream commit")
    git(checkout.work, "push", "--quiet", "origin", checkout.BASE_BRANCH)
    git(checkout.work, "checkout", "--quiet", checkout.JOB_BRANCH)

    job_dir = sandbox(AUTONOMOUS_SANDBOX)
    runner = ScriptedRunner([])
    assert Machine(job_dir, runner).run() == "NEEDS_HUMAN"
    assert runner.calls == [], "no role ran"
    state = _state(job_dir)
    assert "Branch freshness gate blocked" in state["human_question"]
    assert state["runtime"]["human"]["gate"] == "hard" and state["runtime"]["auto_resolved"] == []
    assert state["runtime"]["branch_freshness"]["ok"] is False


def test_head_moved_before_commit_pauses_under_autonomous_sandbox(sandbox, checkout):
    from conftest import git
    step = "head-01"

    def sneaky_commit():
        (checkout.work / "other.txt").write_text("x\n", encoding="utf-8")
        git(checkout.work, "add", "--", "other.txt")
        git(checkout.work, "commit", "--quiet", "-m", "someone else committed")

    job_dir = sandbox(AUTONOMOUS_SANDBOX)
    runner = ScriptedRunner([("brain", brain("NEXT", "start", step)),
                             ("worker", worker(step, checkout, "hello.py", "# h\n", after=sneaky_commit)),
                             ("reviewer", reviewer(step))])
    assert Machine(job_dir, runner).run() == "NEEDS_HUMAN"
    assert runner.script == []
    state = _state(job_dir)
    assert state["human_question"].startswith("Refusing to commit head-01")
    assert state["runtime"]["retry_phase"] == "COMMITTING" and state["runtime"]["human"]["gate"] == "hard"
    assert state["runtime"]["auto_resolved"] == []
    assert checkout.commits_ahead_of_base() == 1, "only the foreign commit exists; the batch was not committed"


def test_evidence_harness_error_pauses_under_autonomous_sandbox(sandbox, checkout, tmp_path):
    step = "harness-01"
    job_dir = sandbox(AUTONOMOUS_SANDBOX, test_commands=[[str(tmp_path / "no-such-test-runner")]])
    runner = ScriptedRunner([("brain", brain("NEXT", "start", step)), ("worker", worker(step, checkout, "hello.py", "# e\n"))])
    assert Machine(job_dir, runner).run() == "NEEDS_HUMAN"
    assert runner.script == []
    state = _state(job_dir)
    assert state["human_question"].startswith("Evidence harness error for harness-01")
    assert state["runtime"]["retry_phase"] == "EVIDENCE" and state["runtime"]["human"]["gate"] == "hard"
    assert state["runtime"]["auto_resolved"] == []


@pytest.mark.parametrize("label,result", [
    ("auth", failed_call("Not logged in. Please run /login")),
    ("budget", failed_call("", subtype="error_max_budget_usd")),
])
def test_provider_auth_and_budget_failures_pause_under_autonomous_sandbox(sandbox, label, result):
    job_dir = sandbox(AUTONOMOUS_SANDBOX, job_id=f"provider-{label}")
    runner = ScriptedRunner([("brain", result)])
    assert Machine(job_dir, runner).run() == "NEEDS_HUMAN"
    assert len(runner.calls) == 1, "no automatic retry"
    state = _state(job_dir)
    assert state["runtime"]["human"]["gate"] == "hard" and state["runtime"]["auto_resolved"] == []
    assert state["runtime"]["cap_hit"] is None and state["attempt"] == 1


def test_read_mode_dirty_tree_pauses_under_autonomous_sandbox(sandbox, checkout):
    step = "read-01"
    job_dir = sandbox(AUTONOMOUS_SANDBOX, mode="read")
    runner = ScriptedRunner([("brain", brain("NEXT", "start", step)),
                             ("worker", worker(step, checkout, "hello.py", "# should not happen\n")),
                             ("reviewer", reviewer(step))])
    assert Machine(job_dir, runner).run() == "NEEDS_HUMAN"
    state = _state(job_dir)
    assert state["human_question"].startswith("Read-mode job left 1 changed path(s)")
    assert state["runtime"]["human"]["gate"] == "hard" and state["runtime"]["auto_resolved"] == []
    assert checkout.commits_ahead_of_base() == 0


# -- human controls ---------------------------------------------------------------------

def test_set_config_switches_mode_while_paused_and_approve_continues(sandbox, checkout):
    job_dir = sandbox("standard", max_steps=1)
    runner = ScriptedRunner(one_batch("sw-01", checkout) + [("brain", brain("NEXT", "one more", "sw-02"))])
    assert Machine(job_dir, runner).run() == "NEEDS_HUMAN"
    m = Machine(job_dir, runner)
    with pytest.raises(MachineError, match="execution_mode"):
        m.set_config(execution_mode="yolo")
    assert m.set_config(execution_mode=AUTONOMOUS_SANDBOX)["execution_mode"] == AUTONOMOUS_SANDBOX
    assert _config(job_dir)["execution_mode"] == AUTONOMOUS_SANDBOX
    assert any("execution_mode=autonomous_sandbox" in r for r in _reasons(job_dir))
    assert m.approve("approved: continue") == "PLANNING"
    # The pending decision was approved by a person, so max_batches is 2; the
    # Brain's next NEXT hits the cap again and is now lifted automatically.
    runner.script = (one_batch("sw-02", checkout, filename="second.py")
                     + [("brain", brain("NEXT", "one more", "sw-03"))]          # cap hit again: lifted, re-planned
                     + one_batch("sw-03", checkout, filename="third.py") + [("brain", brain("DONE"))])
    assert Machine(job_dir, runner).run() == "DONE"
    assert checkout.commits_ahead_of_base() == 3
    rt = _state(job_dir)["runtime"]
    assert rt["config"]["caps"]["max_batches"] == 3 and [r["lifted_to"] for r in rt["auto_resolved"]] == [3]
