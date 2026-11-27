"""Read-only Hermes advisory bridge for orchestrator incidents.

The orchestrator stays authoritative. Hermes may inspect only the exported
job audit data and returns advice that is recorded but never executed.

Hermes is optional. The export hook (bin/export_for_hermes.sh) and the
advisory wrapper (/usr/local/sbin/hermes-orchestrator-advice) are not part of
the clean-room kit; when either is missing, request_advice() records a
disabled result and logs one warning instead of raising.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ORCH_ROOT = Path(__file__).resolve().parents[1]
EXPORT_SCRIPT = ORCH_ROOT / "bin" / "export_for_hermes.sh"
EXPORT_ROOT = Path("/srv/orchestrator-export")
REQUEST_DIR = EXPORT_ROOT / "_advisory_requests"
WRAPPER = Path("/usr/local/sbin/hermes-orchestrator-advice")
ADVICE_FILE = "hermes_advice.jsonl"
DEFAULT_TIMEOUT = 120

log = logging.getLogger(__name__)


def missing_dependencies(export_script=None, wrapper=None):
    """Return the list of optional Hermes components that are not installed."""
    export_script = EXPORT_SCRIPT if export_script is None else export_script
    wrapper = WRAPPER if wrapper is None else wrapper
    return [str(p) for p in (export_script, wrapper) if not Path(p).is_file()]


def is_available(export_script=None, wrapper=None):
    return not missing_dependencies(export_script, wrapper)


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_id(value):
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in str(value))[:120]


def _append_jsonl(path, record):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _prepare_export():
    subprocess.run(
        [str(EXPORT_SCRIPT)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=20,
    )


def _request_text(job_id, trigger, reason):
    return f"""You are an advisory layer only. Do not modify files, save memory, or create/update skills.
Analyze orchestrator job {job_id} using /srv/orchestrator-export and the preloaded skill. Reusable operator knowledge is available under /srv/orchestrator-export/_knowledge. Prefer current job and live system evidence over stale guidance or vault notes.
Trigger: {trigger}
Current reason: {reason}
Check whether this resembles a known loop/harness/retry pattern, but do not force that diagnosis.
If the skill does not apply, say so explicitly and diagnose from this job's own evidence.
Return at most 8 concise bullets covering: diagnosis, evidence, skill applicability, first next check, and confidence.
The orchestrator remains authoritative; do not instruct it to execute commands automatically.
"""


def request_advice(job_dir, trigger, reason, *, timeout=DEFAULT_TIMEOUT):
    """Run one read-only Hermes advisory query and record the result.

    Failures are returned as data. They must never alter orchestrator control flow.
    When the optional Hermes components are not installed the record carries
    ok=False and error="disabled: ...", and nothing is executed.
    """
    job_dir = Path(job_dir)
    try:
        job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
        job_id = str(job["job_id"])
    except (OSError, ValueError, KeyError, TypeError):
        job_id = job_dir.name
    record = {
        "ts": _now(),
        "job_id": job_id,
        "trigger": str(trigger),
        "reason": str(reason),
        "ok": False,
        "advice": None,
        "error": None,
    }
    missing = missing_dependencies()
    if missing:
        record["error"] = "disabled: Hermes advisory not installed (missing " + ", ".join(missing) + ")"
        log.warning("Hermes advisory disabled: missing %s", ", ".join(missing))
        return record
    try:
        _prepare_export()
        REQUEST_DIR.mkdir(parents=True, exist_ok=True)
        request_path = REQUEST_DIR / f"{_safe_id(job_id)}-{os.getpid()}.txt"
        request_path.write_text(_request_text(job_id, trigger, reason), encoding="utf-8")
        os.chmod(request_path, 0o640)
        subprocess.run(["setfacl", "-m", "u:hermes:r", str(request_path)], check=True, timeout=5)
        if not WRAPPER.is_file():
            raise RuntimeError(f"Hermes advisory wrapper not installed: {WRAPPER}")
        proc = subprocess.run(
            ["sudo", "-n", "-u", "hermes", str(WRAPPER), str(request_path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(EXPORT_ROOT),
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "Hermes advisory failed").strip()
            raise RuntimeError(detail[-2000:])
        advice = proc.stdout.strip()
        if not advice:
            raise RuntimeError("Hermes returned empty advisory output")
        record["ok"] = True
        record["advice"] = advice[-12000:]
    except Exception as exc:
        record["error"] = str(exc)[:2000]
    finally:
        try:
            if "request_path" in locals():
                request_path.unlink(missing_ok=True)
        except OSError:
            pass
        _append_jsonl(job_dir / ADVICE_FILE, record)
    return record


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog="python3 -m core.hermes_advisor")
    parser.add_argument("--job-dir", required=True)
    parser.add_argument("--trigger", default="manual")
    parser.add_argument("--reason", default="manual advisory check")
    args = parser.parse_args(argv)
    record = request_advice(args.job_dir, args.trigger, args.reason)
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0 if record.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
