#!/usr/bin/env bash
# On-host acceptance runner: records machine-readable evidence for every
# definition-of-done item (docs/ACCEPTANCE.md) after install and after reboot.
# Usage: sudo bash /opt/ai-orchestrator/installer/acceptance.sh [claude|openai|hybrid]
#            [--phase post-install|post-reboot] [--job-id <id>] [--out-dir <dir>]
# Without a profile argument the profile comes from profiles/active.json.
# Exits with the code of `python -m core.acceptance` (0 only on ACCEPTANCE PASS).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PROFILE=""
ARGS=()
if [ $# -gt 0 ]; then
  case "$1" in
    claude|openai|hybrid) PROFILE="$1"; shift ;;
    --*) ;;
    *) echo "ACCEPTANCE FAIL unknown profile '$1' (expected claude|openai|hybrid)" >&2; exit 2 ;;
  esac
fi
while [ $# -gt 0 ]; do
  case "$1" in
    --phase|--job-id|--out-dir)
      [ $# -ge 2 ] || { echo "ACCEPTANCE FAIL $1 needs a value" >&2; exit 2; }
      ARGS+=("$1" "$2"); shift 2 ;;
    --phase=*|--job-id=*|--out-dir=*) ARGS+=("$1"); shift ;;
    *) echo "ACCEPTANCE FAIL unknown argument '$1'" >&2; exit 2 ;;
  esac
done

if [ -z "$PROFILE" ]; then
  ACTIVE="$ROOT/profiles/active.json"
  [ -f "$ACTIVE" ] || { echo "ACCEPTANCE FAIL no profile given and $ACTIVE missing" >&2; exit 2; }
  PROFILE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("profile",""))' "$ACTIVE")"
  case "$PROFILE" in
    claude|openai|hybrid) ;;
    *) echo "ACCEPTANCE FAIL $ACTIVE has profile '$PROFILE' (expected claude|openai|hybrid)" >&2; exit 2 ;;
  esac
fi

# Prefer the kit's venv python (pytest lives there); fall back to system python3.
PYTHON=python3
if [ -x "$ROOT/.venv/bin/python" ]; then
  PYTHON="$ROOT/.venv/bin/python"
  export PATH="$ROOT/.venv/bin:$PATH"
fi

set +e
"$PYTHON" -m core.acceptance "$PROFILE" ${ARGS[@]+"${ARGS[@]}"}
rc=$?
set -e
exit "$rc"
