"""Fail-closed job submission: python3 -m core.submit_job ...

Builds the Job contract from the command line and calls core.machine.create_job,
which writes jobs/<job-id>/{job.json,state.json,events.jsonl} with the job
QUEUED. Nothing else happens here: no provider is called, no git remote is
touched and nothing is ever pushed. The job is started afterwards with
`systemctl start orchestrator@<job-id>.service` (see docs/INSTALL.md).

Every refusal prints exactly one `ERROR ...` line on stderr and exits 2:
the job already exists, --repo-path is not the top level of a git checkout,
--branch is a protected branch, a write-mode job has no entry in the repo
policy (the snippet to add is printed after the ERROR line), the checkout's
origin does not match that policy, or the job/config fails validation.
Success prints exactly one `JOB SUBMITTED job_id=<id> dir=<job_dir>` line.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from core import gitsafe
from core.contracts import SCHEMA_VERSION, ContractError, validate_job
from core.machine import DEFAULT_EXECUTION_MODE, EXECUTION_MODES, JOB_FILE, STATE_FILE, MachineError, create_job
from core.providers import WORKER_PROVIDERS
from core.repo_policy import DEFAULT_POLICY_PATH, RepoPolicyError, load_repo_policy
from core.state import StateError
from core.supervisor import DEFAULT_JOBS_DIR

__all__ = ["SubmitError", "build_parser", "policy_snippet", "submit", "main"]

EXIT_ERROR = 2


class SubmitError(Exception):
    """One fail-closed refusal; str(exc) is the text of the ERROR line."""

    def __init__(self, message, extra_lines=()):
        super().__init__(message)
        self.extra_lines = list(extra_lines)


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def policy_snippet(repo, expected_origin, canonical_base_branch="main"):
    """The JSON the user must merge into config/repo_policy.json for `repo`."""
    return {"repos": {repo: {"expected_origin": expected_origin, "canonical_base_branch": canonical_base_branch}}}


def _git_toplevel(repo_path):
    """Resolved top level of the checkout at repo_path, or None when it is not one."""
    path = Path(repo_path)
    if not path.is_dir():
        return None
    top = gitsafe.run_git(path, ["rev-parse", "--show-toplevel"])
    if not top.ok or not top.stdout.strip():
        return None
    try:
        resolved = Path(top.stdout.strip()).resolve()
    except OSError:
        return None
    return resolved if resolved == path.resolve() else None


def build_parser():
    p = argparse.ArgumentParser(
        prog="python3 -m core.submit_job",
        description="Create a QUEUED orchestrator job directory (fail-closed; never pushes).",
    )
    p.add_argument("--job-id", required=True, help="unique job id; becomes jobs/<job-id>/ and the systemd instance name")
    p.add_argument("--repo", required=True, help="repository key, e.g. owner/name (must match config/repo_policy.json for write jobs)")
    p.add_argument("--repo-path", required=True, help="local checkout the Worker edits (top level of a git work tree)")
    p.add_argument("--branch", required=True, help="work branch the checkout must be on; main/master are refused")
    p.add_argument("--mode", required=True, choices=("read", "write"))
    p.add_argument("--goal", required=True, help="what the whole job must achieve")
    p.add_argument("--input-file", action="append", default=[], metavar="PATH",
                   help="file shown to the Brain (absolute, or relative to the kit root); repeatable")
    p.add_argument("--max-steps", type=int, default=None, help="cap on batches (job.max_steps)")
    p.add_argument("--max-repairs-per-step", type=int, default=None, help="cap on repairs per batch")
    p.add_argument("--worker-provider", choices=WORKER_PROVIDERS, default=None,
                   help="Worker provider; default is the Worker route of the active profile")
    p.add_argument("--execution-mode", choices=EXECUTION_MODES, default=DEFAULT_EXECUTION_MODE,
                   help="how much the job decides on its own before pausing in NEEDS_HUMAN: production "
                        "(conservative), standard (default), autonomous_sandbox (auto-resolves reversible "
                        "ambiguity and lifts repair/review/call caps; hard safety gates still pause)")
    p.add_argument("--origin", default=None,
                   help="expected origin URL of the checkout (default: policy expected_origin for write jobs, "
                        "else https://github.com/<repo>.git)")
    p.add_argument("--test-command", action="append", default=[], metavar="ARGV_JSON",
                   help='test command the orchestrator runs after every batch, as a JSON argv list, '
                        'e.g. \'["python3", "-m", "pytest", "-q"]\'; repeatable')
    p.add_argument("--jobs-dir", default=str(DEFAULT_JOBS_DIR))
    p.add_argument("--policy-file", default=str(DEFAULT_POLICY_PATH))
    return p


def _parse_test_commands(raw_items):
    commands = []
    for raw in raw_items:
        try:
            argv = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SubmitError(f"--test-command {raw!r} is not a JSON list: {exc}") from None
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
            raise SubmitError(f"--test-command {raw!r} must be a non-empty JSON list of non-empty strings")
        commands.append(argv)
    return commands


def submit(args):
    """Run every check, create the job and return its directory. Raises SubmitError."""
    job = {
        "schema_version": SCHEMA_VERSION,
        "job_id": args.job_id,
        "repo": args.repo,
        "branch": args.branch,
        "mode": args.mode,
        "goal": args.goal,
        "created_at": _utc_now(),
    }
    if args.input_file:
        job["input_files"] = list(args.input_file)
    if args.max_steps is not None:
        job["max_steps"] = args.max_steps
    if args.max_repairs_per_step is not None:
        job["max_repairs_per_step"] = args.max_repairs_per_step
    try:
        job = validate_job(job)   # first: a safe job_id is needed before any path is derived from it
    except ContractError as exc:
        raise SubmitError(f"invalid job: {exc}") from None
    test_commands = _parse_test_commands(args.test_command)

    jobs_dir = Path(args.jobs_dir)
    job_dir = jobs_dir / args.job_id
    if job_dir.is_dir() and ((job_dir / STATE_FILE).exists() or (job_dir / JOB_FILE).exists() or any(job_dir.iterdir())):
        raise SubmitError(f"job already exists: {job_dir}")
    if job_dir.exists() and not job_dir.is_dir():
        raise SubmitError(f"job path exists and is not a directory: {job_dir}")

    top = _git_toplevel(args.repo_path)
    if top is None:
        raise SubmitError(f"--repo-path {args.repo_path!r} is not the top level of a git checkout")
    repo_path = str(top)

    if args.branch in gitsafe.PROTECTED_BRANCHES:
        raise SubmitError(f"--branch {args.branch!r} is protected; jobs never work on {sorted(gitsafe.PROTECTED_BRANCHES)}")

    origin = args.origin
    if args.mode == "write":
        try:
            policy = load_repo_policy(args.repo, args.policy_file)
        except RepoPolicyError as exc:
            actual = gitsafe.origin_url(repo_path) or "<origin-url>"
            snippet = json.dumps(policy_snippet(args.repo, actual), indent=2)
            raise SubmitError(
                f"write-mode job refused: no usable repo policy for {args.repo!r} in config/repo_policy.json "
                f"({args.policy_file}): {exc}. Add this to config/repo_policy.json (merge into \"repos\"; "
                f"set canonical_base_branch to the real base branch):",
                extra_lines=snippet.splitlines(),
            ) from None
        actual = gitsafe.origin_url(repo_path)
        if actual is None or gitsafe.normalize_remote_url(actual) != gitsafe.normalize_remote_url(policy["expected_origin"]):
            raise SubmitError(
                f"checkout origin {actual!r} does not match repo policy expected_origin "
                f"{policy['expected_origin']!r} for {args.repo!r}; fix the checkout or config/repo_policy.json"
            )
        if origin is None:
            origin = policy["expected_origin"]

    config = {"repo_path": repo_path, "test_commands": test_commands, "execution_mode": args.execution_mode}
    if origin is not None:
        config["origin"] = origin
    if args.worker_provider is not None:
        config["worker_provider"] = args.worker_provider
    try:
        create_job(job_dir, job, config)
    except (MachineError, ContractError, StateError) as exc:
        raise SubmitError(f"cannot create job: {exc}") from None
    return job_dir


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        job_dir = submit(args)
    except SubmitError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        for line in exc.extra_lines:
            print(line, file=sys.stderr)
        return EXIT_ERROR
    print(f"JOB SUBMITTED job_id={args.job_id} dir={job_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
