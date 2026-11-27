"""Unit tests for core.gitsafe.push_branch: refusals never reach git push; a real push is plain and same-name."""
import pytest

from core import gitsafe


def _forbid_push(monkeypatch):
    """Fail the test if anything tries to run `git push`; other git calls (symbolic-ref) pass through."""
    real = gitsafe.run_git

    def guarded(repo, args, **kwargs):
        assert args[:1] != ["push"], f"git push must not be invoked: {args}"
        return real(repo, args, **kwargs)

    monkeypatch.setattr(gitsafe, "run_git", guarded)


@pytest.mark.parametrize("branch", sorted(gitsafe.PROTECTED_BRANCHES))
def test_push_branch_refuses_protected_branches(checkout, monkeypatch, branch):
    _forbid_push(monkeypatch)
    res = gitsafe.push_branch(checkout.work, branch)
    assert not res.ok and res.exit_code == -1
    assert "protected" in res.error and branch in res.error
    assert not checkout.origin_has_branch(checkout.JOB_BRANCH)


def test_push_branch_refuses_branch_not_checked_out(checkout, monkeypatch):
    _forbid_push(monkeypatch)
    res = gitsafe.push_branch(checkout.work, "job/other")
    assert not res.ok and res.exit_code == -1
    assert "job/other" in res.error and checkout.JOB_BRANCH in res.error
    assert not checkout.origin_has_branch("job/other")


def test_push_branch_refuses_when_head_is_detached(checkout, monkeypatch):
    _forbid_push(monkeypatch)
    assert gitsafe.run_git(checkout.work, ["checkout", "--quiet", "--detach"]).ok
    res = gitsafe.push_branch(checkout.work, checkout.JOB_BRANCH)
    assert not res.ok and "None" in res.error


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_push_branch_rejects_invalid_branch_argument(checkout, monkeypatch, bad):
    _forbid_push(monkeypatch)
    with pytest.raises(gitsafe.GitSafeError):
        gitsafe.push_branch(checkout.work, bad)


def test_push_branch_pushes_current_branch_plainly(checkout, monkeypatch):
    """The real push uses exactly `push -u origin <b>:<b>`: no force/delete/mirror flags."""
    argv_seen = []
    real = gitsafe.run_git

    def spy(repo, args, **kwargs):
        argv_seen.append(list(args))
        return real(repo, args, **kwargs)

    monkeypatch.setattr(gitsafe, "run_git", spy)
    assert not checkout.origin_has_branch(checkout.JOB_BRANCH)
    res = gitsafe.push_branch(checkout.work, checkout.JOB_BRANCH)
    assert res.ok, res.stderr
    pushes = [a for a in argv_seen if a[:1] == ["push"]]
    assert pushes == [["push", "-u", "origin", f"{checkout.JOB_BRANCH}:{checkout.JOB_BRANCH}"]]
    assert checkout.origin_sha(checkout.JOB_BRANCH) == checkout.rev_parse("HEAD")
    assert checkout.origin_sha(checkout.BASE_BRANCH) == checkout.base_sha


def test_push_branch_reports_remote_failure_without_raising(checkout, tmp_path):
    assert gitsafe.run_git(checkout.work, ["remote", "set-url", "origin", str(tmp_path / "missing.git")]).ok
    res = gitsafe.push_branch(checkout.work, checkout.JOB_BRANCH)
    assert not res.ok and res.exit_code != 0
    assert res.error or res.stderr.strip()


def test_staging_plan_ignores_python_runtime_artifacts(checkout):
    """Generated Python caches must never become orchestrator commits, even without .gitignore."""
    cache = checkout.work / "__pycache__"
    cache.mkdir()
    (cache / "module.cpython-314.pyc").write_bytes(b"cache")
    (checkout.work / "result.py").write_text("answer = 42\n", encoding="utf-8")
    plan = gitsafe.staging_plan(checkout.work, ["*"])
    assert plan.ok
    assert plan.paths == ["result.py"]
    assert all("__pycache__" not in item.path and not item.path.endswith(".pyc") for item in plan.changed)
