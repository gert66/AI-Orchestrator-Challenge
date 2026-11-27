"""Fail-closed, on-host acceptance runner for the starter kit (stdlib only).

Runs one named check per definition-of-done item (see docs/ACCEPTANCE.md) and
records machine-readable evidence:

  <out-dir>/acceptance-<UTCstamp>.json   schema_version 1, one entry per check
  <out-dir>/acceptance-<UTCstamp>.md     the same as a human-readable table

Exit code 0 and exactly one ``ACCEPTANCE PASS ...`` line on stdout only when
no check is FAIL; otherwise exit 1 with one ``ACCEPTANCE FAIL ...`` line on
stderr. SKIP and MANUAL results are listed but never fail the run.

Usage (normally via installer/acceptance.sh):

  python3 -m core.acceptance <claude|openai|hybrid> [--phase post-install|post-reboot]
                             [--job-id <id>] [--out-dir <dir>]

Importing this module has no side effects; every check is a small function
that takes a ``Context`` so the offline test suite can monkeypatch the paths
and the ``run_command`` subprocess wrapper (no root, systemctl or network).
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from core import gemini_runner, machine, provider_registry

__all__ = [
    "SCHEMA_VERSION", "PHASES", "PROFILES", "CHECK_IDS", "Context",
    "run_checks", "write_artifacts", "main",
]

SCHEMA_VERSION = 1
PHASES = ("post-install", "post-reboot")
PROFILES = ("claude", "openai", "hybrid")
STATUSES = ("PASS", "FAIL", "SKIP", "MANUAL")

KIT_ROOT = Path(__file__).resolve().parents[1]
INSTALL_ROOT = Path("/opt/ai-orchestrator")
STAMP_FILE = Path("/var/lib/ai-orchestrator/install-stamp.json")
BOOT_ID_FILE = Path("/proc/sys/kernel/random/boot_id")
UPTIME_FILE = Path("/proc/uptime")
DEFAULT_OUT_DIR = INSTALL_ROOT / "evidence" / "acceptance"
STAMP_KEYS = ("installed_at", "installed_epoch", "profile", "kit_commit", "boot_id")

# The CLI each provider route needs on the host. "auto" is the budget-aware
# Claude/Gemini Worker route and therefore needs the Claude CLI.
PROVIDER_CLI = {"anthropic": "claude", "openai": "codex", "auto": "claude", "google": "gemini"}
CLI_BIN_DIR = Path("/home/orchestrator/.npm-global/bin")
# Gemini is reached over HTTPS with GEMINI_API_KEY (core.gemini_runner), placed
# on the host by installer/gemini-secret.sh. Only presence, owner and modes are
# checked; the value is never recorded.
SECRETS_FILE = Path("/home/orchestrator/.config/ai-orchestrator/secrets.env")
SECRETS_OWNER = "orchestrator"
SECRETS_DIR_MODES = frozenset({0o700, 0o500})
SECRETS_FILE_MODES = frozenset({0o600, 0o400})
GEMINI_SETUP_HINT = "run: sudo bash /opt/ai-orchestrator/installer/gemini-secret.sh set (docs/INSTALL.md section 4.1)"

TIMERS = ("orchestrator-health.timer", "orchestrator-watchdog.timer")
HEALTH_SERVICE = "orchestrator-health.service"
MONITOR_MAX_AGE_SECONDS = 600
OUTPUT_TAIL_LINES = 40
COMMAND_TIMEOUT_SECONDS = 1800


def utc_now():
    return datetime.now(timezone.utc)


def run_command(argv, *, cwd=None, env=None, timeout=COMMAND_TIMEOUT_SECONDS):
    """Run argv without a shell and return {"argv", "exit_code", "stdout", "stderr", "error"}.

    Never raises: a missing binary or a timeout is reported in "error" with a
    negative exit code so the calling check records a FAIL instead of crashing.
    """
    record = {"argv": [str(a) for a in argv], "exit_code": None, "stdout": "", "stderr": "", "error": None}
    try:
        proc = subprocess.run(
            record["argv"], cwd=str(cwd) if cwd else None, env=env, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
        record.update(exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)
    except FileNotFoundError as exc:
        record.update(exit_code=-1, error=f"not found: {exc.filename or record['argv'][0]}")
    except subprocess.TimeoutExpired:
        record.update(exit_code=-2, error=f"timed out after {timeout}s")
    except OSError as exc:
        record.update(exit_code=-3, error=str(exc))
    return record


def tail(text, limit=OUTPUT_TAIL_LINES):
    lines = (text or "").splitlines()
    return lines[-limit:]


def _read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def read_boot_id():
    return _read_text(BOOT_ID_FILE) or "unknown"


def read_uptime_seconds():
    text = _read_text(UPTIME_FILE)
    try:
        return float(text.split()[0]) if text else None
    except (ValueError, IndexError):
        return None


def load_stamp(path):
    """Parse the install stamp. Returns (stamp_dict, error_or_None); never raises."""
    path = Path(path)
    if not path.is_file():
        return None, f"install stamp missing: {path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"install stamp {path} is not valid JSON: {exc}"
    if not isinstance(data, dict):
        return None, f"install stamp {path} is not a JSON object"
    missing = [k for k in STAMP_KEYS if k not in data]
    if missing:
        return data, f"install stamp {path} lacks keys: {', '.join(missing)}"
    return data, None


def kit_commit(kit_root):
    """HEAD of the source checkout when git metadata is present, else 'unknown'."""
    if not (Path(kit_root) / ".git").exists():
        return "unknown"
    rec = run_command(["git", "rev-parse", "HEAD"], cwd=kit_root, timeout=30)
    return rec["stdout"].strip() if rec["exit_code"] == 0 and rec["stdout"].strip() else "unknown"


class Context:
    """Everything the checks read; tests build one against tmp_path."""

    def __init__(self, profile, *, phase=None, job_id=None, kit_root=None, stamp_file=None,
                 boot_id=None, out_dir=None):
        if profile not in PROFILES:
            raise ValueError(f"profile must be one of {PROFILES}, got {profile!r}")
        if phase is not None and phase not in PHASES:
            raise ValueError(f"phase must be one of {PHASES}, got {phase!r}")
        self.profile = profile
        self.forced_phase = phase
        self.phase = phase or "post-install"
        self.job_id = job_id
        self.kit_root = Path(kit_root) if kit_root else KIT_ROOT
        self.stamp_file = Path(stamp_file) if stamp_file else STAMP_FILE
        self.boot_id = boot_id if boot_id is not None else read_boot_id()
        self.out_dir = Path(out_dir) if out_dir else DEFAULT_OUT_DIR
        self.venv_python = self.kit_root / ".venv" / "bin" / "python"
        self.profiles_dir = self.kit_root / "profiles"
        self.status_file = self.kit_root / "monitor" / "status.json"
        self.jobs_dir = machine.ORCH_ROOT / "jobs"
        self.policy_file = self.kit_root / "config" / "repo_policy.json"
        self.cli_bin_dir = CLI_BIN_DIR
        self.secrets_file = SECRETS_FILE
        self.secrets_owner = SECRETS_OWNER
        self.is_root = hasattr(os, "geteuid") and os.geteuid() == 0
        self.stamp = None
        self.stamp_error = None

    def env_with_venv(self):
        env = dict(os.environ)
        if self.venv_python.is_file():
            env["PATH"] = f"{self.venv_python.parent}{os.pathsep}{env.get('PATH', '')}"
        return env


# --- checks ------------------------------------------------------------------
# Each returns {"status", "detail", "evidence"}; run_checks adds id/title.

def check_install_stamp(ctx):
    stamp, error = load_stamp(ctx.stamp_file)
    ctx.stamp, ctx.stamp_error = stamp, error
    evidence = {"path": str(ctx.stamp_file)}
    if stamp:
        evidence.update({k: stamp.get(k) for k in STAMP_KEYS})
    if error:
        return {"status": "FAIL", "detail": error, "evidence": evidence}
    detail = f"stamp written {stamp['installed_at']} for profile={stamp['profile']} kit_commit={stamp['kit_commit']}"
    if stamp["profile"] != ctx.profile:
        detail += f" (note: acceptance requested profile={ctx.profile})"
    return {"status": "PASS", "detail": detail, "evidence": evidence}


def detect_reboot(stamp_boot_id, current_boot_id, forced_phase=None):
    """Return (status, detail, phase) from the stamp's boot id and the running one."""
    same = stamp_boot_id == current_boot_id
    if forced_phase == "post-reboot":
        if same:
            return "FAIL", "phase forced to post-reboot but boot_id is unchanged since install", "post-reboot"
        return "PASS", "boot_id changed since install", "post-reboot"
    if same:
        return "SKIP", "no reboot since install", "post-install"
    return "PASS", "boot_id changed since install", "post-reboot"


def check_reboot_survived(ctx):
    evidence = {"current_boot_id": ctx.boot_id, "stamp_boot_id": None, "forced_phase": ctx.forced_phase}
    if not ctx.stamp or "boot_id" not in ctx.stamp:
        ctx.phase = ctx.forced_phase or "post-install"
        return {"status": "FAIL", "detail": f"cannot determine reboot: {ctx.stamp_error or 'stamp has no boot_id'}",
                "evidence": evidence}
    evidence["stamp_boot_id"] = ctx.stamp["boot_id"]
    status, detail, phase = detect_reboot(ctx.stamp["boot_id"], ctx.boot_id, ctx.forced_phase)
    ctx.phase = phase
    evidence["phase"] = phase
    return {"status": status, "detail": detail, "evidence": evidence}


def _script_result(rec, name):
    evidence = {"argv": rec["argv"], "exit_code": rec["exit_code"], "error": rec["error"],
                "output_tail": tail(rec["stdout"] + ("\n" + rec["stderr"] if rec["stderr"] else ""))}
    if rec["exit_code"] == 0:
        return {"status": "PASS", "detail": f"{name} exited 0", "evidence": evidence}
    detail = f"{name} exited {rec['exit_code']}" + (f": {rec['error']}" if rec["error"] else "")
    return {"status": "FAIL", "detail": detail, "evidence": evidence}


def check_diagnose(ctx):
    script = ctx.kit_root / "installer" / "diagnose.sh"
    rec = run_command(["bash", str(script), ctx.profile], cwd=ctx.kit_root)
    result = _script_result(rec, "diagnose.sh")
    if result["status"] == "FAIL" and not ctx.is_root:
        result["detail"] += " (not running as root; diagnose needs root for a complete result - re-run acceptance.sh with sudo; sudo is never attempted here)"
    return result


def check_systemd_health(ctx):
    if shutil.which("systemctl") is None:
        return {"status": "FAIL", "detail": "systemctl not found", "evidence": {"units": {}}}
    units = {}
    for unit in TIMERS:
        units[unit] = {
            "is_enabled": run_command(["systemctl", "is-enabled", unit], timeout=60)["stdout"].strip(),
            "is_active": run_command(["systemctl", "is-active", unit], timeout=60)["stdout"].strip(),
        }
    units[HEALTH_SERVICE] = {
        "is_enabled": run_command(["systemctl", "is-enabled", HEALTH_SERVICE], timeout=60)["stdout"].strip(),
    }
    bad = [u for u in TIMERS if units[u]["is_enabled"] != "enabled" or units[u]["is_active"] != "active"]
    if bad:
        return {"status": "FAIL", "detail": "timer(s) not enabled+active: " + ", ".join(
            f"{u} enabled={units[u]['is_enabled']!r} active={units[u]['is_active']!r}" for u in bad),
            "evidence": {"units": units}}
    return {"status": "PASS", "detail": "both timers enabled and active", "evidence": {"units": units}}


def check_monitor_fresh(ctx):
    evidence = {"path": str(ctx.status_file), "age_seconds": None, "max_age_seconds": MONITOR_MAX_AGE_SECONDS}
    if not ctx.status_file.is_file():
        return {"status": "FAIL", "detail": f"{ctx.status_file} missing", "evidence": evidence}
    try:
        data = json.loads(ctx.status_file.read_text(encoding="utf-8"))
        age = time.time() - float(data["timestamp"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"status": "FAIL", "detail": f"{ctx.status_file} is not valid JSON with a numeric timestamp: {exc}",
                "evidence": evidence}
    evidence["age_seconds"] = round(age, 1)
    evidence["profile"] = data.get("profile")
    if -60 <= age < MONITOR_MAX_AGE_SECONDS:
        return {"status": "PASS", "detail": f"status.json is {age:.0f}s old", "evidence": evidence}
    return {"status": "FAIL", "detail": f"status.json is stale ({age:.0f}s old, limit {MONITOR_MAX_AGE_SECONDS}s)",
            "evidence": evidence}


def check_preflight(ctx):
    script = ctx.kit_root / "installer" / "preflight.sh"
    rec = run_command(["bash", str(script)], cwd=ctx.kit_root, env=ctx.env_with_venv())
    result = _script_result(rec, "preflight.sh")
    result["evidence"]["venv_on_path"] = ctx.venv_python.is_file()
    return result


def check_pytest_suite(ctx):
    if not ctx.venv_python.is_file():
        return {"status": "SKIP", "detail": f"venv python missing at {ctx.venv_python}; pytest not run",
                "evidence": {"venv_python": str(ctx.venv_python)}}
    rec = run_command([str(ctx.venv_python), "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"], cwd=ctx.kit_root)
    result = _script_result(rec, "pytest")
    lines = [l for l in rec["stdout"].splitlines() if l.strip()]
    result["evidence"]["summary"] = lines[-1] if lines else ""
    if result["status"] == "PASS":
        result["detail"] = result["evidence"]["summary"] or result["detail"]
    return result


def check_profile_routing(ctx):
    matrix, problems = {}, []
    for name in PROFILES:
        path = ctx.profiles_dir / f"{name}.json"
        try:
            profile = provider_registry.load_profile(path)
            routes = {role: provider_registry.provider_for_role(profile, role) for role in provider_registry.ROLES}
        except (provider_registry.ProfileError, ValueError) as exc:
            matrix[name] = {"error": str(exc)}
            problems.append(f"{name}: {exc}")
            continue
        matrix[name] = routes
        for role, provider in routes.items():
            if provider not in provider_registry.PROVIDERS and provider != "auto":
                problems.append(f"{name}: {role} -> {provider!r} is not a provider")
    openai_routes = matrix.get("openai", {})
    anthropic_in_openai = [r for r, p in openai_routes.items() if p == "anthropic"]
    if anthropic_in_openai:
        problems.append(f"openai profile routes {anthropic_in_openai} to anthropic (hidden Claude dependency)")
    evidence = {"matrix": matrix, "openai_routes_to_anthropic": anthropic_in_openai}
    if problems:
        return {"status": "FAIL", "detail": "; ".join(problems), "evidence": evidence}
    return {"status": "PASS", "detail": "brain/worker/reviewer resolve for claude, openai and hybrid; openai never routes to anthropic",
            "evidence": evidence}


def required_clis(ctx):
    """CLI names the active profile needs, resolved through the provider registry."""
    profile = provider_registry.load_profile(ctx.profiles_dir / f"{ctx.profile}.json")
    clis = []
    for role in provider_registry.ROLES:
        cli = PROVIDER_CLI.get(provider_registry.provider_for_role(profile, role))
        if cli and cli not in clis:
            clis.append(cli)
    return clis


def check_provider_cli_login(ctx):
    try:
        clis = required_clis(ctx)
    except (provider_registry.ProfileError, ValueError, OSError) as exc:
        return {"status": "FAIL", "detail": f"cannot resolve provider CLIs: {exc}", "evidence": {}}
    evidence, missing = {}, []
    for cli in clis:
        binary = ctx.cli_bin_dir / cli
        path = str(binary) if binary.is_file() else shutil.which(cli)
        if not path:
            evidence[cli] = {"path": None, "version_exit_code": None}
            missing.append(cli)
            continue
        rec = run_command([path, "--version"], timeout=120)
        evidence[cli] = {"path": path, "version_exit_code": rec["exit_code"], "version": rec["stdout"].strip()[:200]}
    if missing:
        return {"status": "FAIL", "detail": f"provider CLI missing: {', '.join(missing)}", "evidence": evidence}
    return {"status": "MANUAL", "detail": "interactive login required; see docs/INSTALL.md", "evidence": evidence}


def profile_uses_gemini(profile_path):
    """True when the profile's extras.gemini flag is set (the hybrid profile)."""
    try:
        data = json.loads(Path(profile_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    extras = data.get("extras") if isinstance(data, dict) else None
    return bool(isinstance(extras, dict) and extras.get("gemini"))


def _owner_name(path):
    try:
        return Path(path).owner()
    except (KeyError, OSError, NotImplementedError):
        return None


def inspect_secrets_file(path, owner):
    """Owner/mode/presence facts about the secrets file; the key's value is never returned.

    Returns (problems, evidence). ``evidence['key_present']`` says whether
    core.gemini_runner.load_secrets() would find a non-empty GEMINI_API_KEY.
    """
    path = Path(path)
    directory = path.parent
    evidence = {"path": str(path), "expected_owner": owner, "exists": path.is_file(), "dir_owner": None,
                "dir_mode": None, "file_owner": None, "file_mode": None, "key_present": None}
    problems = []
    if not directory.is_dir():
        problems.append(f"{directory} missing")
        return problems, evidence
    dir_mode = directory.stat().st_mode & 0o777
    evidence["dir_owner"], evidence["dir_mode"] = _owner_name(directory), f"{dir_mode:o}"
    if evidence["dir_owner"] != owner:
        problems.append(f"{directory} owned by {evidence['dir_owner']!r}, expected {owner}")
    if dir_mode not in SECRETS_DIR_MODES:
        problems.append(f"{directory} mode {dir_mode:o}, expected 700")
    if not path.is_file():
        problems.append(f"{path} missing")
        return problems, evidence
    file_mode = path.stat().st_mode & 0o777
    evidence["file_owner"], evidence["file_mode"] = _owner_name(path), f"{file_mode:o}"
    if evidence["file_owner"] != owner:
        problems.append(f"{path} owned by {evidence['file_owner']!r}, expected {owner}")
    if file_mode not in SECRETS_FILE_MODES:
        problems.append(f"{path} mode {file_mode:o}, expected 600")
    try:
        # The parser the runner uses; the value stays in this local scope only.
        present = bool((gemini_runner.load_secrets(path, environ={}).get("GEMINI_API_KEY") or "").strip())
    except OSError as exc:
        problems.append(f"cannot read {path}: {exc}")
        return problems, evidence
    evidence["key_present"] = present
    if not present:
        problems.append(f"GEMINI_API_KEY line missing or empty in {path}")
    return problems, evidence


def check_gemini_secret(ctx):
    profile_path = ctx.profiles_dir / f"{ctx.profile}.json"
    if not profile_uses_gemini(profile_path):
        return {"status": "SKIP", "detail": f"profile {ctx.profile} does not route to Gemini (extras.gemini is not set)",
                "evidence": {"profile": str(profile_path), "uses_gemini": False}}
    try:
        problems, evidence = inspect_secrets_file(ctx.secrets_file, ctx.secrets_owner)
    except OSError as exc:  # e.g. permission denied on the orchestrator home when not root
        detail = f"cannot inspect {ctx.secrets_file}: {exc}"
        if not ctx.is_root:
            detail += " (not running as root; re-run acceptance.sh with sudo)"
        return {"status": "FAIL", "detail": detail, "evidence": {"path": str(ctx.secrets_file), "uses_gemini": True}}
    evidence["uses_gemini"] = True
    if not evidence["exists"]:
        # Absence is the documented manual step, never a failure: the Worker
        # 'auto' route falls back to Claude Sonnet until the key is placed.
        return {"status": "MANUAL", "detail": f"GEMINI_API_KEY not placed yet ({ctx.secrets_file} missing); Worker 'auto' "
                                              f"falls back to Claude Sonnet until it is - {GEMINI_SETUP_HINT}",
                "evidence": evidence}
    if problems:
        return {"status": "FAIL", "detail": "; ".join(problems) + " (fix with installer/gemini-secret.sh set, verify with check)",
                "evidence": evidence}
    return {"status": "PASS", "detail": f"{ctx.secrets_file} present for {ctx.secrets_owner} (dir 700, file 600) "
                                        f"with GEMINI_API_KEY set; value not recorded", "evidence": evidence}


def summarize_history(history):
    keys = ("step_id", "outcome", "verdict", "repairs", "reviews", "commit", "pushed")
    return [{k: entry.get(k) for k in keys} for entry in history if isinstance(entry, dict)]


def check_real_job_flow(ctx):
    if not ctx.job_id:
        return {"status": "SKIP", "detail": "no --job-id given; real job flow not evaluated", "evidence": {}}
    job_dir = ctx.jobs_dir / ctx.job_id
    evidence = {"job_dir": str(job_dir), "phase": None, "history": []}
    try:
        state = json.loads((job_dir / machine.STATE_FILE).read_text(encoding="utf-8"))
        job = json.loads((job_dir / machine.JOB_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"status": "FAIL", "detail": f"cannot read job {ctx.job_id}: {exc}", "evidence": evidence}
    history = (state.get("runtime") or {}).get("history") or []
    evidence["phase"] = state.get("phase")
    evidence["history"] = summarize_history(history)
    problems = []
    if state.get("phase") != "DONE":
        problems.append(f"phase is {state.get('phase')!r}, expected DONE")
    committed = [e for e in history if isinstance(e, dict) and e.get("outcome") == "committed"]
    if not committed:
        problems.append("no history entry with outcome 'committed'")
    push_required = False
    try:
        from core.repo_policy import load_repo_policy
        push_required = bool(load_repo_policy(job.get("repo"), ctx.policy_file).get("push_job_branch"))
    except Exception as exc:  # policy missing/invalid: recorded, push not required
        evidence["policy_error"] = str(exc)
    evidence["push_required"] = push_required
    if push_required and (not history or history[-1].get("pushed") is not True):
        problems.append("repo policy has push_job_branch=true but the last batch was not pushed")
    if problems:
        return {"status": "FAIL", "detail": "; ".join(problems), "evidence": evidence}
    return {"status": "PASS", "detail": f"job {ctx.job_id} is DONE with {len(committed)} committed batch(es)"
            + ("; last batch pushed" if push_required else ""), "evidence": evidence}


CHECKS = (
    ("install_stamp", "Install stamp written by install.sh", check_install_stamp),
    ("reboot_survived", "Host rebooted since install (boot_id changed)", check_reboot_survived),
    ("diagnose", "installer/diagnose.sh passes", check_diagnose),
    ("systemd_health", "Health and watchdog timers enabled and active", check_systemd_health),
    ("monitor_fresh", "monitor/status.json is fresh", check_monitor_fresh),
    ("preflight", "installer/preflight.sh passes (fail-closed gate)", check_preflight),
    ("pytest_suite", "Offline pytest suite passes in the venv", check_pytest_suite),
    ("profile_routing", "Claude/OpenAI/Hybrid profiles are role-routable", check_profile_routing),
    ("provider_cli_login", "Provider CLI present (login is the manual step)", check_provider_cli_login),
    ("gemini_secret", "GEMINI_API_KEY placed on the host for the Gemini Worker route (hybrid)", check_gemini_secret),
    ("real_job_flow", "Real Brain->Worker->Evidence->Reviewer->Commit/Push job", check_real_job_flow),
)
CHECK_IDS = tuple(c[0] for c in CHECKS)


def _run_check(check_id, title, fn, ctx):
    try:
        out = fn(ctx)
        status = out.get("status")
    except Exception as exc:  # a crashing check is a FAIL, never a crashed run
        out = {"status": "FAIL", "detail": f"check crashed: {type(exc).__name__}: {exc}", "evidence": {}}
        status = "FAIL"
    if status not in STATUSES:
        out = {"status": "FAIL", "detail": f"check returned invalid status {status!r}", "evidence": out.get("evidence", {})}
    return {"id": check_id, "title": title, "status": out["status"], "detail": out.get("detail", ""),
            "evidence": out.get("evidence") or {}}


def run_checks(ctx, checks=None):
    checks = CHECKS if checks is None else checks
    started = utc_now()
    results = [_run_check(cid, title, fn, ctx) for cid, title, fn in checks]
    finished = utc_now()
    return {
        "schema_version": SCHEMA_VERSION,
        "started_at": started.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "finished_at": finished.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "host": {
            "hostname": socket.gethostname(),
            "kernel": platform.release(),
            "boot_id": ctx.boot_id,
            "uptime_seconds": read_uptime_seconds(),
        },
        "profile": ctx.profile,
        "kit_commit": (ctx.stamp or {}).get("kit_commit") or kit_commit(ctx.kit_root),
        "phase": ctx.phase,
        "job_id": ctx.job_id,
        "checks": results,
        "result": "FAIL" if any(r["status"] == "FAIL" for r in results) else "PASS",
    }


def render_markdown(report):
    counts = {s: sum(1 for c in report["checks"] if c["status"] == s) for s in STATUSES}
    lines = [
        f"# Acceptance {report['result']} - phase={report['phase']} profile={report['profile']}",
        "",
        f"- started: {report['started_at']}  finished: {report['finished_at']}",
        f"- host: {report['host']['hostname']} kernel {report['host']['kernel']} boot_id {report['host']['boot_id']}"
        f" uptime {report['host']['uptime_seconds']}s",
        f"- kit_commit: {report['kit_commit']}  job_id: {report.get('job_id') or '-'}",
        "- " + "  ".join(f"{s}: {n}" for s, n in counts.items()),
        "",
        "| check | status | detail |",
        "|---|---|---|",
    ]
    for c in report["checks"]:
        detail = " ".join(str(c["detail"]).split()).replace("|", "\\|")
        lines.append(f"| `{c['id']}` | {c['status']} | {detail} |")
    lines.append("")
    return "\n".join(lines)


def write_artifacts(report, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    json_path = out_dir / f"acceptance-{stamp}.json"
    n = 1
    while json_path.exists():  # two runs within one second must not overwrite each other
        json_path = out_dir / f"acceptance-{stamp}-{n}.json"
        n += 1
    md_path = json_path.with_suffix(".md")
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


def build_parser():
    p = argparse.ArgumentParser(prog="python3 -m core.acceptance", description=__doc__.split("\n\n")[0])
    p.add_argument("profile", choices=PROFILES, help="profile under test (installer/acceptance.sh reads profiles/active.json)")
    p.add_argument("--phase", choices=PHASES, default=None,
                   help="force the phase; post-reboot with an unchanged boot_id is a FAIL (default: detected from the install stamp)")
    p.add_argument("--job-id", default=None, help="job whose state.json proves the real Brain->Worker->Reviewer->Commit/Push flow")
    p.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help=f"where the evidence artifacts go (default: {DEFAULT_OUT_DIR})")
    p.add_argument("--kit-root", default=None, help=argparse.SUPPRESS)
    p.add_argument("--stamp-file", default=None, help=argparse.SUPPRESS)
    return p


def main(argv=None, stdout=None, stderr=None):
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    args = build_parser().parse_args(argv)
    ctx = Context(args.profile, phase=args.phase, job_id=args.job_id, kit_root=args.kit_root,
                  stamp_file=args.stamp_file, out_dir=args.out_dir)
    report = run_checks(ctx)
    try:
        json_path, _ = write_artifacts(report, ctx.out_dir)
    except OSError as exc:
        print(f"ACCEPTANCE FAIL cannot write evidence under {ctx.out_dir}: {exc}", file=stderr)
        return 1
    failed = [c["id"] for c in report["checks"] if c["status"] == "FAIL"]
    if failed:
        print(f"ACCEPTANCE FAIL {len(failed)} check(s) failed: {', '.join(failed)} evidence={json_path}", file=stderr)
        return 1
    print(f"ACCEPTANCE PASS phase={report['phase']} profile={report['profile']} evidence={json_path}", file=stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
