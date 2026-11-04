# Linux stability notes

## Patched in this branch

- `bridge.py`: MCP startup timeouts increased:
  - `initialize`: 12s -> 30s
  - `tools/list`: 20s -> 30s
- Reason: cold starts for `npx`-based MCP servers (for example `@modelcontextprotocol/server-filesystem`) can exceed the previous 12s limit on Linux.
- `bridge.py`: WebSocket `Origin` allowlist plus a per-install token, and `vscode_mcp.py`
  `tools/list` now advertises the catalog whenever a workspace root resolves (including
  `ZS_WORKSPACE_ROOT`) instead of requiring a VS Code window. See "Bridge auth" below.

## Targets verified

- `repo`: 28 tools (stable Python MCP server, no npx dependency)
- `vscode`: 28 tools (same backend)
- Bridge endpoint: `ws://127.0.0.1:17613`

## Bridge auth (troubleshooting)

The bridge refuses a connection before it accepts it when the handshake comes from a
web page or lacks the per-install token. Both cases close with **`1008` (policy
violation)**, so the extension can tell them apart from "bridge not running".

- **Where the token lives**: `~/.snapgent/bridge_token` (mode `600`, directory `700`),
  generated on the bridge's first run. It is printed once in the bridge window as
  `bridge token: …`; nothing else ever logs the value.
- **Pairing**: open the Snapgent popup. When the bridge refuses the connection the
  status reads `Bridge token required` and a token field appears. Paste the token,
  press Save; the extension keeps it in `chrome.storage.local` under `bridgeToken`
  and reconnects.
- **Forgetting / rotating**: `🗝 Forget saved token` in the popup clears the stored
  copy and prompts again. `🔑 Rotate token` (shown while connected) asks the bridge to
  generate a new secret and write it to the token file. The bridge replies with the
  new token first, then **closes every authenticated connection** (close code `1012`,
  "service restart") so no session keeps running on the old secret. The extension
  saves the token before the socket drops and reconnects with it; while it dials back
  in the popup reads `Token rotated · reconnecting` rather than `Bridge offline`,
  because a `1012` close is not a pairing failure. It returns to `Connected` within
  a second or two. A raw client must reconnect with the token it received in the
  reply. Rotation is refused while `ZS_BRIDGE_TOKEN` pins the value. To rotate
  by hand, delete `~/.snapgent/bridge_token` and restart the bridge, then pair again.
- **Check without starting**: `bash Linux_Start.sh --check` prints the Python and
  `websockets` versions, the auth mode, the token path and the endpoint, then exits
  without installing anything or starting the bridge.
- **Overrides**: `ZS_BRIDGE_TOKEN` pins an exact token, `ZS_BRIDGE_TOKEN_FILE` moves
  the file, and `ZS_BRIDGE_AUTH=0` (`off`, `false`, `no`) disables token auth — the
  origin allowlist stays active either way.

### Close code 1008

`logs/bridge_debug.log` (and the bridge window) names the reason:

- `rejected client from origin 'https://…'` — a web page tried to open the socket.
  Only `chrome-extension://` origins, and clients that send no `Origin` at all, are
  accepted. Nothing to fix; this is the protection working.
- `rejected client with a missing or invalid bridge token` — the extension is not
  paired yet, or the token was rotated since it was saved. Paste the current token
  in the popup.

Repeated rejections are logged at most once per minute, with a
`(N more since the last report)` suffix, so an unpaired extension retrying every few
seconds does not flood the log. A status of `Bridge offline` (rather than `Bridge
token required`) means the bridge is not running: start it with
`bash Linux_Start.sh` (`start.bat` on Windows).

## Workflow

1. Work on `linux-stability`, never `main`.
2. Before changing: `git fetch upstream && git merge upstream/main`
3. Run `python3 -m unittest`
4. Run `bash Linux_Start.sh` smoke test.
5. Commit only after both pass.
