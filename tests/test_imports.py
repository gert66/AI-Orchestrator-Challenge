"""Every module under core/ and monitor/ imports without side effects or errors."""
import importlib
import pkgutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _modules():
    found = []
    for package in ("core", "monitor"):
        for info in pkgutil.iter_modules([str(ROOT / package)]):
            found.append(f"{package}.{info.name}")
    return sorted(found)


MODULES = _modules()


def test_module_discovery():
    assert "core.contracts" in MODULES
    assert "core.machine" in MODULES
    assert "core.supervisor" in MODULES
    assert "monitor.collect_status" in MODULES
    assert not (ROOT / "providers").exists(), "providers/ duplicates core/ runners; use core.*"


@pytest.mark.parametrize("name", MODULES)
def test_import(name):
    importlib.import_module(name)


def test_notify_degrades_without_hook(tmp_path):
    from core import notify

    missing = tmp_path / "missing.sh"
    assert notify.is_enabled(missing) is False
    assert notify.notify("DONE", "job-1", "detail", script=missing) is False
    assert notify.notify("WORKING", "job-1", script=missing) is False


def test_hermes_advisor_degrades_without_wrapper(tmp_path, monkeypatch):
    from core import hermes_advisor

    missing = tmp_path / "missing.sh"
    monkeypatch.setattr(hermes_advisor, "EXPORT_SCRIPT", missing)
    monkeypatch.setattr(hermes_advisor, "WRAPPER", missing)
    assert hermes_advisor.is_available() is False

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "job.json").write_text('{"job_id": "job-1"}', encoding="utf-8")
    rec = hermes_advisor.request_advice(job_dir, "unit", "why")
    assert rec["ok"] is False
    assert rec["job_id"] == "job-1"
    assert rec["error"].startswith("disabled:")
    assert not (job_dir / hermes_advisor.ADVICE_FILE).exists()  # no side effects when disabled

    # Even a job dir without job.json must not raise.
    rec = hermes_advisor.request_advice(tmp_path / "nojob", "unit", "why")
    assert rec["ok"] is False and rec["job_id"] == "nojob"


def test_gemini_runner_reads_env_before_optional_secrets(tmp_path):
    from core import gemini_runner

    # Missing secrets file leaves env untouched.
    assert gemini_runner.load_secrets(tmp_path / "no-secrets.env", environ={}) == {}
    secrets = tmp_path / "s.env"
    secrets.write_text("# comment\nGEMINI_API_KEY='from-file'\n", encoding="utf-8")
    # Environment wins over the file.
    env = {"GEMINI_API_KEY": "from-env"}
    assert gemini_runner.load_secrets(secrets, environ=env)["GEMINI_API_KEY"] == "from-env"
    # File is used only as a fallback.
    assert gemini_runner.load_secrets(secrets, environ={})["GEMINI_API_KEY"] == "from-file"
    # The default location must not be tied to any other tool's config.
    assert "hermes" not in str(gemini_runner.DEFAULT_SECRETS_FILE)
