#!/usr/bin/env bash
# Linux/macOS/WSL entry point for the AI Orchestrator Starter Kit; the Bash
# twin of START-HERE.ps1 with identical semantics.
#
# Usage: installer/start-here.sh <server> <claude|openai|hybrid> [user]
#   ACCEPT_NEW_HOST_KEY=1   add StrictHostKeyChecking=accept-new (first contact with a new host)
#   SSH_KEY=/path/to/key    use an explicit SSH private key for ssh/scp
#
# Fail-closed, in order: local ssh/scp/tar present -> kit directory and profile
# valid -> key-based SSH (BatchMode, never a password prompt) and passwordless
# sudo -> filtered tarball upload (no .git, .venv, __pycache__,
# monitor/status.json or other local state) re-extracted idempotently under
# /root/ai-orchestrator-starter -> 'sudo bash installer/install.sh <profile>'
# streamed to this terminal (preflight, systemd units and diagnose run inside).
# The last line printed is exactly one of
#   STARTER INSTALL OK profile=<profile> server=<server>
#   STARTER INSTALL FAILED exit=<code>
# and the exit code matches (0 on success).
set -euo pipefail

KIT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE_DIR=/root/ai-orchestrator-starter
REMOTE_ARCHIVE=ai-orchestrator-starter.tgz   # relative to the SSH user's home
INSTALLED_DIR=/opt/ai-orchestrator
# Local state that must never reach the host. Keep in sync with START-HERE.ps1.
EXCLUDES=(.git .venv __pycache__ .pytest_cache '*.pyc' monitor/status.json profiles/active.json jobs)

SERVER="${1:-}"
PROFILE="${2:-}"
SSH_USER="${3:-root}"

step() { printf '==> %s\n' "$*"; }

abort() { # abort <code> <message>
  local code="$1"; shift
  [ "$code" -ne 0 ] || code=1
  echo "ERROR: $*"
  echo "STARTER INSTALL FAILED exit=$code"
  exit "$code"
}

# --- 1. local tools -----------------------------------------------------------
for tool in ssh scp tar; do
  command -v "$tool" >/dev/null 2>&1 || abort 3 "'$tool' was not found on PATH (OpenSSH client and tar are required)."
done

# --- 2. kit directory, arguments and profile ------------------------------------
[ -f "$KIT_DIR/installer/install.sh" ] || abort 3 "installer/install.sh is missing in the kit directory ($KIT_DIR)."
[ -n "$SERVER" ] && [ -n "$PROFILE" ] || abort 2 "Usage: $0 <server> <claude|openai|hybrid> [user]"
[[ "$SERVER" =~ ^[A-Za-z0-9][A-Za-z0-9.:-]*$ ]] || abort 2 "Invalid server name: '$SERVER'"
[[ "$SSH_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] || abort 2 "Invalid SSH user name: '$SSH_USER'"
case "$PROFILE" in
  claude|openai|hybrid) ;;
  *) abort 2 "Unknown profile '$PROFILE'. Expected one of: claude, openai, hybrid." ;;
esac
[ -f "$KIT_DIR/profiles/$PROFILE.json" ] || abort 3 "profiles/$PROFILE.json is missing in the kit directory ($KIT_DIR)."

TARGET="$SSH_USER@$SERVER"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15)
if [ -n "${SSH_KEY:-}" ]; then
  [ -f "$SSH_KEY" ] || abort 3 "SSH_KEY does not exist: '$SSH_KEY'"
  SSH_OPTS+=(-i "$SSH_KEY" -o IdentitiesOnly=yes)
fi
if [ "${ACCEPT_NEW_HOST_KEY:-0}" = "1" ]; then SSH_OPTS+=(-o StrictHostKeyChecking=accept-new); fi

echo "Kit directory : $KIT_DIR"
echo "Target        : $TARGET"
echo "Profile       : $PROFILE"

# --- 3. SSH and sudo checks ---------------------------------------------------
step "Testing key-based SSH to $TARGET"
set +e
ssh "${SSH_OPTS[@]}" "$TARGET" 'echo SSH_OK'
rc=$?
set -e
if [ "$rc" -ne 0 ]; then
  abort 4 "SSH to $TARGET failed (exit $rc). Key-based SSH is required: this entry point never prompts for passwords. Load your key (ssh-agent or ~/.ssh/id_*) and make sure it is authorised on the host. If this is the first contact with a new host, re-run with ACCEPT_NEW_HOST_KEY=1."
fi
step "Checking passwordless sudo for $SSH_USER"
ssh "${SSH_OPTS[@]}" "$TARGET" 'sudo -n true' \
  || abort 4 "'sudo -n true' failed for $SSH_USER on $SERVER; the installer needs root via passwordless sudo."

# --- 4. filtered upload -------------------------------------------------------
step "Building filtered kit archive (no .git, .venv, __pycache__, status.json or other local state)"
LOCAL_ARCHIVE="$(mktemp "${TMPDIR:-/tmp}/ai-orchestrator-starter.XXXXXX")"
trap 'rm -f "$LOCAL_ARCHIVE"' EXIT
TAR_ARGS=(-c -z -f "$LOCAL_ARCHIVE")
for pattern in "${EXCLUDES[@]}"; do TAR_ARGS+=("--exclude=$pattern"); done
tar "${TAR_ARGS[@]}" -C "$KIT_DIR" . || abort 5 "tar failed to build the kit archive."
echo "Archive: $LOCAL_ARCHIVE ($(( $(wc -c <"$LOCAL_ARCHIVE") / 1024 )) KB)"

step "Uploading archive to $TARGET:~/$REMOTE_ARCHIVE"
scp "${SSH_OPTS[@]}" -q "$LOCAL_ARCHIVE" "$TARGET:$REMOTE_ARCHIVE" || abort 5 "scp upload failed."

step "Re-creating $REMOTE_DIR on the host from the archive"
# chmod: a checkout without executable bits (e.g. one built by Windows tar.exe)
# must still work, because install.sh runs sibling scripts from this directory.
ssh "${SSH_OPTS[@]}" "$TARGET" \
  "set -e; sudo rm -rf $REMOTE_DIR; sudo mkdir -p $REMOTE_DIR; sudo tar --no-same-owner -xzf ~/$REMOTE_ARCHIVE -C $REMOTE_DIR; rm -f ~/$REMOTE_ARCHIVE; sudo chmod +x $REMOTE_DIR/installer/*.sh $REMOTE_DIR/monitor/*.py; sudo test -f $REMOTE_DIR/installer/install.sh; sudo test -f $REMOTE_DIR/profiles/$PROFILE.json; echo UPLOAD_OK" \
  || abort 5 "Remote extraction into $REMOTE_DIR failed."

# --- 5. remote install (preflight + systemd + diagnose inside install.sh) ------
step "Running sudo bash installer/install.sh $PROFILE on $SERVER (output streamed below)"
# Absolute script path: install.sh is cwd-independent and /root is not readable
# by a non-root SSH user. ssh returns install.sh's own exit code.
set +e
ssh "${SSH_OPTS[@]}" "$TARGET" "sudo bash $REMOTE_DIR/installer/install.sh $PROFILE"
rc=$?
set -e
if [ "$rc" -ne 0 ]; then
  echo "ERROR: installer/install.sh exited $rc on $SERVER. Read the last FAIL / INSTALL FAIL line above, fix the kit, re-run this script."
  echo "STARTER INSTALL FAILED exit=$rc"
  exit "$rc"
fi

echo
echo "Install, preflight and diagnose passed on the host."
if [ "$PROFILE" = hybrid ]; then
  echo "Manual steps left: the provider logins, once, as the orchestrator user, and the Gemini API key (below):"
else
  echo "The only manual step left is the provider login, once, as the orchestrator user:"
fi
echo "  ssh $TARGET"
echo "  sudo -iu orchestrator"
case "$PROFILE" in
  claude) echo "  claude          # follow the interactive login" ;;
  openai) echo "  codex login" ;;
  *)      echo "  claude          # follow the interactive login"; echo "  codex login" ;;
esac
if [ "$PROFILE" = hybrid ]; then
  # Worker 'auto' -> Gemini Flash over HTTPS (core/gemini_runner.py, no Gemini
  # CLI). The key is typed on the host; this script never handles it.
  echo
  echo "Gemini (Worker \"auto\" prefers Gemini Flash over the Gemini HTTPS API; no Gemini CLI is installed or needed):"
  echo "  GEMINI_API_KEY belongs on the host in /home/orchestrator/.config/ai-orchestrator/secrets.env (dir 700, file 600, owner orchestrator)."
  echo "  Until it is there, Worker \"auto\" falls back to Claude Sonnet on every call (recorded as a fallback event);"
  echo "  Brain and Reviewer are unaffected. Type the key on the host - never pass it to this script or on a command line:"
  echo "    ssh -t $TARGET sudo bash $INSTALLED_DIR/installer/gemini-secret.sh set"
  echo "  Verify afterwards without revealing it (offline checks; add --live for one real API request):"
  echo "    ssh $TARGET sudo bash $INSTALLED_DIR/installer/gemini-secret.sh check --live"
fi
echo "Reboot re-check (must print DIAGNOSE PASS again):"
echo "  sudo bash $INSTALLED_DIR/installer/diagnose.sh $PROFILE"
echo "STARTER INSTALL OK profile=$PROFILE server=$SERVER"
exit 0
