#!/usr/bin/env bash
set -euo pipefail
USER_NAME="${1:-orchestrator}"
if ! id "$USER_NAME" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "$USER_NAME"
fi
install -d -o "$USER_NAME" -g "$USER_NAME" "/home/$USER_NAME/.npm-global"
