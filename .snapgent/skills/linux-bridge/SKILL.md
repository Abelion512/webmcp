---
name: linux-bridge
description: Use when building, running, configuring, or debugging the Linux scoped MCP workspace server and its WebSocket bridge.
---

# Linux bridge skill

Builds, runs, configures, and debugs the Linux scoped MCP workspace server
(`workspace_mcp.py`) and its WebSocket bridge (`bridge.py`).

## Build

No build step. `python3 -m unittest` is the check.

```
python3 -m unittest
```

## Run

```
bash Linux_Start.sh
```

The bridge listens on `ws://127.0.0.1:17613`. Keep the terminal open while
using the agent.

## Configure a client

Register the workspace MCP server with `ZS_WORKSPACE_ROOT` set:

```json
{
  "mcpServers": {
    "repo": {
      "command": "/usr/bin/python3",
      "args": ["/path/to/webmcp/workspace_mcp.py"],
      "env": { "ZS_WORKSPACE_ROOT": "/path/to/project" }
    }
  }
}
```

Without `ZS_WORKSPACE_ROOT`, `initialize` succeeds but every tool call returns
a clear error.

## Add a target

Edit `config.json` `mcpServers` (written by `setup-config.py`, ignored by git).

## Sync upstream

```
bash sync-upstream.sh
```

Conflict policy: purged artifacts (the deleted Windows/macOS files) stay
deleted; renamed files (`vscode_mcp.py` -> `workspace_mcp.py`) must be resolved
by hand; everything else merges normally.

## Troubleshoot

- Missing `ZS_WORKSPACE_ROOT`: tool calls fail with
  "Configured workspace root is not set."
- Port already in use by a non-bridge process: `Linux_Start.sh` refuses; set
  `ZS_BRIDGE_PORT` or close the other process.
- Server listed with 0 tools: the workspace root is not set, or `bridge.py`
  failed to start `workspace_mcp.py`. Check `logs/bridge_debug.log`.
