# Plan: Linux-only scoped MCP workspace server

Spec: `docs/linux-mcp-design.md` (binding authority)
Branch: `linux-stability`

## Global Constraints

- `workspace_mcp.py` is Python 3.9+ standard library only; no new dependencies.
  (`bridge.py` uses the pre-existing `websockets` package.)
- MCP protocol version string stays `2024-11-05`.
- 28 tools, exactly, with names unchanged. Tool names are a public contract.
- Workspace root comes only from `ZS_WORKSPACE_ROOT` or `MCP_ROOT`.
- All paths stay inside the root; `normalize_target` keeps its current
  escaping semantics.
- Never work on `main`. Never merge to `main` in this plan.
- Run `python3 -m unittest` after every task. All tests green before commit.
- Commit message format: `<type>: <summary>` with type in
  feat, fix, docs, refactor, test, chore. Max 72 char summary.
- Delete, do not deprecate. This is a fork, not a compatibility layer.

## Purged paths

These files are deleted in Task 5 and must never be restored by a sync:

```
bridge.exe
vscode_mcp.exe
vscode_mcp.spec
start.bat
setup-config.ps1
start-effectcraft.ps1
MacOS_Start.command
```

---

## Task 1: Rename the MCP server module and de-brand it

Files: `vscode_mcp.py` -> `workspace_mcp.py`, `test_vscode_mcp.py` ->
`test_workspace_mcp.py`, `.gitattributes` (unchanged, verification only).

Steps:

1. `git mv vscode_mcp.py workspace_mcp.py`
2. `git mv test_vscode_mcp.py test_workspace_mcp.py`
3. In `test_workspace_mcp.py`, change `import vscode_mcp` to
   `import workspace_mcp` and every `vscode_mcp.` reference to `workspace_mcp.`.
4. In `workspace_mcp.py`:
   - `serverInfo.name` from `"vscode-mcp"` to `"workspace-mcp"`.
   - `get_workspace_info` description from
     `"Get the active VS Code workspace name, path, and Git status."` to
     `"Get the workspace name, path, and Git status."`
   - The `RuntimeError("Open a project folder in VS Code before using the VS
     Code MCP server.")` message from Task 2's replacement text (leave as-is
     here; Task 2 rewrites it).

Verification:

- `python3 -m unittest test_workspace_mcp -v` — all pass.
- `python3 -m unittest` — all pass.
- `grep -rn "import vscode_mcp" .` returns nothing.

Commit: `refactor: rename vscode_mcp module to workspace_mcp`

---

## Task 2: Make the workspace root explicit

Files: `workspace_mcp.py`, `test_workspace_mcp.py`.

Delete these functions entirely:

- `_vscode_active_folder`
- `_vscode_storage_paths`

Delete the Windows `tasklist` branch inside `_vscode_active_folder` (goes with
the function).

`get_root()` becomes:

```python
def get_root() -> Path:
    env_root = os.environ.get("ZS_WORKSPACE_ROOT") or os.environ.get("MCP_ROOT")
    if not env_root:
        raise RuntimeError(
            "Configured workspace root is not set. Set ZS_WORKSPACE_ROOT to the "
            "project folder."
        )
    candidate = Path(env_root).expanduser().resolve()
    if not candidate.is_dir():
        raise RuntimeError(f"Configured workspace root does not exist: {env_root}")
    return candidate
```

In `handle_request`, the `tools/list` branch drops the VS Code gate:

```python
    if method == "tools/list":
        send_message({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
        return
```

In `test_workspace_mcp.py`:

- Delete `test_vscode_storage_paths_include_macos_installations` and
  `test_vscode_storage_paths_include_linux_installations`.
- Add:

```python
    def test_get_root_requires_explicit_workspace(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(RuntimeError):
                workspace_mcp.get_root()
```

  Note: `setUp` patches `get_root`, so call the real function through
  `workspace_mcp.get_root.__wrapped__` is not available. Instead stop the
  patch inside the test:

```python
    def test_get_root_requires_explicit_workspace(self):
        self.root_patch.stop()
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(RuntimeError):
                workspace_mcp.get_root()
        self.root_patch.start()
```

- Add:

```python
    def test_tool_catalog_is_served_without_vscode(self):
        names = [tool["name"] for tool in workspace_mcp.TOOLS]
        self.assertEqual(len(names), 28)
```

Verification: `python3 -m unittest` — all pass.
`grep -rn "_vscode_active_folder\|storage.json\|tasklist" workspace_mcp.py`
returns nothing.

Commit: `refactor: require explicit workspace root in workspace_mcp`

---

## Task 3: Rewrite setup-config.py for Linux only

Files: `setup-config.py`, `test_setup_config.py`.

Delete: `_effectcraft_cli`, `_roblox_studio_mcp`, `_effectcraft_app`,
`_port_listening`, `_effectcraft_running`, `_start_effectcraft_control`,
`EFFECTCRAFT_PORT`, `_first_executable`, `_is_executable` (keep if still used
by nothing else — delete if not), the `time` import, the `socket` import.

Keep: `_load_config`, `_set_server`, `main`.

Rename the module constant `VSCODE_MCP` to `WORKSPACE_MCP` pointing at
`ROOT / "workspace_mcp.py"`.

`_set_server` gains an `env` parameter:

```python
def _set_server(
    servers: Dict[str, object], server_id: str, command: str, args: List[str],
    env: Optional[Dict[str, str]] = None,
) -> None:
    servers[server_id] = {"command": command, "args": args, "env": env or {}}
```

`main()` becomes:

```python
def main() -> int:
    if sys.platform != "linux":
        print("ERROR: setup-config.py supports Linux only.", file=sys.stderr)
        return 1
    try:
        configuration = _load_config()
        servers = configuration["mcpServers"]
        assert isinstance(servers, dict)

        root = os.environ.get("ZS_WORKSPACE_ROOT") or os.getcwd()
        _set_server(
            servers, "repo", sys.executable, [str(WORKSPACE_MCP)],
            {"ZS_WORKSPACE_ROOT": str(Path(root).expanduser().resolve())},
        )
        print(f"workspace MCP configured: {WORKSPACE_MCP} (root: {root})")

        for stale in ("vscode", "roblox", "effectcraft"):
            servers.pop(stale, None)

        CONFIG_PATH.write_text(
            json.dumps(configuration, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return 0
    except (OSError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
```

`test_setup_config.py` — replace the three tests with:

```python
    def test_linux_setup_writes_repo_entry_with_workspace_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            with patch.object(setup_config.sys, "platform", "linux"), patch.object(
                setup_config, "CONFIG_PATH", config_path
            ), patch.dict(
                os.environ, {"ZS_WORKSPACE_ROOT": temp_dir}, clear=False
            ):
                result = setup_config.main()
            self.assertEqual(result, 0)
            config = json.loads(config_path.read_text(encoding="utf-8"))
        entry = config["mcpServers"]["repo"]
        self.assertEqual(entry["args"], [str(setup_config.WORKSPACE_MCP)])
        self.assertEqual(entry["env"]["ZS_WORKSPACE_ROOT"], temp_dir)

    def test_non_linux_setup_is_refused(self):
        with patch.object(setup_config.sys, "platform", "darwin"):
            self.assertEqual(setup_config.main(), 1)
```

Verification: `python3 -m unittest test_setup_config -v` — pass.
`grep -rn "roblox\|effectcraft\|studio" setup-config.py` returns nothing.

Commit: `refactor: make setup-config.py Linux-only with explicit workspace root`

---

## Task 4: Remove Roblox coupling from the bridge

Files: `bridge.py`, `test_bridge.py`.

Delete: `_studio_status`, `_process_running`, `subprocess_run` (only used by
`_process_running`), the `studio_status` branch in `_handle_request`, the
`roblox` guard in `_change_server`, the `vscode` guard in `_change_server`, the
`subprocess` import if unused.

In `_handle_request`, delete the `studio_status` branch. In
`_connected_message`, delete `studio`, `studio_app`, `studio_proc` if present.

In `_change_server`:

```python
        if (
            not isinstance(server_id, str)
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", server_id)
        ):
```

and the `remove` branch drops the `vscode` guard:

```python
            if remove:
                servers.pop(server_id, None)
```

In `test_bridge.py`, `test_duplicate_tool_names_are_server_qualified` uses
server ids `roblox` and `effectcraft`; change them to `alpha` and `beta` and
update the expected catalog to `["alpha/get_state", "beta/get_state"]`.

Verification: `python3 -m unittest test_bridge -v` — pass.
`grep -n "studio\|roblox\|Roblox" bridge.py` returns nothing.

Commit: `refactor: drop Roblox Studio coupling from the bridge`

---

## Task 5: Delete Windows and macOS artifacts

Files: deleted paths (see Purged paths), `.gitignore`, `Linux_Start.sh`,
`README.md` (only the lines naming deleted files).

Steps:

1. `git rm bridge.exe vscode_mcp.exe vscode_mcp.spec start.bat setup-config.ps1 start-effectcraft.ps1 MacOS_Start.command`
2. `.gitignore`: add the Purged paths block and add `workspace_mcp.exe`.
3. `Linux_Start.sh`:
   - `[3/4] Detecting VS Code, Roblox Studio, and EffectCraft...` becomes
     `[3/4] Writing the MCP configuration...`
   - the missing-file check stays.
4. README: remove or rewrite any sentence naming `start.bat`, `bridge.exe`,
   `vscode_mcp.exe`, `setup-config.ps1`, Roblox or Effect Craft as required.
   Do not rewrite the whole README.

Verification:

- `python3 -m unittest` — all pass.
- `git ls-files | grep -E "\.exe$|\.ps1$|\.bat$|\.spec$|MacOS_Start"` returns
  nothing.
- `bash -n Linux_Start.sh` — clean.

Commit: `chore: delete Windows and macOS build artifacts`

---

## Task 6: Add the upstream sync script

Files: `sync-upstream.sh` (new, executable).

> This script performs a merge, which is a shared-history operation. It never
> pushes. It stops and reports on conflict instead of guessing.

```bash
#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Sync this Linux fork with upstream, keeping the purged paths deleted.
#
# Conflict policy:
#   - Purged artifacts (deleted in this fork): our deletion wins, always.
#   - Renamed files (vscode_mcp.py -> workspace_mcp.py): resolve by hand.
#     Port upstream changes from vscode_mcp.py into workspace_mcp.py.
#   - Everything else: normal merge.
set -u

PURGED=(
  bridge.exe
  vscode_mcp.exe
  vscode_mcp.spec
  start.bat
  setup-config.ps1
  start-effectcraft.ps1
  MacOS_Start.command
)

if ! git diff-index --quiet HEAD --; then
  echo "ERROR: working tree is dirty. Commit or stash first."
  exit 1
fi

git fetch upstream || exit 1

if ! git merge --no-edit upstream/main; then
  echo "Merge stopped. Resolve conflicts, then re-run this script."
  exit 1
fi

for path in "${PURGED[@]}"; do
  if git ls-files --error-unmatch "$path" >/dev/null 2>&1; then
    git rm -q --force "$path"
    echo "re-purged $path"
  fi
done

if ! git diff-index --quiet HEAD --; then
  git commit -m "chore: re-purge upstream-renamed artifacts"
fi

python3 -m unittest
echo "Sync complete. Run 'bash Linux_Start.sh' as the smoke test."
```

Verification:

- `bash -n sync-upstream.sh` — clean.
- `test -x sync-upstream.sh` — true.
- `git log --oneline -1` unchanged after a `bash -n` only run (no accidental
  execution).

Commit: `feat: add upstream sync script with purge policy`

---

## Task 7: Update documentation

Files: `docs/linux-stability.md`, `CHANGELOG.md`, `README.md` (pointer only).

Steps:

1. `docs/linux-stability.md`:
   - Replace the "Targets verified" section: targets are now `repo`
     (28 tools) and bridge endpoint `ws://127.0.0.1:17613`.
   - Add a "Client configuration" section with the `ZS_WORKSPACE_ROOT` JSON
     block, and the client keys: Zed uses `context_servers`, Antigravity and
     Claude Code use `mcpServers`, Hermes uses its own MCP list.
   - Keep the Workflow section, and add step 0:
     `0. Run bash sync-upstream.sh to merge upstream/main.`
2. `CHANGELOG.md`: add an entry at the top for this change set under the
   current unreleased heading, or create `## Unreleased` if absent, listing:
   module rename, explicit workspace root, VS Code/Roblox/Effect Craft removal,
   Windows/macOS artifact deletion, sync script.
3. `README.md`: replace the VS Code-only framing sentence with one that names
   the client-agnostic MCP server, and add a pointer to
   `docs/linux-mcp-design.md`. Do not rewrite the whole README.

Verification:

- `python3 -m unittest` — all pass.
- `grep -c "ZS_WORKSPACE_ROOT" docs/linux-stability.md` is at least 1.
- Every command shown in the docs runs.

Commit: `docs: document the Linux MCP server and upstream sync`

---

## Task 8: Add the build/use skill

Files: `.snapgent/skills/linux-bridge/SKILL.md` (new).

Front matter matches the existing `.snapgent/skills/commit-message/SKILL.md`
shape:

```markdown
---
name: linux-bridge
description: Use when building, running, configuring, or debugging the Linux
  scoped MCP workspace server and its WebSocket bridge.
---
```

Body sections, each short and runnable:

- **Build** — no build step; `python3 -m unittest` is the check.
- **Run** — `bash Linux_Start.sh`, bridge on `ws://127.0.0.1:17613`.
- **Configure a client** — the JSON block, and the `ZS_WORKSPACE_ROOT`
  requirement.
- **Add a target** — edit `config.json` `mcpServers`.
- **Sync upstream** — `bash sync-upstream.sh`, plus the conflict policy.
- **Troubleshoot** — missing root, port in use, server listed with 0 tools.

Verification: file exists, front matter parses, every command in it runs.

Commit: `docs: add linux-bridge skill for build and use`

---

## Task 9: Final review

Files: none.

Steps:

1. Whole-branch diff review over the branch's merge base.
2. Confirm: 28 tools, no VS Code/Roblox/Effect Craft reference outside the
   extension, no tracked `.exe`/`.ps1`/`.bat`.
3. `python3 -m unittest` green.
4. Report findings; fix in one dispatch if any.
