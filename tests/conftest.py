"""Shared fixtures: make the kit importable as `core.*` / `monitor.*` from any cwd."""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def root():
    return ROOT


def git(cwd, *args):
    """Run one git command in cwd and return its stdout; fail the test on error."""
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"git {' '.join(args)} failed in {cwd}:\n{proc.stdout}\n{proc.stderr}"
    return proc.stdout


class GitCheckout:
    """A throwaway repository: local bare `origin`, a `main` commit and a job branch checked out."""

    BASE_BRANCH = "main"
    JOB_BRANCH = "job/e2e"

    def __init__(self, base):
        self.origin = base / "origin.git"
        self.work = base / "work"
        git(base, "init", "--quiet", "--bare", str(self.origin))
        git(self.origin, "symbolic-ref", "HEAD", f"refs/heads/{self.BASE_BRANCH}")
        self.work.mkdir()
        git(self.work, "init", "--quiet")
        git(self.work, "symbolic-ref", "HEAD", f"refs/heads/{self.BASE_BRANCH}")
        git(self.work, "config", "user.name", "Orchestrator Test")
        git(self.work, "config", "user.email", "orchestrator-test@example.invalid")
        git(self.work, "config", "commit.gpgsign", "false")
        (self.work / "README.md").write_text("# e2e fixture\n", encoding="utf-8")
        git(self.work, "add", "--", "README.md")
        git(self.work, "commit", "--quiet", "-m", "canonical main")
        git(self.work, "remote", "add", "origin", str(self.origin))
        git(self.work, "push", "--quiet", "-u", "origin", self.BASE_BRANCH)
        git(self.work, "checkout", "--quiet", "-b", self.JOB_BRANCH)
        self.base_sha = self.rev_parse(self.BASE_BRANCH)

    # -- read-only inspection ------------------------------------------------

    def rev_parse(self, ref):
        return git(self.work, "rev-parse", "--verify", ref).strip()

    def origin_has_branch(self, branch):
        proc = subprocess.run(["git", "--git-dir", str(self.origin), "rev-parse", "--verify", "--quiet",
                               f"refs/heads/{branch}"], capture_output=True, text=True, check=False)
        return proc.returncode == 0

    def origin_sha(self, branch):
        return git(self.origin, "rev-parse", "--verify", f"refs/heads/{branch}").strip()

    def commits_ahead_of_base(self):
        return int(git(self.work, "rev-list", "--count", f"{self.BASE_BRANCH}..HEAD").strip())

    def head_files(self):
        return set(git(self.work, "show", "--name-only", "--format=", "HEAD").split())

    def head_message(self):
        return git(self.work, "log", "-1", "--format=%B")

    def is_clean(self):
        return git(self.work, "status", "--porcelain=v1", "--untracked-files=all").strip() == ""


@pytest.fixture
def checkout(tmp_path):
    return GitCheckout(tmp_path)
