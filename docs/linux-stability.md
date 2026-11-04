# Linux stability notes

## Patched in this branch

- `bridge.py`: MCP startup timeouts increased:
  - `initialize`: 12s -> 30s
  - `tools/list`: 20s -> 30s
- Reason: cold starts for `npx`-based MCP servers (for example `@modelcontextprotocol/server-filesystem`) can exceed the previous 12s limit on Linux.

## Targets verified

- `repo`: 28 tools (stable Python MCP server, no npx dependency)
- Bridge endpoint: `ws://127.0.0.1:17613`

## Client configuration

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

Client keys: Zed uses `context_servers`; Antigravity and Claude Code use
`mcpServers`; Hermes uses its own MCP list.

## Workflow

0. Run `bash sync-upstream.sh` to merge upstream/main.
1. Work on `linux-stability`, never `main`.
2. Before changing: `git fetch upstream && git merge upstream/main`
3. Run `python3 -m unittest`
4. Run `bash Linux_Start.sh` smoke test.
5. Commit only after both pass.
