"""Static checks on the installer and systemd units.

Every installer script parses, every unit is well-formed INI with the sections
systemd needs, the fail-closed markers are present, and preflight passes from
the repo root. Nothing here needs root, systemd or the network.
"""
import configparser
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "installer"
SYSTEMD = ROOT / "systemd"
SCRIPTS = sorted(p.name for p in INSTALLER.glob("*.sh"))
UNITS = sorted(p.name for p in SYSTEMD.iterdir() if p.suffix in (".service", ".timer"))
TIMERS = [u for u in UNITS if u.endswith(".timer")]
REQUIRED_UNITS = {
    "orchestrator@.service",
    "orchestrator-health.service",
    "orchestrator-health.timer",
    "orchestrator-watchdog.service",
    "orchestrator-watchdog.timer",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _parse_unit(name: str) -> configparser.ConfigParser:
    # systemd units are INI-like; keys may repeat (Environment=), so keep the last
    # value only for this structural check and disable interpolation ("%i").
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    cp.read_string(_read(SYSTEMD / name), source=name)
    return cp


def test_installer_scripts_exist():
    for required in ("install.sh", "preflight.sh", "diagnose.sh", "install-systemd.sh",
                     "install-providers.sh", "install-node.sh", "create-user.sh", "watchdog.sh", "acceptance.sh",
                     "gemini-secret.sh"):
        assert required in SCRIPTS, f"installer/{required} missing"


@pytest.mark.parametrize("name", SCRIPTS)
def test_bash_syntax(name):
    proc = subprocess.run(["bash", "-n", str(INSTALLER / name)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("name", SCRIPTS)
def test_installer_scripts_are_executable_and_have_shebang(name):
    path = INSTALLER / name
    assert _read(path).startswith("#!/usr/bin/env bash"), f"{name}: missing bash shebang"
    assert os.access(path, os.X_OK), f"{name}: not executable"


def test_all_required_units_present():
    assert REQUIRED_UNITS <= set(UNITS), f"missing units: {sorted(REQUIRED_UNITS - set(UNITS))}"


@pytest.mark.parametrize("name", UNITS)
def test_unit_parses_with_required_sections(name):
    cp = _parse_unit(name)
    assert cp.has_section("Unit"), f"{name}: no [Unit] section"
    if name.endswith(".timer"):
        assert cp.has_section("Timer"), f"{name}: no [Timer] section"
        assert not cp.has_section("Service"), f"{name}: a timer must not carry [Service]"
    else:
        assert cp.has_section("Service"), f"{name}: no [Service] section"
        assert cp.get("Service", "ExecStart", fallback=""), f"{name}: [Service] needs ExecStart"


@pytest.mark.parametrize("name", TIMERS)
def test_timer_is_installed_into_timers_target(name):
    cp = _parse_unit(name)
    assert cp.has_section("Install"), f"{name}: timers need an [Install] section"
    assert cp.get("Install", "WantedBy", fallback="") == "timers.target"


@pytest.mark.parametrize("name", ["orchestrator-health.timer", "orchestrator-watchdog.timer"])
def test_recovery_timers_are_persistent(name):
    cp = _parse_unit(name)
    assert cp.get("Timer", "Persistent", fallback="").lower() == "true", f"{name}: Persistent=true required"
    assert cp.get("Timer", "OnBootSec", fallback=""), f"{name}: OnBootSec required so it fires after reboot"
    assert cp.get("Timer", "OnUnitActiveSec", fallback=""), f"{name}: OnUnitActiveSec required"


def test_health_service_survives_reboot_ordering():
    cp = _parse_unit("orchestrator-health.service")
    assert cp.get("Unit", "After", fallback="") == "network.target"
    assert cp.get("Service", "WorkingDirectory", fallback="") == "/opt/ai-orchestrator"
    assert cp.get("Service", "User", fallback="") == "orchestrator"


def test_watchdog_service_runs_watchdog_script_as_root():
    cp = _parse_unit("orchestrator-watchdog.service")
    assert cp.get("Service", "Type", fallback="") == "oneshot"
    assert cp.get("Service", "User", fallback="") == "root"
    assert cp.get("Service", "ExecStart", fallback="") == "/opt/ai-orchestrator/installer/watchdog.sh"


def test_job_service_path_includes_provider_cli_prefix():
    text = _read(SYSTEMD / "orchestrator@.service")
    assert "Environment=PATH=/home/orchestrator/.npm-global/bin:/usr/local/bin:/usr/bin:/bin" in text
    assert "WorkingDirectory=/opt/ai-orchestrator" in text


@pytest.mark.parametrize(
    "script,markers",
    [
        ("install.sh", ["requirements-dev.txt", "chown -R orchestrator", "preflight.sh", "install-systemd.sh",
                        "diagnose.sh", "INSTALL OK", "/var/lib/ai-orchestrator", "install-stamp.json",
                        "installed_at", "installed_epoch", "kit_commit", "boot_id", "chmod 0644"]),
        ("acceptance.sh", ["core.acceptance", "profiles/active.json", ".venv/bin/python", "--phase", "--job-id",
                           "--out-dir", "set -euo pipefail"]),
        ("preflight.sh", ["installer/acceptance.sh", "core/acceptance.py", "tests/test_acceptance.py",
                          "docs/ACCEPTANCE.md", "docs/evidence/README.md", "installer/gemini-secret.sh",
                          "tests/test_gemini_secret.py"]),
        ("diagnose.sh", ["DIAGNOSE PASS", "orchestrator-watchdog.timer", "orchestrator-health.timer",
                         "orchestrator@.service", "orchestrator-health.service", "--version",
                         "INFO provider login is verified by the first real job, not here",
                         "gemini-secret.sh\" check", "docs/INSTALL.md section 4.1"]),
        ("gemini-secret.sh", ["read -rs", "[ -t 0 ]", "umask 077", "chmod 700", "chmod 600", "runuser",
                              "core.gemini_runner --smoke", "GEMINI SECRET PASS", "GEMINI SECRET FAIL"]),
        ("watchdog.sh", ["reset-failed", "systemctl restart", "collect_status.py", "logger -t orchestrator-watchdog"]),
        ("install-systemd.sh", ["orchestrator-watchdog.timer", "orchestrator-health.timer", "enable --now"]),
        ("install-providers.sh", ["/etc/profile.d/ai-orchestrator.sh", ".npm-global/bin", "FAIL"]),
        ("install-node.sh", ["deb.nodesource.com/setup_20.x", "MIN_MAJOR=18"]),
    ],
)
def test_script_markers(script, markers):
    text = _read(INSTALLER / script)
    for m in markers:
        assert m in text, f"installer/{script} must contain {m!r}"


def test_install_sh_gates_on_preflight_and_diagnose_in_order():
    text = _read(INSTALLER / "install.sh")
    order = [text.index("requirements-dev.txt"), text.index("chown -R orchestrator"),
             text.index("preflight.sh"), text.index("install-systemd.sh"), text.index("diagnose.sh"),
             text.index("INSTALL OK")]
    assert order == sorted(order), "install.sh must run deps -> chown -> preflight -> systemd -> diagnose -> INSTALL OK"


def test_install_sh_writes_stamp_after_diagnose_gate_before_install_ok():
    text = _read(INSTALLER / "install.sh")
    gate = text.index("INSTALL FAIL diagnose did not pass")
    stamp = text.index("install-stamp.json")
    assert gate < stamp < text.index("INSTALL OK"), "stamp must be written after the diagnose gate and before INSTALL OK"
    stamp_block = text[stamp:text.index("INSTALL OK")]
    for key in ("installed_at", "installed_epoch", "profile", "kit_commit", "boot_id"):
        assert f'"{key}"' in stamp_block, f"stamp must record {key}"
    assert "/proc/sys/kernel/random/boot_id" in stamp_block
    assert "rev-parse HEAD" in stamp_block and "unknown" in stamp_block


def test_acceptance_sh_is_a_thin_wrapper():
    path = INSTALLER / "acceptance.sh"
    assert path.is_file()
    assert os.access(path, os.X_OK), "installer/acceptance.sh must be executable"
    assert subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True).returncode == 0
    text = _read(path)
    assert text.startswith("#!/usr/bin/env bash")
    assert "-m core.acceptance" in text
    assert 'exit "$rc"' in text
    assert "sudo" not in text.replace("sudo bash /opt/ai-orchestrator/installer/acceptance.sh", ""), \
        "acceptance.sh must never escalate"


def test_acceptance_docs_mention_every_check_id():
    from core import acceptance

    doc = _read(ROOT / "docs" / "ACCEPTANCE.md")
    for check_id in acceptance.CHECK_IDS:
        assert f"`{check_id}`" in doc, f"docs/ACCEPTANCE.md must mention check id {check_id!r}"
    for m in ("START-HERE.ps1", "installer/start-here.sh", "sudo bash /opt/ai-orchestrator/installer/acceptance.sh <profile>",
              "sudo reboot", "--phase post-reboot", "core.submit_job", "--job-id <id>", "Second clean install",
              "## 3. Recording evidence", "docs/evidence/", "## 4. Limitations", "ACCEPTANCE PASS", "ACCEPTANCE FAIL",
              "install-stamp.json", "acceptance-<UTCstamp>.json", "acceptance-<UTCstamp>.md"):
        assert m in doc, f"docs/ACCEPTANCE.md must contain {m!r}"
    assert (ROOT / "docs" / "evidence" / "README.md").is_file()
    assert "docs/ACCEPTANCE.md" in _read(ROOT / "README.md")
    install_doc = _read(ROOT / "docs" / "INSTALL.md")
    assert "ACCEPTANCE.md" in install_doc and "install-stamp.json" in install_doc
    assert "install-stamp.json" in _read(ROOT / "README.md")


def test_watchdog_never_deletes():
    text = _read(INSTALLER / "watchdog.sh")
    assert not re.search(r"\b(rm|rmdir|unlink|shred)\b", text), "watchdog.sh must not delete anything"


def test_install_docs_exist_and_are_linked():
    doc = ROOT / "docs" / "INSTALL.md"
    assert doc.is_file()
    text = _read(doc)
    for m in ("installer/install.sh", "INSTALL OK", "DIAGNOSE PASS", "sudo reboot", "diagnose.sh", "codex login"):
        assert m in text, f"docs/INSTALL.md must mention {m!r}"
    assert "docs/INSTALL.md" in _read(ROOT / "README.md")


def test_preflight_passes_from_repo_root():
    # Preflight itself runs pytest when available; skip that inner run to avoid recursion.
    env = dict(os.environ, PREFLIGHT_SKIP_PYTEST="1")
    proc = subprocess.run(
        ["bash", "installer/preflight.sh"], cwd=str(ROOT), capture_output=True, text=True, env=env, timeout=300,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "PREFLIGHT PASS" in proc.stdout
    assert "FAIL" not in proc.stdout.replace("PREFLIGHT PASS", "")


def test_diagnose_treats_absent_gemini_secret_as_info_not_fail():
    """Before the manual key placement the base install must still pass diagnose."""
    text = _read(INSTALLER / "diagnose.sh")
    start = text.index("# --- Gemini API key")
    block = text[start:text.index("# --- systemd units")]
    assert 'if [ "$uses_gemini" = "1" ]' in block, "only profiles with extras.gemini are looked at"
    assert "extras" in block and '.get("gemini")' in block
    split = block.index("  else\n    info \"gemini secret: not configured yet")
    present, absent = block[:split], block[split:]
    assert "bad " not in absent, "an absent secret must never be a FAIL"
    assert 'if [ -f "$GEMINI_SECRETS" ]' in present and "bad \"gemini secret:" in present, "a present but insecure file is a FAIL"
    assert "check >/dev/null 2>&1" in present, "the value can never reach the diagnose output"
    assert "GEMINI_API_KEY" not in block.replace("GEMINI_API_KEY set (value not shown)", "").replace(
        "has no GEMINI_API_KEY", ""), "diagnose never reads or echoes the key itself"
    assert "Makes no network calls" in text


def test_codex_sandbox_dependency_is_installed_and_diagnosed():
    install = _read(INSTALLER / "install.sh")
    diagnose = _read(INSTALLER / "diagnose.sh")
    docs = _read(ROOT / "docs" / "INSTALL.md")
    assert "bubblewrap" in install
    assert "bwrap" in diagnose
    assert "bubblewrap" in docs
