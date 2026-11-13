"""Tests for the corrected sandbox hardening and the CSWSH Host policy.

Run from the repo root: /usr/bin/python3 -m pytest tests/test_sandbox_hardening.py -v
"""

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import bridge
import bridge_auth
import workspace_mcp

PYTHON = "/usr/bin/python3"


def run_sandboxed(script, root, timeout=60):
    return workspace_mcp._run_process([PYTHON, "-c", script], root, timeout=timeout)


# ---------------------------------------------------------------- sandbox


def test_sandbox_flags_accepted(tmp_path):
    argv = workspace_mcp._sandboxed_argv(["true"], tmp_path)
    result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stderr == "", result.stderr


def test_capabilities_fully_dropped(tmp_path):
    out = run_sandboxed(
        "print([l for l in open('/proc/self/status') "
        "if l.startswith('CapEff')][0].split()[1])",
        tmp_path,
    )
    assert out.strip() == "0000000000000000"


def test_network_isolated(tmp_path):
    script = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
        "    print('OPEN')\n"
        "except Exception:\n"
        "    print('BLOCKED')\n"
    )
    assert run_sandboxed(script, tmp_path).strip() == "BLOCKED"


def test_home_root_media_masked(tmp_path):
    out = run_sandboxed(
        "import os; print(len(os.listdir('/home')), "
        "len(os.listdir('/root')), len(os.listdir('/media')))",
        tmp_path,
    )
    assert out.strip() == "0 0 0"


def test_host_pids_hidden(tmp_path):
    out = run_sandboxed(
        "import os; print(len([p for p in os.listdir('/proc') if p.isdigit()]))",
        tmp_path,
    )
    assert int(out.strip()) < 20


def test_workspace_is_writable(tmp_path):
    (tmp_path / "probe.txt").write_text("before", encoding="utf-8")
    out = run_sandboxed("open('probe.txt','w').write('after'); print('ok')", tmp_path)
    assert out.strip() == "ok"
    assert (tmp_path / "probe.txt").read_text(encoding="utf-8") == "after"


def test_git_works_inside_sandbox(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=tmp_path, check=True)
    status = workspace_mcp._run_process(["git", "status", "--short"], tmp_path, timeout=60)
    assert "f.txt" in status
    workspace_mcp._run_process(["git", "commit", "-q", "-m", "init"], tmp_path, timeout=60)


def test_stderr_returned_when_stdout_is_empty(tmp_path):
    assert run_sandboxed("import sys; sys.stderr.write('warn')", tmp_path) == "warn"


def test_stdout_wins_over_stderr(tmp_path):
    out = run_sandboxed(
        "import sys; sys.stdout.write('out'); sys.stderr.write('err')", tmp_path
    )
    assert out == "out"


def test_failing_command_error_is_truncated(tmp_path):
    with pytest.raises(RuntimeError) as excinfo:
        run_sandboxed("import sys; sys.stdout.write('B'*500000); sys.exit(3)", tmp_path)
    message = str(excinfo.value)
    assert "status 3" in message
    assert len(message) <= workspace_mcp.MAX_FILE_CHARS + 100


def test_large_output_is_capped_without_full_buffering(tmp_path):
    """A 200 MB payload must not be buffered whole before the cap is applied.

    Before the streaming change the parent buffered the entire payload and
    peaked near 80 MB for 20 MB of output; a capped drain stays flat.
    """
    driver = tmp_path / "driver.py"
    driver.write_text(
        "import resource, sys\n"
        "sys.path.insert(0, sys.argv[2])\n"
        "import workspace_mcp\n"
        "out = workspace_mcp._run_process(\n"
        "    ['/usr/bin/python3', '-c', \"import sys; sys.stdout.write('A'*200000000)\"],\n"
        "    sys.argv[1], timeout=180,\n"
        ")\n"
        "print('LEN', len(out))\n"
        "print('MAXRSS_KIB', resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)\n",
        encoding="utf-8",
    )
    root = str(Path(workspace_mcp.__file__).parent)
    result = subprocess.run(
        [PYTHON, str(driver), str(tmp_path), root],
        capture_output=True,
        text=True,
        timeout=240,
        cwd=root,
    )
    assert result.returncode == 0, result.stderr
    assert f"LEN {workspace_mcp.MAX_FILE_CHARS}" in result.stdout, result.stdout
    rss_kib = int(
        [line for line in result.stdout.splitlines() if line.startswith("MAXRSS_KIB")][0].split()[1]
    )
    # Uncapped buffering of 200 MB would peak far above this; a capped drain
    # stays in the same range as the interpreter baseline.
    assert rss_kib * 1024 < 60_000_000, (
        f"peak RSS {rss_kib} KiB still buffered the whole payload"
    )

# ------------------------------------------------------------ fail-closed


def test_fail_closed_when_bwrap_missing(tmp_path):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args)
        raise AssertionError("unsandboxed execution must not happen")

    with patch.object(workspace_mcp.shutil, "which", return_value=None), patch.object(
        workspace_mcp.subprocess, "run", side_effect=fake_run
    ):
        with pytest.raises(RuntimeError, match="ZS_ALLOW_UNSANDBOXED"):
            workspace_mcp._sandboxed_argv(["true"], tmp_path)
    assert calls == []


def test_allow_unsandboxed_override_falls_back(tmp_path):
    with patch.object(workspace_mcp.shutil, "which", return_value=None), patch.dict(
        "os.environ", {"ZS_ALLOW_UNSANDBOXED": "1"}, clear=False
    ):
        assert workspace_mcp._sandboxed_argv(["true"], tmp_path) == ["true"]


# --------------------------------------------------------- CSWSH Host policy


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.0.0.1:17613",
        "localhost",
        "localhost:17613",
        "[::1]",
        "[::1]:17613",
        "LOCALHOST",
    ],
)
def test_host_allowed_loopback(host):
    assert bridge_auth.host_allowed(host), host


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1.evil.example",
        "localhost.evil.example",
        "0.0.0.0",
        "127.0.0.2",
        "[::1",
        "[::2]:17613",
    ],
)
def test_host_rejected_non_loopback(host):
    assert not bridge_auth.host_allowed(host), host


@pytest.mark.parametrize("host", [None, "", "   "])
def test_missing_or_blank_host_is_left_to_origin_check(host):
    assert bridge_auth.host_allowed(host), host


class _FakeSocket:
    def __init__(self, headers):
        self.request = type("R", (), {"headers": headers, "path": "/"})()
        self.sent = []
        self.close_calls = []

    async def send(self, data):
        self.sent.append(data)
        return True

    async def close(self, code=None, reason=None):
        self.close_calls.append((code, reason))

    def __aiter__(self):
        async def gen():
            return
            yield ""

        return gen()


def _run_handle(headers):
    bridge_instance = bridge.Bridge(token=None)
    socket = _FakeSocket(headers)
    asyncio.run(bridge_instance.handle_client(socket))
    return socket


def test_handle_client_rejects_spoofed_host():
    socket = _run_handle({"Host": "127.0.0.1.evil.example"})
    assert socket.close_calls


def test_handle_client_accepts_ipv6_loopback():
    socket = _run_handle({"Host": "[::1]:17613"})
    assert socket.close_calls == []
