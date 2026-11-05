# Design: Linux-only scoped MCP workspace server

Date: 2026-11-04
Branch: `linux-stability`

## Problem

`snapgent` ships a workspace MCP server (`vscode_mcp.py`) plus a local WebSocket
bridge (`bridge.py`) and a browser extension. The server is coupled to VS Code,
Roblox Studio, Effect Craft and Windows/macOS binaries. On Linux those couplings
are dead weight and actively harmful:

- `vscode_mcp.py:647` returns an empty tool list unless a VS Code window is
  detected, so the server is unusable from Zed, Orca, Antigravity, Claude Code
  or Hermes.
- `setup-config.py` spends ~150 lines probing Roblox Studio and Effect Craft.
- `bridge.py` polls Roblox Studio state on every connection.
- `bridge.exe`, `vscode_mcp.exe`, `start.bat`, `setup-config.ps1`,
  `start-effectcraft.ps1` are Windows-only build/launch artifacts.

## Goal

One stdlib-only Python MCP server that any MCP client can register, scoped to
one directory, plus a WebSocket bridge that lets a web AI chat drive it. Linux
only. No VS Code, no Roblox, no Effect Craft, no Windows binaries.

## Scope

In scope:

- `workspace_mcp.py` (renamed from `vscode_mcp.py`): generic MCP file server.
- `bridge.py`: WebSocket bridge with no Roblox coupling.
- `setup-config.py`: Linux-only, writes a `repo` server entry.
- `Linux_Start.sh`: launcher wording.
- Deletion of Windows/macOS-only artifacts.
- `sync-upstream.sh` plus the conflict policy.
- Docs and a `.snapgent/skills/` entry.

Out of scope:

- The browser extension's provider adapters (ChatGPT, Claude, Gemini, Qwen,
  Kimi, GLM, Mistral, Meta AI, Grok, Arena, Ptero). They stay.
- `snapgent-app/` (Electron app). Inert on Linux, upstream's active work.
- Removing Roblox strings from the extension. Inert when no Roblox server is
  configured.

## Components

### 1. `workspace_mcp.py`

MCP stdio server, protocol `2024-11-05`, 28 tools unchanged (read, write, edit,
search, git). One change in behavior:

- The workspace root comes from `ZS_WORKSPACE_ROOT` (falling back to
  `MCP_ROOT`). If neither is set, `initialize` still succeeds but every tool
  call returns a clear error. The server never guesses a root.
- `tools/list` always returns the full catalog. The VS Code liveness gate is
  deleted.

Deleted: `_vscode_active_folder`, `_vscode_storage_paths`, the Windows
`tasklist` branch.

Interface (what a client registers):

```json
{
  "command": "/usr/bin/python3",
  "args": ["/path/to/workspace_mcp.py"],
  "env": { "ZS_WORKSPACE_ROOT": "/path/to/project" }
}
```

### 2. `bridge.py`

WebSocket server on `127.0.0.1:17613`. Keeps: `ping`, `list_tools`,
`call_tool`, `restart_mcp`, `add_server`, `remove_server`, the server-qualified
tool catalog, the 5s restart monitor.

Removed: `studio_status` request, `_studio_status`, `_process_running`, the
`roblox` protection in `_change_server`, and the `vscode` removal guard.

### 3. `setup-config.py`

Linux-only. Writes `config.json`:

```json
{
  "mcpServers": {
    "repo": {
      "command": "<sys.executable>",
      "args": ["<ROOT>/workspace_mcp.py"],
      "env": { "ZS_WORKSPACE_ROOT": "<workspace root>" }
    }
  }
}
```

`ZS_WORKSPACE_ROOT` for the generated entry comes from the environment, else
the current directory of the launcher. Removed: all Roblox and Effect Craft
discovery, the port probe, the process probe.

### 4. Sync with upstream

`sync-upstream.sh`:

1. `git fetch upstream`
2. `git merge upstream/main`
3. Re-apply the purged paths (`git rm` on the deleted-artifact list)
4. Report conflicts instead of guessing when a renamed file diverges

Conflict policy, documented in the script header:

- Purged artifacts: our deletion wins, always.
- Renamed files (`vscode_mcp.py` -> `workspace_mcp.py`): manual resolution.
  Upstream changes to `vscode_mcp.py` must be ported by hand.
- Everything else: normal merge.

## Data flow

```
CLI agent (Hermes / Claude Code)  ──stdio MCP──>  workspace_mcp.py  ──>  scoped folder
IDE (Zed / Orca / Antigravity)    ──stdio MCP──>  workspace_mcp.py  ──>  scoped folder
Web AI chat                       ──extension──>  bridge.py  ──stdio MCP──>  workspace_mcp.py
```

## Error handling

- Missing `ZS_WORKSPACE_ROOT`: tool calls fail with
  `Configured workspace root is not set. Set ZS_WORKSPACE_ROOT to the project folder.`
- Path escaping the root: `ValueError: Path escapes workspace root: <path>` (unchanged).
- Port already in use by a non-bridge process: `Linux_Start.sh` refuses (unchanged).

## Testing

Existing suites, kept green: `test_bridge.py`, `test_setup_config.py`,
`test_vscode_mcp.py` (renamed to `test_workspace_mcp.py`). Run with
`python3 -m unittest`.

New assertions:

- `workspace_mcp`: `tools/list` returns 28 tools with no VS Code present.
- `workspace_mcp`: `get_root()` raises when `ZS_WORKSPACE_ROOT` is unset.
- `setup-config`: writes a `repo` entry carrying `ZS_WORKSPACE_ROOT`.
- `bridge`: no `studio_status` in the request dispatch table.

## Risks

- Renaming files guarantees `modify/delete` merge conflicts on every upstream
  release that touches them. See the sync policy above.
