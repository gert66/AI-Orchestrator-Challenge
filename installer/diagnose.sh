#!/usr/bin/env bash
# Fail-closed post-install / post-reboot gate for the AI Orchestrator host.
# Usage: sudo /opt/ai-orchestrator/installer/diagnose.sh [claude|openai|hybrid]
# Exits 1 on any FAIL line and prints `DIAGNOSE PASS` only when every check passes.
# Makes no network calls and needs no credentials; provider login state is
# out of scope (it is verified by the first real job).
set -u
ROOT=/opt/ai-orchestrator
TARGET_USER=orchestrator
NPM_BIN="/home/$TARGET_USER/.npm-global/bin"
VENV_PY="$ROOT/.venv/bin/python"
STATUS="$ROOT/monitor/status.json"
MAX_STATUS_AGE=180
MIN_NODE_MAJOR=18
fail=0

ok(){ printf 'OK   %s\n' "$*"; }
bad(){ printf 'FAIL %s\n' "$*"; fail=1; }
info(){ printf 'INFO %s\n' "$*"; }

# Run a command as the orchestrator user when we are root, otherwise directly.
as_user(){
  if [ "$(id -u)" -eq 0 ]; then
    runuser -u "$TARGET_USER" -- env HOME="/home/$TARGET_USER" PATH="$NPM_BIN:$ROOT/.venv/bin:/usr/local/bin:/usr/bin:/bin" "$@"
  else
    env HOME="${HOME:-/home/$TARGET_USER}" PATH="$NPM_BIN:$ROOT/.venv/bin:/usr/local/bin:/usr/bin:/bin" "$@"
  fi
}

# --- profile under test -----------------------------------------------------
PROFILE="${1:-}"
if [ -z "$PROFILE" ]; then
  PROFILE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["profile"])' "$ROOT/profiles/active.json" 2>/dev/null || true)"
fi
case "$PROFILE" in
  claude|openai|hybrid) ok "profile under test: $PROFILE" ;;
  *) bad "profile '$PROFILE' is not one of claude|openai|hybrid (pass it as \$1 or fix $ROOT/profiles/active.json)" ;;
esac
case "$PROFILE" in
  claude) CLIS="claude" ;;
  openai) CLIS="codex" ;;
  hybrid) CLIS="claude codex" ;;
  *) CLIS="" ;;
esac

# --- base tools ---------------------------------------------------------------
for t in python3 git curl node npm bwrap; do
  if command -v "$t" >/dev/null 2>&1; then ok "$t: $(command -v "$t")"; else bad "$t not found"; fi
done
if command -v node >/dev/null 2>&1; then
  nv="$(node --version 2>/dev/null || true)"; nv="${nv#v}"; nmajor="${nv%%.*}"
  case "$nmajor" in ''|*[!0-9]*) nmajor=0 ;; esac
  if [ "$nmajor" -ge "$MIN_NODE_MAJOR" ]; then ok "node v$nv (>= $MIN_NODE_MAJOR)"; else bad "node v$nv is older than v$MIN_NODE_MAJOR"; fi
fi

# --- install tree and ownership ------------------------------------------------
if [ -d "$ROOT" ]; then
  owner="$(stat -c %U "$ROOT" 2>/dev/null || echo unknown)"
  if [ "$owner" = "$TARGET_USER" ]; then ok "$ROOT owned by $TARGET_USER"; else bad "$ROOT owned by '$owner', expected $TARGET_USER"; fi
else
  bad "$ROOT missing"
fi
if id "$TARGET_USER" >/dev/null 2>&1; then ok "user $TARGET_USER exists"; else bad "user $TARGET_USER missing"; fi

# --- venv and core imports -------------------------------------------------------
if [ -x "$VENV_PY" ]; then
  ok "venv python: $VENV_PY"
  if (cd "$ROOT" && "$VENV_PY" -c 'import core.contracts, core.supervisor, core.providers' >/dev/null 2>&1); then
    ok "venv imports core.contracts, core.supervisor, core.providers"
  else
    bad "venv cannot import core.contracts/core.supervisor/core.providers from $ROOT"
  fi
  if "$VENV_PY" -c 'import pytest' >/dev/null 2>&1; then ok "pytest installed in venv"; else bad "pytest missing from venv (requirements-dev.txt not installed)"; fi
else
  bad "venv python missing at $VENV_PY"
fi

# --- active profile --------------------------------------------------------------
ACTIVE="$ROOT/profiles/active.json"
if [ -f "$ACTIVE" ]; then
  active_profile="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("profile",""))' "$ACTIVE" 2>/dev/null)"
  if [ $? -ne 0 ]; then
    bad "$ACTIVE is not valid JSON"
  elif [ "$active_profile" = "$PROFILE" ]; then
    ok "active.json profile=$active_profile matches requested profile"
  else
    bad "active.json profile='$active_profile' does not match requested '$PROFILE'"
  fi
else
  bad "$ACTIVE missing"
fi

# --- provider CLIs for the profile --------------------------------------------------
for cli in $CLIS; do
  if [ -x "$NPM_BIN/$cli" ]; then
    if as_user timeout 60 "$NPM_BIN/$cli" --version >/dev/null 2>&1; then
      ok "$cli --version exits 0 ($NPM_BIN/$cli)"
    else
      bad "$cli --version failed ($NPM_BIN/$cli)"
    fi
  else
    bad "$cli missing from $NPM_BIN (profile=$PROFILE)"
  fi
done
echo "INFO provider login is verified by the first real job, not here"

# --- Gemini API key (profiles with extras.gemini: Worker 'auto' -> Gemini over HTTPS, no CLI) ---
# Absence is not a failure: it is the documented manual step after install and
# the Worker 'auto' route falls back to Claude Sonnet until then. A file that
# exists but is not private to orchestrator is a failure. The value is never printed.
GEMINI_SECRETS="/home/$TARGET_USER/.config/ai-orchestrator/secrets.env"
uses_gemini="$(python3 -c 'import json,sys; p=json.load(open(sys.argv[1])); print(1 if (p.get("extras") or {}).get("gemini") else 0)' "$ROOT/profiles/$PROFILE.json" 2>/dev/null || echo 0)"
if [ "$uses_gemini" = "1" ]; then
  if [ -f "$GEMINI_SECRETS" ]; then
    if bash "$ROOT/installer/gemini-secret.sh" check >/dev/null 2>&1; then
      ok "gemini secret: $GEMINI_SECRETS present, owner $TARGET_USER, dir 700 / file 600, GEMINI_API_KEY set (value not shown)"
    else
      bad "gemini secret: $GEMINI_SECRETS exists but is not private or has no GEMINI_API_KEY; run 'sudo bash $ROOT/installer/gemini-secret.sh check' for details"
    fi
  else
    info "gemini secret: not configured yet ($GEMINI_SECRETS); Worker 'auto' falls back to Claude Sonnet until 'sudo bash $ROOT/installer/gemini-secret.sh set' is done (docs/INSTALL.md section 4.1)"
  fi
fi

# --- systemd units -----------------------------------------------------------------
for u in orchestrator@.service orchestrator-health.service orchestrator-health.timer orchestrator-watchdog.service orchestrator-watchdog.timer; do
  if [ -f "/etc/systemd/system/$u" ]; then ok "unit installed: $u"; else bad "unit not installed: /etc/systemd/system/$u"; fi
done
if command -v systemctl >/dev/null 2>&1; then
  for t in orchestrator-health.timer orchestrator-watchdog.timer; do
    st="$(systemctl is-enabled "$t" 2>/dev/null || true)"
    if [ "$st" = "enabled" ]; then ok "$t is enabled"; else bad "$t is-enabled='$st', expected enabled"; fi
  done
  st="$(systemctl is-active orchestrator-health.timer 2>/dev/null || true)"
  if [ "$st" = "active" ]; then ok "orchestrator-health.timer is active"; else bad "orchestrator-health.timer is-active='$st', expected active"; fi
else
  bad "systemctl not found"
fi

# --- monitor snapshot ----------------------------------------------------------------
if [ ! -f "$STATUS" ] && [ -x "$VENV_PY" ]; then
  info "status.json missing; collecting once as $TARGET_USER"
  as_user "$VENV_PY" "$ROOT/monitor/collect_status.py" >/dev/null 2>&1 || true
fi
if [ -f "$STATUS" ]; then
  age="$(python3 -c '
import json,sys,time
d=json.load(open(sys.argv[1])); print(int(time.time()-float(d["timestamp"])))' "$STATUS" 2>/dev/null)"
  if [ $? -ne 0 ] || [ -z "$age" ]; then
    bad "$STATUS is not valid JSON with a numeric timestamp"
  elif [ "$age" -le "$MAX_STATUS_AGE" ] && [ "$age" -ge -60 ]; then
    ok "status.json is fresh (${age}s old, limit ${MAX_STATUS_AGE}s)"
  else
    bad "status.json is stale (${age}s old, limit ${MAX_STATUS_AGE}s); health timer is not writing it"
  fi
else
  bad "$STATUS missing"
fi

if [ "$fail" -ne 0 ]; then
  echo "DIAGNOSE FAIL profile=$PROFILE"
  exit 1
fi
echo "DIAGNOSE PASS profile=$PROFILE"
exit 0
