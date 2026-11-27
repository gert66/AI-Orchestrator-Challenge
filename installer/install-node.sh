#!/usr/bin/env bash
# Guarantee Node.js >= 18 with npm (the Claude and Codex CLIs need it).
# Ubuntu's apt "nodejs" can be older than 18, so use NodeSource Node 20 LTS
# whenever a suitable node/npm is not already present.
set -euo pipefail
MIN_MAJOR=18
NODESOURCE_URL="https://deb.nodesource.com/setup_20.x"

node_major(){
  local v
  v="$(node --version 2>/dev/null || true)"
  v="${v#v}"; v="${v%%.*}"
  case "$v" in ''|*[!0-9]*) echo 0 ;; *) echo "$v" ;; esac
}

if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1 \
   && [ "$(node_major)" -ge "$MIN_MAJOR" ]; then
  echo "Node $(node --version) and npm $(npm --version) already present"
  exit 0
fi

echo "Installing Node.js 20 LTS from NodeSource (current major: $(node_major))"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y curl ca-certificates gnupg
curl -fsSL "$NODESOURCE_URL" -o /tmp/nodesource_setup.sh
bash /tmp/nodesource_setup.sh
rm -f /tmp/nodesource_setup.sh
DEBIAN_FRONTEND=noninteractive apt-get install -y nodejs

if [ "$(node_major)" -lt "$MIN_MAJOR" ]; then
  echo "FAIL node $(node --version 2>/dev/null || echo missing) is older than v${MIN_MAJOR}" >&2
  exit 1
fi
command -v npm >/dev/null 2>&1 || { echo "FAIL npm missing after nodejs install" >&2; exit 1; }
echo "OK   node $(node --version) npm $(npm --version)"
