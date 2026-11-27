# Acceptance gate

A clean Ubuntu VM must install from the starter kit, survive reboot, pass diagnostics, and execute a real job through Brain, Worker, tests/evidence, Reviewer, optional Repair, commit and push.

The VM Monitor and recovery/watchdog layer must remain healthy after reboot.

VM2 is not a clone target. It is the clean-room examiner.

## How the gate is proven

`installer/acceptance.sh` (a thin wrapper around `python3 -m core.acceptance`)
runs on the host itself, after install and again after reboot. It is
fail-closed: every check below is recorded as `PASS`, `FAIL`, `SKIP` or
`MANUAL`; the run exits 0 and prints exactly one
`ACCEPTANCE PASS phase=<phase> profile=<profile> evidence=<json path>` line
only when no check is `FAIL`. Any `FAIL` exits 1 with one
`ACCEPTANCE FAIL <n> check(s) failed ...` line on stderr. `SKIP` and `MANUAL`
never fail the run but are always listed, so nothing is silently omitted.

Each run writes two artifacts under `/opt/ai-orchestrator/evidence/acceptance/`
(override with `--out-dir`):

- `acceptance-<UTCstamp>.json` — `schema_version` 1 with `started_at`,
  `finished_at`, `host` (`hostname`, `kernel`, `boot_id`, `uptime_seconds`),
  `profile`, `kit_commit`, `phase` (`post-install` | `post-reboot`), one
  `checks[]` entry per check (`id`, `title`, `status`, `detail`, `evidence`)
  and the overall `result`;
- `acceptance-<UTCstamp>.md` — the same as a human-readable table.

The phase is detected from `/var/lib/ai-orchestrator/install-stamp.json`
(written by `installer/install.sh` after its diagnose gate: `installed_at`,
`installed_epoch`, `profile`, `kit_commit`, `boot_id`): when the running
`boot_id` differs from the stamp's, the host rebooted since install and the
phase is `post-reboot`. `--phase post-reboot` forces the phase, and then an
unchanged `boot_id` is a `FAIL` (the reboot did not happen).

## 1. Checklist: definition of done -> check id -> artifact

Every definition-of-done item from `MISSION.md` maps to a check id in
`core/acceptance.py` and to the artifact that records it.

| # | Definition-of-done item (MISSION.md) | Check id | Recorded in |
|---|---|---|---|
| 1 | Installs on clean VM2 from the documented entry point; no undocumented manual technical repair | `install_stamp` (stamp exists, profile + kit_commit), `diagnose`, `preflight` | `acceptance-*.json` (`checks[].evidence`: stamp keys, `output_tail`, exit codes); `STARTER INSTALL OK` line from `START-HERE.ps1` / `installer/start-here.sh` |
| 2 | Preflight is fail-closed | `preflight` (exit 0 of `installer/preflight.sh` with the venv on `PATH`) | `acceptance-*.json` `evidence.output_tail` ends with `PREFLIGHT PASS` |
| 3 | Passes diagnostics | `diagnose` (`installer/diagnose.sh <profile>` exit 0) | `acceptance-*.json` `evidence.output_tail` ends with `DIAGNOSE PASS profile=<profile>` |
| 4 | Survives reboot | `reboot_survived` (`boot_id` differs from the install stamp; `SKIP` before the reboot) | `acceptance-*.json` with `phase: "post-reboot"` and `evidence.current_boot_id != evidence.stamp_boot_id` |
| 5 | Systemd/watchdog/monitor are healthy after reboot | `systemd_health` (both timers enabled + active, health service state), `monitor_fresh` (`monitor/status.json` < 600 s old) | `acceptance-*.json` `evidence.units` raw `systemctl` outputs and `evidence.age_seconds`, in the `post-reboot` run |
| 6 | Both provider profiles are genuinely role-routable (Claude, OpenAI without a hidden Claude dependency, Hybrid explicit) | `profile_routing` (brain/worker/reviewer for `profiles/claude.json`, `openai.json`, `hybrid.json` via `core.provider_registry`; openai routes no role to anthropic) | `acceptance-*.json` `evidence.matrix` (role -> provider per profile), `evidence.openai_routes_to_anthropic == []` |
| 7 | The offline test suite passes on the host | `pytest_suite` (`.venv/bin/python -m pytest -q -p no:cacheprovider tests`) | `acceptance-*.json` `evidence.summary` (pytest summary line) |
| 8 | Provider tooling is present; interactive login is the single documented manual step | `provider_cli_login` (`MANUAL` with the CLI `--version` exit code; `FAIL` only if the binary is absent) | `acceptance-*.json` `evidence.<cli>.path`, `version_exit_code` |
| 8b | Hybrid: the Gemini Worker route has its `GEMINI_API_KEY` on the host, placed by the documented manual step (`installer/gemini-secret.sh set`, docs/INSTALL.md 4.1), private to `orchestrator` | `gemini_secret` (`SKIP` for profiles without `extras.gemini`; `MANUAL` while the file is absent — the route falls back to Claude Sonnet; `PASS` when present, owner `orchestrator`, dir 700, file 600, non-empty key; `FAIL` only when a present file is not private or has no key) | `acceptance-*.json` `evidence.exists`, `dir_mode`, `file_mode`, `file_owner`, `key_present` — the value itself is never recorded; the live proof is `gemini-secret.sh check --live` on the host |
| 9 | A real Brain -> Worker -> Tests/Evidence -> Reviewer -> Repair when required -> Commit/Push flow ran | `real_job_flow` (`jobs/<job-id>/state.json` is `DONE`, at least one batch `committed`, last batch `pushed` when the repo policy sets `push_job_branch`) | `acceptance-*.json` `evidence.history` (step_id, outcome, verdict, repairs, reviews, commit, pushed) plus `jobs/<job-id>/evidence/*.json` on the host |
| 10 | Acceptance evidence and limitations are recorded | this document, `docs/evidence/<date>-<vm>/` | the copied `acceptance-*.json` / `.md` pairs (section 3) and the Limitations section below |
| 11 | A second clean installation passes after any discovered repair has been encoded in the kit | the whole sequence in section 2 run again on a fresh VM2 | a second `docs/evidence/<date>-<vm>/` directory whose `post-install` and `post-reboot` runs are `PASS` |

## 2. VM2 command sequence

Run from a checkout of the kit on the workstation (steps 1, 8) and on VM2
over SSH (steps 2-7). Every step must end with the line named in brackets
before the next one starts.

1. Install from the documented entry point
   [`STARTER INSTALL OK profile=<profile> server=<server>`]:

   ```powershell
   .\START-HERE.ps1 -Server <server-ip> -Profile <claude|openai|hybrid>
   ```

   or `installer/start-here.sh <server-ip> <claude|openai|hybrid>`.

2. Post-install acceptance [`ACCEPTANCE PASS phase=post-install ...`]:

   ```bash
   sudo bash /opt/ai-orchestrator/installer/acceptance.sh <profile>
   ```

   `reboot_survived` is `SKIP` here, `provider_cli_login` is `MANUAL`,
   `gemini_secret` is `MANUAL` for `hybrid` (`SKIP` otherwise),
   `real_job_flow` is `SKIP`.

3. The manual step(s): provider login as `orchestrator`
   (`claude` / `codex login`, see `docs/INSTALL.md` section 4) and, for
   `hybrid`, the Gemini API key typed on the host
   (`ssh -t root@<server-ip> sudo bash /opt/ai-orchestrator/installer/gemini-secret.sh set`,
   then `... gemini-secret.sh check --live`; section 4.1). From then on
   `gemini_secret` is `PASS`.

4. Reboot:

   ```bash
   sudo reboot
   ```

5. Post-reboot acceptance [`ACCEPTANCE PASS phase=post-reboot ...`]:

   ```bash
   sudo bash /opt/ai-orchestrator/installer/acceptance.sh <profile> --phase post-reboot
   ```

   `--phase post-reboot` makes an unchanged `boot_id` a `FAIL`, so this run
   cannot pass unless the host actually rebooted and the timers came back.

6. Submit and run a real job (`docs/INSTALL.md` section 5) as `orchestrator`
   [`JOB SUBMITTED job_id=<id> ...`, then `state.json` `phase: "DONE"`]:

   ```bash
   python3 -m core.submit_job --job-id <id> --repo owner/name --repo-path /home/orchestrator/repos/name \
     --branch job/<id> --mode write --goal "..." --test-command '["python3", "-m", "pytest", "-q"]'
   sudo systemctl start orchestrator@<id>.service && journalctl -u orchestrator@<id>.service -f
   ```

7. Acceptance with the job as proof [`ACCEPTANCE PASS phase=post-reboot ...`
   with `real_job_flow` `PASS`]:

   ```bash
   sudo bash /opt/ai-orchestrator/installer/acceptance.sh <profile> --phase post-reboot --job-id <id>
   ```

8. Second clean install: if any step above needed a repair, encode the repair
   in the kit first, then rebuild VM2 from a clean image (a control-plane
   action that needs explicit approval) and repeat steps 1-7. The kit is
   accepted only when this second pass needs no repair.

## 3. Recording evidence

The two acceptance artifacts (`acceptance-<UTCstamp>.json` and the matching
`.md`) from every VM2 run are copied off the host into this repository under
`docs/evidence/<date>-<vm>/`, e.g. `docs/evidence/2026-09-30-vm2/`, one file
pair per run (`post-install`, `post-reboot`, `post-reboot` with `--job-id`).
`docs/evidence/README.md` describes the layout. The artifacts contain no
credentials: only hostnames, kernel/boot ids, exit codes, command output tails
and the job history summary. No real evidence is committed until a VM2 run has
actually happened.

```bash
scp root@<server-ip>:/opt/ai-orchestrator/evidence/acceptance/acceptance-*.json docs/evidence/<date>-<vm>/
scp root@<server-ip>:/opt/ai-orchestrator/evidence/acceptance/acceptance-*.md docs/evidence/<date>-<vm>/
```

## 4. Limitations

- Interactive provider login / 2FA is the single documented manual step. The
  runner records it as `MANUAL` in `provider_cli_login` (the CLI's `--version`
  exit code is the only non-interactive proof it can gather); login state
  itself is proven by the real job in `real_job_flow`.
- `pytest_suite` runs only when `/opt/ai-orchestrator/.venv` exists (the
  installer creates it); without the venv the check is `SKIP`, not `PASS`.
- `real_job_flow` needs provider credentials on the host: a real job cannot
  run, and the check stays `SKIP`, until step 3 of the sequence is done and a
  job id is passed.
- `gemini_secret` proves only that the key file is present and private on the
  host; it never reads the value into evidence and makes no network call.
  Whether the key is accepted by Gemini is shown by
  `installer/gemini-secret.sh check --live` (one request through
  `core.gemini_runner`, output without the value) and by the `provider`
  recorded per Worker call in the real job. A `hybrid` job run without the
  key still completes: every Worker `auto` call falls back to Claude Sonnet
  with a `gemini -> claude-sonnet` fallback event.
- The clean-room proof is executed on VM2 by the sequence above, not by the
  offline test suite. `tests/test_acceptance.py` proves the runner's logic
  (stamp parsing, reboot detection, aggregation, artifacts, routing, job
  history) against synthetic inputs; `diagnose`, `systemd_health`,
  `monitor_fresh` and `provider_cli_login` only produce meaningful evidence
  on an installed host.
- `diagnose` needs root for a complete result; when `acceptance.sh` is run
  without `sudo` the check records the failure and its reason, it never
  escalates.
- The `kit_commit` recorded in the stamp is `unknown` when the kit was
  uploaded without `.git` (the entry point uploads a filtered tarball);
  record the source commit in the evidence directory name or its README.
