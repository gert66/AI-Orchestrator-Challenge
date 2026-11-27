# Installing on a bare Ubuntu host

This is the complete, documented procedure. If any step below needs a manual
technical repair that is not written here, that is a kit bug: fix the kit, not
the host.

Supported target: a clean Ubuntu 22.04/24.04/26.04 VM with root SSH access and
outbound HTTPS (apt, NodeSource, npm registry). The clean-room acceptance described below was executed on Ubuntu 26.04.1 LTS. Nothing from any other host is
copied: no credentials, tokens, login state, logs or project data.

### Before installation: keep emergency access safe

When creating the VM, save the root password at the moment the cloud provider
shows it (or immediately after a password reset) in a password manager and
verify that it was saved correctly before continuing. Never put that password
in this kit, screenshots, logs, chat, configuration files or source control.
The normal installation and day-to-day route is SSH-key authentication; the
root password is only emergency/fallback access. The one place it is ever
typed is ssh's own prompt during first contact (section 1.3); no kit script
takes it as a parameter, prompt, variable or file. If a root password has ever
been exposed in a screenshot or transcript, reset it before relying on it.

## 1. The entry point: `START-HERE.ps1` (Windows) / `installer/start-here.sh`

This is THE documented way to install. You need: a checkout of the kit on your
workstation, the address of a fresh Ubuntu host, and either key-based SSH to
it or (first contact only) its root password. Nothing else is installed
locally.

### 1.1 Windows, step by step (wizard)

1. Windows 10/11 or Server 2019+ already ship the OpenSSH client and
   `tar.exe`. If `ssh` is missing: Settings > Apps > Optional features > add
   *OpenSSH Client*.
2. Open PowerShell (5.1 or 7) and go to the kit directory:
   `cd C:\path\to\AI-Orchestrator-Starter`.
3. Start the wizard:

   ```powershell
   .\START-HERE.ps1
   # If Windows refuses to run scripts ("running scripts is disabled on this system"):
   powershell -ExecutionPolicy Bypass -File .\START-HERE.ps1
   ```

4. Answer the questions: server IP or host name; profile (`1` Claude, `2`
   OpenAI, `3` Hybrid — Claude for Brain/Reviewer, Worker `auto` prefers
   Gemini Flash through the Gemini HTTPS API and falls back to Claude Sonnet;
   for Hybrid the wizard also asks whether to open the on-host session for
   `GEMINI_API_KEY` at the end, section 4.1); how you reach the host (`1`
   key-based SSH already works, `2` a specific private key file, `3` first
   contact with only a root password); whether this is the first SSH
   connection to the host from this PC (accept its new host key). A summary
   is shown and nothing touches the host before you confirm with Enter.
5. If you chose `3`, `ssh` itself asks for the root password once (see 1.3);
   type it into that prompt. From then on everything is key-based.
6. Wait for the last line: `STARTER INSTALL OK ...` (section 1.5) and follow
   the printed follow-ups: provider login (section 4), for Hybrid the Gemini
   API key on the host (section 4.1; typed there, never into this script) and
   the reboot re-check (section 3).

With `-Server` and `-Profile` on the command line the script asks nothing
else — exactly the previous behaviour — and `-NonInteractive` never prompts:

```powershell
.\START-HERE.ps1 -Server <server-ip> -Profile <claude|openai|hybrid>
.\START-HERE.ps1 -Server <server-ip> -Profile claude -BootstrapWithPassword -AcceptNewHostKey
.\START-HERE.ps1 -Server <server-ip> -Profile hybrid -SetupGeminiSecret
.\START-HERE.ps1 -Server <server-ip> -Profile openai -SshKey C:\Users\<you>\.ssh\<key> -NonInteractive -AcceptNewHostKey
```

Parameters:

- `-Server <ip|host>`, `-Profile <claude|openai|hybrid>` — prompted when absent
  (an error with `-NonInteractive`).
- `-User <name>` — SSH user, default `root`; a non-root user needs passwordless
  sudo.
- `-SshKey <path>` — use this private key for every `ssh`/`scp` call
  (`-i <path> -o IdentitiesOnly=yes`). The file must exist, and it must not be
  inside the kit directory (that directory is uploaded). In bootstrap mode it
  is the key that is generated when missing and installed on the host.
- `-BootstrapWithPassword` — first contact with a host that only has a root
  password (section 1.3). Needs a human at the console; refused together with
  `-NonInteractive`.
- `-SetupGeminiSecret` — after `STARTER INSTALL OK` is certain, open one
  interactive `ssh -t` session that runs
  `sudo bash /opt/ai-orchestrator/installer/gemini-secret.sh set` on the host,
  where you type `GEMINI_API_KEY` into a hidden prompt (section 4.1). A bare
  switch: it carries no value, and the script has no way to receive the key.
  The wizard offers the same step for the `hybrid` profile. Needs a human at
  the console; refused together with `-NonInteractive`. A failed or skipped
  key step never changes the install result.
- `-AcceptNewHostKey` — pass `StrictHostKeyChecking=accept-new` so the first
  contact with a freshly created host does not fail on an unknown host key.
  Off by default: without it ssh asks interactively in the bootstrap session
  and fails closed (`BatchMode=yes`) everywhere else.
- `-NonInteractive` — never prompt; missing `-Server`/`-Profile` is an error.

### 1.2 Linux / macOS / WSL

```bash
installer/start-here.sh <server-ip> <claude|openai|hybrid> [user]

# Select a specific private key explicitly:
SSH_KEY=/path/to/private-key installer/start-here.sh <server-ip> <claude|openai|hybrid>
# First contact with a freshly created host:
ACCEPT_NEW_HOST_KEY=1 installer/start-here.sh <server-ip> <claude|openai|hybrid>
```

Same install semantics as the Windows script; it has no wizard, no bootstrap
mode and no guided Gemini step. If the host only has a root password, install
your public key first with `ssh-copy-id root@<server-ip>` (you type the
password into ssh's prompt), then run the script. For `hybrid` it prints the
same `installer/gemini-secret.sh` commands as the Windows script (section 4.1).

### 1.3 First contact with only a root password (`-BootstrapWithPassword`)

A freshly created VM often has nothing but a root password. The Windows entry
point turns that into key-based access without ever handling the password
itself:

1. It generates a dedicated local key when there is none yet:
   `~\.ssh\ai-orchestrator-starter_ed25519` (ed25519, no passphrase, comment
   `ai-orchestrator-starter`; or the `-SshKey` path you gave). Protect that
   file like a password; it is never uploaded.
2. It opens one interactive `ssh` session to `root@<server>` with
   `PubkeyAuthentication=no` (no `BatchMode`, no `-i`). **`ssh` itself asks for
   the root password and you type it into ssh's own prompt.** The script has no
   password parameter, no prompt of its own, reads no environment variable or
   file for it, and never pipes anything into `ssh` or puts a secret in a
   command argument — the only thing passed is the public key line
   `ssh-ed25519 <base64> ai-orchestrator-starter`, read from the `.pub` file
   and validated (type allow-list, base64 body) before use.
3. Over that session it runs, as `root`:
   `umask 077; mkdir -p ~/.ssh; chmod 700 ~/.ssh; touch ~/.ssh/authorized_keys;
   chmod 600 ~/.ssh/authorized_keys; grep -qxF '<key>' ~/.ssh/authorized_keys
   || printf '%s\n' '<key>' >> ~/.ssh/authorized_keys` — idempotent, safe
   permissions, nothing else is changed on the host (no `sshd_config`, no
   password change).
4. Every following `ssh`/`scp` call uses `-o BatchMode=yes
   -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no
   -i <key> -o IdentitiesOnly=yes`; a password prompt can never appear again.
   On a re-run the script first checks whether the key already works and skips
   the password step when it does.

If the bootstrap session fails (wrong password, `PermitRootLogin
prohibit-password` / `PasswordAuthentication no` on the host, host key not
accepted) the script stops with exit 4 and nothing has been changed on the
host; the local key is kept and reused on the next run. This mode installs a
key only; it never disables password login or touches provider state.

### 1.4 What the entry point does

The entry point is fail-closed and does everything in one go:

1. checks that `ssh`, `scp` and `tar` exist locally (Windows: the built-in
   OpenSSH client and `tar.exe`; `ssh-keygen` too in bootstrap mode), that
   `installer/install.sh` and `profiles/<profile>.json` exist in the kit
   directory, and that the profile is one of `claude`, `openai`, `hybrid`;
2. (bootstrap mode only) installs the public key as described in 1.3;
3. tests `ssh -o BatchMode=yes -o ConnectTimeout=15 <user>@<server> echo SSH_OK`
   and `sudo -n true`, aborting if either fails;
4. builds a filtered tarball of the kit — never `.git`, `.venv`, `__pycache__`,
   `.pytest_cache`, `*.pyc`, `monitor/status.json`, `profiles/active.json` or
   `jobs/` — uploads it with `scp`, then removes and re-extracts
   `/root/ai-orchestrator-starter` on the host, so re-runs are idempotent;
5. runs `sudo bash installer/install.sh <profile>` on the host with the output
   streamed to your console. The installer itself runs preflight, installs the
   systemd units and ends with diagnose (see section 2).

No credentials, tokens, login state or provider configuration are ever read
from or copied to the host by the entry point; the provider login (section 4)
and, for `hybrid`, the Gemini API key (section 4.1) stay manual, interactive
steps on the host. With `-SetupGeminiSecret` (or the wizard's yes) the Windows
script opens that on-host session for you after step 5 succeeded; it passes
only the command name, and the outcome of that session does not change the
result line.

### 1.5 The result line

The last line printed is exactly one of:

- `STARTER INSTALL OK profile=<profile> server=<server>` — exit code 0; the
  host passed install, preflight and diagnose. The script then prints the one
  remaining manual step (provider login, section 4) and the reboot re-check
  command (section 3).
- `STARTER INSTALL FAILED exit=<code>` — the process exits with `<code>`. A
  non-zero exit of `installer/install.sh` is passed through unchanged; local
  aborts use 2 (bad arguments, or cancelled at the wizard summary), 3 (missing
  tool, kit file or SSH key file), 4 (SSH, bootstrap or sudo) and 5 (upload).
  Read the last `ERROR` / `FAIL` line above it, fix the cause, re-run the same
  command.

### Manual alternative (same result, no entry point)

Clone or copy the kit to `/root/ai-orchestrator-starter` yourself and run the
installer directly:

```bash
ssh root@<server-ip>
git clone <kit-repository-url> /root/ai-orchestrator-starter
cd /root/ai-orchestrator-starter
sudo bash installer/install.sh <claude|openai|hybrid>
```

## 2. What the installer does

`installer/install.sh <claude|openai|hybrid>` is what the entry point runs on
the host. It is idempotent (re-running it on an installed host is safe) and
performs, in order:

1. apt packages: `git curl jq acl python3 python3-venv python3-pip ca-certificates rsync bubblewrap`. `bubblewrap` is required by the Codex Linux sandbox; installing it for every profile keeps profile switches idempotent.
2. Node.js >= 18 (`installer/install-node.sh`; NodeSource Node 20 LTS when apt's
   `nodejs` is absent or too old; early exit when a suitable node/npm exists).
3. The `orchestrator` service user (`installer/create-user.sh`).
4. Copies the kit to `/opt/ai-orchestrator` and writes `profiles/active.json`
   from the chosen profile.
5. Creates `/opt/ai-orchestrator/.venv` and installs `requirements-dev.txt`
   (pytest) so preflight can run the whole suite.
6. `chown -R orchestrator:orchestrator /opt/ai-orchestrator`.
7. Provider CLIs for the profile into `/home/orchestrator/.npm-global/bin`
   (`claude`, `codex`, or both for `hybrid`; `installer/install-providers.sh`
   fails closed if a required binary is missing) and
   `/etc/profile.d/ai-orchestrator.sh` so login shells find them.
8. `installer/preflight.sh` as `orchestrator` with the venv on `PATH`; any
   `FAIL` aborts the install. Preflight also normalises the executable bit on
   `installer/*.sh`, so a kit checked out or uploaded from Windows still works.
9. systemd units (`installer/install-systemd.sh`): `orchestrator@.service`,
   `orchestrator-health.service/.timer` (status snapshot every minute) and
   `orchestrator-watchdog.service/.timer` (recovery every 5 minutes).
10. `installer/diagnose.sh <profile>` as the final gate.
11. Writes the install stamp `/var/lib/ai-orchestrator/install-stamp.json`
    (mode 0644: `installed_at`, `installed_epoch`, `profile`, `kit_commit`,
    `boot_id`), overwritten on every successful re-install. The acceptance
    runner (`installer/acceptance.sh`, see [ACCEPTANCE.md](ACCEPTANCE.md))
    reads it to prove what was installed and whether the host rebooted since.

### What the two result lines mean

- `DIAGNOSE PASS profile=<profile>` — every host check passed: tools present
  (python3, git, curl, node >= 18, npm), `/opt/ai-orchestrator` owned by
  `orchestrator`, the venv imports `core.*`, `active.json` matches the
  requested profile, the profile's provider CLI(s) exist and answer
  `--version`, all systemd units are installed, both timers are enabled, the
  health timer is active and `monitor/status.json` is fresh (< 180 s).
  For profiles with `extras.gemini` (`hybrid`) it also looks at
  `/home/orchestrator/.config/ai-orchestrator/secrets.env`: absent is an
  `INFO` line (the manual step of section 4.1 is still open; the install
  passes), present and private (owner `orchestrator`, dir 700, file 600,
  non-empty `GEMINI_API_KEY`) is `OK`, present but not private is a `FAIL`.
  The value is never read into the output.
  Diagnose makes no network calls and needs no credentials.
- `INSTALL OK profile=<profile>` — printed only after preflight, systemd
  installation and diagnose all passed. Anything else (an `INSTALL FAIL ...`
  line, a `FAIL ...` line, or a non-zero exit) means the install did not
  complete; read the last `FAIL` line, fix the kit, re-run the same command.

## 3. Reboot check

The health and watchdog timers are `Persistent=true` and wanted by
`timers.target`, so they come back on their own. Prove it:

```bash
sudo reboot
# wait for the host to come back, then (the entry point prints this command too):
sudo bash /opt/ai-orchestrator/installer/diagnose.sh <profile>
```

`diagnose.sh` reads the profile from `/opt/ai-orchestrator/profiles/active.json`
when no argument is given. It must print `DIAGNOSE PASS` again. If the health
timer or a job instance is down, the watchdog (`installer/watchdog.sh`) restarts
it within 5 minutes and logs what it did under `journalctl -t orchestrator-watchdog`.

To record the result as evidence (diagnose, timers, monitor freshness, the
reboot itself via the install stamp's `boot_id`, profile routing, pytest) run
the acceptance runner; it must print `ACCEPTANCE PASS phase=post-reboot ...`:

```bash
sudo bash /opt/ai-orchestrator/installer/acceptance.sh <profile> --phase post-reboot
```

The full clean-room sequence (post-install run, reboot, post-reboot run, real
job, second clean install) and where the artifacts go are in
[docs/ACCEPTANCE.md](ACCEPTANCE.md).

## 4. The manual steps: provider login (and, for hybrid, the Gemini key)

The installer never handles credentials. Interactive login to the provider is
the one manual step for every profile, done once, as the `orchestrator` user,
after `STARTER INSTALL OK` / `INSTALL OK`; `hybrid` has a second one, the
Gemini API key (section 4.1):

```bash
sudo -iu orchestrator
claude          # Claude profile / hybrid: follow the interactive login
codex login     # OpenAI profile / hybrid
```

Login state is verified by the first real job, not by `diagnose.sh`
(it prints `INFO provider login is verified by the first real job, not here`).

### 4.1 Hybrid: the Gemini API key (`GEMINI_API_KEY`)

The `hybrid` profile (`extras.gemini: true`, Worker `auto`) routes LOW/NORMAL
Worker calls to Gemini Flash first. Gemini is reached **directly over HTTPS
by `core/gemini_runner.py`**; there is no Gemini CLI, nothing extra is
installed, and no login exists. The only thing it needs is `GEMINI_API_KEY`,
which the runner reads from its environment first and otherwise from

```
/home/orchestrator/.config/ai-orchestrator/secrets.env     # KEY=VALUE lines
```

(`ORCHESTRATOR_SECRETS_FILE` overrides the path). The `orchestrator@.service`
unit sets `HOME=/home/orchestrator`, so that file is exactly what jobs see.

**Without the key nothing breaks:** the install, `diagnose.sh` and the
acceptance runner pass; Brain and Reviewer are unaffected; every Worker
`auto` call tries Gemini, gets `GEMINI_API_KEY is not configured` and falls
back to Claude Sonnet, recorded as a `fallback_events` entry
(`gemini -> claude-sonnet`) and `provider_route =
auto_<complexity>_gemini_to_claude_sonnet` in the call telemetry. Placing the
key is therefore a documented manual step, not an install gate.

**Placing it — the key is typed on the host, never on your PC:** the entry
points never take the key as a parameter, prompt, environment variable or
file, and never log it. Open a terminal session on the host and run the
helper as root (or as `orchestrator`); it reads the key with a hidden prompt
(`read -rs`), refuses to run without a terminal or with any argument, and
writes the file through shell builtins (the value never becomes a process
argument or appears in any output):

```bash
ssh -t root@<server-ip> sudo bash /opt/ai-orchestrator/installer/gemini-secret.sh set
# (with an explicit key: ssh -i <key> -t root@<server-ip> sudo bash /opt/ai-orchestrator/installer/gemini-secret.sh set)
```

`START-HERE.ps1 -SetupGeminiSecret` (or answering yes in the wizard for
`hybrid`) opens exactly this session for you once the install has succeeded.
The result is:

```
/home/orchestrator/.config/ai-orchestrator/     owner orchestrator, mode 700
/home/orchestrator/.config/ai-orchestrator/secrets.env   owner orchestrator, mode 600
GEMINI_API_KEY=<value>                          other KEY=VALUE lines are kept
```

Doing it by hand instead (same result) as `orchestrator`:

```bash
sudo -iu orchestrator
umask 077
mkdir -p ~/.config/ai-orchestrator && chmod 700 ~/.config/ai-orchestrator
touch ~/.config/ai-orchestrator/secrets.env && chmod 600 ~/.config/ai-orchestrator/secrets.env
"${EDITOR:-nano}" ~/.config/ai-orchestrator/secrets.env      # add the line GEMINI_API_KEY=<value>
```

Never put the key on a command line (`GEMINI_API_KEY=... command`, `echo
... > file`), in the kit, in a screenshot, in a chat or in `journalctl`; the
shell history and the process list would keep it.

**Verifying it — without revealing it:** the offline check confirms owner,
modes and that a non-empty `GEMINI_API_KEY` line exists; `--live` adds one
minimal request through the runner's own HTTPS path
(`python3 -m core.gemini_runner --smoke`, run as `orchestrator` with the
verified file) and prints one `GEMINI SMOKE OK|FAIL` line with the HTTP
outcome — never the key:

```bash
sudo bash /opt/ai-orchestrator/installer/gemini-secret.sh check          # offline: ends with GEMINI SECRET PASS|FAIL
sudo bash /opt/ai-orchestrator/installer/gemini-secret.sh check --live   # + GEMINI SMOKE OK model=... http=200
```

`GEMINI SMOKE FAIL verdict=rejected http=401|403|400` means the key was
refused (re-run `set`); `verdict=unavailable http=429|503` means the key was
accepted but Gemini is throttled or down (the Worker route falls back to
Claude Sonnet in that case too); `verdict=not_configured` means the file is
not where the runner looks. `diagnose.sh` reports the file's presence and
privacy (section 2) and the acceptance runner records it as `gemini_secret`
(`SKIP` for `claude`/`openai`, `MANUAL` while absent, `PASS` when present
and private, `FAIL` only when a present file is not private). To rotate the
key, run `set` again; to remove Gemini, delete the file (the route falls
back to Claude Sonnet) or switch to the `claude` profile.

## 5. Running a job

A job is one directory under `/opt/ai-orchestrator/jobs/<job-id>/` that the
state machine (`core/machine.py`) drives through
`QUEUED -> PLANNING -> WORKING -> EVIDENCE -> REVIEWING -> COMMITTING -> ... -> DONE`.
Everything below is done on the host as the `orchestrator` user
(`sudo -iu orchestrator`) from `/opt/ai-orchestrator` with the kit's venv:

```bash
sudo -iu orchestrator
cd /opt/ai-orchestrator
. .venv/bin/activate
```

### 5.1 Register the repository in `config/repo_policy.json`

Write-mode jobs are fail-closed: the Brain, Worker and Reviewer cannot
override the branch-freshness gate, and that gate needs an explicit policy
per repository key. Merge an entry like this into the `repos` object of
`/opt/ai-orchestrator/config/repo_policy.json` (`expected_origin` must be the
exact `origin` URL of the checkout, `.git` suffix optional;
`canonical_base_branch` is the branch job branches are measured against):

```json
{
  "schema_version": 1,
  "repos": {
    "owner/name": {
      "expected_origin": "https://github.com/owner/name.git",
      "canonical_base_branch": "main",
      "push_job_branch": false
    }
  }
}
```

`push_job_branch` is optional (default `false`); set it to `true` to have
the orchestrator push the job branch to `origin` after each committed batch
(details and the credential prerequisite in section 5.7).

Read-mode jobs (`--mode read`) do not consult the policy. If a write job is
submitted without an entry, `core.submit_job` refuses with one `ERROR` line
followed by the exact JSON to add.

### 5.2 Clone the target repository

Checkouts live under `/home/orchestrator/repos/<name>` and must be on the
job branch before the job starts (`main`/`master` are refused):

```bash
mkdir -p /home/orchestrator/repos
git clone https://github.com/owner/name.git /home/orchestrator/repos/name
cd /home/orchestrator/repos/name
git checkout -b job/<job-id>
cd /opt/ai-orchestrator
```

The tree must be clean, `origin` must match the policy and the branch must
not be behind `origin/<canonical_base_branch>`; the gate re-checks all of that
at job start and before the first Worker write.

### 5.3 Submit the job

```bash
python3 -m core.submit_job \
  --job-id <job-id> \
  --repo owner/name \
  --repo-path /home/orchestrator/repos/name \
  --branch job/<job-id> \
  --mode write \
  --goal "What the whole job must achieve" \
  --input-file docs/SPEC.md \
  --test-command '["python3", "-m", "pytest", "-q"]' \
  --max-steps 10 --max-repairs-per-step 2
```

Optional: `--worker-provider claude|codex|gemini|auto` (default: the Worker
route of the active profile), `--execution-mode
production|standard|autonomous_sandbox` (default `standard`; see 5.8),
`--input-file` and `--test-command` are repeatable, `--jobs-dir` /
`--policy-file` override the defaults (`jobs/`, `config/repo_policy.json`). The command only creates
`jobs/<job-id>/{job.json,state.json,events.jsonl}` with the job `QUEUED`; it
calls no provider and never touches a git remote. It prints exactly one line:

```
JOB SUBMITTED job_id=<job-id> dir=/opt/ai-orchestrator/jobs/<job-id>
```

and exits 0, or prints exactly one `ERROR ...` line on stderr and exits 2
(duplicate job id, `--repo-path` is not the top level of a git checkout,
protected branch, missing/mismatching repo policy for a write job, invalid
job or test command).

### 5.4 Start it and watch

```bash
sudo systemctl start orchestrator@<job-id>.service
journalctl -u orchestrator@<job-id>.service -f
```

The unit runs `python3 -m core.supervisor --job-id <job-id>` as
`orchestrator` and stops when the job is `DONE`, `ERROR` or `NEEDS_HUMAN`.
The watchdog restarts a job instance that died in a runnable phase.

### 5.5 Reading the job directory

- `jobs/<job-id>/state.json` — canonical state: `phase`, `step_id`,
  `human_question` (set while `NEEDS_HUMAN`), `last_error`, `wait_until`, and
  `runtime` with `counters`, `history` (one entry per closed batch with its
  `outcome`, `verdict`, `commit` and `paths`), `branch_freshness` and
  `totals`.
- `jobs/<job-id>/events.jsonl` — every phase transition with its reason.
- `jobs/<job-id>/evidence/<step>-evidence-<n>.json` — what the orchestrator
  itself observed after each Worker run: branch, HEAD, `git status`, the
  real diff and every `--test-command` result (`status`, `exit_code`,
  `stdout`, `stderr`, `tests_summary`). This, not the Worker's report, is
  what the Reviewer and the commit gate trust.
- `jobs/<job-id>/steps/*.json` — every validated Brain/Worker/Reviewer
  output; `logs/` and `calls.jsonl` — classification and telemetry per call.

### 5.6 Answering a `NEEDS_HUMAN`

When `state.json` shows `"phase": "NEEDS_HUMAN"`, read `human_question`, fix
the cause if it is technical, then record the answer with
`core.machine.Machine.approve()` (the supervisor has exited, so the job lock
is free) and start the unit again:

```bash
python3 -c "from core.machine import Machine; print(Machine('jobs/<job-id>', runner=None).approve('approved: <your answer>'))"
sudo systemctl start orchestrator@<job-id>.service
```

`approve()` lifts the cap that was hit (if any) and returns the job to the
phase it paused in. The same class offers `abort(reason)`, `set_config(...)`
(test commands, timeouts, caps, `execution_mode`), `extend_scope(paths)`,
`refresh_evidence()` and `complete()` for the other human decisions.
`runtime.human.gate` in `state.json` says whether the pause is a `hard`
safety gate or a `soft` one (and `hard_topic` names the topic when a
Brain/Reviewer question was classified as hard).

### 5.7 What the kit does with the result

Each reviewed batch is committed **on the job branch of the local checkout**
(`git add` of the in-scope paths + `git commit`; commit messages carry the
step id and the job id). By default nothing is pushed: after `DONE` the
result is in `/home/orchestrator/repos/<name>` on branch `job/<job-id>` and
`origin` is untouched.

Pushing is **opt-in per repository** with `"push_job_branch": true` in the
repo's entry in `config/repo_policy.json` (see
`templates/repo_policy.example.json`). With it set, the orchestrator itself
runs `git push -u origin job/<job-id>:job/<job-id>` right after each
successful batch commit of a write-mode job:

- only the job branch `job/<job-id>` is ever pushed; `main`/`master` are
  refused unconditionally, and so is any branch that is not the one checked
  out;
- the push is plain: never `--force`, `--force-with-lease`, `--delete` or
  `--mirror`, so a diverged remote branch makes the push fail instead of
  overwriting it;
- the Brain, Worker and Reviewer still cannot push (`Bash(git push:*)` stays
  in their disallowed tools); only the machine does;
- a successful push records `"pushed": true` and `"pushed_to":
  "origin/job/<job-id>"` on the batch's `history` entry in `state.json`;
- a failed push **keeps the local commit**, records `"pushed": false` and
  `"push_error"` on the history entry, and pauses the job in `NEEDS_HUMAN`
  with a question that names the push failure. Fix the remote/credentials,
  then `approve()` (the job continues at `PLANNING`; the Brain sees the
  batch as committed) or `abort()`.

Prerequisite: the `orchestrator` user needs **non-interactive push
credentials** for `origin` (an SSH deploy key with write access, or a
credential helper such as `git credential-store`/`gh auth setup-git` set up
for that user). This is a documented setup step you perform on the host; the
kit never copies or ships credentials, and `preflight.sh` does not test the
push path. `tests/test_machine_e2e.py` proves the commit flow, the
push-enabled flow and the push-failure pause offline against a throwaway
bare repository.

### 5.8 Execution modes

`--execution-mode` (persisted as `runtime.config.execution_mode` in
`state.json`, shown by `Machine.status()` and in the first supervisor log
line) sets how much a job decides on its own before pausing in
`NEEDS_HUMAN`:

| mode | AI-role policy | machine gates |
|---|---|---|
| `production` | conservative: ask for any material ambiguity or anything touching live users, data, money, security, integrations | every gate pauses |
| `standard` (default) | routine reversible choices are made autonomously; unresolved product/business decisions are asked | every gate pauses |
| `autonomous_sandbox` | reversible product/technical ambiguity is decided by the Brain and documented in its `reason` | **soft** gates are auto-resolved; **hard** gates pause |

Soft gates (auto-resolved only under `autonomous_sandbox`): the caps
`max_batches`, `max_repairs_per_batch`, `max_reviews_per_batch`,
`max_claude_calls` and `max_attempts` (lifted exactly as `approve()` would,
then the Brain is re-asked), an ordinary Brain/Reviewer `NEEDS_HUMAN`
question (deferred back to the Brain once), and a repair that produced an
identical diff (handed back to the Brain once). Every automatic decision is
appended to `runtime.auto_resolved` in `state.json` and recorded as an
event, and `runtime.human.answer` starts with `auto-` so the Brain can tell
it from a person's answer. The same decision is never replayed: a question
repeated verbatim, a second identical diff, or `max_attempts` hit again by
the same call pauses for a human.

Hard gates pause in **every** mode: the branch freshness gate, git preflight
and HEAD checks, the staging/commit/push gates and a read-mode job that
changed files, an evidence harness that could not run the tests, and
AUTH/BUDGET/CONFIG/TASK_ERROR provider failures. A Brain/Reviewer question is
also treated as hard when its wording touches credentials/auth,
budget/spending/contract consent, production/live users/real customer data,
destructive or irreversible actions, or branch/repository safety
(`core.machine.HARD_GATE_TOPICS`; deliberately broad and fail-closed).
`production` and `standard` differ only in the policy text handed to the
Brain and Worker (`core.machine.EXECUTION_POLICY`); the machine gates are
identical. `tests/test_execution_modes.py` proves all of this offline.

See `README.md` for the role-routing profiles and `docs/ACCEPTANCE.md` for the
clean-room acceptance gate.
