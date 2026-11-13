"""Tests for workspace-root resolution and live project switching.

Both launchers resolve a project argument the same way: an existing path is
used as-is; otherwise a bare name is looked up under ZS_PROJECTS_ROOT, then
the folder containing this repo, then the current directory. Linux_Start.sh
must hand the bridge an absolute path even after it cds into its own folder,
and switch-project.sh must pass that absolute path to setup-config.py and
wait for the bridge's restart acknowledgement.

Resolution tests run the real scripts with a stub python3 first on PATH. The
bridge tests run the real switch-project.sh against a fake WebSocket server,
so the restart wait, health reporting, and config rewrite are exercised.
"""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import websockets
except ImportError:  # pragma: no cover - websockets is a dev requirement
    websockets = None


ROOT = Path(__file__).resolve().parent

# Captures the path each script resolved. The launchers call python3 for the
# version check, websockets check, setup-config.py, and bridge.py; only the
# last two matter here.
STUB = """#!/usr/bin/env bash
case "${1:-}" in
  *setup-config.py) printf '%s\\n' "${2:-}" > "$ZS_TEST_SETUP_ROOT" ;;
  bridge.py) printf '%s\\n' "${ZS_WORKSPACE_ROOT:-}" > "$ZS_TEST_ROOT_CAPTURE" ;;
esac
exit 0
"""

SUCCESS_RESPONSE = {
    "type": "mcp_status",
    "id": "switch",
    "ok": True,
    "alive": True,
    "servers": [{"id": "repo", "alive": True, "ready": True}],
}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipUnless(sys.platform == "linux", "launcher scripts target Linux")
class ProjectResolutionTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.base = Path(self._temp.name).resolve()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        for name in ("python3", "python"):
            stub = self.bin / name
            stub.write_text(STUB, encoding="utf-8")
            stub.chmod(0o755)
        self.setup_root = self.base / "setup-root.txt"
        self.bridge_root = self.base / "bridge-root.txt"
        self.venv_marker = self.base / "venv-used"

    def _copy_launcher(self, name: str) -> Path:
        tool = self.base / "tool"
        tool.mkdir(exist_ok=True)
        copied = tool / name
        shutil.copy2(ROOT / name, copied)
        copied.chmod(0o755)
        if name == "Linux_Start.sh":
            (tool / "bridge.py").write_text("", encoding="utf-8")
        return copied

    def _run(self, script: Path, args: list, cwd: Path, **env_overrides: str):
        env = os.environ.copy()
        for key in ("ZS_WORKSPACE_ROOT", "ZS_PROJECTS_ROOT"):
            env.pop(key, None)
        env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")
        env["ZS_TEST_SETUP_ROOT"] = str(self.setup_root)
        env["ZS_TEST_ROOT_CAPTURE"] = str(self.bridge_root)
        env["ZS_TEST_VENV_MARKER"] = str(self.venv_marker)
        env["ZS_BRIDGE_PORT"] = str(free_port())
        env.update(env_overrides)
        return subprocess.run(
            ["bash", "--", str(script), *args],
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def _captured(self, path: Path) -> Path:
        self.assertTrue(path.exists(), "launcher never reached the stubbed step")
        return Path(path.read_text(encoding="utf-8").strip()).resolve()

    def test_linux_start_relative_path_survives_cd(self):
        launcher = self._copy_launcher("Linux_Start.sh")
        project = self.base / "proj"
        project.mkdir()
        result = self._run(launcher, ["proj"], self.base)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._captured(self.bridge_root), project.resolve())

    def test_linux_start_bare_name_uses_projects_root(self):
        launcher = self._copy_launcher("Linux_Start.sh")
        project = self.base / "projects" / "App"
        project.mkdir(parents=True)
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        result = self._run(
            launcher,
            ["App"],
            elsewhere,
            ZS_PROJECTS_ROOT=str(self.base / "projects"),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._captured(self.bridge_root), project.resolve())

    def test_linux_start_bare_name_uses_parent_of_launcher(self):
        launcher = self._copy_launcher("Linux_Start.sh")
        project = self.base / "App"
        project.mkdir()
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        result = self._run(launcher, ["App"], elsewhere)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._captured(self.bridge_root), project.resolve())

    def test_linux_start_unknown_project_fails_before_launch(self):
        launcher = self._copy_launcher("Linux_Start.sh")
        result = self._run(launcher, ["Nope"], self.base)
        self.assertEqual(result.returncode, 1)
        self.assertIn("project not found", result.stdout)
        self.assertFalse(self.bridge_root.exists())

    def test_linux_start_without_argument_uses_launch_directory(self):
        launcher = self._copy_launcher("Linux_Start.sh")
        project = self.base / "project"
        project.mkdir()
        result = self._run(launcher, [], project)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._captured(self.bridge_root), project.resolve())

    def test_linux_start_path_with_spaces(self):
        launcher = self._copy_launcher("Linux_Start.sh")
        project = self.base / "project with spaces"
        project.mkdir()
        result = self._run(launcher, [project.name], self.base)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._captured(self.bridge_root), project.resolve())

    def test_linux_start_dash_leading_folder(self):
        launcher = self._copy_launcher("Linux_Start.sh")
        project = self.base / "-dash"
        project.mkdir()
        result = self._run(launcher, [project.name], self.base)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._captured(self.bridge_root), project.resolve())

    def test_switch_relative_path_passes_absolute_root_to_setup(self):
        script = self._copy_launcher("switch-project.sh")
        project = self.base / "proj"
        project.mkdir()
        result = self._run(script, ["proj"], self.base)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._captured(self.setup_root), project.resolve())

    def test_switch_bare_name_uses_projects_root(self):
        script = self._copy_launcher("switch-project.sh")
        project = self.base / "projects" / "App"
        project.mkdir(parents=True)
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        result = self._run(
            script,
            ["App"],
            elsewhere,
            ZS_PROJECTS_ROOT=str(self.base / "projects"),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._captured(self.setup_root), project.resolve())

    def test_switch_bare_name_uses_parent_of_launcher(self):
        script = self._copy_launcher("switch-project.sh")
        project = self.base / "App"
        project.mkdir()
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        result = self._run(script, ["App"], elsewhere)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._captured(self.setup_root), project.resolve())

    def test_switch_unknown_project_does_not_configure(self):
        script = self._copy_launcher("switch-project.sh")
        result = self._run(script, ["Nope"], self.base)
        self.assertEqual(result.returncode, 1)
        self.assertIn("project not found", result.stdout)
        self.assertFalse(self.setup_root.exists())

    def test_switch_requires_an_argument(self):
        script = self._copy_launcher("switch-project.sh")
        result = self._run(script, [], self.base)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Usage", result.stdout)
        self.assertFalse(self.setup_root.exists())

    def test_switch_path_with_spaces(self):
        script = self._copy_launcher("switch-project.sh")
        project = self.base / "project with spaces"
        project.mkdir()
        result = self._run(script, [project.name], self.base)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._captured(self.setup_root), project.resolve())

    def test_switch_dash_leading_folder(self):
        script = self._copy_launcher("switch-project.sh")
        project = self.base / "-dash"
        project.mkdir()
        result = self._run(script, [project.name], self.base)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._captured(self.setup_root), project.resolve())

    def test_switch_prefers_bundled_venv_python(self):
        script = self._copy_launcher("switch-project.sh")
        project = self.base / "proj"
        project.mkdir()
        venv_bin = self.base / "tool" / ".venv-snapgent" / "bin"
        venv_bin.mkdir(parents=True)
        venv_python = venv_bin / "python"
        venv_python.write_text(
            "#!/usr/bin/env bash\n"
            f'printf "venv\\n" > "{self.venv_marker}"\n'
            + STUB.split("\n", 1)[1],
            encoding="utf-8",
        )
        venv_python.chmod(0o755)
        result = self._run(script, ["proj"], self.base)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.venv_marker.exists())
        self.assertEqual(self._captured(self.setup_root), project.resolve())

    def _copy_switch_tool(self) -> Path:
        tool = self.base / "tool"
        tool.mkdir(exist_ok=True)
        for name in ("switch-project.sh", "setup-config.py"):
            shutil.copy2(ROOT / name, tool / name)
        script = tool / "switch-project.sh"
        script.chmod(0o755)
        return script

    def _run_switch_with_fake_bridge(self, response: dict, delay: float = 0.0):
        script = self._copy_switch_tool()
        project = self.base / "proj"
        project.mkdir(exist_ok=True)
        port = free_port()
        env = os.environ.copy()
        for key in ("ZS_WORKSPACE_ROOT", "ZS_PROJECTS_ROOT"):
            env.pop(key, None)
        env["ZS_BRIDGE_PORT"] = str(port)

        async def scenario():
            async def handler(connection, _path=None):
                await connection.recv()
                if delay:
                    await asyncio.sleep(delay)
                await connection.send(json.dumps(response))

            async with websockets.serve(handler, "127.0.0.1", port):
                proc = await asyncio.create_subprocess_exec(
                    "bash", "--", str(script), "proj",
                    cwd=str(self.base),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    out, err = await asyncio.wait_for(proc.communicate(), timeout=45)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    self.fail("switch-project.sh did not finish")
                return proc.returncode, out.decode(), err.decode()

        return asyncio.run(scenario())

    @unittest.skipUnless(websockets is not None, "websockets is required")
    def test_switch_restarts_bridge_and_reports_active_project(self):
        rc, out, err = self._run_switch_with_fake_bridge(SUCCESS_RESPONSE)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("New project is active", out)
        config = json.loads(
            (self.base / "tool" / "config.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            config["mcpServers"]["repo"]["env"]["ZS_WORKSPACE_ROOT"],
            str((self.base / "proj").resolve()),
        )

    @unittest.skipUnless(websockets is not None, "websockets is required")
    def test_switch_waits_through_silent_restart(self):
        rc, out, err = self._run_switch_with_fake_bridge(SUCCESS_RESPONSE, delay=5.5)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("New project is active", out)
        self.assertNotIn("No restart response", out)

    @unittest.skipUnless(websockets is not None, "websockets is required")
    def test_switch_reports_repo_server_failure(self):
        response = {
            **SUCCESS_RESPONSE,
            "alive": False,
            "servers": [
                {
                    "id": "repo",
                    "alive": False,
                    "ready": False,
                    "error": "workspace vanished",
                }
            ],
        }
        rc, out, err = self._run_switch_with_fake_bridge(response)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("Bridge restart failed", out)
        self.assertIn("workspace vanished", out)
        self.assertNotIn("New project is active", out)


if __name__ == "__main__":
    unittest.main()
