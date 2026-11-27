#!/usr/bin/env bash
# AI Orchestrator watchdog. Run by orchestrator-watchdog.timer as root.
# Recovers the health timer, refreshes a stale monitor snapshot and restarts
# failed orchestrator@ job instances. It only restarts/refreshes; it never
# deletes anything. Every action is logged with `logger -t orchestrator-watchdog`.
set -u
ROOT=/opt/ai-orchestrator
TARGET_USER=orchestrator
STATUS="$ROOT/monitor/status.json"
STALE_SECONDS=600

log(){ logger -t orchestrator-watchdog -- "$*" 2>/dev/null || true; echo "orchestrator-watchdog: $*"; }

# 1. Health timer must be active.
if ! systemctl is-active --quiet orchestrator-health.timer; then
  log "orchestrator-health.timer inactive; restarting"
  systemctl restart orchestrator-health.timer || log "restart of orchestrator-health.timer failed"
fi

# 2. monitor/status.json must exist and be younger than STALE_SECONDS.
refresh=0
if [ ! -f "$STATUS" ]; then
  log "status.json missing; collecting"
  refresh=1
else
  now="$(date +%s)"
  mtime="$(stat -c %Y "$STATUS" 2>/dev/null || echo 0)"
  if [ $((now - mtime)) -gt "$STALE_SECONDS" ]; then
    log "status.json older than ${STALE_SECONDS}s; collecting"
    refresh=1
  fi
fi
if [ "$refresh" -eq 1 ]; then
  runuser -u "$TARGET_USER" -- env HOME="/home/$TARGET_USER" \
    "$ROOT/.venv/bin/python" "$ROOT/monitor/collect_status.py" \
    || log "collect_status.py failed"
fi

# 3. Failed orchestrator@<job> instances: reset-failed then restart.
systemctl list-units --all --plain --no-legend 'orchestrator@*.service' 2>/dev/null \
  | awk '{print $1}' | while read -r unit; do
  [ -n "$unit" ] || continue
  if systemctl is-enabled --quiet "$unit" 2>/dev/null \
     && [ "$(systemctl is-failed "$unit" 2>/dev/null)" = "failed" ]; then
    log "$unit failed; reset-failed and restart"
    systemctl reset-failed "$unit" || log "reset-failed $unit failed"
    systemctl restart "$unit" || log "restart $unit failed"
  fi
done


# 4. Recover inactive jobs that are runnable from persisted state.
#    autonomous_sandbox soft gates are also auto-approved. Hard NEEDS_HUMAN
#    gates remain stopped for a person. This section never edits repository data.
JOBS_DIR="$ROOT/jobs"
if [ -d "$JOBS_DIR" ]; then
  for state_file in "$JOBS_DIR"/*/state.json; do
    [ -f "$state_file" ] || continue
    job_id="$(basename "$(dirname "$state_file")")"
    read -r phase mode gate wait_due enabled <<EOF
$(runuser -u "$TARGET_USER" -- env HOME="/home/$TARGET_USER" "$ROOT/.venv/bin/python" - "$state_file" <<'PY'
import json, sys
from datetime import datetime, timezone
d=json.load(open(sys.argv[1]))
rt=d.get("runtime") or {}
cfg=rt.get("config") or {}
human=rt.get("human") or {}
phase=d.get("phase") or ""
mode=cfg.get("execution_mode") or "standard"
gate=human.get("gate") or ""
due="yes"
if phase=="WAITING":
    raw=d.get("wait_until")
    if raw:
        try:
            t=datetime.fromisoformat(raw.replace("Z","+00:00"))
            due="yes" if datetime.now(timezone.utc) >= t else "no"
        except Exception:
            due="yes"
print(phase, mode, gate, due, "yes" if cfg.get("watchdog_enabled", False) else "no")
PY
)
EOF
    unit="orchestrator@${job_id}.service"
    [ "$enabled" = "yes" ] || continue

    if [ "$phase" = "NEEDS_HUMAN" ] && [ "$mode" = "autonomous_sandbox" ] && [ "$gate" = "soft" ]; then
      if ! systemctl is-active --quiet "$unit"; then
        log "$job_id has soft NEEDS_HUMAN in autonomous_sandbox; auto-approving and restarting"
        runuser -u "$TARGET_USER" -- env HOME="/home/$TARGET_USER" "$ROOT/.venv/bin/python" - "$job_id" <<'PY' \
          || { log "auto-approve failed for $job_id"; continue; }
import sys
from pathlib import Path
sys.path.insert(0, "/opt/ai-orchestrator")
from core.machine import Machine
job=Path("/opt/ai-orchestrator/jobs")/sys.argv[1]
Machine(job, runner=None).approve("watchdog auto-approved soft gate under autonomous_sandbox")
PY
        systemctl start "$unit" || log "restart $unit after soft-gate approval failed"
      fi
      continue
    fi

    case "$phase" in
      QUEUED|PLANNING|WORKING|EVIDENCE|REVIEWING|COMMITTING)
        if ! systemctl is-active --quiet "$unit"; then
          log "$job_id is runnable in phase $phase but service is inactive; starting"
          systemctl start "$unit" || log "start $unit failed"
        fi
        ;;
      WAITING)
        if [ "$wait_due" = "yes" ] && ! systemctl is-active --quiet "$unit"; then
          log "$job_id wait is due and service is inactive; starting"
          systemctl start "$unit" || log "start $unit failed"
        fi
        ;;
      *)
        ;;
    esac
  done
fi

exit 0
