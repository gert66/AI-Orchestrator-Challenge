"""Offline tests for core/acceptance.py (the on-host acceptance runner).

Everything runs against tmp_path with monkeypatched paths and a fake
``run_command``: no root, no systemctl, no provider CLI, no network.
"""
import io
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core import acceptance

ROOT = Path(__file__).resolve().parents[1]
BOOT_A = "11111111-1111-1111-1111-111111111111"
BOOT_B = "22222222-2222-2222-2222-222222222222"


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _stamp(profile="claude", boot_id=BOOT_A, **extra):
    data = {"installed_at": "2026-09-19T10:00:00Z", "installed_epoch": 1_789_000_000.0, "profile": profile,
            "kit_commit": "abc123", "boot_id": boot_id}
    data.update(extra)
    return data


@pytest.fixture
def kit(tmp_path):
    """A fake installed kit: real profiles, empty policy, no venv, no .git."""
    root = tmp_path / "kit"
    (root / "profiles").mkdir(parents=True)
    for name in acceptance.PROFILES:
        shutil.copy(ROOT / "profiles" / f"{name}.json", root / "profiles" / f"{name}.json")
    (root / "monitor").mkdir()
    (root / "installer").mkdir()
    _write_json(root / "config" / "repo_policy.json", {"schema_version": 1, "repos": {}})
    return root


@pytest.fixture
def make_ctx(kit, tmp_path):
    def build(profile="claude", *, phase=None, job_id=None, stamp=None, boot_id=BOOT_A):
        stamp_file = tmp_path / "stamp" / "install-stamp.json"
        if stamp is not None:
            _write_json(stamp_file, stamp)
        ctx = acceptance.Context(profile, phase=phase, job_id=job_id, kit_root=kit, stamp_file=stamp_file,
                                 boot_id=boot_id, out_dir=tmp_path / "out")
        ctx.jobs_dir = tmp_path / "jobs"
        ctx.cli_bin_dir = tmp_path / "bin"
        return ctx
    return build


def _fake_run(mapping):
    """run_command replacement: mapping of argv[0]/argv[-1] tokens -> (exit_code, stdout)."""
    calls = []

    def run(argv, **kwargs):
        argv = [str(a) for a in argv]
        calls.append(argv)
        for token, (code, out) in mapping.items():
            if token in argv:
                return {"argv": argv, "exit_code": code, "stdout": out, "stderr": "", "error": None}
        return {"argv": argv, "exit_code": -1, "stdout": "", "stderr": "", "error": f"unexpected command {argv}"}
    run.calls = calls
    return run


# --- module surface ----------------------------------------------------------

def test_help_exits_zero():
    proc = subprocess.run([sys.executable, "-m", "core.acceptance", "--help"], cwd=str(ROOT),
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "--phase" in proc.stdout and "--job-id" in proc.stdout and "--out-dir" in proc.stdout
    with pytest.raises(SystemExit) as exc:
        acceptance.main(["--help"], stdout=io.StringIO(), stderr=io.StringIO())
    assert exc.value.code == 0


def test_check_ids_are_the_documented_set():
    assert acceptance.CHECK_IDS == ("install_stamp", "reboot_survived", "diagnose", "systemd_health", "monitor_fresh",
                                    "preflight", "pytest_suite", "profile_routing", "provider_cli_login",
                                    "gemini_secret", "real_job_flow")
    assert len(set(acceptance.CHECK_IDS)) == len(acceptance.CHECK_IDS)


def test_context_rejects_bad_profile_and_phase(kit):
    with pytest.raises(ValueError):
        acceptance.Context("gemini", kit_root=kit, boot_id=BOOT_A)
    with pytest.raises(ValueError):
        acceptance.Context("claude", phase="during-lunch", kit_root=kit, boot_id=BOOT_A)


# --- install stamp -----------------------------------------------------------

def test_load_stamp_missing_invalid_and_incomplete(tmp_path):
    missing = tmp_path / "nope.json"
    data, err = acceptance.load_stamp(missing)
    assert data is None and "missing" in err
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    data, err = acceptance.load_stamp(bad)
    assert data is None and "not valid JSON" in err
    _write_json(tmp_path / "list.json", [1, 2])
    data, err = acceptance.load_stamp(tmp_path / "list.json")
    assert data is None and "not a JSON object" in err
    partial = _write_json(tmp_path / "partial.json", {"installed_at": "x", "profile": "claude"})
    data, err = acceptance.load_stamp(partial)
    assert data == {"installed_at": "x", "profile": "claude"}
    assert "installed_epoch" in err and "kit_commit" in err and "boot_id" in err


def test_check_install_stamp_pass_records_profile_and_commit(make_ctx):
    ctx = make_ctx(stamp=_stamp(profile="openai"))
    out = acceptance.check_install_stamp(ctx)
    assert out["status"] == "PASS"
    assert out["evidence"]["profile"] == "openai" and out["evidence"]["kit_commit"] == "abc123"
    assert "requested profile=claude" in out["detail"]  # mismatch is visible, not hidden
    assert ctx.stamp["boot_id"] == BOOT_A


def test_check_install_stamp_missing_fails(make_ctx):
    ctx = make_ctx(stamp=None)
    out = acceptance.check_install_stamp(ctx)
    assert out["status"] == "FAIL" and "missing" in out["detail"]
    assert out["evidence"]["path"].endswith("install-stamp.json")


# --- reboot detection --------------------------------------------------------

@pytest.mark.parametrize("stamp_boot,current,forced,expected", [
    (BOOT_A, BOOT_A, None, ("SKIP", "post-install")),
    (BOOT_A, BOOT_B, None, ("PASS", "post-reboot")),
    (BOOT_A, BOOT_B, "post-reboot", ("PASS", "post-reboot")),
    (BOOT_A, BOOT_A, "post-reboot", ("FAIL", "post-reboot")),
    (BOOT_A, BOOT_A, "post-install", ("SKIP", "post-install")),
])
def test_detect_reboot(stamp_boot, current, forced, expected):
    status, detail, phase = acceptance.detect_reboot(stamp_boot, current, forced)
    assert (status, phase) == expected
    if status == "SKIP":
        assert detail == "no reboot since install"
    if status == "FAIL":
        assert "unchanged" in detail


def test_check_reboot_survived_sets_context_phase(make_ctx):
    ctx = make_ctx(stamp=_stamp(boot_id=BOOT_A), boot_id=BOOT_B)
    acceptance.check_install_stamp(ctx)
    out = acceptance.check_reboot_survived(ctx)
    assert out["status"] == "PASS" and ctx.phase == "post-reboot"
    assert out["evidence"] == {"current_boot_id": BOOT_B, "stamp_boot_id": BOOT_A, "forced_phase": None,
                               "phase": "post-reboot"}

    same = make_ctx(stamp=_stamp(boot_id=BOOT_A), boot_id=BOOT_A)
    acceptance.check_install_stamp(same)
    out = acceptance.check_reboot_survived(same)
    assert out["status"] == "SKIP" and same.phase == "post-install"

    forced = make_ctx(phase="post-reboot", stamp=_stamp(boot_id=BOOT_A), boot_id=BOOT_A)
    acceptance.check_install_stamp(forced)
    out = acceptance.check_reboot_survived(forced)
    assert out["status"] == "FAIL" and forced.phase == "post-reboot"


def test_check_reboot_survived_without_stamp_fails(make_ctx):
    ctx = make_ctx(stamp=None, phase="post-reboot")
    acceptance.check_install_stamp(ctx)
    out = acceptance.check_reboot_survived(ctx)
    assert out["status"] == "FAIL" and "cannot determine reboot" in out["detail"]
    assert ctx.phase == "post-reboot"


# --- aggregation -------------------------------------------------------------

def _const(status, detail="d"):
    return lambda ctx: {"status": status, "detail": detail, "evidence": {"k": 1}}


def test_run_checks_skip_and_manual_do_not_fail(make_ctx):
    ctx = make_ctx(stamp=_stamp())
    report = acceptance.run_checks(ctx, checks=(("a", "A", _const("PASS")), ("b", "B", _const("SKIP")),
                                                 ("c", "C", _const("MANUAL"))))
    assert report["result"] == "PASS"
    assert [c["status"] for c in report["checks"]] == ["PASS", "SKIP", "MANUAL"]
    assert report["schema_version"] == 1 and report["profile"] == "claude" and report["phase"] == "post-install"
    assert set(report["host"]) == {"hostname", "kernel", "boot_id", "uptime_seconds"}
    assert report["host"]["boot_id"] == BOOT_A
    assert report["kit_commit"] == "unknown"  # no .git under the fake kit and the stamp was not read


def test_run_checks_any_fail_fails_and_crash_is_fail(make_ctx):
    def boom(ctx):
        raise RuntimeError("kaboom")

    def bogus(ctx):
        return {"status": "MAYBE", "detail": "?"}

    ctx = make_ctx(stamp=_stamp())
    report = acceptance.run_checks(ctx, checks=(("a", "A", _const("PASS")), ("b", "B", _const("FAIL")),
                                                 ("c", "C", boom), ("d", "D", bogus)))
    assert report["result"] == "FAIL"
    statuses = {c["id"]: c["status"] for c in report["checks"]}
    assert statuses == {"a": "PASS", "b": "FAIL", "c": "FAIL", "d": "FAIL"}
    assert "kaboom" in next(c for c in report["checks"] if c["id"] == "c")["detail"]
    assert "invalid status" in next(c for c in report["checks"] if c["id"] == "d")["detail"]


# --- artifacts and the single result line -----------------------------------

def _run_main(monkeypatch, tmp_path, kit, argv, checks):
    monkeypatch.setattr(acceptance, "CHECKS", checks)
    out, err = io.StringIO(), io.StringIO()
    out_dir = tmp_path / "evidence"
    rc = acceptance.main([*argv, "--out-dir", str(out_dir), "--kit-root", str(kit),
                          "--stamp-file", str(tmp_path / "stamp" / "install-stamp.json")], stdout=out, stderr=err)
    return rc, out.getvalue(), err.getvalue(), out_dir


def test_main_pass_writes_artifacts_and_one_line(monkeypatch, tmp_path, kit):
    monkeypatch.setattr(acceptance, "read_boot_id", lambda: BOOT_B)
    _write_json(tmp_path / "stamp" / "install-stamp.json", _stamp(boot_id=BOOT_A))
    checks = (acceptance.CHECKS[0], acceptance.CHECKS[1], ("m", "manual", _const("MANUAL")),
              ("s", "skip", _const("SKIP")))
    rc, out, err, out_dir = _run_main(monkeypatch, tmp_path, kit, ["claude"], checks)
    assert rc == 0 and err == ""
    lines = out.splitlines()
    assert len(lines) == 1
    json_files = sorted(out_dir.glob("acceptance-*.json"))
    md_files = sorted(out_dir.glob("acceptance-*.md"))
    assert len(json_files) == 1 and len(md_files) == 1
    assert lines[0] == f"ACCEPTANCE PASS phase=post-reboot profile=claude evidence={json_files[0]}"

    report = json.loads(json_files[0].read_text(encoding="utf-8"))
    assert set(report) >= {"schema_version", "started_at", "finished_at", "host", "profile", "kit_commit", "phase",
                           "checks", "result"}
    assert report["result"] == "PASS" and report["phase"] == "post-reboot" and report["kit_commit"] == "abc123"
    for check in report["checks"]:
        assert set(check) == {"id", "title", "status", "detail", "evidence"}
    assert [c["id"] for c in report["checks"]] == ["install_stamp", "reboot_survived", "m", "s"]
    assert [c["status"] for c in report["checks"]] == ["PASS", "PASS", "MANUAL", "SKIP"]

    md = md_files[0].read_text(encoding="utf-8")
    assert md.startswith("# Acceptance PASS - phase=post-reboot profile=claude")
    assert "| check | status | detail |" in md
    for cid in ("install_stamp", "reboot_survived", "m", "s"):
        assert f"| `{cid}` |" in md


def test_main_fail_exit_1_one_stderr_line(monkeypatch, tmp_path, kit):
    monkeypatch.setattr(acceptance, "read_boot_id", lambda: BOOT_A)
    _write_json(tmp_path / "stamp" / "install-stamp.json", _stamp(boot_id=BOOT_A))
    checks = (acceptance.CHECKS[0], acceptance.CHECKS[1], ("x", "extra", _const("FAIL")))
    rc, out, err, out_dir = _run_main(monkeypatch, tmp_path, kit, ["claude", "--phase", "post-reboot"], checks)
    assert rc == 1 and out == ""
    lines = err.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("ACCEPTANCE FAIL 2 check(s) failed")
    assert "reboot_survived" in lines[0] and "x" in lines[0]
    report = json.loads(next(out_dir.glob("acceptance-*.json")).read_text(encoding="utf-8"))
    assert report["result"] == "FAIL" and report["phase"] == "post-reboot"
    statuses = {c["id"]: c["status"] for c in report["checks"]}
    assert statuses == {"install_stamp": "PASS", "reboot_survived": "FAIL", "x": "FAIL"}


def test_main_skip_only_before_reboot_passes(monkeypatch, tmp_path, kit):
    monkeypatch.setattr(acceptance, "read_boot_id", lambda: BOOT_A)
    _write_json(tmp_path / "stamp" / "install-stamp.json", _stamp(boot_id=BOOT_A))
    checks = (acceptance.CHECKS[0], acceptance.CHECKS[1])
    rc, out, err, _ = _run_main(monkeypatch, tmp_path, kit, ["openai"], checks)
    assert rc == 0 and err == ""
    assert out.startswith("ACCEPTANCE PASS phase=post-install profile=openai evidence=")


def test_write_artifacts_never_overwrites(tmp_path):
    report = {"schema_version": 1, "started_at": "", "finished_at": "", "host": {"hostname": "h", "kernel": "k",
              "boot_id": "b", "uptime_seconds": 1}, "profile": "claude", "kit_commit": "x", "phase": "post-install",
              "job_id": None, "checks": [], "result": "PASS"}
    first = acceptance.write_artifacts(report, tmp_path / "ev")
    second = acceptance.write_artifacts(report, tmp_path / "ev")
    assert first != second and all(p.exists() for p in first + second)
    assert first[1].suffix == ".md" and first[0].suffix == ".json"


# --- individual host checks with a fake run_command --------------------------

def test_check_diagnose_pass_and_fail(monkeypatch, make_ctx):
    ctx = make_ctx()
    monkeypatch.setattr(acceptance, "run_command", _fake_run({"claude": (0, "OK x\nDIAGNOSE PASS profile=claude\n")}))
    out = acceptance.check_diagnose(ctx)
    assert out["status"] == "PASS" and out["evidence"]["exit_code"] == 0
    assert out["evidence"]["output_tail"][-1] == "DIAGNOSE PASS profile=claude"
    assert out["evidence"]["argv"][:2] == ["bash", str(ctx.kit_root / "installer" / "diagnose.sh")]

    monkeypatch.setattr(acceptance, "run_command", _fake_run({"claude": (1, "FAIL something\nDIAGNOSE FAIL\n")}))
    ctx.is_root = False
    out = acceptance.check_diagnose(ctx)
    assert out["status"] == "FAIL" and "exited 1" in out["detail"]
    assert "sudo is never attempted" in out["detail"]


def test_check_diagnose_tail_is_limited(monkeypatch, make_ctx):
    ctx = make_ctx()
    text = "\n".join(f"line {i}" for i in range(100)) + "\n"
    monkeypatch.setattr(acceptance, "run_command", _fake_run({"claude": (0, text)}))
    out = acceptance.check_diagnose(ctx)
    assert len(out["evidence"]["output_tail"]) == acceptance.OUTPUT_TAIL_LINES
    assert out["evidence"]["output_tail"][-1] == "line 99"


def test_check_systemd_health(monkeypatch, make_ctx):
    ctx = make_ctx()
    monkeypatch.setattr(acceptance.shutil, "which", lambda name: "/bin/systemctl" if name == "systemctl" else None)
    answers = {"is-enabled": (0, "enabled\n"), "is-active": (0, "active\n")}
    monkeypatch.setattr(acceptance, "run_command", _fake_run(answers))
    out = acceptance.check_systemd_health(ctx)
    assert out["status"] == "PASS"
    assert set(out["evidence"]["units"]) == {*acceptance.TIMERS, acceptance.HEALTH_SERVICE}
    assert out["evidence"]["units"]["orchestrator-health.timer"] == {"is_enabled": "enabled", "is_active": "active"}

    answers = {"is-enabled": (0, "enabled\n"), "is-active": (3, "inactive\n")}
    monkeypatch.setattr(acceptance, "run_command", _fake_run(answers))
    out = acceptance.check_systemd_health(ctx)
    assert out["status"] == "FAIL" and "orchestrator-watchdog.timer" in out["detail"]

    monkeypatch.setattr(acceptance.shutil, "which", lambda name: None)
    assert acceptance.check_systemd_health(ctx)["status"] == "FAIL"


def test_check_monitor_fresh(make_ctx):
    ctx = make_ctx()
    assert acceptance.check_monitor_fresh(ctx)["status"] == "FAIL"
    _write_json(ctx.status_file, {"timestamp": time.time() - 30, "profile": "claude"})
    out = acceptance.check_monitor_fresh(ctx)
    assert out["status"] == "PASS" and 0 <= out["evidence"]["age_seconds"] < 600
    _write_json(ctx.status_file, {"timestamp": time.time() - 3600})
    assert acceptance.check_monitor_fresh(ctx)["status"] == "FAIL"
    ctx.status_file.write_text("nope", encoding="utf-8")
    assert acceptance.check_monitor_fresh(ctx)["status"] == "FAIL"


def test_check_preflight_uses_venv_path_when_present(monkeypatch, make_ctx):
    ctx = make_ctx()
    seen = {}

    def run(argv, **kwargs):
        seen["argv"], seen["env"] = [str(a) for a in argv], kwargs.get("env") or {}
        return {"argv": seen["argv"], "exit_code": 0, "stdout": "PREFLIGHT PASS\n", "stderr": "", "error": None}
    monkeypatch.setattr(acceptance, "run_command", run)
    out = acceptance.check_preflight(ctx)
    assert out["status"] == "PASS" and out["evidence"]["venv_on_path"] is False
    assert seen["argv"] == ["bash", str(ctx.kit_root / "installer" / "preflight.sh")]

    ctx.venv_python.parent.mkdir(parents=True)
    ctx.venv_python.write_text("", encoding="utf-8")
    out = acceptance.check_preflight(ctx)
    assert out["evidence"]["venv_on_path"] is True
    assert seen["env"]["PATH"].startswith(str(ctx.venv_python.parent))


def test_check_pytest_suite_skips_without_venv_and_records_summary(monkeypatch, make_ctx):
    ctx = make_ctx()
    out = acceptance.check_pytest_suite(ctx)
    assert out["status"] == "SKIP" and "venv python missing" in out["detail"]

    ctx.venv_python.parent.mkdir(parents=True)
    ctx.venv_python.write_text("", encoding="utf-8")
    monkeypatch.setattr(acceptance, "run_command", _fake_run({"tests": (0, "....\n12 passed in 0.5s\n")}))
    out = acceptance.check_pytest_suite(ctx)
    assert out["status"] == "PASS" and out["evidence"]["summary"] == "12 passed in 0.5s"
    monkeypatch.setattr(acceptance, "run_command", _fake_run({"tests": (1, "F\n1 failed in 0.5s\n")}))
    assert acceptance.check_pytest_suite(ctx)["status"] == "FAIL"


# --- profile routing ---------------------------------------------------------

def test_profile_routing_passes_for_shipped_profiles(make_ctx):
    ctx = make_ctx()
    out = acceptance.check_profile_routing(ctx)
    assert out["status"] == "PASS", out["detail"]
    matrix = out["evidence"]["matrix"]
    assert set(matrix) == {"claude", "openai", "hybrid"}
    for name in matrix:
        assert set(matrix[name]) == {"brain", "worker", "reviewer"}
    assert set(matrix["openai"].values()) == {"openai"}
    assert out["evidence"]["openai_routes_to_anthropic"] == []
    assert matrix["claude"] == {"brain": "anthropic", "worker": "anthropic", "reviewer": "anthropic"}
    assert matrix["hybrid"]["worker"] == "auto"


def test_profile_routing_fails_on_hidden_claude_dependency(make_ctx):
    ctx = make_ctx()
    _write_json(ctx.profiles_dir / "openai.json",
                {"profile": "openai", "roles": {"brain": "anthropic", "worker": "openai", "reviewer": "openai"}})
    out = acceptance.check_profile_routing(ctx)
    assert out["status"] == "FAIL" and "hidden Claude dependency" in out["detail"]
    assert out["evidence"]["openai_routes_to_anthropic"] == ["brain"]


def test_profile_routing_fails_on_broken_profile(make_ctx):
    ctx = make_ctx()
    (ctx.profiles_dir / "hybrid.json").write_text("{", encoding="utf-8")
    out = acceptance.check_profile_routing(ctx)
    assert out["status"] == "FAIL" and "error" in out["evidence"]["matrix"]["hybrid"]


# --- provider CLIs -----------------------------------------------------------

@pytest.mark.parametrize("profile,clis", [("claude", ["claude"]), ("openai", ["codex"]), ("hybrid", ["claude"])])
def test_required_clis_follow_the_registry(make_ctx, profile, clis):
    assert acceptance.required_clis(make_ctx(profile)) == clis


def test_provider_cli_login_manual_or_fail(monkeypatch, make_ctx):
    ctx = make_ctx("openai")
    monkeypatch.setattr(acceptance.shutil, "which", lambda name: None)
    out = acceptance.check_provider_cli_login(ctx)
    assert out["status"] == "FAIL" and "codex" in out["detail"]
    assert out["evidence"]["codex"] == {"path": None, "version_exit_code": None}

    monkeypatch.setattr(acceptance.shutil, "which", lambda name: f"/fake/{name}")
    monkeypatch.setattr(acceptance, "run_command", _fake_run({"--version": (0, "codex 1.2.3\n")}))
    out = acceptance.check_provider_cli_login(ctx)
    assert out["status"] == "MANUAL" and out["detail"] == "interactive login required; see docs/INSTALL.md"
    assert out["evidence"]["codex"]["version_exit_code"] == 0 and out["evidence"]["codex"]["version"] == "codex 1.2.3"


# --- Gemini API key on the host -----------------------------------------------

FAKE_GEMINI_KEY = "AIza" + "ACCEPTANCE" + "0123456789abcdefghijklmnop"


def _secrets(ctx, tmp_path, value=FAKE_GEMINI_KEY, dir_mode=0o700, file_mode=0o600):
    path = tmp_path / "orch-home" / ".config" / "ai-orchestrator" / "secrets.env"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(dir_mode)
    path.write_text(f"# placed by installer/gemini-secret.sh\nGEMINI_API_KEY={value}\n", encoding="utf-8")
    path.chmod(file_mode)
    ctx.secrets_file = path
    ctx.secrets_owner = path.owner()
    return path


@pytest.mark.parametrize("profile,uses", [("claude", False), ("openai", False), ("hybrid", True)])
def test_profile_uses_gemini_follows_extras(kit, profile, uses):
    assert acceptance.profile_uses_gemini(kit / "profiles" / f"{profile}.json") is uses
    assert acceptance.profile_uses_gemini(kit / "profiles" / "missing.json") is False


def test_gemini_secret_skips_for_profiles_without_gemini(make_ctx, tmp_path):
    for profile in ("claude", "openai"):
        ctx = make_ctx(profile)
        ctx.secrets_file = tmp_path / "never-looked-at.env"
        out = acceptance.check_gemini_secret(ctx)
        assert out["status"] == "SKIP" and profile in out["detail"]
        assert out["evidence"]["uses_gemini"] is False


def test_gemini_secret_absent_is_manual_never_fail(make_ctx, tmp_path):
    ctx = make_ctx("hybrid")
    ctx.secrets_file = tmp_path / "orch-home" / ".config" / "ai-orchestrator" / "secrets.env"
    out = acceptance.check_gemini_secret(ctx)
    assert out["status"] == "MANUAL"
    assert "not placed yet" in out["detail"] and "gemini-secret.sh set" in out["detail"] and "Claude Sonnet" in out["detail"]
    assert out["evidence"]["exists"] is False and out["evidence"]["uses_gemini"] is True
    # directory present (maybe with lax modes) but no file: still the manual step
    ctx.secrets_file.parent.mkdir(parents=True)
    ctx.secrets_file.parent.chmod(0o755)
    out = acceptance.check_gemini_secret(ctx)
    assert out["status"] == "MANUAL" and out["evidence"]["exists"] is False
    # the whole run stays PASS with the key absent
    report = acceptance.run_checks(ctx, checks=(("gemini_secret", "g", acceptance.check_gemini_secret),))
    assert report["result"] == "PASS"


def test_gemini_secret_present_and_private_passes_without_recording_the_value(make_ctx, tmp_path):
    ctx = make_ctx("hybrid")
    path = _secrets(ctx, tmp_path)
    out = acceptance.check_gemini_secret(ctx)
    assert out["status"] == "PASS", out
    assert out["evidence"]["key_present"] is True
    assert out["evidence"]["dir_mode"] == "700" and out["evidence"]["file_mode"] == "600"
    assert out["evidence"]["file_owner"] == path.owner()
    assert FAKE_GEMINI_KEY not in json.dumps(out)
    # quoted values are accepted exactly like the runner accepts them
    path.write_text(f"GEMINI_API_KEY='{FAKE_GEMINI_KEY}'\n", encoding="utf-8")
    assert acceptance.check_gemini_secret(ctx)["status"] == "PASS"


@pytest.mark.parametrize("dir_mode,file_mode,expect", [
    (0o755, 0o600, "expected 700"),
    (0o700, 0o644, "expected 600"),
    (0o700, 0o640, "expected 600"),
])
def test_gemini_secret_insecure_modes_fail(make_ctx, tmp_path, dir_mode, file_mode, expect):
    ctx = make_ctx("hybrid")
    _secrets(ctx, tmp_path, dir_mode=dir_mode, file_mode=file_mode)
    out = acceptance.check_gemini_secret(ctx)
    assert out["status"] == "FAIL" and expect in out["detail"] and "gemini-secret.sh" in out["detail"]
    assert FAKE_GEMINI_KEY not in json.dumps(out)


def test_gemini_secret_empty_value_or_wrong_owner_fails(make_ctx, tmp_path):
    ctx = make_ctx("hybrid")
    _secrets(ctx, tmp_path, value="")
    out = acceptance.check_gemini_secret(ctx)
    assert out["status"] == "FAIL" and "missing or empty" in out["detail"] and out["evidence"]["key_present"] is False
    _secrets(ctx, tmp_path)
    ctx.secrets_owner = "orchestrator-someone-else"
    out = acceptance.check_gemini_secret(ctx)
    assert out["status"] == "FAIL" and "expected orchestrator-someone-else" in out["detail"]


def test_gemini_secret_unreadable_is_fail_with_root_hint(make_ctx, tmp_path, monkeypatch):
    ctx = make_ctx("hybrid")
    _secrets(ctx, tmp_path)
    ctx.is_root = False

    def denied(path, owner):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(acceptance, "inspect_secrets_file", denied)
    out = acceptance.check_gemini_secret(ctx)
    assert out["status"] == "FAIL" and "cannot inspect" in out["detail"] and "sudo" in out["detail"]


# --- real job flow -----------------------------------------------------------

def _job(ctx, job_id, phase="DONE", history=None, repo="owner/name"):
    job_dir = ctx.jobs_dir / job_id
    _write_json(job_dir / "job.json", {"job_id": job_id, "repo": repo, "mode": "write"})
    _write_json(job_dir / "state.json", {"job_id": job_id, "phase": phase, "runtime": {"history": history or []}})
    return job_dir


def _entry(**over):
    entry = {"step_id": "01-step", "outcome": "committed", "verdict": "PASS", "repairs": 1, "reviews": 2,
             "commit": "deadbeef", "paths": ["a.py"]}
    entry.update(over)
    return entry


def _policy(ctx, push):
    _write_json(ctx.policy_file, {"schema_version": 1, "repos": {"owner/name": {
        "expected_origin": "https://example.invalid/owner/name.git", "canonical_base_branch": "main",
        "push_job_branch": push}}})


def test_real_job_flow_skips_without_job_id(make_ctx):
    out = acceptance.check_real_job_flow(make_ctx())
    assert out["status"] == "SKIP" and "--job-id" in out["detail"]


def test_real_job_flow_pass_without_push_policy(make_ctx):
    ctx = make_ctx(job_id="j1")
    _job(ctx, "j1", history=[_entry()])
    _policy(ctx, push=False)
    out = acceptance.check_real_job_flow(ctx)
    assert out["status"] == "PASS", out["detail"]
    assert out["evidence"]["push_required"] is False
    assert out["evidence"]["history"] == [{"step_id": "01-step", "outcome": "committed", "verdict": "PASS",
                                           "repairs": 1, "reviews": 2, "commit": "deadbeef", "pushed": None}]


def test_real_job_flow_requires_push_when_policy_says_so(make_ctx):
    ctx = make_ctx(job_id="j2")
    _policy(ctx, push=True)
    _job(ctx, "j2", history=[_entry(pushed=False, push_error="denied")])
    out = acceptance.check_real_job_flow(ctx)
    assert out["status"] == "FAIL" and "not pushed" in out["detail"]
    _job(ctx, "j2", history=[_entry(pushed=True, pushed_to="origin/job/j2")])
    out = acceptance.check_real_job_flow(ctx)
    assert out["status"] == "PASS" and out["evidence"]["push_required"] is True
    assert out["evidence"]["history"][0]["pushed"] is True


def test_real_job_flow_fails_when_not_done_or_never_committed(make_ctx):
    ctx = make_ctx(job_id="j3")
    _policy(ctx, push=False)
    _job(ctx, "j3", phase="NEEDS_HUMAN", history=[_entry()])
    out = acceptance.check_real_job_flow(ctx)
    assert out["status"] == "FAIL" and "expected DONE" in out["detail"]
    _job(ctx, "j3", phase="DONE", history=[_entry(outcome="dry-run (not committed)")])
    out = acceptance.check_real_job_flow(ctx)
    assert out["status"] == "FAIL" and "committed" in out["detail"]
    ctx.job_id = "missing"
    out = acceptance.check_real_job_flow(ctx)
    assert out["status"] == "FAIL" and "cannot read job" in out["detail"]


def test_real_job_flow_records_missing_policy_without_requiring_push(make_ctx):
    ctx = make_ctx(job_id="j4")
    _job(ctx, "j4", history=[_entry()], repo="unregistered/repo")
    out = acceptance.check_real_job_flow(ctx)
    assert out["status"] == "PASS" and "policy_error" in out["evidence"] and out["evidence"]["push_required"] is False
