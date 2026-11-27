#!/usr/bin/env bash
# Place or verify GEMINI_API_KEY on the host for the Gemini Worker route
# (hybrid profile: Worker 'auto' prefers Gemini Flash over HTTPS; there is no
# Gemini CLI, core/gemini_runner.py talks to the API directly).
#
# Usage (on the host, as root via sudo or as the orchestrator user):
#   sudo bash /opt/ai-orchestrator/installer/gemini-secret.sh set            # interactive: type the key here
#   sudo bash /opt/ai-orchestrator/installer/gemini-secret.sh check          # offline: file, owner, modes, key present
#   sudo bash /opt/ai-orchestrator/installer/gemini-secret.sh check --live   # plus one minimal Gemini API request
#
# The runner reads GEMINI_API_KEY from its environment first and otherwise from
# /home/orchestrator/.config/ai-orchestrator/secrets.env (KEY=VALUE lines;
# ORCHESTRATOR_SECRETS_FILE overrides the path for the runner and for this
# script alike).
#
# Safety properties of 'set':
#   - the key is read ONLY from an interactive terminal with `read -rs` (not
#     echoed); never from an argument, a pipe, a file or the environment, and
#     the command refuses to run without a TTY;
#   - the file is written with umask 077 through bash builtins, so the value
#     never appears in a process list, a log or this script's output;
#   - result: directory 700 and file 600, both owned by orchestrator; other
#     KEY=VALUE lines already in the file are kept.
# 'check' never prints the value: only presence, owner, modes and (with
# --live) the HTTP outcome of one request made through core.gemini_runner.
set -u
set +x   # never trace this script: the value would land in the trace
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE_USER=orchestrator
VENV_PY="$ROOT/.venv/bin/python"
MIN_KEY_LENGTH=16

ok(){ printf 'OK   %s\n' "$*"; }
bad(){ printf 'FAIL %s\n' "$*"; fail=1; }
info(){ printf 'INFO %s\n' "$*"; }
abort(){ printf 'ERROR %s\n' "$*" >&2; echo "GEMINI SECRET FAIL" >&2; exit "${2:-2}"; }
usage(){ echo "Usage: $0 set | check [--live]" >&2; exit 2; }

# --- who owns the secret and where it lives ---------------------------------
if [ "$(id -u)" -eq 0 ]; then
  TARGET_USER="$SERVICE_USER"
  id "$TARGET_USER" >/dev/null 2>&1 || abort "user $TARGET_USER does not exist; run installer/install.sh first"
  TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
else
  TARGET_USER="$(id -un)"
  TARGET_HOME="${HOME:-$(getent passwd "$TARGET_USER" | cut -d: -f6)}"
  if [ "$TARGET_USER" != "$SERVICE_USER" ] && [ -z "${ORCHESTRATOR_SECRETS_FILE:-}" ]; then
    abort "run this as root (sudo) or as $SERVICE_USER: the orchestrator service reads /home/$SERVICE_USER/.config/ai-orchestrator/secrets.env, not $TARGET_USER's home"
  fi
fi
SECRETS_FILE="${ORCHESTRATOR_SECRETS_FILE:-$TARGET_HOME/.config/ai-orchestrator/secrets.env}"
SECRETS_DIR="$(dirname "$SECRETS_FILE")"

# Run a command as the target user (directly when we already are that user).
as_target(){
  if [ "$(id -u)" -eq 0 ]; then
    runuser -u "$TARGET_USER" -- env HOME="$TARGET_HOME" PATH="$ROOT/.venv/bin:/usr/local/bin:/usr/bin:/bin" "$@"
  else
    env HOME="$TARGET_HOME" PATH="$ROOT/.venv/bin:/usr/local/bin:/usr/bin:/bin" "$@"
  fi
}

# --- check: offline verification, never prints the value -----------------------
check_secret(){
  fail=0
  if [ ! -d "$SECRETS_DIR" ]; then
    bad "not configured: $SECRETS_DIR missing (run: sudo bash $ROOT/installer/gemini-secret.sh set)"
    return 1
  fi
  local owner mode
  owner="$(stat -c %U "$SECRETS_DIR" 2>/dev/null || echo unknown)"
  mode="$(stat -c %a "$SECRETS_DIR" 2>/dev/null || echo unknown)"
  if [ "$owner" = "$TARGET_USER" ]; then ok "$SECRETS_DIR owned by $TARGET_USER"; else bad "$SECRETS_DIR owned by '$owner', expected $TARGET_USER (chown $TARGET_USER:$TARGET_USER $SECRETS_DIR)"; fi
  case "$mode" in
    700|500) ok "$SECRETS_DIR mode $mode" ;;
    *) bad "$SECRETS_DIR mode $mode, expected 700 (chmod 700 $SECRETS_DIR)" ;;
  esac
  if [ ! -f "$SECRETS_FILE" ]; then
    bad "not configured: $SECRETS_FILE missing (run: sudo bash $ROOT/installer/gemini-secret.sh set)"
    return 1
  fi
  owner="$(stat -c %U "$SECRETS_FILE" 2>/dev/null || echo unknown)"
  mode="$(stat -c %a "$SECRETS_FILE" 2>/dev/null || echo unknown)"
  if [ "$owner" = "$TARGET_USER" ]; then ok "$SECRETS_FILE owned by $TARGET_USER"; else bad "$SECRETS_FILE owned by '$owner', expected $TARGET_USER (chown $TARGET_USER:$TARGET_USER $SECRETS_FILE)"; fi
  case "$mode" in
    600|400) ok "$SECRETS_FILE mode $mode" ;;
    *) bad "$SECRETS_FILE mode $mode, expected 600 (chmod 600 $SECRETS_FILE)" ;;
  esac
  # A GEMINI_API_KEY line with a non-empty value; the value itself is never printed.
  if grep -Eq "^[[:space:]]*GEMINI_API_KEY[[:space:]]*=[[:space:]]*['\"]?[^[:space:]'\"]+" "$SECRETS_FILE" 2>/dev/null; then
    ok "GEMINI_API_KEY is set in $SECRETS_FILE (value not shown)"
  else
    bad "GEMINI_API_KEY line missing or empty in $SECRETS_FILE (run: sudo bash $ROOT/installer/gemini-secret.sh set)"
  fi
  [ "$fail" -eq 0 ]
}

# --- check --live: one minimal request through the runner's own HTTPS path -------
live_smoke(){
  local py="$VENV_PY"
  [ -x "$py" ] || py="$(command -v python3 || true)"
  [ -n "$py" ] || { bad "no python found for the live smoke ($VENV_PY missing and python3 not on PATH)"; return; }
  # cwd=$ROOT so `core` imports; the file path is passed explicitly so the
  # runner reads exactly the file verified above. Output never contains the key.
  if (cd "$ROOT" && as_target env ORCHESTRATOR_SECRETS_FILE="$SECRETS_FILE" "$py" -m core.gemini_runner --smoke); then
    ok "live smoke request accepted (see GEMINI SMOKE line above)"
  else
    bad "live smoke request failed (see GEMINI SMOKE line above); the key may be invalid, or the API is unreachable/unavailable"
  fi
}

# --- set: interactive only -------------------------------------------------------
set_secret(){
  [ $# -eq 0 ] || abort "'set' takes no arguments: the key is typed at the prompt, never passed on the command line"
  { [ -t 0 ] && [ -t 1 ]; } || abort "'set' needs an interactive terminal (use: ssh -t <host> sudo bash $0 set); the key is never read from a pipe, a file or the environment"
  echo "Placing GEMINI_API_KEY for $TARGET_USER in $SECRETS_FILE (directory 700, file 600)."
  echo "Input is hidden and is neither echoed, logged nor stored anywhere else."
  local key key2
  IFS= read -rs -p 'Paste GEMINI_API_KEY (input hidden): ' key; echo
  IFS= read -rs -p 'Paste it again to confirm: ' key2; echo
  [ "$key" = "$key2" ] || abort "the two entries differ; nothing was written"
  [ -n "$key" ] || abort "empty input; nothing was written"
  [ "${#key}" -ge "$MIN_KEY_LENGTH" ] || abort "input is shorter than $MIN_KEY_LENGTH characters; nothing was written"
  case "$key" in
    *[![:graph:]]*|*\'*|*\"*|*\\*) abort "the key may only contain printable characters without spaces, quotes or backslashes; nothing was written" ;;
  esac

  # Directory as the target user so any created parent belongs to it.
  as_target sh -c 'umask 077 && mkdir -p -- "$1" && chmod 700 -- "$1"' sh "$SECRETS_DIR" \
    || abort "cannot create $SECRETS_DIR" 3
  # Keep other KEY=VALUE lines, replace any existing GEMINI_API_KEY line. Bash
  # builtins only (printf), so the value never becomes a process argument.
  local tmp="$SECRETS_DIR/.secrets.env.$$"
  (
    umask 077
    {
      if [ -f "$SECRETS_FILE" ]; then grep -Ev '^[[:space:]]*GEMINI_API_KEY[[:space:]]*=' "$SECRETS_FILE"; fi
      printf 'GEMINI_API_KEY=%s\n' "$key"
    } > "$tmp"
  ) || { rm -f "$tmp"; abort "cannot write $tmp" 3; }
  chmod 600 "$tmp" || { rm -f "$tmp"; abort "cannot chmod $tmp" 3; }
  if [ "$(id -u)" -eq 0 ]; then chown "$TARGET_USER:$TARGET_USER" "$tmp" || { rm -f "$tmp"; abort "cannot chown $tmp" 3; }; fi
  mv -f "$tmp" "$SECRETS_FILE" || { rm -f "$tmp"; abort "cannot move $tmp into place" 3; }
  unset key key2
  echo "Written: $SECRETS_FILE"
}

# --- dispatch ----------------------------------------------------------------------
cmd="${1:-}"; [ $# -gt 0 ] && shift
case "$cmd" in
  set)
    set_secret "$@"
    check_secret || { echo "GEMINI SECRET FAIL file=$SECRETS_FILE"; exit 1; }
    echo "GEMINI SECRET PASS file=$SECRETS_FILE"
    ;;
  check)
    live=0
    for arg in "$@"; do
      case "$arg" in
        --live) live=1 ;;
        *) usage ;;
      esac
    done
    check_secret
    rc=$?
    if [ "$rc" -eq 0 ] && [ "$live" -eq 1 ]; then
      live_smoke
      rc=$fail
    elif [ "$live" -eq 1 ]; then
      info "live smoke skipped: the offline checks above failed"
    fi
    if [ "$rc" -ne 0 ]; then echo "GEMINI SECRET FAIL file=$SECRETS_FILE"; exit 1; fi
    echo "GEMINI SECRET PASS file=$SECRETS_FILE"
    ;;
  *) usage ;;
esac
exit 0
