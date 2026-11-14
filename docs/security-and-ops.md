# Security & operations

How the workspace MCP server and the bridge keep a local agent session
inside one folder, what they deliberately do **not** protect against, and
how to diagnose a running bridge.

## Workspace containment

`workspace_mcp.py` scopes every tool to `ZS_WORKSPACE_ROOT`
(or `MCP_ROOT`). The root is resolved once in `get_root()`; it must exist
and be a directory, otherwise every tool fails with a clear error. Tools
never guess a root.

Every path a tool touches goes through `normalize_target()`: the path is
resolved (symlinks included) and must stay lexically under the root.
That blocks `../outside.txt` and a direct symlink target outside the root.

Traversal helpers (`_iter_files`, `list_tree`, `list_dir`,
`get_project_info`) additionally resolve **every entry** before listing
or reading it (`_inside_root()`):

- `os.walk(..., followlinks=False)` never descends into a symlinked
  directory.
- A symlinked *file* pointing outside the root is skipped by
  `find_files`/`search_files` — previously `search_files` would read it
  and leak external content (for example `/etc/...` or `~/.ssh/...`).
- A symlink whose target resolves *inside* the root stays usable.

Residual risk: resolve-then-use is not atomic (TOCTOU). An attacker who
can swap a symlink between the check and the read, inside a workspace
they already can write to, is outside this threat model — they already
have write access to the workspace.

## `run_command` policy

`run_command` is arbitrary command execution **by design** (that is what
the tool is for), so `shell=False` on POSIX: the command is one argv
entry, args are separate entries, never a shell string.

On top of that, defense-in-depth (not a security boundary):

- Exact-basename deny list, checked before execution: `sudo`, `su`,
  `doas`, `pkexec`, `dd`, `mkfs*`, `fdisk`, `parted`, `shutdown`,
  `reboot`, `halt`, `poweroff`, `init`. `/usr/bin/sudo` is also blocked
  (basename comparison). This stops the common footguns, not a determined
  bypass (`python -c ...` can still do anything the user can).
- `timeout` is optional, integer seconds, bounded to **5–300**
  (default 30). Output is capped at 200 KB.
- On Windows (`cmd.exe /c`, which reintroduces a shell), shell
  metacharacters (`& | ; < > \``) in command or args are rejected.

`chmod` is intentionally **not** blocked: `chmod +x` on a script is a
legitimate workspace operation.

Real isolation for untrusted work is a separate OS user, container, or
VM — not this denylist.

## Sandbox (`bubblewrap`)

`run_command`, the `git_*` helpers, `run_project_script` and `run_tests` all
execute inside `bubblewrap` when it is usable on the host (`workspace_mcp.py`):

- **Namespaces:** user, IPC, PID, UTS and cgroup are unshared; the network
  namespace is unshared unless `ZS_SANDBOX_NETWORK=1`.
- **Capabilities:** every Linux capability is dropped (`--cap-drop ALL`).
- **Filesystem:** the host root is bind-mounted read-only and only the
  workspace root plus an ephemeral `/tmp` are writable. `/home`, `/root`,
  `/mnt` and `/media` are masked with tmpfs, and the environment is an explicit
  allowlist (`--clearenv`), so host secrets and the bridge token never reach
  the command.
- **Lifetime:** `--die-with-parent` and `--new-session`.

**Fail closed.** A missing or unusable `bwrap` raises instead of falling back
to unsandboxed execution. `ZS_ALLOW_UNSANDBOXED=1` restores the old unsandboxed
fallback for hosts where unprivileged user namespaces are disabled — without it
every sandboxed tool would simply be dead there.

`_bwrap_usable()` probes the real flag set (`--unshare-all --cap-drop ALL
--new-session`), not a trivial `--ro-bind`, so a host that only appears to work
cannot silently disable the sandbox.

Output is drained in capped chunks (200 KB) rather than buffered whole, so a
runaway command cannot exhaust memory before the cap is applied; a command that
keeps producing output past its `timeout` is killed.

## Bridge handshake auth

The bridge listens on loopback, but any local process and any web page can reach
`ws://127.0.0.1:17613`, so the handshake is checked before a client is accepted
(`bridge_auth.py`):

- **Origin allowlist.** Only `chrome-extension://`, `moz-extension://` and
  `safari-web-extension://` origins, plus clients that send no `Origin` at all
  (native processes), get through. Anything else closes with `1008`.
- **Per-install token.** `~/.snapgent/bridge_token` (mode `600`, directory `700`),
  generated on first run and compared in constant time. It is required from
  clients that send no `Origin` (the bridge cannot tell those apart from any other
  local process) and from everything when `ZS_BRIDGE_AUTH=strict` is set; a browser
  extension is accepted on the allowlist alone so pairing needs no copy-paste.
  A connection that should have presented it closes with `1008`.
  `ZS_BRIDGE_AUTH=0` turns only this check off, never the allowlist.
  `ZS_BRIDGE_TOKEN` pins the value and disables rotation.
- **Rotation revokes sessions.** `rotate_token` writes the new secret, answers
  with it, then closes every authenticated socket (`1012`) so a leaked token
  cannot keep a live connection.

Pairing, rotation, and the close codes are written up in
[`linux-stability.md`](linux-stability.md#bridge-auth-troubleshooting).

## Bridge resilience

`bridge.py` spawns each configured MCP server as an asyncio subprocess
(`asyncio.create_subprocess_exec`) and speaks MCP over its stdio pipes.
The bridge event loop therefore never blocks on workspace I/O — that
I/O happens in the subprocess.

Dead subprocess recovery, two layers:

1. **Monitor** (`_monitor_servers`): every 5 s, any server that is not
   alive is restarted. Consecutive failures back off exponentially —
   5 s, 10 s, 20 s, … capped at 300 s (`MCPServer.restart_attempts` /
   `next_restart_at`). A successful start resets the counter.
2. **Inline restart** (`Bridge._call_tool`): if a tool call resolves to
   no live server, the bridge tries one restart (prefixed server
   `name/tool`, the extension-selected server, or every configured
   server) before returning `unknown_tool`.

`restart_mcp` and the `add_server`/`remove_server` requests still do a
full stop+start of all servers.

## Diagnosing a running bridge

Send `{"type": "get_bridge_status"}` over the bridge WebSocket (this is a
bridge request, not a workspace tool — the workspace subprocess cannot
see bridge clients or PIDs). Response `{"type": "bridge_status",
"status": {...}}` contains:

- `bridge_version`, `host`, `port`, `uptime_seconds`, `started_at`
- `clients_connected`, `tools_available`
- `servers[]`: `id`, `alive`, `ready`, `tools`, `pid`, `error`,
  `restart_attempts`, `next_restart_in_seconds`, `rss_kib`
  (RSS from `/proc/<pid>/status` on Linux, `null` elsewhere)
- `log_tail`: last 30 lines of `logs/bridge_debug.log`

`_server_status()` (used in the `connected` broadcast) carries the same
per-server fields minus RSS/log tail.

## Logging

Default log format is plain text: `2026-11-04 22:06:35 message` in
`logs/bridge_debug.log`, so `tail`/`grep` keep working.

Structured JSON logging is opt-in:

```bash
ZS_BRIDGE_LOG_JSON=1 python3 bridge.py
```

The bridge config path can be overridden with `ZS_BRIDGE_CONFIG`
(default `config.json` next to `bridge.py`). `e2e/e2e_live.py`
uses a temporary config this way, because `config.json` is gitignored
and a CI checkout has no configured servers otherwise.

Each line becomes one JSON object: `{"ts": "...", "level": "INFO",
"message": "..."}`. Do not enable it if anything downstream parses the
text format.

## Tests & CI

| Suite | Command | What it proves |
| --- | --- | --- |
| Python unit | `/usr/bin/python3 -m pytest -q` | workspace tools, symlink guard, `run_command` policy, bridge stdio, backoff, status |
| Extension (Node) | `node snapgent-extension/test-parser.js` etc. | parser + provider DOM reading against a stub DOM, no npm install |
| Live e2e | `/usr/bin/python3 e2e/e2e_live.py` | real bridge + real subprocess on an ephemeral port: tools, symlink, policy, write/edit/delete, kill → inline restart, kill → monitor restart |

GitHub Actions (`.github/workflows/tests.yml`) runs the Python and Node
suites on every push/PR, plus the live e2e as a separate job. Path
labels (`.github/labeler.yml`) cover `bridge`, `extension`,
`documentation`, `security`, `ci`, and `tests`.

Note on the async question: `workspace_mcp.py` is a synchronous stdio
server, so one slow tool call (for example a full-repo search) serializes
later calls on the *same* server. Making it concurrent would need a
thread dispatch with a stdout lock and a mutation-ordering policy; that
is a separate design decision, not an `asyncio.to_thread` fix in the
bridge.
