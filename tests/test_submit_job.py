"""core.submit_job: fail-closed job submission, fully offline (no provider, no network, no push)."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from core import submit_job
from core.contracts import validate_job
from core.state import load_state
from core.submit_job import main

ROOT = Path(__file__).resolve().parents[1]
REPO_KEY = "example/e2e-fixture"


def _policy_file(tmp_path, checkout, repo=REPO_KEY):
    path = tmp_path / "repo_policy.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "repos": {repo: {"expected_origin": str(checkout.origin), "canonical_base_branch": checkout.BASE_BRANCH}},
    }), encoding="utf-8")
    return path


def _argv(tmp_path, checkout, policy, job_id="job-1", mode="write", **extra):
    argv = [
        "--job-id", job_id, "--repo", REPO_KEY, "--repo-path", str(checkout.work),
        "--branch", checkout.JOB_BRANCH, "--mode", mode, "--goal", "Prove submission works.",
        "--jobs-dir", str(tmp_path / "jobs"), "--policy-file", str(policy),
        "--worker-provider", "claude",
    ]
    for key, value in extra.items():
        flag = "--" + key.replace("_", "-")
        for item in (value if isinstance(value, list) else [value]):
            argv += [flag, str(item)]
    return argv


def _error_lines(capsys):
    captured = capsys.readouterr()
    assert captured.out == "", "nothing may be printed on stdout when submission is refused"
    lines = [ln for ln in captured.err.splitlines() if ln.startswith("ERROR ")]
    assert len(lines) == 1, captured.err
    return lines[0], captured.err


def test_help_runs_as_module():
    proc = subprocess.run([sys.executable, "-m", "core.submit_job", "--help"], cwd=str(ROOT),
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    for flag in ("--job-id", "--repo-path", "--mode", "--policy-file", "--jobs-dir", "--worker-provider"):
        assert flag in proc.stdout


def test_submit_write_job_success(tmp_path, checkout, capsys):
    policy = _policy_file(tmp_path, checkout)
    rc = main(_argv(tmp_path, checkout, policy, input_file=["MISSION.md", "README.md"], max_steps=3,
                    max_repairs_per_step=1, test_command=json.dumps(["python3", "-c", "print('ok')"])))
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    job_dir = tmp_path / "jobs" / "job-1"
    assert captured.out.splitlines() == [f"JOB SUBMITTED job_id=job-1 dir={job_dir}"]

    job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    assert validate_job(job) == job
    assert job["mode"] == "write" and job["repo"] == REPO_KEY and job["branch"] == checkout.JOB_BRANCH
    assert job["input_files"] == ["MISSION.md", "README.md"]
    assert job["max_steps"] == 3 and job["max_repairs_per_step"] == 1
    assert job["created_at"].endswith("Z")

    state = load_state(job_dir / "state.json")
    assert state["phase"] == "QUEUED" and state["job_id"] == "job-1"
    cfg = state["runtime"]["config"]
    assert cfg["repo_path"] == str(checkout.work.resolve())
    assert cfg["origin"] == str(checkout.origin)          # taken from the repo policy
    assert cfg["test_commands"] == [["python3", "-c", "print('ok')"]]
    assert cfg["worker_provider"] == "claude"
    assert cfg["caps"]["max_batches"] == 3 and cfg["caps"]["max_repairs_per_batch"] == 1
    for sub in ("steps", "evidence", "logs"):
        assert (job_dir / sub).is_dir()
    assert "QUEUED" in (job_dir / "events.jsonl").read_text(encoding="utf-8")
    # Submission never touches the remote.
    assert not checkout.origin_has_branch(checkout.JOB_BRANCH)


def test_duplicate_job_id_is_refused(tmp_path, checkout, capsys):
    policy = _policy_file(tmp_path, checkout)
    assert main(_argv(tmp_path, checkout, policy)) == 0
    capsys.readouterr()
    assert main(_argv(tmp_path, checkout, policy)) == 2
    line, _ = _error_lines(capsys)
    assert "already exists" in line and "job-1" in line


def test_non_git_repo_path_is_refused(tmp_path, checkout, capsys):
    policy = _policy_file(tmp_path, checkout)
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    argv = _argv(tmp_path, checkout, policy)
    argv[argv.index("--repo-path") + 1] = str(plain)
    assert main(argv) == 2
    line, _ = _error_lines(capsys)
    assert "not the top level of a git checkout" in line
    assert not (tmp_path / "jobs" / "job-1").exists()


def test_write_mode_without_policy_is_refused_and_names_the_snippet(tmp_path, checkout, capsys):
    empty = tmp_path / "empty_policy.json"
    empty.write_text('{"schema_version":1,"repos":{}}', encoding="utf-8")
    assert main(_argv(tmp_path, checkout, empty)) == 2
    line, err = _error_lines(capsys)
    assert "config/repo_policy.json" in line and REPO_KEY in line
    # The snippet printed after the ERROR line is exactly what must be merged into the policy file.
    snippet = json.loads(err.split("\n", 1)[1])
    assert snippet == submit_job.policy_snippet(REPO_KEY, str(checkout.origin), checkout.BASE_BRANCH)
    assert not (tmp_path / "jobs" / "job-1").exists()


def test_write_mode_with_wrong_origin_is_refused(tmp_path, checkout, capsys):
    policy = tmp_path / "repo_policy.json"
    policy.write_text(json.dumps({"schema_version": 1, "repos": {
        REPO_KEY: {"expected_origin": "https://github.com/example/other.git", "canonical_base_branch": "main"}}}),
        encoding="utf-8")
    assert main(_argv(tmp_path, checkout, policy)) == 2
    line, _ = _error_lines(capsys)
    assert "does not match repo policy expected_origin" in line


def test_read_mode_without_policy_succeeds(tmp_path, checkout, capsys):
    # core.machine only consults the repo policy for write jobs (Machine._branch_freshness_check),
    # so a read job needs no entry in config/repo_policy.json.
    empty = tmp_path / "empty_policy.json"
    empty.write_text('{"schema_version":1,"repos":{}}', encoding="utf-8")
    assert main(_argv(tmp_path, checkout, empty, mode="read")) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [f"JOB SUBMITTED job_id=job-1 dir={tmp_path / 'jobs' / 'job-1'}"]
    state = load_state(tmp_path / "jobs" / "job-1" / "state.json")
    assert state["phase"] == "QUEUED"
    assert state["runtime"]["config"]["origin"] == f"https://github.com/{REPO_KEY}.git"


def test_protected_branch_is_refused(tmp_path, checkout, capsys):
    policy = _policy_file(tmp_path, checkout)
    argv = _argv(tmp_path, checkout, policy)
    argv[argv.index("--branch") + 1] = "main"
    assert main(argv) == 2
    line, _ = _error_lines(capsys)
    assert "protected" in line


@pytest.mark.parametrize("bad_id", ["", "bad id", "/etc/passwd", "../x"])
def test_invalid_job_id_is_refused(tmp_path, checkout, capsys, bad_id):
    policy = _policy_file(tmp_path, checkout)
    assert main(_argv(tmp_path, checkout, policy, job_id=bad_id)) == 2
    line, _ = _error_lines(capsys)
    assert "ERROR" in line
    assert not (tmp_path / "jobs").exists() or not any((tmp_path / "jobs").iterdir())


def test_invalid_test_command_is_refused(tmp_path, checkout, capsys):
    policy = _policy_file(tmp_path, checkout)
    assert main(_argv(tmp_path, checkout, policy, test_command="pytest -q")) == 2
    line, _ = _error_lines(capsys)
    assert "--test-command" in line
