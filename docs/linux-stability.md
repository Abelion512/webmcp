# Linux stability notes

## Patched in this branch

- `bridge.py`: MCP startup timeouts increased:
  - `initialize`: 12s -> 30s
  - `tools/list`: 20s -> 30s
- Reason: cold starts for `npx`-based MCP servers (for example `@modelcontextprotocol/server-filesystem`) can exceed the previous 12s limit on Linux.

## Targets verified

- `vscode`: 28 tools
- `repo-fs`: 14 tools (uses `npx --prefer-offline`)
- Bridge endpoint: `ws://127.0.0.1:17613`

## Workflow

1. Work on `linux-stability`, never `main`.
2. Before changing: `git fetch upstream && git merge upstream/main`
3. Run `python3 -m unittest`
4. Run `bash Linux_Start.sh` smoke test.
5. Commit only after both pass.
