<#
.SYNOPSIS
  Windows entry point for the AI Orchestrator Starter Kit.

.DESCRIPTION
  Installs the kit on a bare Ubuntu host in one go, fail-closed:
    1. checks that ssh / scp / tar are available locally (Windows OpenSSH client
       and the built-in tar.exe on Windows 10+ / Server 2019+),
    2. validates the kit directory ($PSScriptRoot) and the chosen profile,
    3. tests key-based SSH (BatchMode, no password prompts) and passwordless sudo,
    4. uploads a filtered tarball of the kit (no .git, .venv, __pycache__,
       monitor/status.json or other local state) and re-extracts it on the host
       under /root/ai-orchestrator-starter (idempotent on re-runs),
    5. runs 'sudo bash installer/install.sh <profile>' remotely with the output
       streamed to this console; the installer itself runs preflight, installs
       the systemd units and ends with diagnose.
  The last line printed is exactly one of
    STARTER INSTALL OK profile=<profile> server=<server>
    STARTER INSTALL FAILED exit=<code>
  and the process exit code matches (0 on success).

  Run it without parameters for a short interactive wizard (server, profile,
  how you reach the host, confirmation). With -Server and -Profile given the
  script asks nothing further; with -NonInteractive it never prompts at all.

  First contact with a host that only has a root password (-BootstrapWithPassword,
  or answer 3 in the wizard): the script generates a dedicated local ed25519 key
  when there is none yet, opens ONE interactive ssh session in which YOU type
  the root password into ssh's own prompt, installs only the PUBLIC key into
  ~/.ssh/authorized_keys on the host (directory 700, file 600, idempotent) and
  from then on works key-based only (BatchMode=yes, IdentitiesOnly=yes,
  PasswordAuthentication=no). The script never accepts, reads, stores, logs or
  passes a password anywhere: no parameter, no prompt of its own, no
  environment variable, no file, no pipe, no command argument.

  Hybrid profile and Gemini: Brain/Reviewer run on Claude, the Worker route
  'auto' prefers Gemini Flash through the Gemini HTTPS API (core/gemini_runner.py;
  there is no Gemini CLI) and falls back to Claude Sonnet when Gemini is not
  configured or fails. Gemini needs GEMINI_API_KEY on the HOST, in
  /home/orchestrator/.config/ai-orchestrator/secrets.env (dir 700, file 600).
  This script never asks for, accepts, stores or forwards that key: no
  parameter, no prompt, no environment variable, no file. -SetupGeminiSecret
  (or answering yes in the wizard) only opens, after a successful install, an
  interactive ssh session running 'installer/gemini-secret.sh set' ON THE HOST,
  where you type the key into a hidden prompt; the install result does not
  depend on that step.

  Works on Windows PowerShell 5.1 and PowerShell 7. The Linux/macOS/WSL
  companion with the same install semantics is installer/start-here.sh (it has
  no wizard, no bootstrap mode and no guided Gemini step; use ssh-copy-id and
  the printed gemini-secret.sh commands there).

.PARAMETER Server
  Host name or IP address of the target Ubuntu host. Prompted when absent.
.PARAMETER Profile
  Provider profile: claude, openai or hybrid (a file in profiles/). Prompted when absent.
.PARAMETER User
  SSH user on the host (default root). Needs key-based SSH and passwordless sudo.
.PARAMETER SshKey
  Path to the SSH private key to use for ssh/scp (adds -i <key> and
  IdentitiesOnly=yes). In bootstrap mode this is the key that is generated
  (when the file does not exist yet) and whose public half is installed.
  Default: no -i (ssh-agent or ~/.ssh/id_*); in bootstrap mode
  ~/.ssh/ai-orchestrator-starter_ed25519.
.PARAMETER BootstrapWithPassword
  First contact with a host that only has a root password: see DESCRIPTION.
  Needs a human at the console (cannot be combined with -NonInteractive).
.PARAMETER SetupGeminiSecret
  After STARTER INSTALL OK is certain, open one interactive ssh session (-t)
  that runs 'sudo bash /opt/ai-orchestrator/installer/gemini-secret.sh set'
  on the host, where YOU type GEMINI_API_KEY into a hidden prompt (see
  DESCRIPTION). Bare switch: it carries no value. Needs a human at the
  console (cannot be combined with -NonInteractive). Meant for -Profile hybrid.
.PARAMETER NonInteractive
  Never prompt; missing -Server / -Profile is an error.
.PARAMETER AcceptNewHostKey
  Pass StrictHostKeyChecking=accept-new to ssh/scp so the first contact with a
  freshly created host does not fail on an unknown host key. Off by default.

.EXAMPLE
  .\START-HERE.ps1
  # interactive wizard
.EXAMPLE
  .\START-HERE.ps1 -Server <server-ip> -Profile claude
.EXAMPLE
  .\START-HERE.ps1 -Server <server-ip> -Profile claude -BootstrapWithPassword -AcceptNewHostKey
.EXAMPLE
  .\START-HERE.ps1 -Server <server-ip> -Profile hybrid -SetupGeminiSecret
  # install, then type GEMINI_API_KEY on the host in the guided session
.EXAMPLE
  .\START-HERE.ps1 -Server <server-ip> -Profile openai -SshKey C:\Users\me\.ssh\my_key -NonInteractive -AcceptNewHostKey
#>
[CmdletBinding()]
param(
    [string]$Server = '',
    # Shadows the automatic $PROFILE variable inside this script on purpose.
    [string]$Profile = '',
    [string]$User = 'root',
    [string]$SshKey = '',
    [switch]$BootstrapWithPassword,
    [switch]$SetupGeminiSecret,
    [switch]$NonInteractive,
    [switch]$AcceptNewHostKey
)

$ErrorActionPreference = 'Stop'

$KnownProfiles = @('claude', 'openai', 'hybrid')
$RemoteDir = '/root/ai-orchestrator-starter'
$RemoteArchive = 'ai-orchestrator-starter.tgz'   # relative to the SSH user's home
$InstalledDir = '/opt/ai-orchestrator'
# On-host helper that places/verifies GEMINI_API_KEY; the key is typed there, never here.
$GeminiSecretScript = "$InstalledDir/installer/gemini-secret.sh"
$GeminiSecretsFile = '/home/orchestrator/.config/ai-orchestrator/secrets.env'
# Local state that must never reach the host. Keep in sync with installer/start-here.sh.
$Excludes = @('.git', '.venv', '__pycache__', '.pytest_cache', '*.pyc',
              'monitor/status.json', 'profiles/active.json', 'jobs')
# Public key types this script is willing to install on the host.
$AllowedKeyTypes = @('ssh-ed25519', 'ssh-rsa', 'ecdsa-sha2-nistp256', 'ecdsa-sha2-nistp384', 'ecdsa-sha2-nistp521')
$DefaultBootstrapKey = Join-Path (Join-Path $HOME '.ssh') 'ai-orchestrator-starter_ed25519'

function Write-Step([string]$Message) { Write-Host "==> $Message" }

function Abort([string]$Message, [int]$Code = 1) {
    if ($Code -eq 0) { $Code = 1 }
    Write-Host "ERROR: $Message"
    Write-Host "STARTER INSTALL FAILED exit=$Code"
    exit $Code
}

# Runs a native command with stdout/stderr streamed to the console; the caller
# reads $LASTEXITCODE afterwards. ErrorActionPreference is relaxed around the
# call because Windows PowerShell 5.1 turns native stderr output into
# terminating errors under 'Stop', which would hide the real exit code.
function Invoke-Native([string]$Exe, [string[]]$Arguments) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @Arguments 2>&1 | ForEach-Object { Write-Host ([string]$_) }
    } finally {
        $ErrorActionPreference = $previous
    }
}

# Runs a native command with the console fully inherited (stdin, stdout and
# stderr untouched). Used for the single interactive ssh session of bootstrap
# mode so that ssh's own password prompt talks to the human directly: nothing
# is redirected, captured or fed in by this script.
function Invoke-NativeInteractive([string]$Exe, [string[]]$Arguments) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @Arguments
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Assert-ToolPresent([string]$Name, [string]$Hint) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        Abort "'$Name' was not found on PATH. $Hint" 3
    }
}

# Returns '<type> <base64> ai-orchestrator-starter' from the first line of a
# .pub file, or aborts. Only this public half is ever read by the script; the
# private key file is opened by ssh alone. The fixed comment and the strict
# character check make the line safe to embed in the remote shell command.
function Get-PublicKeyLine([string]$PubPath) {
    if (-not (Test-Path -LiteralPath $PubPath -PathType Leaf)) {
        Abort "Public key file is missing: '$PubPath' (expected next to the private key)." 3
    }
    $line = Get-Content -LiteralPath $PubPath -TotalCount 1
    $fields = ([string]$line).Trim() -split '\s+'
    if ($fields.Count -lt 2 -or $AllowedKeyTypes -notcontains $fields[0] -or $fields[1] -notmatch '^[A-Za-z0-9+/]+={0,2}$') {
        Abort "'$PubPath' is not a public key line this script can install (expected e.g. 'ssh-ed25519 AAAA...')." 3
    }
    return "$($fields[0]) $($fields[1]) ai-orchestrator-starter"
}

Write-Host 'AI Orchestrator Starter - Windows entry point'

# --- 1. local tools -----------------------------------------------------------
Assert-ToolPresent 'ssh' 'Install the Windows OpenSSH client (Settings > Apps > Optional features > OpenSSH Client).'
Assert-ToolPresent 'scp' 'Install the Windows OpenSSH client (Settings > Apps > Optional features > OpenSSH Client).'
Assert-ToolPresent 'tar' 'tar.exe ships with Windows 10 (1803+) and Windows Server 2019+.'

# --- 2. kit directory, wizard, profile ----------------------------------------
$KitDir = $PSScriptRoot
if (-not $KitDir) { Abort 'Run this script from its file (.\START-HERE.ps1); $PSScriptRoot is empty.' 3 }
if (-not (Test-Path (Join-Path $KitDir 'installer/install.sh'))) {
    Abort "installer/install.sh is missing next to START-HERE.ps1 ($KitDir). Run the script from a complete kit checkout." 3
}

if ($BootstrapWithPassword -and $NonInteractive) {
    Abort "-BootstrapWithPassword needs a human at the console (the root password is typed into ssh's own prompt); it cannot be combined with -NonInteractive." 2
}
if ($SetupGeminiSecret -and $NonInteractive) {
    Abort "-SetupGeminiSecret needs a human at the console (GEMINI_API_KEY is typed into a hidden prompt on the host); it cannot be combined with -NonInteractive." 2
}

# The wizard only runs when the caller left -Server or -Profile out and did
# not ask for -NonInteractive. A call with both parameters behaves exactly as
# before: no questions at all.
$Wizard = (-not $NonInteractive) -and ((-not $Server) -or (-not $Profile))
if ($Wizard) {
    Write-Host ''
    Write-Host 'Setup wizard. Answer the questions below; Enter accepts the default shown in [brackets].'
    Write-Host 'Nothing is changed on the host before you confirm the summary at the end.'
    Write-Host ''
}

if (-not $Server) {
    if ($NonInteractive) { Abort '-Server is required with -NonInteractive.' 2 }
    $Server = Read-Host 'Server IP address or host name'
}
$Server = $Server.Trim()
if ($Server -notmatch '^[A-Za-z0-9][A-Za-z0-9.:-]*$') { Abort "Invalid server name: '$Server'" 2 }
if ($User -notmatch '^[a-z_][a-z0-9_-]*$') { Abort "Invalid SSH user name: '$User'" 2 }

if (-not $Profile) {
    if ($NonInteractive) { Abort "-Profile is required with -NonInteractive (one of: $($KnownProfiles -join ', '))." 2 }
    Write-Host 'Profiles: 1 Claude, 2 OpenAI, 3 Hybrid'
    Write-Host '  1  Claude   - Brain, Worker and Reviewer on the Claude CLI (login on the host afterwards)'
    Write-Host '  2  OpenAI   - Brain, Worker and Reviewer on the Codex CLI (login on the host afterwards)'
    Write-Host '  3  Hybrid   - Brain/Reviewer on Claude; Worker "auto" prefers Gemini Flash over the Gemini HTTPS API'
    Write-Host '                (GEMINI_API_KEY placed on the host after install; no Gemini CLI) and falls back to Claude Sonnet'
    $choice = (Read-Host 'Choose profile (1/2/3 or name)').Trim()
    $menu = @{ '1' = 'claude'; '2' = 'openai'; '3' = 'hybrid' }
    if ($menu.ContainsKey($choice)) { $Profile = $menu[$choice] } else { $Profile = $choice }
}
$Profile = $Profile.Trim().ToLowerInvariant()
if ($KnownProfiles -notcontains $Profile) {
    Abort "Unknown profile '$Profile'. Expected one of: $($KnownProfiles -join ', ')." 2
}
if (-not (Test-Path (Join-Path $KitDir "profiles/$Profile.json"))) {
    Abort "profiles/$Profile.json is missing in the kit directory ($KitDir)." 3
}

# Hybrid needs GEMINI_API_KEY on the host for the Gemini Worker route. The key
# never passes through this script; the wizard only offers to open the on-host
# session (installer/gemini-secret.sh set) after the install.
if ($Wizard -and $Profile -eq 'hybrid' -and (-not $SetupGeminiSecret)) {
    Write-Host ''
    Write-Host 'Hybrid uses Gemini Flash for the Worker "auto" route through the Gemini API: GEMINI_API_KEY must be placed'
    Write-Host "on the host in $GeminiSecretsFile (dir 700, file 600, owner orchestrator)."
    Write-Host 'This script never asks for that key. After a successful install it can open an ssh session on the host'
    Write-Host 'where you type the key yourself into a hidden prompt; nothing is stored on this PC. Until the key is placed,'
    Write-Host 'Worker "auto" falls back to Claude Sonnet on every call (logged); Brain and Reviewer are unaffected.'
    $gem = (Read-Host 'Open that on-host session at the end of the install? (y/N)').Trim()
    if ($gem -match '^[Yy]') { $SetupGeminiSecret = $true }
}

if ($Wizard -and (-not $SshKey) -and (-not $BootstrapWithPassword)) {
    Write-Host ''
    Write-Host "How do you reach $User@$Server today?"
    Write-Host '  1  Key-based SSH already works (ssh-agent or ~/.ssh/id_*)'
    Write-Host '  2  Key-based SSH works with a specific private key file'
    Write-Host '  3  First contact: the host only has a root password (you will type it into ssh''s own prompt once;'
    Write-Host '     this script then installs a dedicated key and never uses the password again)'
    $access = (Read-Host 'Choose 1, 2 or 3 [1]').Trim()
    switch ($access) {
        ''  { }
        '1' { }
        '2' { $SshKey = (Read-Host 'Path to the private key file').Trim() }
        '3' { $BootstrapWithPassword = $true }
        default { Abort "Invalid choice '$access'. Expected 1, 2 or 3." 2 }
    }
}
if ($Wizard -and (-not $AcceptNewHostKey)) {
    $fresh = (Read-Host 'Is this the first SSH connection to this host from this PC? Accept its new host key (y/N)').Trim()
    if ($fresh -match '^[Yy]') { $AcceptNewHostKey = $true }
}

# --- key selection --------------------------------------------------------------
if ($BootstrapWithPassword -and (-not $SshKey)) { $SshKey = $DefaultBootstrapKey }
$KeyPath = ''
if ($SshKey) {
    $KeyPath = $SshKey.Trim()
    if (-not [System.IO.Path]::IsPathRooted($KeyPath)) { $KeyPath = Join-Path (Get-Location).ProviderPath $KeyPath }
    $KeyPath = [System.IO.Path]::GetFullPath($KeyPath)
    # The kit directory is uploaded to the host: a private key inside it would go along.
    $fullKit = [System.IO.Path]::GetFullPath($KitDir).TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    if ($KeyPath.StartsWith($fullKit, [System.StringComparison]::OrdinalIgnoreCase)) {
        Abort "The SSH key must not live inside the kit directory ($KitDir): that directory is uploaded to the host. Keep keys under ~/.ssh." 2
    }
    if ((-not $BootstrapWithPassword) -and (-not (Test-Path -LiteralPath $KeyPath -PathType Leaf))) {
        Abort "SSH key does not exist: '$KeyPath'" 3
    }
}

$Target = "$User@$Server"
Write-Host ''
Write-Host "Kit directory : $KitDir"
Write-Host "Target        : $Target"
Write-Host "Profile       : $Profile"
if ($KeyPath) { Write-Host "SSH key       : $KeyPath" } else { Write-Host 'SSH key       : ssh-agent / ~/.ssh/id_* (no -i)' }
if ($BootstrapWithPassword) { Write-Host 'Access        : first contact via root password (typed into ssh), then key-based only' } else { Write-Host 'Access        : key-based' }
Write-Host "Host key      : $(if ($AcceptNewHostKey) { 'accept-new (first contact)' } else { 'must already be in known_hosts (or ssh asks)' })"
if ($Profile -eq 'hybrid' -or $SetupGeminiSecret) {
    Write-Host "Gemini        : Worker 'auto' via GEMINI_API_KEY on the host ($GeminiSecretsFile); $(if ($SetupGeminiSecret) { 'guided on-host entry after the install' } else { 'place it on the host afterwards (commands printed at the end)' })"
}

if ($Wizard) {
    Write-Host ''
    Write-Host 'This will:'
    if ($BootstrapWithPassword) {
        Write-Host "  - generate $KeyPath if it does not exist (ed25519, no passphrase) and install its public key on the host"
    }
    Write-Host "  - upload the kit to ${Target}:$RemoteDir (no .git, .venv, __pycache__ or local state)"
    Write-Host "  - run 'sudo bash installer/install.sh $Profile' on $Server (preflight, systemd units, diagnose)"
    if ($SetupGeminiSecret) {
        Write-Host "  - then open an ssh session on $Server running 'installer/gemini-secret.sh set', where you type GEMINI_API_KEY (hidden)"
    }
    $go = (Read-Host 'Proceed? (Y/n)').Trim()
    if ($go -match '^[Nn]') { Abort 'Cancelled at the summary; nothing was changed on the host.' 2 }
}

# --- 3. SSH options ---------------------------------------------------------------
# Options shared by every ssh/scp call, including the one interactive bootstrap session.
$CommonOpts = @('-o', 'ConnectTimeout=15')
if ($AcceptNewHostKey) { $CommonOpts += @('-o', 'StrictHostKeyChecking=accept-new') }
$KeyOpts = @()
if ($KeyPath) { $KeyOpts = @('-i', $KeyPath, '-o', 'IdentitiesOnly=yes') }
# Key-based options for everything except the bootstrap session: ssh can never
# fall back to a password prompt.
$SshOpts = @('-o', 'BatchMode=yes', '-o', 'PasswordAuthentication=no', '-o', 'KbdInteractiveAuthentication=no') + $CommonOpts + $KeyOpts

# --- 3a. first contact: install the public key (bootstrap mode only) ------------
if ($BootstrapWithPassword) {
    if (-not (Test-Path -LiteralPath $KeyPath -PathType Leaf)) {
        Assert-ToolPresent 'ssh-keygen' 'It ships with the Windows OpenSSH client (Settings > Apps > Optional features > OpenSSH Client).'
        Write-Step "Generating a dedicated ed25519 key at $KeyPath (no passphrase; protect this file like a password)"
        $keyDir = Split-Path -Parent $KeyPath
        if ($keyDir -and (-not (Test-Path -LiteralPath $keyDir))) { New-Item -ItemType Directory -Path $keyDir -Force | Out-Null }
        # Empty passphrase: Windows PowerShell 5.1 drops empty-string arguments to
        # native commands, so hand it a literal "" there; PowerShell 7.3+ (Standard
        # argument passing) forwards '' correctly and would keep the quotes.
        $legacyArgs = (-not (Test-Path Variable:PSNativeCommandArgumentPassing)) -or ($PSNativeCommandArgumentPassing -eq 'Legacy')
        $emptyPassphrase = if ($legacyArgs) { '""' } else { '' }
        Invoke-Native 'ssh-keygen' @('-q', '-t', 'ed25519', '-N', $emptyPassphrase, '-C', 'ai-orchestrator-starter', '-f', $KeyPath)
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $KeyPath) -or -not (Test-Path -LiteralPath "$KeyPath.pub")) {
            Abort "ssh-keygen failed to create $KeyPath (exit $LASTEXITCODE)." 3
        }
    }
    $PubKeyLine = Get-PublicKeyLine "$KeyPath.pub"

    # Re-runs: when the key is already authorised there is nothing to bootstrap.
    Write-Step "Checking whether $KeyPath is already authorised for $Target"
    Invoke-Native 'ssh' ($SshOpts + @('-o', 'LogLevel=QUIET', $Target, 'echo SSH_OK'))
    if ($LASTEXITCODE -eq 0) {
        Write-Host 'Key already authorised; skipping the password step.'
    } else {
        Write-Step "First contact: installing the public key for $User on $Server"
        Write-Host 'ssh will now ask for the root password of the host (and, on a new host, whether to trust its host key).'
        Write-Host 'Type the password into that prompt; this script never sees it. Only this public key is sent:'
        Write-Host "  $PubKeyLine"
        $remoteBootstrap = 'set -e; umask 077; mkdir -p ~/.ssh; chmod 700 ~/.ssh; touch ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys; ' +
                           "grep -qxF '$PubKeyLine' ~/.ssh/authorized_keys || printf '%s\n' '$PubKeyLine' >> ~/.ssh/authorized_keys; echo BOOTSTRAP_OK"
        # No BatchMode, no -i: the human authenticates with the password inside
        # ssh itself. PubkeyAuthentication=no keeps ssh from offering unrelated
        # local keys first, so the prompt appears right away.
        $bootstrapOpts = @('-o', 'BatchMode=no', '-o', 'PubkeyAuthentication=no', '-o', 'PreferredAuthentications=keyboard-interactive,password') + $CommonOpts
        Invoke-NativeInteractive 'ssh' ($bootstrapOpts + @($Target, $remoteBootstrap))
        if ($LASTEXITCODE -ne 0) {
            Abort ("Bootstrap ssh session to $Target failed (exit $LASTEXITCODE): wrong password, password login disabled for $User on the host " +
                   "(sshd PermitRootLogin / PasswordAuthentication), or host key not accepted. Nothing was changed on the host. " +
                   'Fix the cause and re-run; the local key is kept and reused.') 4
        }
        Write-Host 'Public key installed. Everything from here on is key-based (BatchMode=yes, IdentitiesOnly=yes).'
    }
}

# --- 3b. SSH and sudo checks ----------------------------------------------------
Write-Step "Testing key-based SSH to $Target"
Invoke-Native 'ssh' ($SshOpts + @($Target, 'echo SSH_OK'))
if ($LASTEXITCODE -ne 0) {
    Abort ("SSH to $Target failed (exit $LASTEXITCODE). Key-based SSH is required: this entry point never prompts for " +
           'passwords. Load your key (ssh-agent, -SshKey or ~/.ssh/id_*) and make sure it is authorised on the host. ' +
           'If this is the first contact with a new host, re-run with -AcceptNewHostKey; if the host only has a root ' +
           'password so far, re-run with -BootstrapWithPassword (or run the wizard without parameters).') 4
}
Write-Step "Checking passwordless sudo for $User"
Invoke-Native 'ssh' ($SshOpts + @($Target, 'sudo -n true'))
if ($LASTEXITCODE -ne 0) { Abort "'sudo -n true' failed for $User on $Server (exit $LASTEXITCODE); the installer needs root via passwordless sudo." 4 }

# --- 4. filtered upload -------------------------------------------------------
Write-Step 'Building filtered kit archive (no .git, .venv, __pycache__, status.json or other local state)'
$LocalArchive = Join-Path ([System.IO.Path]::GetTempPath()) ("ai-orchestrator-starter-" + [guid]::NewGuid().ToString('N') + '.tgz')
$tarArgs = @('-c', '-z', '-f', $LocalArchive)
foreach ($pattern in $Excludes) { $tarArgs += "--exclude=$pattern" }
$tarArgs += @('-C', $KitDir, '.')
try {
    Invoke-Native 'tar' $tarArgs
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $LocalArchive)) { Abort "tar failed to build the kit archive (exit $LASTEXITCODE)." 5 }
    $sizeKb = [int]((Get-Item $LocalArchive).Length / 1KB)
    Write-Host "Archive: $LocalArchive ($sizeKb KB)"

    Write-Step "Uploading archive to ${Target}:~/$RemoteArchive"
    Invoke-Native 'scp' ($SshOpts + @('-q', $LocalArchive, "${Target}:$RemoteArchive"))
    if ($LASTEXITCODE -ne 0) { Abort "scp upload failed (exit $LASTEXITCODE)." 5 }
} finally {
    if (Test-Path $LocalArchive) { Remove-Item -Force $LocalArchive -ErrorAction SilentlyContinue }
}

Write-Step "Re-creating $RemoteDir on the host from the archive"
# Windows tar.exe records no executable bits, so restore them for the scripts
# install.sh runs directly from the uploaded directory.
$remoteExtract = "set -e; sudo rm -rf $RemoteDir; sudo mkdir -p $RemoteDir; " +
                 "sudo tar --no-same-owner -xzf ~/$RemoteArchive -C $RemoteDir; rm -f ~/$RemoteArchive; " +
                 "sudo chmod +x $RemoteDir/installer/*.sh $RemoteDir/monitor/*.py; " +
                 "sudo test -f $RemoteDir/installer/install.sh; sudo test -f $RemoteDir/profiles/$Profile.json; echo UPLOAD_OK"
Invoke-Native 'ssh' ($SshOpts + @($Target, $remoteExtract))
if ($LASTEXITCODE -ne 0) { Abort "Remote extraction into $RemoteDir failed (exit $LASTEXITCODE)." 5 }

# --- 5. remote install (preflight + systemd + diagnose inside install.sh) ------
Write-Step "Running sudo bash installer/install.sh $Profile on $Server (output streamed below)"
# Absolute script path: install.sh is cwd-independent and /root is not
# readable by a non-root SSH user, so no 'cd' into $RemoteDir is needed.
$remoteInstall = "sudo bash $RemoteDir/installer/install.sh $Profile"
Invoke-Native 'ssh' ($SshOpts + @($Target, $remoteInstall))
# ssh returns the remote command's exit code, i.e. install.sh's own exit code.
$rc = [int]$LASTEXITCODE
if ($rc -ne 0) {
    Write-Host "ERROR: installer/install.sh exited $rc on $Server. Read the last FAIL / INSTALL FAIL line above, fix the kit, re-run this script."
    Write-Host "STARTER INSTALL FAILED exit=$rc"
    exit $rc
}

$SshHint = if ($KeyPath) { "ssh -i `"$KeyPath`"" } else { 'ssh' }
Write-Host ''
Write-Host 'Install, preflight and diagnose passed on the host.'
if ($Profile -eq 'hybrid') {
    Write-Host 'Manual steps left: the provider logins, once, as the orchestrator user, and the Gemini API key (below):'
} else {
    Write-Host 'The only manual step left is the provider login, once, as the orchestrator user:'
}
Write-Host "  $SshHint $Target"
Write-Host '  sudo -iu orchestrator'
switch ($Profile) {
    'claude' { Write-Host '  claude          # follow the interactive login' }
    'openai' { Write-Host '  codex login' }
    default  { Write-Host '  claude          # follow the interactive login'; Write-Host '  codex login' }
}

# --- 6. Gemini API key for the hybrid Worker route (typed on the host, never here) ---
if ($Profile -eq 'hybrid' -or $SetupGeminiSecret) {
    Write-Host ''
    Write-Host 'Gemini (Worker "auto" prefers Gemini Flash over the Gemini HTTPS API; no Gemini CLI is installed or needed):'
    Write-Host "  GEMINI_API_KEY belongs on the host in $GeminiSecretsFile (dir 700, file 600, owner orchestrator)."
    Write-Host '  Until it is there, Worker "auto" falls back to Claude Sonnet on every call (recorded as a fallback event);'
    Write-Host '  Brain and Reviewer are unaffected. Type the key on the host - never pass it to this script or on a command line:'
    Write-Host "    $SshHint -t $Target sudo bash $GeminiSecretScript set"
    Write-Host '  Verify afterwards without revealing it (offline checks; add --live for one real API request):'
    Write-Host "    $SshHint $Target sudo bash $GeminiSecretScript check --live"
}
if ($SetupGeminiSecret) {
    Write-Step "Opening an interactive session on $Server: type GEMINI_API_KEY into the hidden prompt there (nothing is stored on this PC)"
    # Console inherited, key-based ssh with a tty: the on-host script reads the
    # key with `read -rs` from that tty; this script passes only the command name.
    Invoke-NativeInteractive 'ssh' ($SshOpts + @('-t', $Target, "sudo bash $GeminiSecretScript set"))
    if ($LASTEXITCODE -eq 0) {
        Write-Host 'Gemini API key placed on the host and verified (gemini-secret.sh check passed; value never shown).'
    } else {
        Write-Host "NOTE: the on-host Gemini key step did not complete (exit $LASTEXITCODE); the install itself succeeded. Re-run it any time with:"
        Write-Host "  $SshHint -t $Target sudo bash $GeminiSecretScript set"
    }
}

Write-Host 'Reboot re-check (must print DIAGNOSE PASS again):'
Write-Host "  sudo bash $InstalledDir/installer/diagnose.sh $Profile"
Write-Host "STARTER INSTALL OK profile=$Profile server=$Server"
exit 0
