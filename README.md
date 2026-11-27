# AI Orchestrator Challenge

Standalone educational edition for the AI Agent Overnight Challenge. Use your own disposable Ubuntu VM, provider account and repository. Never use production credentials or infrastructure. Historical operational logs and private repository history are not included.

[Challenge overview and five project briefs](https://github.com/gert66/ai-overnight-challenge). Fork your chosen challenge into your own account before running write-mode jobs. The repository policy is empty by default.

## Layout

- `core/` — deterministic control plane (contracts, state machine, supervisor, provider runners).
- `schemas/` — JSON Schema documents for the Brain/Worker/Reviewer/Job/State contracts (mirrors `core/contracts.py`).
- `prompts/` — Brain/Worker/Reviewer prompt templates rendered by `core/machine.py` (`string.Template`, split into a stable system prefix and a dynamic part).
- `config/repo_policy.json` — repository safety policy read by write-mode jobs (empty by default; see `templates/repo_policy.example.json`).
- `profiles/` — provider role routing (`claude`, `openai`, `hybrid`).
- `installer/` — Ubuntu install, preflight, diagnostics, acceptance and watchdog scripts.
- `docs/evidence/` — acceptance artifacts copied from VM2 runs (see `docs/ACCEPTANCE.md`).
- `systemd/` — job, health-timer and watchdog units installed by `installer/install-systemd.sh`.
- `tests/` — offline pytest suite (stdlib + pytest only; no network, no provider CLI).

## Quick start: install on a bare Ubuntu host

The documented entry point is `START-HERE.ps1` (Windows PowerShell 5.1 / PowerShell 7, needs the Windows OpenSSH client and `tar.exe`) or its twin `installer/start-here.sh` (Linux / macOS / WSL). Run it from a checkout of the kit.

**Windows, first time:** open PowerShell in the kit directory and run the wizard; it asks for the server, the profile, how you reach the host and a final confirmation:

```powershell
.\START-HERE.ps1
# if scripts are blocked on this PC:  powershell -ExecutionPolicy Bypass -File .\START-HERE.ps1
```

If the fresh host only has a root password, choose "first contact" (or pass `-BootstrapWithPassword`): the script generates a dedicated local ed25519 key, `ssh` itself asks you for the root password once — the script never takes, reads, stores or logs it — and only the public key is installed into `~/.ssh/authorized_keys` on the host; everything afterwards is key-based (`BatchMode=yes`, `IdentitiesOnly=yes`). Explicit, prompt-free forms:

```powershell
.\START-HERE.ps1 -Server <server-ip> -Profile <claude|openai|hybrid>
.\START-HERE.ps1 -Server <server-ip> -Profile claude -BootstrapWithPassword -AcceptNewHostKey
.\START-HERE.ps1 -Server <server-ip> -Profile hybrid -SetupGeminiSecret
.\START-HERE.ps1 -Server <server-ip> -Profile openai -SshKey C:\Users\<you>\.ssh\<key> -NonInteractive
```

**Hybrid and Gemini:** `hybrid` runs Brain/Reviewer on Claude and routes the Worker `auto` to Gemini Flash first, reached directly over HTTPS by `core/gemini_runner.py` with `GEMINI_API_KEY` — there is no Gemini CLI and nothing extra is installed. The key lives on the host in `/home/orchestrator/.config/ai-orchestrator/secrets.env` (owner `orchestrator`, dir 700, file 600) and is typed there: `ssh -t root@<server-ip> sudo bash /opt/ai-orchestrator/installer/gemini-secret.sh set` (hidden prompt, no argument, no pipe), verified with `... gemini-secret.sh check --live` (never prints the value). `-SetupGeminiSecret`, or the wizard's question for `hybrid`, opens that on-host session after a successful install; the entry point itself never takes, stores or logs the key. Until the key is placed, install/diagnose/acceptance still pass and Worker `auto` falls back to Claude Sonnet (recorded as a fallback event). Details: [docs/INSTALL.md](docs/INSTALL.md) section 4.1.

```bash
installer/start-here.sh <server-ip> <claude|openai|hybrid>

# Optional: select a specific private key
SSH_KEY=/path/to/private-key installer/start-here.sh <server-ip> <claude|openai|hybrid>
```

Either one checks key-based SSH, uploads the kit (without `.git`, `.venv`, `__pycache__` or local state) to `/root/ai-orchestrator-starter`, runs `sudo bash installer/install.sh <profile>` on the host (preflight, systemd units, diagnose) and ends with exactly one of

```
STARTER INSTALL OK profile=<profile> server=<server>
STARTER INSTALL FAILED exit=<code>
```

with a matching process exit code. After `STARTER INSTALL OK` the manual steps are the interactive provider login as `orchestrator` (`claude` / `codex login`) and, for `hybrid`, the Gemini API key on the host (above); the reboot check is `sudo bash /opt/ai-orchestrator/installer/diagnose.sh <profile>` (must print `DIAGNOSE PASS`). The step-by-step Windows walkthrough, all parameters, the bootstrap details, the manual alternative (`sudo bash installer/install.sh <profile>` on the host) and what `INSTALL OK` / `DIAGNOSE PASS` mean are in [docs/INSTALL.md](docs/INSTALL.md). A successful install also writes `/var/lib/ai-orchestrator/install-stamp.json` (profile, kit commit, boot id).

## Acceptance evidence

`sudo bash /opt/ai-orchestrator/installer/acceptance.sh <profile> [--phase post-reboot] [--job-id <id>]` (`core/acceptance.py`) runs every definition-of-done check on the host — install stamp, reboot detection from the boot id, diagnose, systemd timers, monitor freshness, preflight, pytest, profile routing, provider CLIs and the real job's history — and writes `acceptance-<stamp>.json` + `.md` under `/opt/ai-orchestrator/evidence/acceptance/`. It exits 0 with one `ACCEPTANCE PASS ...` line only when no check is `FAIL`. The checklist, the exact VM2 sequence (install -> acceptance -> reboot -> acceptance -> real job -> acceptance -> second clean install), where the artifacts are kept (`docs/evidence/`) and the known limitations are in [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md).

## Running a job

On an installed host, as `orchestrator` in `/opt/ai-orchestrator`: register the repository in `config/repo_policy.json`, clone it under `/home/orchestrator/repos/<name>` on a job branch, then

```bash
python3 -m core.submit_job --job-id <id> --repo owner/name --repo-path /home/orchestrator/repos/name \
  --branch job/<id> --mode write --goal "..." --test-command '["python3", "-m", "pytest", "-q"]'
sudo systemctl start orchestrator@<id>.service && journalctl -u orchestrator@<id>.service -f
```

`core.submit_job` is fail-closed (one `ERROR` line, exit 2) and never touches a remote; reviewed batches are committed on the job branch and, only when the repo's policy sets `"push_job_branch": true`, pushed by the orchestrator to `origin` as `job/<job-id>` (never `main`/`master`, never forced; a failed push keeps the commit and pauses in `NEEDS_HUMAN`; the `orchestrator` user needs non-interactive push credentials). Reading `jobs/<id>/state.json` / `evidence/`, answering `NEEDS_HUMAN` with `Machine.approve()` and the full procedure are in [docs/INSTALL.md](docs/INSTALL.md#5-running-a-job). `--execution-mode production|standard|autonomous_sandbox` (default `standard`) sets how much a job decides on its own: `autonomous_sandbox` lifts repair/review/call caps and defers ordinary product/technical questions back to the Brain, while hard safety gates (credentials, spending, production/customer data, destructive actions, branch freshness and git/commit safety) pause in every mode — see [docs/INSTALL.md §5.8](docs/INSTALL.md#58-execution-modes). `tests/test_machine_e2e.py` drives the whole Brain -> Worker -> Evidence -> Reviewer -> Commit -> Push flow offline; `tests/test_execution_modes.py` proves the soft-vs-hard gate behaviour per mode.

## Preflight and tests (local)

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
bash installer/preflight.sh      # fail-closed static gate; runs pytest when it is installed
python3 -m pytest -q             # the suite on its own
```

`installer/preflight.sh` exits nonzero on any `FAIL` line and prints `PREFLIGHT PASS` on success. If pytest is not installed it prints a `WARN` line and only the static checks gate the result.

## Provider profiles

`profiles/<name>.json` maps each role (`brain`, `worker`, `reviewer`) to a provider: `anthropic`, `openai`, `google` or `auto`. The installer copies the chosen profile to `profiles/active.json`; `core.provider_registry.load_profile()` resolves the profile in force in this order:

1. an explicit path (`python3 -m core.supervisor --profile-file ...`),
2. the `ORCHESTRATOR_PROFILE_FILE` environment variable,
3. `profiles/active.json`,
4. `profiles/claude.json`, with one logged warning (only when nothing above exists; a missing or invalid explicit/env file is an error).

Per-role routing (`core/providers.py`):

| Profile role value | Brain / Reviewer | Worker default |
|---|---|---|
| `anthropic` | Claude CLI | `claude` |
| `openai` | Codex CLI, `--sandbox read-only` | `codex` (`--sandbox workspace-write`) |
| `google` | configuration failure (no structured Gemini Brain/Reviewer adapter) | `gemini` |
| `auto` | configuration failure | `auto` (budget-aware Claude/Gemini) |

A job's explicit `config.worker_provider` still overrides the profile's Worker default. Nothing falls back to Claude silently: the `openai` profile needs only the `codex` CLI installed and never invokes Claude; `hybrid` routes Brain/Reviewer to Claude and the Worker to `auto`. Call logs record the real provider (`claude`, `codex`, `gemini`) per call.

## Optional integrations

The kit runs without any of these; each degrades to a logged warning when absent.

- Slack notifications: `bin/notify_slack.sh` (`core/notify.py`).
- Hermes read-only advisor: `bin/export_for_hermes.sh` and `/usr/local/sbin/hermes-orchestrator-advice` (`core/hermes_advisor.py`).
- Gemini Worker (`core/gemini_runner.py`, direct HTTPS, no Gemini CLI): `GEMINI_API_KEY` from the environment, or as a fallback a `KEY=VALUE` file at `~/.config/ai-orchestrator/secrets.env` (override with `ORCHESTRATOR_SECRETS_FILE`). Place it on the host with `installer/gemini-secret.sh set` (typed into a hidden prompt), verify with `installer/gemini-secret.sh check [--live]` / `python3 -m core.gemini_runner --smoke` (never prints the value). Never commit that file. Without it the `auto` Worker route falls back to Claude Sonnet.
