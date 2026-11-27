"""Profile-driven Brain/Worker/Reviewer routing, fully offline.

run_claude / run_codex / run_gemini are replaced by fakes that record the call
and return a minimal ClaudeResult, so no subprocess or network is ever used.
"""
import json
import logging

import pytest

from core import classify, machine, provider_registry, providers
from core.claude_runner import ClaudeResult

ROLES = ("brain", "worker", "reviewer")


def _load(root, name):
    with open(root / "profiles" / f"{name}.json", encoding="utf-8") as fh:
        return json.load(fh)


def _fake_result(prompt, cwd, provider):
    result = ClaudeResult(
        argv=[f"fake-{provider}"], cwd=str(cwd), exit_code=0, stdout="{}", stderr="",
        duration_seconds=0.0, timed_out=False, structured=True, schema_requested=True,
        parsed_json={"type": "result", "subtype": "success", "is_error": False, "structured_output": {}},
        prompt_chars=len(prompt),
    )
    result.provider = provider
    return result


@pytest.fixture
def calls(monkeypatch):
    """Patch every provider entry point used by core.providers; return the call log."""
    log = []

    def fake_claude(prompt, *, cwd, **kwargs):
        log.append(("claude", kwargs))
        return _fake_result(prompt, cwd, "claude")

    def fake_codex(prompt, *, cwd, **kwargs):
        log.append(("codex", kwargs))
        return _fake_result(prompt, cwd, "codex")

    def fake_gemini(prompt, *, cwd, **kwargs):
        log.append(("gemini", kwargs))
        return _fake_result(prompt, cwd, "gemini")

    monkeypatch.setattr(providers, "run_claude", fake_claude)
    monkeypatch.setattr(providers, "run_codex", fake_codex)
    monkeypatch.setattr(providers, "run_gemini", fake_gemini)
    return log


def _run(runner, role, worker_provider=None):
    return runner(
        role, f"{role} prompt", cwd="/tmp", json_schema={"type": "object"}, permission_mode="plan",
        disallowed_tools=[], worker_provider=worker_provider, worker_complexity="normal",
    )


def _runner(root, name):
    return providers.build_runner(claude_bin="fake-claude", codex_bin="fake-codex", profile=_load(root, name))


# -- (a) claude profile ------------------------------------------------------

def test_claude_profile_routes_every_role_to_claude(root, calls):
    runner = _runner(root, "claude")
    for role in ROLES:
        result = _run(runner, role)
        assert result.provider == "claude"
    assert [name for name, _ in calls] == ["claude"] * 3
    assert runner.default_worker_provider == "claude"


# -- (b) openai profile ------------------------------------------------------

def test_openai_profile_routes_every_role_to_codex_and_never_claude(root, calls):
    runner = _runner(root, "openai")
    results = {role: _run(runner, role) for role in ROLES}
    assert all(r.provider == "codex" for r in results.values())
    assert [name for name, _ in calls] == ["codex"] * 3, "Claude must never be called in the openai profile"
    sandboxes = {role: kwargs["sandbox"] for role, (_, kwargs) in zip(ROLES, calls)}
    assert sandboxes == {"brain": "read-only", "worker": "workspace-write", "reviewer": "read-only"}
    assert runner.default_worker_provider == "codex"


def test_openai_profile_codex_receives_schema_and_stable_prefix(root, calls):
    runner = _runner(root, "openai")
    runner("brain", "p", cwd="/tmp", json_schema={"type": "object", "title": "brain"}, permission_mode="plan",
           disallowed_tools=[], append_system_prompt_file="/tmp/prefix.md")
    name, kwargs = calls[0]
    assert name == "codex"
    assert kwargs["json_schema"] == {"type": "object", "title": "brain"}
    assert kwargs["append_system_prompt_file"] == "/tmp/prefix.md"
    assert kwargs["codex_bin"] == "fake-codex"


# -- (c) hybrid profile ------------------------------------------------------

def test_hybrid_profile_routes_brain_reviewer_to_claude_and_worker_auto(root, calls):
    runner = _runner(root, "hybrid")
    assert runner.role_providers == {"brain": "anthropic", "worker": "auto", "reviewer": "anthropic"}
    assert runner.default_worker_provider == "auto"
    assert _run(runner, "brain").provider == "claude"
    assert _run(runner, "reviewer").provider == "claude"
    assert [name for name, _ in calls] == ["claude", "claude"]
    # Worker "auto" without a quota router prefers Gemini for normal work.
    worker = _run(runner, "worker")
    assert worker.provider == "gemini"
    assert worker.provider_route == "auto_normal_gemini"
    assert calls[-1][0] == "gemini"


# -- (d) load_profile resolution ----------------------------------------------

def test_load_profile_falls_back_to_claude_with_one_warning(root, tmp_path, monkeypatch, caplog):
    monkeypatch.delenv(provider_registry.PROFILE_FILE_ENV, raising=False)
    monkeypatch.setattr(provider_registry, "ACTIVE_PROFILE_FILE", tmp_path / "missing-active.json")
    with caplog.at_level(logging.WARNING, logger="core.provider_registry"):
        profile = provider_registry.load_profile()
    assert profile["profile"] == "claude"
    assert profile["source"] == "fallback"
    assert profile["source_path"] == str(root / "profiles" / "claude.json")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "active.json" in warnings[0].getMessage()
    assert profile["roles"] == {"brain": "anthropic", "worker": "anthropic", "reviewer": "anthropic"}


def test_load_profile_prefers_active_json_over_fallback(root, tmp_path, monkeypatch, caplog):
    monkeypatch.delenv(provider_registry.PROFILE_FILE_ENV, raising=False)
    active = tmp_path / "active.json"
    active.write_text(json.dumps(_load(root, "openai")), encoding="utf-8")
    monkeypatch.setattr(provider_registry, "ACTIVE_PROFILE_FILE", active)
    with caplog.at_level(logging.WARNING, logger="core.provider_registry"):
        profile = provider_registry.load_profile()
    assert profile["profile"] == "openai" and profile["source"] == "active"
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_load_profile_honours_env_var_and_explicit_path(root, tmp_path, monkeypatch):
    active = tmp_path / "active.json"
    active.write_text(json.dumps(_load(root, "claude")), encoding="utf-8")
    monkeypatch.setattr(provider_registry, "ACTIVE_PROFILE_FILE", active)
    env_file = tmp_path / "env-profile.json"
    env_file.write_text(json.dumps(_load(root, "hybrid")), encoding="utf-8")
    monkeypatch.setenv(provider_registry.PROFILE_FILE_ENV, str(env_file))
    profile = provider_registry.load_profile()
    assert profile["profile"] == "hybrid" and profile["source"] == provider_registry.PROFILE_FILE_ENV
    # An explicit path wins over the environment variable.
    explicit = provider_registry.load_profile(root / "profiles" / "openai.json")
    assert explicit["profile"] == "openai" and explicit["source"] == "explicit"


def test_load_profile_fails_closed_on_missing_or_invalid_explicit_file(tmp_path, monkeypatch):
    monkeypatch.setenv(provider_registry.PROFILE_FILE_ENV, str(tmp_path / "nope.json"))
    with pytest.raises(provider_registry.ProfileError):
        provider_registry.load_profile()
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"profile": "bad", "roles": {"brain": "acme", "worker": "openai", "reviewer": "openai"}}),
                   encoding="utf-8")
    with pytest.raises(provider_registry.ProfileError):
        provider_registry.load_profile(bad)


def test_provider_for_role(root):
    profile = _load(root, "hybrid")
    assert provider_registry.provider_for_role(profile, "brain") == "anthropic"
    assert provider_registry.provider_for_role(profile, "worker") == "auto"
    with pytest.raises(ValueError):
        provider_registry.provider_for_role(profile, "janitor")


def test_build_runner_uses_load_profile_by_default(root, tmp_path, monkeypatch, calls):
    active = tmp_path / "active.json"
    active.write_text(json.dumps(_load(root, "openai")), encoding="utf-8")
    monkeypatch.delenv(provider_registry.PROFILE_FILE_ENV, raising=False)
    monkeypatch.setattr(provider_registry, "ACTIVE_PROFILE_FILE", active)
    runner = providers.build_runner(claude_bin="fake-claude", codex_bin="fake-codex")
    assert runner.profile["profile"] == "openai"
    assert _run(runner, "reviewer").provider == "codex"
    assert [name for name, _ in calls] == ["codex"]


# -- (e) explicit job worker_provider overrides the profile ---------------------

def test_explicit_job_worker_provider_overrides_profile_default(root, calls):
    runner = _runner(root, "openai")
    assert _run(runner, "worker", worker_provider="claude").provider == "claude"
    assert _run(runner, "worker", worker_provider="gemini").provider == "gemini"
    assert _run(runner, "worker").provider == "codex"
    assert [name for name, _ in calls] == ["claude", "gemini", "codex"]


def test_machine_worker_provider_default_comes_from_profile(root, tmp_path, monkeypatch):
    active = tmp_path / "active.json"
    active.write_text(json.dumps(_load(root, "openai")), encoding="utf-8")
    monkeypatch.delenv(provider_registry.PROFILE_FILE_ENV, raising=False)
    monkeypatch.setattr(provider_registry, "ACTIVE_PROFILE_FILE", active)
    job = {"job_id": "job-1", "repo": "example/repo", "branch": "feature", "mode": "read", "goal": "g"}
    runtime = machine.new_runtime(job, {"repo_path": str(tmp_path)})
    assert runtime["config"]["worker_provider"] == "codex"
    explicit = machine.new_runtime(job, {"repo_path": str(tmp_path), "worker_provider": "claude"})
    assert explicit["config"]["worker_provider"] == "claude"
    assert providers.profile_worker_provider(_load(root, "hybrid")) == "auto"
    assert providers.profile_worker_provider(_load(root, "claude")) == "claude"


# -- google for brain/reviewer is a structured configuration failure --------------

def test_google_brain_or_reviewer_is_a_config_failure_not_claude(root, calls):
    profile = {"profile": "google-brain", "roles": {"brain": "google", "worker": "google", "reviewer": "google"}}
    runner = providers.build_runner(claude_bin="fake-claude", codex_bin="fake-codex", profile=profile)
    for role in ("brain", "reviewer"):
        result = _run(runner, role)
        assert result.exit_code != 0 and result.provider == "google"
        assert "provider configuration error" in result.error and role in result.error
        assert classify.classify(result).category == classify.CONFIG
    assert calls == [], "no provider may be called for an unroutable role"
    assert _run(runner, "worker").provider == "gemini"
    assert [name for name, _ in calls] == ["gemini"]


def test_invalid_codex_sandbox_is_a_structured_failure(tmp_path):
    from core import codex_runner

    result = codex_runner.run_codex("p", cwd=tmp_path, json_schema={}, codex_bin="fake-codex", sandbox="danger")
    assert result.exit_code != 0 and result.provider == "codex"
    assert classify.classify(result).category == classify.CONFIG
