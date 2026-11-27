"""Offline tests for the Gemini API key placement and the non-secret smoke check.

installer/gemini-secret.sh is driven through a pseudo-terminal against a
tmp_path secrets file (ORCHESTRATOR_SECRETS_FILE, the same override the runner
honours), so no root, no orchestrator user and no network are needed. The
smoke check in core.gemini_runner is exercised with a fake HTTP opener.
"""
import io
import os
import pty
import re
import select
import stat
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

import pytest

from core import gemini_runner

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "installer" / "gemini-secret.sh"
# Built at runtime so no test file ever carries a key-shaped literal.
FAKE_KEY = "AIza" + "TESTKEY" + "0123456789abcdefghijklmnopqrstuvwxyz"[:28]
PROMPT_FIRST = b"(input hidden)"
PROMPT_CONFIRM = b"again to confirm"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="bash + pty tests are POSIX only")


def _env(secrets_file):
    env = {k: v for k, v in os.environ.items() if k not in ("GEMINI_API_KEY", "ORCHESTRATOR_SECRETS_FILE")}
    env["ORCHESTRATOR_SECRETS_FILE"] = str(secrets_file)
    return env


def _run_with_tty(argv, env, answers, timeout=30):
    """Run argv on a pty; type each answer only after its hidden prompt appeared.

    Returns (exit_code, everything the process wrote to the terminal).
    """
    master, slave = pty.openpty()
    proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True,
                            start_new_session=True)
    os.close(slave)
    out = b""
    pending = list(answers)
    answered = 0
    deadline = time.monotonic() + timeout
    try:
        while True:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                out += chunk
            prompts = out.count(PROMPT_FIRST) + out.count(PROMPT_CONFIRM)
            while pending and prompts > answered:
                os.write(master, pending.pop(0).encode() + b"\n")
                answered += 1
            if proc.poll() is not None and not ready:
                break
            if time.monotonic() > deadline:
                proc.kill()
                break
        proc.wait(timeout=10)
    finally:
        os.close(master)
    return proc.returncode, out.decode("utf-8", errors="replace")


def _run(argv, env, stdin=subprocess.DEVNULL, timeout=60):
    return subprocess.run(argv, env=env, stdin=stdin, capture_output=True, text=True, timeout=timeout, check=False)


@pytest.fixture
def secrets_file(tmp_path):
    return tmp_path / "home" / ".config" / "ai-orchestrator" / "secrets.env"


# --- installer/gemini-secret.sh ---------------------------------------------

def test_script_is_static_safe():
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash")
    assert os.access(SCRIPT, os.X_OK)
    assert subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True).returncode == 0
    assert "set +x" in text, "tracing must be off: the value would land in the trace"
    assert "read -rs" in text and "[ -t 0 ]" in text, "the key comes from a hidden prompt on a real terminal only"
    assert "umask 077" in text and "chmod 700" in text and "chmod 600" in text
    assert "printf 'GEMINI_API_KEY=%s\\n' \"$key\"" in text, "the value is written by a bash builtin"
    assert "runuser" in text and "chown \"$TARGET_USER:$TARGET_USER\"" in text
    # the value is never echoed, logged or handed to an external command as an argument
    for line in text.splitlines():
        if "$key" in line or "${key}" in line:
            assert not re.match(r"\s*(echo|logger|printf '%s\\n'|info|ok|bad|abort)\b", line), f"leaks the value: {line.strip()}"
            assert "GEMINI_API_KEY=%s" in line or "read " in line or line.strip().startswith(("[", "case", "unset", "*", "IFS=")), \
                f"unexpected use of the value: {line.strip()}"
    assert "--live" in text and "core.gemini_runner --smoke" in text
    assert "GEMINI SECRET PASS" in text and "GEMINI SECRET FAIL" in text


def test_set_needs_a_terminal_and_never_takes_an_argument(secrets_file, tmp_path):
    env = _env(secrets_file)
    # a pipe is not a terminal: fail closed, nothing written
    proc = _run(["bash", str(SCRIPT), "set"], env, stdin=subprocess.PIPE)
    assert proc.returncode != 0
    assert "interactive terminal" in proc.stderr and "GEMINI SECRET FAIL" in proc.stderr
    assert not secrets_file.exists() and not secrets_file.parent.exists()
    # the key on the command line is refused before any prompt
    proc = _run(["bash", str(SCRIPT), "set", FAKE_KEY], env)
    assert proc.returncode != 0
    assert "never passed on the command line" in proc.stderr
    assert FAKE_KEY not in proc.stdout + proc.stderr
    assert not secrets_file.exists()
    # unknown command and unknown check flag
    assert _run(["bash", str(SCRIPT), "frobnicate"], env).returncode == 2
    assert _run(["bash", str(SCRIPT), "check", "--verbose"], env).returncode == 2


def test_set_writes_private_file_via_hidden_prompt_and_check_passes(secrets_file):
    env = _env(secrets_file)
    rc, out = _run_with_tty(["bash", str(SCRIPT), "set"], env, [FAKE_KEY, FAKE_KEY])
    assert rc == 0, out
    assert FAKE_KEY not in out, "the typed key must never appear on the terminal"
    assert "GEMINI SECRET PASS" in out
    assert secrets_file.read_text(encoding="utf-8") == f"GEMINI_API_KEY={FAKE_KEY}\n"
    assert stat.S_IMODE(secrets_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(secrets_file.parent.stat().st_mode) == 0o700
    assert not list(secrets_file.parent.glob(".secrets.env.*")), "no temp file left behind"
    # the runner's own parser finds it
    assert gemini_runner.load_secrets(secrets_file, environ={})["GEMINI_API_KEY"] == FAKE_KEY

    proc = _run(["bash", str(SCRIPT), "check"], env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.rstrip().splitlines()[-1] == f"GEMINI SECRET PASS file={secrets_file}"
    assert "GEMINI_API_KEY is set" in proc.stdout and "value not shown" in proc.stdout
    assert FAKE_KEY not in proc.stdout + proc.stderr


def test_set_replaces_existing_key_and_keeps_other_lines(secrets_file):
    env = _env(secrets_file)
    secrets_file.parent.mkdir(parents=True)
    secrets_file.parent.chmod(0o700)
    secrets_file.write_text("# managed by hand\nOTHER_TOKEN=keep-me\nGEMINI_API_KEY=old-value-to-replace\n", encoding="utf-8")
    secrets_file.chmod(0o644)
    rc, out = _run_with_tty(["bash", str(SCRIPT), "set"], env, [FAKE_KEY, FAKE_KEY])
    assert rc == 0, out
    lines = secrets_file.read_text(encoding="utf-8").splitlines()
    assert lines == ["# managed by hand", "OTHER_TOKEN=keep-me", f"GEMINI_API_KEY={FAKE_KEY}"]
    assert stat.S_IMODE(secrets_file.stat().st_mode) == 0o600, "a lax mode is tightened on rewrite"
    assert "old-value-to-replace" not in out and FAKE_KEY not in out


@pytest.mark.parametrize("answers,message", [
    ([FAKE_KEY, FAKE_KEY + "x"], "two entries differ"),
    (["", ""], "empty input"),
    (["short", "short"], "shorter than"),
    (["has a space in it and is long", "has a space in it and is long"], "printable characters"),
])
def test_set_rejects_bad_input_without_writing(secrets_file, answers, message):
    env = _env(secrets_file)
    rc, out = _run_with_tty(["bash", str(SCRIPT), "set"], env, answers)
    assert rc != 0
    assert message in out and "nothing was written" in out
    assert not secrets_file.exists()


def test_check_reports_missing_and_insecure_files_without_the_value(secrets_file):
    env = _env(secrets_file)
    proc = _run(["bash", str(SCRIPT), "check"], env)
    assert proc.returncode == 1
    assert "not configured" in proc.stdout and proc.stdout.rstrip().endswith(f"GEMINI SECRET FAIL file={secrets_file}")

    secrets_file.parent.mkdir(parents=True)
    secrets_file.parent.chmod(0o755)
    secrets_file.write_text(f"GEMINI_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    secrets_file.chmod(0o644)
    proc = _run(["bash", str(SCRIPT), "check"], env)
    assert proc.returncode == 1
    assert "expected 700" in proc.stdout and "expected 600" in proc.stdout
    assert FAKE_KEY not in proc.stdout + proc.stderr

    secrets_file.parent.chmod(0o700)
    secrets_file.chmod(0o600)
    secrets_file.write_text("GEMINI_API_KEY=\n", encoding="utf-8")
    proc = _run(["bash", str(SCRIPT), "check"], env)
    assert proc.returncode == 1 and "missing or empty" in proc.stdout

    secrets_file.write_text(f"GEMINI_API_KEY='{FAKE_KEY}'\n", encoding="utf-8")
    proc = _run(["bash", str(SCRIPT), "check"], env)
    assert proc.returncode == 0, proc.stdout
    # --live after a failed offline check is skipped, never attempted
    secrets_file.chmod(0o644)
    proc = _run(["bash", str(SCRIPT), "check", "--live"], env)
    assert proc.returncode == 1 and "live smoke skipped" in proc.stdout and "GEMINI SMOKE" not in proc.stdout


def test_check_live_runs_the_runner_smoke_as_documented(secrets_file, tmp_path, monkeypatch):
    """--live must call `core.gemini_runner --smoke`; a fake python records the call and fails closed."""
    env = _env(secrets_file)
    secrets_file.parent.mkdir(parents=True)
    secrets_file.parent.chmod(0o700)
    secrets_file.write_text(f"GEMINI_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    secrets_file.chmod(0o600)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "python3.log"
    fake_py = fake_bin / "python3"
    fake_py.write_text("#!/usr/bin/env bash\n"
                       f"printf '%s\\n' \"$*\" > '{log}'\n"
                       "printf 'secrets=%s\\n' \"${ORCHESTRATOR_SECRETS_FILE:-unset}\" >> '" + str(log) + "'\n"
                       "echo 'GEMINI SMOKE FAIL verdict=rejected http=401 model=x error=fake'\n"
                       "exit 1\n", encoding="utf-8")
    fake_py.chmod(0o755)
    # the kit checkout has no .venv/bin/python, so the script falls back to python3 from PATH
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    if (ROOT / ".venv" / "bin" / "python").exists():
        pytest.skip("a real .venv is present; the PATH fallback cannot be exercised")
    proc = _run(["bash", str(SCRIPT), "check", "--live"], env)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert log.read_text(encoding="utf-8").splitlines() == ["-m core.gemini_runner --smoke", f"secrets={secrets_file}"]
    assert "GEMINI SMOKE FAIL" in proc.stdout and "live smoke request failed" in proc.stdout
    assert FAKE_KEY not in proc.stdout + proc.stderr


# --- core.gemini_runner smoke check -----------------------------------------

class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _opener(status=200, body=None, capture=None, exc=None):
    def open_fn(request, timeout=None):
        if capture is not None:
            capture.append({"url": request.full_url, "headers": dict(request.header_items()),
                            "data": request.data, "timeout": timeout})
        if exc is not None:
            raise exc
        if status != 200:
            raise urllib.error.HTTPError(request.full_url, status, "err", {}, io.BytesIO(body or b""))
        payload = body if body is not None else (
            b'{"steps":[{"type":"model_output","content":[{"type":"text","text":"OK"}]}],'
            b'"usage":{"total_input_tokens":5,"total_output_tokens":1,"total_tokens":6}}')
        return _Response(payload)
    return open_fn


def test_smoke_check_not_configured_without_key(tmp_path):
    out = gemini_runner.smoke_check(environ={}, secrets_file=tmp_path / "absent.env", opener=_opener())
    assert out["ok"] is False and out["verdict"] == "not_configured" and out["http_status"] is None
    assert "GEMINI_API_KEY is not configured" in out["error"]
    line = gemini_runner.format_smoke_line(out)
    assert line.startswith("GEMINI SMOKE FAIL verdict=not_configured")


def test_smoke_check_success_uses_the_runner_path_and_hides_the_key(tmp_path):
    calls = []
    secrets = tmp_path / "s.env"
    secrets.write_text(f"GEMINI_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    out = gemini_runner.smoke_check(environ={}, secrets_file=secrets, opener=_opener(capture=calls))
    assert out["ok"] is True and out["verdict"] == "ok" and out["http_status"] == 200
    assert out["text"] == "OK" and out["usage"]["total_tokens"] == 6
    assert out["model"] == gemini_runner.DEFAULT_MODEL and out["service_tier"] == gemini_runner.FLEX_TIER
    assert len(calls) == 1
    assert calls[0]["url"] == gemini_runner.DEFAULT_URL
    assert calls[0]["headers"]["X-goog-api-key"] == FAKE_KEY
    assert b'"service_tier": "flex"' in calls[0]["data"]
    assert FAKE_KEY not in repr({k: v for k, v in out.items()})
    line = gemini_runner.format_smoke_line(out)
    assert line.startswith("GEMINI SMOKE OK model=") and "http=200" in line and FAKE_KEY not in line


@pytest.mark.parametrize("status,verdict", [(400, "rejected"), (401, "rejected"), (403, "rejected"),
                                            (429, "unavailable"), (503, "unavailable"), (418, "error")])
def test_smoke_check_classifies_http_errors_and_redacts(status, verdict):
    body = f'{{"error":"denied for key {FAKE_KEY}"}}'.encode()
    out = gemini_runner.smoke_check(api_key=FAKE_KEY, opener=_opener(status=status, body=body))
    assert out["ok"] is False and out["verdict"] == verdict and out["http_status"] == status
    assert FAKE_KEY not in out["error"] and "***" in out["error"]
    assert FAKE_KEY not in gemini_runner.format_smoke_line(out)


def test_smoke_check_network_error_is_reported_not_raised():
    out = gemini_runner.smoke_check(api_key=FAKE_KEY, opener=_opener(exc=urllib.error.URLError("no route")))
    assert out["ok"] is False and out["verdict"] == "error" and "URLError" in out["error"]


def test_main_prints_one_line_and_exit_code(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    stdout = io.StringIO()
    rc = gemini_runner.main(["--smoke"], stdout=stdout, environ={}, secrets_file=tmp_path / "none.env")
    assert rc == 1
    assert stdout.getvalue().splitlines() == [gemini_runner.format_smoke_line(
        gemini_runner.smoke_check(environ={}, secrets_file=tmp_path / "none.env"))]
    stdout = io.StringIO()
    rc = gemini_runner.main(["--smoke", "--timeout", "5"], stdout=stdout, api_key=FAKE_KEY, opener=_opener())
    assert rc == 0
    lines = stdout.getvalue().splitlines()
    assert len(lines) == 1 and lines[0].startswith("GEMINI SMOKE OK") and FAKE_KEY not in lines[0]
    with pytest.raises(SystemExit) as exc:
        gemini_runner.main([], stdout=io.StringIO())
    assert exc.value.code == 2, "--smoke is required; nothing runs implicitly"


def test_module_entry_point_needs_no_network_when_unconfigured(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "GEMINI_API_KEY"}
    env["ORCHESTRATOR_SECRETS_FILE"] = str(tmp_path / "absent.env")
    proc = subprocess.run([sys.executable, "-m", "core.gemini_runner", "--smoke"], cwd=str(ROOT), env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 1
    assert proc.stdout.splitlines() == ["GEMINI SMOKE FAIL verdict=not_configured http=None "
                                        f"model={gemini_runner.DEFAULT_MODEL} error=GEMINI_API_KEY is not configured"]
