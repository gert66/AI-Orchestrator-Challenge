"""Static checks on the documented entry points: START-HERE.ps1 and installer/start-here.sh.

No network, no SSH and no PowerShell execution required. Both scripts must
implement the same fail-closed flow (upload the filtered kit, run
installer/install.sh, print one final result line) and must not carry any
host-specific or private data. When a Linux `pwsh` happens to be installed the
PowerShell script is additionally parsed (parse-only, nothing is executed).
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PS1 = ROOT / "START-HERE.ps1"
SH = ROOT / "installer" / "start-here.sh"
ENTRY_POINTS = [PS1, SH]
PROFILES = ("claude", "openai", "hybrid")
EXCLUDE_TOKENS = (".git", ".venv", "__pycache__", "status.json")
RESULT_OK = "STARTER INSTALL OK"
RESULT_FAILED = "STARTER INSTALL FAILED"
OLD_STUB = "Starter upload/install wiring is the next build gate"
# Built by concatenation so this test file itself never carries the literal.
PRIVATE_HOME = "/home/" + "myngle"

IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
TOKEN_LIKE = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}"          # OpenAI / Anthropic style API keys
    r"|sk-ant-[A-Za-z0-9_-]{8,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"      # GitHub tokens
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"    # Slack tokens
    r"|AKIA[0-9A-Z]{16}"                # AWS access keys
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
OPAQUE_RUN = re.compile(r"[A-Za-z0-9+/=_-]{40,}")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _token_like(text: str):
    """Return the first token-like string in text, or None.

    Besides well-known key prefixes, any long opaque run that mixes letters and
    digits (a base64/hex secret) counts; comment rulers ('-----') do not.
    """
    match = TOKEN_LIKE.search(text)
    if match:
        return match.group(0)
    for run in OPAQUE_RUN.findall(text):
        if re.search(r"[A-Za-z]", run) and re.search(r"\d", run):
            return run
    return None


@pytest.mark.parametrize("path", ENTRY_POINTS, ids=lambda p: p.name)
def test_entry_point_exists(path):
    assert path.is_file(), f"{path.relative_to(ROOT)} is missing"


@pytest.mark.parametrize("path", ENTRY_POINTS, ids=lambda p: p.name)
def test_entry_point_runs_installer_with_a_known_profile(path):
    text = _read(path)
    assert "installer/install.sh" in text
    for profile in PROFILES:
        assert profile in text, f"{path.name} must know profile {profile!r}"
    assert "/root/ai-orchestrator-starter" in text
    assert "sudo bash" in text
    assert "diagnose.sh" in text, "post-install reboot re-check must be printed"


@pytest.mark.parametrize("path", ENTRY_POINTS, ids=lambda p: p.name)
def test_entry_point_excludes_local_state_from_upload(path):
    text = _read(path)
    for token in EXCLUDE_TOKENS:
        assert token in text, f"{path.name} must exclude {token!r} from the upload"


@pytest.mark.parametrize("path", ENTRY_POINTS, ids=lambda p: p.name)
def test_entry_point_prints_exactly_the_two_result_lines(path):
    text = _read(path)
    assert RESULT_OK in text
    assert RESULT_FAILED in text
    assert OLD_STUB not in text, "the placeholder stub sentence must be gone"


@pytest.mark.parametrize("path", ENTRY_POINTS, ids=lambda p: p.name)
def test_entry_point_uses_batchmode_ssh_with_timeout(path):
    text = _read(path)
    assert "BatchMode=yes" in text
    assert "ConnectTimeout=15" in text
    assert "echo SSH_OK" in text


def test_sh_supports_explicit_ssh_key():
    text = _read(SH)
    assert 'SSH_KEY' in text
    assert '-i "$SSH_KEY"' in text
    assert 'IdentitiesOnly=yes' in text


@pytest.mark.parametrize("path", ENTRY_POINTS, ids=lambda p: p.name)
def test_entry_point_carries_no_private_data(path):
    text = _read(path)
    assert not IPV4.search(text), f"{path.name} contains an IP address literal"
    assert PRIVATE_HOME not in text, f"{path.name} references a private home path"
    found = _token_like(text)
    assert found is None, f"{path.name} contains a token-like string: {found[:12]}..."


NATIVE_CALL = re.compile(r"^\s*Invoke-Native(?:Interactive)? '(\w[\w-]*)'", re.M)


def test_ps1_is_fail_closed():
    text = _read(PS1)
    assert re.search(r"\$ErrorActionPreference\s*=\s*['\"]Stop['\"]", text), "ps1 must set ErrorActionPreference Stop"
    calls = list(NATIVE_CALL.finditer(text))
    assert {m.group(1) for m in calls} >= {"ssh", "scp", "tar", "ssh-keygen"}, "ps1 must run ssh/scp/tar/ssh-keygen via Invoke-Native*"
    # Every native call is followed by a $LASTEXITCODE decision before the next one.
    for current, following in zip(calls, calls[1:] + [None]):
        end = following.start() if following else len(text)
        segment = text[current.end():end]
        assert "$LASTEXITCODE" in segment, f"ps1 must check $LASTEXITCODE right after: {current.group(0).strip()}"
    for tool in ("'ssh'", "'scp'", "'tar'"):
        assert tool in text, f"ps1 must check for {tool}"
    for param in ("$Server", "$Profile", "$User", "$NonInteractive", "$SshKey", "$BootstrapWithPassword", "$AcceptNewHostKey",
                  "$SetupGeminiSecret"):
        assert param in text, f"ps1 must declare parameter {param}"
    assert "Read-Host" in text, "ps1 must still prompt when parameters are absent"
    assert "$PSScriptRoot" in text, "ps1 must resolve the kit directory from its own location"


def test_ps1_wizard_keeps_noninteractive_semantics():
    text = _read(PS1)
    assert "-Server is required with -NonInteractive" in text
    assert "-Profile is required with -NonInteractive" in text
    # the wizard only runs when a required parameter is absent and prompts are allowed
    assert re.search(r"\$Wizard\s*=\s*\(-not \$NonInteractive\) -and \(\(-not \$Server\) -or \(-not \$Profile\)\)", text)
    # bootstrap and the guided Gemini step need a human at a prompt, so both are refused non-interactively
    assert re.search(r"if \(\$BootstrapWithPassword -and \$NonInteractive\)\s*\{\s*Abort", text)
    assert re.search(r"if \(\$SetupGeminiSecret -and \$NonInteractive\)\s*\{\s*Abort", text)
    # every prompt is guarded by the wizard flag or a -NonInteractive abort nearby
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if "Read-Host" not in line:
            continue
        window = "\n".join(lines[max(0, index - 14):index + 1])
        assert "$Wizard" in window or "$NonInteractive" in window, f"unguarded prompt: {line.strip()}"
    # the wizard ends with an explicit confirmation before anything touches the host
    assert "Read-Host 'Proceed? (Y/n)'" in text
    confirm = text.find("Read-Host 'Proceed? (Y/n)'")
    first_ssh = text.find("Invoke-Native 'ssh'")
    assert 0 < confirm < first_ssh, "the confirmation must come before the first ssh call"


def test_ps1_never_touches_a_password():
    """The root password is typed by the human into ssh's own prompt and nowhere else."""
    text = _read(PS1)
    for token in ("SecureString", "PSCredential", "sshpass", "plink", "SSH_ASKPASS", "StandardInput",
                  "Start-Process", "-pw ", "expect ", "putty", "DISPLAY="):
        assert token not in text, f"ps1 must not use {token!r}"
    param_block = re.search(r"^param\((.*?)^\)", text, re.S | re.M).group(1)
    params = {name: kind for kind, name in re.findall(r"\[(\w+)\]\$(\w+)", param_block)}
    assert set(params) >= {"BootstrapWithPassword", "SetupGeminiSecret", "SshKey", "AcceptNewHostKey"}
    for name, kind in params.items():
        if re.search("pass|secret|cred|key|token", name, re.I):
            # a secret-sounding name is allowed only as a bare switch (carries no value) or as the key *path*
            assert kind == "switch" or name == "SshKey", f"no parameter may carry a secret: [{kind}]${name}"
    assert params["BootstrapWithPassword"] == "switch" and params["SetupGeminiSecret"] == "switch"
    for line in text.splitlines():
        if "Read-Host" in line:
            assert not re.search(r"pass(word|phrase)|secret|credential|api[_ ]?key", line, re.I), f"prompt must not ask for a secret: {line.strip()}"
    assert not re.search(r"\$env:\w*(pass|secret|token|cred|key)", text, re.I), "no secret may come from the environment"
    assert not re.search(r"GEMINI_API_KEY\s*=", text), "the script never assigns or exports the Gemini key"
    # nothing is ever piped or redirected into ssh
    assert not re.search(r"\|\s*&?\s*'?ssh\b", text), "nothing may be piped into ssh"
    assert not re.search(r"<\s*\$", text), "nothing may be redirected into a native command"
    # the interactive session inherits the console untouched
    interactive = re.search(r"function Invoke-NativeInteractive.*?\n}", text, re.S).group(0)
    body = interactive.split("{", 1)[1]
    for token in ("|", "2>&1", "<", "Read-Host", "Write-Output"):
        assert token not in body, f"Invoke-NativeInteractive must not contain {token!r}"
    assert "& $Exe @Arguments" in body


def test_ps1_bootstrap_sends_only_the_public_key():
    text = _read(PS1)
    # dedicated ed25519 key, generated locally, kept out of the uploaded kit directory
    assert "'ssh-keygen'" in text
    assert "'-t', 'ed25519'" in text
    assert "ai-orchestrator-starter_ed25519" in text
    assert "must not live inside the kit directory" in text
    # only the .pub file is ever read; the private key is opened by ssh alone
    reads = [line for line in text.splitlines() if "Get-Content" in line]
    assert len(reads) == 1, f"exactly one file read (the .pub) expected, got: {reads}"
    assert "$PubPath" in reads[0] and "-TotalCount 1" in reads[0]
    assert 'Get-PublicKeyLine "$KeyPath.pub"' in text
    # the public key line is validated (type allow-list + base64 body) before it is embedded
    assert "ssh-ed25519" in text
    assert re.search(r"-notmatch '\^\[A-Za-z0-9\+/\]\+=\{0,2\}\$'", text), "base64 body of the public key must be validated"
    # the one interactive session: no BatchMode=yes, pubkey off so the human gets ssh's prompt
    start = text.find("$bootstrapOpts =")
    call = text.find("Invoke-NativeInteractive 'ssh'")
    assert 0 < start < call
    bootstrap = text[start:call]
    assert "BatchMode=yes" not in bootstrap and "PubkeyAuthentication=no" in bootstrap
    assert "$CommonOpts" in bootstrap, "AcceptNewHostKey semantics must also apply to the bootstrap session"
    assert "'-i'" not in bootstrap and "$KeyOpts" not in bootstrap, "no private key is offered during the password session"
    # the remote side installs the key with safe permissions and idempotently
    remote = re.search(r"\$remoteBootstrap\s*=\s*(.*?)\n\s*#", text, re.S).group(1)
    for token in ("umask 077", "mkdir -p ~/.ssh", "chmod 700 ~/.ssh", "chmod 600 ~/.ssh/authorized_keys",
                  "grep -qxF '$PubKeyLine'", "printf '%s\\n' '$PubKeyLine' >> ~/.ssh/authorized_keys", "echo BOOTSTRAP_OK"):
        assert token in remote, f"remote bootstrap command must contain {token!r}"
    for forbidden in ("PasswordAuthentication", "passwd", "chpasswd", "sshd_config", "systemctl", "apt"):
        assert forbidden not in remote, f"bootstrap must not touch {forbidden!r} on the host"
    # after bootstrap every operation is key-based
    ssh_opts = re.search(r"^\$SshOpts\s*=\s*(.*)$", text, re.M).group(1)
    for token in ("BatchMode=yes", "PasswordAuthentication=no", "KbdInteractiveAuthentication=no", "$CommonOpts", "$KeyOpts"):
        assert token in ssh_opts, f"$SshOpts must contain {token!r}"
    assert "$KeyOpts = @('-i', $KeyPath, '-o', 'IdentitiesOnly=yes')" in text
    assert text.count("$SshOpts") >= 6, "ssh/scp calls after bootstrap must reuse $SshOpts"


def test_ps1_keeps_accept_new_host_key_semantics():
    text = _read(PS1)
    code = re.sub(r"\A<#.*?#>", "", text, count=1, flags=re.S)   # drop the comment-based help
    assert "StrictHostKeyChecking=no" not in text, "host keys are never blindly accepted"
    assert re.search(r"if \(\$AcceptNewHostKey\) \{ \$CommonOpts \+= @\('-o', 'StrictHostKeyChecking=accept-new'\) \}", code)
    assert code.count("StrictHostKeyChecking=accept-new") == 1, "accept-new is added in exactly one guarded place"


def test_ps1_supports_explicit_ssh_key():
    text = _read(PS1)
    assert "[string]$SshKey" in text
    assert "IdentitiesOnly=yes" in text
    assert "SSH key does not exist" in text, "a missing explicit key must fail closed"


def test_ps1_gemini_setup_is_guided_on_the_host_and_secret_free():
    """Hybrid needs GEMINI_API_KEY on the host; the script only opens the on-host session."""
    text = _read(PS1)
    code = re.sub(r"\A<#.*?#>", "", text, count=1, flags=re.S)
    assert "gemini_runner" in text and "no Gemini CLI" in text, "help must say Gemini is HTTPS + GEMINI_API_KEY, not a CLI"
    assert "/home/orchestrator/.config/ai-orchestrator/secrets.env" in text
    assert "$GeminiSecretScript = \"$InstalledDir/installer/gemini-secret.sh\"" in code
    # the wizard explains hybrid and offers the on-host step; the hint block prints the exact commands
    assert "Hybrid   - Brain/Reviewer on Claude; Worker \"auto\" prefers Gemini Flash" in code
    assert re.search(r"if \(\$Wizard -and \$Profile -eq 'hybrid' -and \(-not \$SetupGeminiSecret\)\)", code)
    assert "sudo bash $GeminiSecretScript set\"" in code
    assert "sudo bash $GeminiSecretScript check --live\"" in code
    assert "falls back to Claude Sonnet" in code
    # exactly one guided call: interactive (console inherited), key-based ssh options, a tty, no extra argument
    calls = re.findall(r"Invoke-NativeInteractive 'ssh' \((.*)\)\n", code)
    gemini_calls = [c for c in calls if "GeminiSecretScript" in c]
    assert len(gemini_calls) == 1, calls
    call = gemini_calls[0]
    assert call.startswith("$SshOpts + @('-t', $Target, \"sudo bash $GeminiSecretScript set\")"), call
    assert "Invoke-Native 'ssh'" not in code[code.index("Invoke-NativeInteractive 'ssh' ($SshOpts"):], \
        "the guided session is the last ssh call; nothing runs on the host after it"
    # it happens only after install.sh succeeded and never changes the install result
    install_gate = code.index("STARTER INSTALL FAILED exit=$rc")
    guided = code.index("Invoke-NativeInteractive 'ssh' ($SshOpts")
    final_ok = code.index('Write-Host "STARTER INSTALL OK profile=')
    assert install_gate < guided < final_ok
    after = code[guided:final_ok]
    assert "Abort" not in after and "exit" not in after.replace("exit $LASTEXITCODE", "").replace("(exit ", ""), \
        "a failed Gemini step must not turn a successful install into STARTER INSTALL FAILED"
    assert "Re-run it any time with" in after
    # no bootstrap-style password options and no piping around the guided call
    assert "PubkeyAuthentication=no" not in after and "BatchMode=no" not in after
    assert "gemini-secret.sh" not in _read(SH).rsplit('echo "STARTER INSTALL OK', 1)[1], "sh twin: hint only, before the result line"


def test_sh_prints_the_same_gemini_hint_for_hybrid():
    text = _read(SH)
    assert 'if [ "$PROFILE" = hybrid ]' in text
    block = text[text.index("Gemini (Worker"):text.index("Reboot re-check")]
    assert "/home/orchestrator/.config/ai-orchestrator/secrets.env" in block
    assert "ssh -t $TARGET sudo bash $INSTALLED_DIR/installer/gemini-secret.sh set" in block
    assert "sudo bash $INSTALLED_DIR/installer/gemini-secret.sh check --live" in block
    assert "GEMINI_API_KEY=" not in text and "read " not in text, "the sh twin never handles the key either"


def test_ps1_and_sh_share_the_same_exclude_list():
    ps1_excludes = re.search(r"\$Excludes\s*=\s*@\((.*?)\)", _read(PS1), re.S)
    sh_excludes = re.search(r"EXCLUDES=\((.*?)\)", _read(SH), re.S)
    assert ps1_excludes and sh_excludes, "both scripts must declare an explicit exclude list"
    ps1_set = set(re.findall(r"'([^']+)'", ps1_excludes.group(1)))
    sh_set = set(re.findall(r"'([^']+)'|(\S+)", sh_excludes.group(1)))
    sh_set = {a or b for a, b in sh_set}
    assert ps1_set == sh_set, f"exclude lists differ: ps1={sorted(ps1_set)} sh={sorted(sh_set)}"
    assert {".git", ".venv", "__pycache__", "monitor/status.json"} <= ps1_set


def test_sh_has_bash_shebang_and_parses():
    text = _read(SH)
    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text
    proc = subprocess.run(["bash", "-n", str(SH)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_entry_point_is_documented():
    install_doc = _read(ROOT / "docs" / "INSTALL.md")
    readme = _read(ROOT / "README.md")
    for doc in (install_doc, readme):
        assert "START-HERE.ps1" in doc
        assert "installer/start-here.sh" in doc
    for marker in ("-Server", "-Profile", "-SshKey", "-BootstrapWithPassword", "-AcceptNewHostKey", "-NonInteractive",
                   "-SetupGeminiSecret", "IdentitiesOnly=yes", "authorized_keys", RESULT_OK, RESULT_FAILED,
                   "### 4.1", "installer/gemini-secret.sh set", "installer/gemini-secret.sh check --live",
                   "/home/orchestrator/.config/ai-orchestrator/secrets.env", "GEMINI_API_KEY", "chmod 700", "chmod 600",
                   "python3 -m core.gemini_runner --smoke", "`gemini_secret`"):
        assert marker in install_doc, f"docs/INSTALL.md must document {marker!r}"
    for marker in ("-SshKey", "-BootstrapWithPassword", "-SetupGeminiSecret", "gemini-secret.sh", "GEMINI_API_KEY"):
        assert marker in readme, f"README.md must mention {marker!r}"
    for doc in (install_doc, readme):
        assert "Gemini CLI" in doc and "no Gemini CLI" in doc.replace("No Gemini CLI", "no Gemini CLI"), \
            "docs must state that no Gemini CLI exists or is installed"


def test_ps1_parses_when_pwsh_is_available():
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh not installed; parse-only check skipped")
    command = (
        "$tokens = $null; $errors = $null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{PS1}', [ref]$tokens, [ref]$errors) | Out-Null; "
        "$errors | ForEach-Object { $_.Message }; "
        "Write-Output ('PARSE_ERRORS=' + $errors.Count)"
    )
    proc = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-Command", command],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "PARSE_ERRORS=0" in proc.stdout, proc.stdout
