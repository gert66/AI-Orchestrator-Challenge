#!/usr/bin/env bash
# Install the provider CLIs required by a profile into the orchestrator user's
# npm prefix and fail closed if the expected binaries are not present afterwards.
# Login/credentials are out of scope here (see docs/INSTALL.md).
set -euo pipefail
PROFILE="${1:-claude}"
TARGET_USER="orchestrator"
HOME_DIR="/home/orchestrator"
PREFIX="$HOME_DIR/.npm-global"
BIN="$PREFIX/bin"
as_user(){ runuser -u "$TARGET_USER" -- env HOME="$HOME_DIR" PATH="$BIN:/usr/local/bin:/usr/bin:/bin" "$@"; }

case "$PROFILE" in
  claude) PACKAGES="@anthropic-ai/claude-code"; REQUIRED="claude" ;;
  openai) PACKAGES="@openai/codex"; REQUIRED="codex" ;;
  hybrid) PACKAGES="@anthropic-ai/claude-code @openai/codex"; REQUIRED="claude codex" ;;
  *) echo "Unknown profile: $PROFILE" >&2; exit 2 ;;
esac

install -d -o "$TARGET_USER" -g "$TARGET_USER" "$PREFIX"
as_user npm config set prefix "$PREFIX"
# shellcheck disable=SC2086
as_user npm install -g $PACKAGES

# Profile-independent PATH for interactive shells (login as orchestrator, etc.).
install -m 0644 /dev/null /etc/profile.d/ai-orchestrator.sh
cat >/etc/profile.d/ai-orchestrator.sh <<'EOF'
# AI Orchestrator: provider CLIs live in the orchestrator user's npm prefix.
case ":$PATH:" in
  *":/home/orchestrator/.npm-global/bin:"*) ;;
  *) PATH="/home/orchestrator/.npm-global/bin:$PATH" ;;
esac
export PATH
EOF
chmod 0644 /etc/profile.d/ai-orchestrator.sh

fail=0
for cli in $REQUIRED; do
  if [ -x "$BIN/$cli" ]; then
    printf 'OK   %s present at %s\n' "$cli" "$BIN/$cli"
  else
    printf 'FAIL %s missing from %s (profile=%s)\n' "$cli" "$BIN" "$PROFILE"
    fail=1
  fi
done
[ "$fail" -eq 0 ] || exit 1
echo "PROVIDERS OK profile=$PROFILE"
