import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import workspace_mcp


class WorkspaceMcpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.root_patch = patch.object(workspace_mcp, "get_root", return_value=self.workspace)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.addCleanup(self.temp.cleanup)

    def test_catalog_has_30_unique_tools(self):
        names = [tool["name"] for tool in workspace_mcp.TOOLS]
        self.assertEqual(len(names), 30)
        self.assertEqual(len(set(names)), 30)
        for name in (
            "delete_file",
            "apply_patch",
            "git_add",
            "git_commit",
            "git_push",
            "git_create_pr",
        ):
            self.assertIn(name, names)

    def test_delete_file_refuses_unsafe_targets(self):
        workspace_mcp.create_file("notes.txt", "x")
        (self.workspace / "folder").mkdir()
        (self.workspace / ".git").mkdir()
        (self.workspace / ".git" / "config").write_text("x", encoding="utf-8")
        self.assertIn("Deleted", workspace_mcp.delete_file("notes.txt"))
        self.assertFalse((self.workspace / "notes.txt").exists())
        with self.assertRaises(IsADirectoryError):
            workspace_mcp.delete_file("folder")
        with self.assertRaises(ValueError):
            workspace_mcp.delete_file(".git/config")
        with self.assertRaises(ValueError):
            workspace_mcp.delete_file(".")
        with self.assertRaises(ValueError):
            workspace_mcp.delete_file("../outside.txt")
        with self.assertRaises(FileNotFoundError):
            workspace_mcp.delete_file("missing.txt")

    def test_apply_patch_is_all_or_nothing(self):
        workspace_mcp.create_file("app.txt", "alpha beta gamma")
        workspace_mcp.apply_patch(
            "app.txt",
            [
                {"old_text": "alpha", "new_text": "one"},
                {"old_text": "gamma", "new_text": "three"},
            ],
        )
        self.assertEqual(workspace_mcp.read_file("app.txt"), "one beta three")
        with self.assertRaises(ValueError):
            workspace_mcp.apply_patch(
                "app.txt",
                [
                    {"old_text": "one", "new_text": "1"},
                    {"old_text": "missing", "new_text": "x"},
                ],
            )
        self.assertEqual(workspace_mcp.read_file("app.txt"), "one beta three")
        with self.assertRaises(ValueError):
            workspace_mcp.apply_patch("app.txt", [])

    def test_get_root_requires_explicit_workspace(self):
        self.root_patch.stop()
        try:
            with patch.dict("os.environ", {}, clear=True):
                with self.assertRaises(RuntimeError):
                    workspace_mcp.get_root()
        finally:
            self.root_patch.start()

    def test_tool_catalog_is_served_without_vscode(self):
        names = [tool["name"] for tool in workspace_mcp.TOOLS]
        self.assertEqual(len(names), 30)

    def test_file_operations_stay_in_workspace(self):
        workspace_mcp.create_file("src/app.js", "export function start() {}\n")
        workspace_mcp.create_file(".github/workflows/check.yml", "name: check\n")
        self.assertIn("export function start", workspace_mcp.read_file("src/app.js"))
        outline = workspace_mcp.get_file_outline("src/app.js")
        self.assertEqual(outline, [{"name": "start", "line": 1}])
        workspace_mcp.edit_file("src/app.js", "start", "run")
        self.assertIn("function run", workspace_mcp.read_file("src/app.js"))
        self.assertEqual(workspace_mcp.find_files(".", "*.js"), ["src/app.js"])
        self.assertEqual(
            workspace_mcp.find_files(".", "*.yml"),
            [".github/workflows/check.yml"],
        )
        self.assertEqual(len(workspace_mcp.read_files(["src/app.js"])), 1)
        with self.assertRaises(ValueError):
            workspace_mcp.read_file("../outside.txt")

    def test_copy_and_move_do_not_overwrite(self):
        (self.workspace / "source.txt").write_text("content", encoding="utf-8")
        workspace_mcp.copy_file("source.txt", "copy.txt")
        self.assertEqual((self.workspace / "copy.txt").read_text(encoding="utf-8"), "content")
        with self.assertRaises(FileExistsError):
            workspace_mcp.copy_file("source.txt", "copy.txt")
        workspace_mcp.move_file("source.txt", "moved.txt")
        self.assertFalse((self.workspace / "source.txt").exists())
        self.assertTrue((self.workspace / "moved.txt").exists())

    def test_edit_requires_unique_snippet_by_default(self):
        (self.workspace / "duplicate.txt").write_text("same same", encoding="utf-8")
        with self.assertRaises(ValueError):
            workspace_mcp.edit_file("duplicate.txt", "same", "new")
        workspace_mcp.edit_file("duplicate.txt", "same", "new", replace_all=True)
        self.assertEqual(
            (self.workspace / "duplicate.txt").read_text(encoding="utf-8"),
            "new new",
        )

    def test_symlink_traversal_does_not_leak_external_files(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("TOPSECRET\n", encoding="utf-8")
        (outside / "sub").mkdir()
        (outside / "sub" / "deep.txt").write_text("DEEPSECRET\n", encoding="utf-8")
        os.symlink(outside / "secret.txt", self.workspace / "link.txt")
        os.symlink(outside / "sub", self.workspace / "linkdir")
        os.symlink(outside, self.workspace / "linkroot")
        # Traversal (find/search/tree/list) must not surface external content.
        self.assertEqual(workspace_mcp.find_files(".", "*"), [])
        self.assertEqual(workspace_mcp.search_files(".", "TOPSECRET"), [])
        self.assertEqual(workspace_mcp.search_files(".", "DEEPSECRET"), [])
        self.assertEqual(workspace_mcp.list_tree("."), [])
        self.assertEqual(workspace_mcp.list_dir("."), [])
        # Direct read was already blocked by normalize_target; keep it so.
        with self.assertRaises(ValueError):
            workspace_mcp.read_file("link.txt")

    def test_internal_symlink_is_still_traversable(self):
        workspace_mcp.create_file("real.txt", "inside content\n")
        os.symlink(self.workspace / "real.txt", self.workspace / "alias.txt")
        self.assertIn("real.txt", workspace_mcp.find_files(".", "*.txt"))
        self.assertIn("alias.txt", workspace_mcp.find_files(".", "*.txt"))
        self.assertEqual(len(workspace_mcp.search_files(".", "inside content")), 2)

    def test_run_command_blocks_dangerous_binaries(self):
        for command in ("sudo", "/usr/bin/sudo", "dd", "mkfs.ext4", "shutdown"):
            with self.assertRaises(ValueError, msg=command):
                workspace_mcp.run_command(command, ["-h"])

    def test_run_command_timeout_bounds(self):
        with self.assertRaises(ValueError):
            workspace_mcp.run_command("echo", ["hi"], timeout=1)
        with self.assertRaises(ValueError):
            workspace_mcp.run_command("echo", ["hi"], timeout=9999)
        with self.assertRaises(ValueError):
            workspace_mcp.run_command("echo", ["hi"], timeout="soon")  # type: ignore[arg-type]
        self.assertEqual(workspace_mcp.run_command("echo", ["hi"], timeout=10), "hi")
        # Default (no timeout) still works.
        self.assertEqual(workspace_mcp.run_command("echo", ["ok"]), "ok")

    def test_run_command_rejects_non_string_args(self):
        with self.assertRaises(ValueError):
            workspace_mcp.run_command("echo", [123])  # type: ignore[list-item]
        # A bare string would otherwise be spread into one arg per character.
        with self.assertRaises(ValueError):
            workspace_mcp.run_command("echo", "hi")  # type: ignore[arg-type]

    def test_git_push_refuses_default_branch(self):
        with patch.object(
            workspace_mcp, "get_root", return_value=self.workspace
        ), patch.object(
            workspace_mcp, "_host_process", side_effect=AssertionError("must not run")
        ):
            for branch in ("main", "master"):
                with self.assertRaises(ValueError, msg=branch):
                    workspace_mcp.git_push(branch)

    def test_git_create_pr_refuses_default_head(self):
        with patch.object(
            workspace_mcp, "get_root", return_value=self.workspace
        ), patch.object(
            workspace_mcp, "_current_branch", return_value="main"
        ), patch.object(
            workspace_mcp, "_host_process", side_effect=AssertionError("must not run")
        ):
            with self.assertRaises(ValueError):
                workspace_mcp.git_create_pr("title")

    def test_git_push_rejects_option_injection(self):
        with patch.object(workspace_mcp, "get_root", return_value=self.workspace):
            with self.assertRaises(ValueError):
                workspace_mcp.git_push("--upload-pack=evil")

    def test_git_create_pr_requires_a_title(self):
        with self.assertRaises(ValueError):
            workspace_mcp.git_create_pr("   ")

    def test_git_push_uses_current_branch_when_omitted(self):
        calls = []
        with patch.object(
            workspace_mcp, "get_root", return_value=self.workspace
        ), patch.object(
            workspace_mcp, "_current_branch", return_value="feature/x"
        ), patch.object(
            workspace_mcp,
            "_host_process",
            side_effect=lambda command, timeout=0: calls.append(command) or "ok",
        ):
            workspace_mcp.git_push()
        self.assertEqual(calls, [["git", "push", "-u", "origin", "feature/x"]])

    def test_run_process_does_not_retry_unsandboxed_for_command_errors(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="Permission denied\n"
            )

        with patch.object(
            workspace_mcp, "_sandboxed_argv", return_value=["bwrap", "--", "cat"]
        ), patch.object(workspace_mcp.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(RuntimeError):
                workspace_mcp._run_process(["cat"], self.workspace)
        self.assertEqual(len(calls), 1)

    def test_run_process_never_retries_unsandboxed_on_faked_bwrap_stderr(self):
        # An attacker-controlled command can print "bwrap: ..." itself; the
        # failed sandboxed result must surface as an error, never trigger a
        # full-host re-run.
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="bwrap: setting up uid map: denied\n"
            )

        with patch.object(
            workspace_mcp, "_sandboxed_argv", return_value=["bwrap", "--", "sh"]
        ), patch.object(workspace_mcp.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(RuntimeError):
                workspace_mcp._run_process(["sh"], self.workspace)
        self.assertEqual(len(calls), 1)

    def test_sandboxed_argv_binds_host_root_before_proc_and_dev(self):
        argv = workspace_mcp._sandboxed_argv(["true"], Path("/tmp"))
        self.assertTrue(argv[0].endswith("bwrap"))
        root_bind = argv.index("--ro-bind")
        proc = argv.index("--proc")
        dev = argv.index("--dev")
        # Host root first: fresh /proc and /dev mounts placed before it would
        # be shadowed by the read-only root bind and leak host PIDs.
        self.assertLess(root_bind, proc)
        self.assertLess(proc, dev)
        self.assertIn("--clearenv", argv)
        self.assertIn("--tmpfs", argv)
        # Secrets must not ride through: env is an explicit allowlist.
        self.assertLess(argv.index("--clearenv"), argv.index("--setenv"))


if __name__ == "__main__":
    unittest.main()
