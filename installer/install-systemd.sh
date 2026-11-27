#!/usr/bin/env bash
# Install and enable the AI Orchestrator systemd units. Idempotent.
set -euo pipefail
SRC=/opt/ai-orchestrator/systemd
DEST=/etc/systemd/system
UNITS="orchestrator@.service orchestrator-health.service orchestrator-health.timer orchestrator-watchdog.service orchestrator-watchdog.timer"

for u in $UNITS; do
  install -m 0644 "$SRC/$u" "$DEST/$u"
done
chmod +x /opt/ai-orchestrator/installer/watchdog.sh /opt/ai-orchestrator/monitor/collect_status.py
systemctl daemon-reload

# `enable --now` is a no-op when already enabled/active; restart picks up unit edits.
systemctl enable --now orchestrator-health.timer
systemctl enable --now orchestrator-watchdog.timer
systemctl restart orchestrator-health.timer
systemctl restart orchestrator-watchdog.timer

# Produce a fresh monitor/status.json immediately so diagnose can gate on it.
systemctl start orchestrator-health.service
echo "SYSTEMD OK"
