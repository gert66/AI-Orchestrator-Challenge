#!/usr/bin/env bash
# Fail-closed static gate for the starter kit. Exits nonzero on the first FAIL.
# Usage: bash installer/preflight.sh   (from any cwd)
#   PREFLIGHT_SKIP_PYTEST=1  skip the pytest stage (used by the test suite itself)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

fail() { echo "FAIL $*"; exit 1; }

for f in MISSION.md README.md pytest.ini \
  core/supervisor.py core/machine.py core/contracts.py core/provider_registry.py core/repo_policy.py core/submit_job.py \
  core/acceptance.py \
  prompts/brain.md prompts/worker.md prompts/reviewer.md config/repo_policy.json \
  schemas/brain.schema.json schemas/worker.schema.json schemas/reviewer.schema.json schemas/job.schema.json schemas/state.schema.json \
  profiles/claude.json profiles/openai.json profiles/hybrid.json \
  installer/install.sh installer/diagnose.sh installer/install-systemd.sh installer/watchdog.sh installer/start-here.sh \
  installer/acceptance.sh installer/gemini-secret.sh \
  systemd/orchestrator@.service systemd/orchestrator-health.service systemd/orchestrator-health.timer \
  systemd/orchestrator-watchdog.service systemd/orchestrator-watchdog.timer docs/INSTALL.md docs/ACCEPTANCE.md docs/evidence/README.md \
  monitor/collect_status.py monitor/index.html monitor/update_mission.py START-HERE.ps1 \
  tests/test_contracts.py tests/test_schemas.py tests/test_profiles.py tests/test_installer_static.py tests/test_imports.py \
  tests/test_role_routing.py tests/test_entry_point_static.py tests/test_submit_job.py tests/test_machine_e2e.py \
  tests/test_acceptance.py tests/test_gemini_secret.py; do
  test -f "$ROOT/$f" || fail "MISSING $f"
done
echo "OK   required files present"

# Normalise the executable bit: a checkout made on Windows (core.filemode=false)
# or an upload built by Windows tar.exe loses it, and install.sh runs these
# scripts directly.
chmod +x "$ROOT"/installer/*.sh || fail "cannot set executable bit on installer/*.sh"
for s in "$ROOT"/installer/*.sh; do [ -x "$s" ] || fail "not executable: ${s#"$ROOT"/}"; done
echo "OK   installer scripts executable"

bash -n "$ROOT"/installer/*.sh || fail "bash syntax"
echo "OK   installer scripts parse"

python3 -m py_compile "$ROOT"/core/*.py "$ROOT"/monitor/*.py "$ROOT"/tests/*.py || fail "py_compile"
echo "OK   python sources compile"

python3 -c "import sys; sys.path.insert(0,'$ROOT'); import core.supervisor, core.machine, core.contracts, core.provider_registry" \
  || fail "core imports"
echo "OK   core imports"

for p in schemas/*.json profiles/*.json config/*.json templates/*.json; do
  python3 -c "import json,sys; json.load(open(sys.argv[1], encoding='utf-8'))" "$ROOT/$p" || fail "invalid JSON $p"
done
echo "OK   JSON files parse"

scan="$(mktemp -p "$ROOT")"; trap 'rm -f "$scan"' EXIT
set +e
grep -RIlE --exclude=preflight.sh --exclude-dir=__pycache__ \
  "/home/myngle/(Myngle|MasterRisk|worktrees|myngle-company-hub)|Sales Cockpit|HubSpot" \
  "$ROOT/core" "$ROOT/systemd" "$ROOT/monitor" "$ROOT/tests" "$ROOT/installer" "$ROOT/profiles" "$ROOT/schemas" >"$scan"
rc=$?
set -e
[ "$rc" -le 1 ] || fail "PRIVACY SCAN ERROR rc=$rc"
if [ -s "$scan" ]; then echo "PRIVATE REFERENCE FOUND"; cat "$scan"; fail "privacy scan"; fi
echo "OK   privacy scan"

avail_kb="$(df -Pk "$ROOT" | awk 'NR==2{print $4}')"
[ "$avail_kb" -ge 524288 ] || fail "LOW DISK SPACE (${avail_kb} KB available)"
echo "OK   disk space"

if [ "${PREFLIGHT_SKIP_PYTEST:-0}" = "1" ]; then
  echo "SKIP pytest (PREFLIGHT_SKIP_PYTEST=1)"
elif python3 -c "import pytest" >/dev/null 2>&1; then
  set +e
  python3 -m pytest -q "$ROOT/tests"
  prc=$?
  set -e
  [ "$prc" -eq 0 ] || fail "pytest exited $prc"
  echo "OK   pytest"
else
  echo "WARN pytest not installed; static checks only (pip install -r requirements-dev.txt to enable)"
fi

echo "PREFLIGHT PASS"
