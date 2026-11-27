"""Notification wrapper for an optional Slack shell hook.

The hook (bin/notify_slack.sh) is not part of the clean-room kit. When it is
absent, notifications are disabled: notify() logs one warning and returns
False instead of raising, so the supervisor keeps working without it.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

ORCH_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_NOTIFY_SCRIPT = ORCH_ROOT / "bin" / "notify_slack.sh"
NOTIFIABLE_PHASES = frozenset({"DONE", "ERROR", "NEEDS_HUMAN"})

log = logging.getLogger(__name__)


class NotifyError(RuntimeError):
    pass


def is_enabled(script=DEFAULT_NOTIFY_SCRIPT):
    """True when the optional notification hook is installed."""
    return Path(script).is_file()


def notify(status, job_id, detail="", script=DEFAULT_NOTIFY_SCRIPT, timeout=20):
    """Send one notification. Returns True if sent, False if skipped/disabled.

    Raises NotifyError only when the hook exists but fails to run.
    """
    if status not in NOTIFIABLE_PHASES:
        return False
    script = Path(script)
    if not script.is_file():
        log.warning("notifications disabled: hook not installed at %s", script)
        return False
    try:
        result = subprocess.run(
            [str(script), status, job_id, detail or ""],
            text=True, capture_output=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NotifyError(f"notification failed: {exc}") from exc
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "unknown error").strip()
        raise NotifyError(f"notification script exited {result.returncode}: {msg}")
    return True
