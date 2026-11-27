#!/usr/bin/env bash
# Bare-Ubuntu installer for the AI Orchestrator Starter Kit.
# Usage: sudo bash installer/install.sh <claude|openai|hybrid>
# Idempotent: re-running on an already-installed host must succeed.
# Fail-closed: preflight, then install-systemd, then diagnose gate the result.
set -euo pipefail
PROFILE="${1:-claude}"
case "$PROFILE" in claude|openai|hybrid) ;; *) echo "Unknown profile: $PROFILE (expected claude|openai|hybrid)" >&2; exit 2;; esac
if [ "$(id -u)" -ne 0 ]; then echo "Run as root" >&2; exit 2; fi

TARGET=/opt/ai-orchestrator
TARGET_USER=orchestrator
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

step(){ printf '\n==> %s\n' "$*"; }

step "System packages"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y git curl jq acl python3 python3-venv python3-pip ca-certificates rsync bubblewrap

step "Node.js (>= 18)"
"$SCRIPT_DIR/install-node.sh"

step "Service user"
"$SCRIPT_DIR/create-user.sh" "$TARGET_USER"

step "Copy kit to $TARGET"
install -d -m 0755 "$TARGET"
if [ "$SRC_DIR" != "$TARGET" ]; then
  # Refresh the kit in place on re-runs. Excluded paths are host state and are
  # neither copied from the source checkout nor deleted from the target.
  rsync -a --delete \
    --exclude '.git' --exclude '.venv' --exclude '__pycache__' --exclude '.pytest_cache' \
    --exclude 'profiles/active.json' --exclude 'monitor/status.json' --exclude 'jobs/' --exclude '/evidence/' \
    "$SRC_DIR/" "$TARGET/"
fi
cp "$TARGET/profiles/$PROFILE.json" "$TARGET/profiles/active.json"
chmod +x "$TARGET"/installer/*.sh "$TARGET"/monitor/*.py

step "Python venv + dev requirements"
if [ ! -x "$TARGET/.venv/bin/python" ]; then
  python3 -m venv "$TARGET/.venv"
fi
"$TARGET/.venv/bin/pip" install --upgrade pip
# Runtime is stdlib-only (requirements.txt is informational); dev deps give preflight pytest.
"$TARGET/.venv/bin/pip" install -r "$TARGET/requirements-dev.txt"

step "Ownership"
chown -R orchestrator:orchestrator "$TARGET"

step "Provider CLIs for profile=$PROFILE"
"$TARGET/installer/install-providers.sh" "$PROFILE"

step "Preflight (as $TARGET_USER, venv python)"
if ! runuser -u "$TARGET_USER" -- env HOME="/home/$TARGET_USER" \
    PATH="$TARGET/.venv/bin:/home/$TARGET_USER/.npm-global/bin:/usr/local/bin:/usr/bin:/bin" \
    bash -c "cd '$TARGET' && bash installer/preflight.sh"; then
  echo "INSTALL FAIL preflight did not pass" >&2
  exit 1
fi

step "systemd units"
"$TARGET/installer/install-systemd.sh"

step "Diagnose (fail-closed gate)"
if ! "$TARGET/installer/diagnose.sh" "$PROFILE"; then
  echo "INSTALL FAIL diagnose did not pass" >&2
  exit 1
fi

step "Install stamp"
# Read by installer/acceptance.sh (core/acceptance.py): proves when/what was
# installed and, via boot_id, whether the host rebooted since. Overwritten on
# every successful re-install.
STAMP_DIR=/var/lib/ai-orchestrator
STAMP="$STAMP_DIR/install-stamp.json"
install -d -m 0755 -o root -g root "$STAMP_DIR"
KIT_COMMIT="$(git -C "$SRC_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
BOOT_ID="$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || echo unknown)"
python3 - "$STAMP" "$PROFILE" "$KIT_COMMIT" "$BOOT_ID" <<'PY'
import json, sys, time
from datetime import datetime, timezone
path, profile, commit, boot_id = sys.argv[1:5]
now = time.time()
stamp = {
    "installed_at": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    "installed_epoch": now,
    "profile": profile,
    "kit_commit": commit,
    "boot_id": boot_id,
}
with open(path, "w", encoding="utf-8") as fh:
    json.dump(stamp, fh, indent=2, sort_keys=True)
    fh.write("\n")
PY
chmod 0644 "$STAMP"
echo "OK   install stamp written: $STAMP"

echo "INSTALL OK profile=$PROFILE"
