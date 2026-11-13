#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -u

# Switch the workspace MCP server to another project WITHOUT restarting the
# bridge: rewrites config.json and asks the running bridge to restart its
# MCP servers (restart_mcp), which re-reads config.json.
#
# Usage:
#   bash switch-project.sh CyClops
#   bash switch-project.sh /path/to/project
#   bash switch-project.sh ../CyClops

SCRIPT_DIR="$(cd -- "$(dirname "$0")" && pwd)"
ZS_TERMINAL_WORKDIR="${PWD}"
arg="${1:-}"

if [ -z "$arg" ]; then
    echo "Usage: bash switch-project.sh <project name or path>"
    exit 1
fi

if [ -d "$arg" ]; then
    root="$arg"
else
    BASES=("${ZS_PROJECTS_ROOT:-}" "$SCRIPT_DIR/.." "$ZS_TERMINAL_WORKDIR")
    found=""
    for base in "${BASES[@]}"; do
        [ -z "$base" ] && continue
        if [ -d "$base/$arg" ]; then found="$base/$arg"; break; fi
    done
    if [ -z "$found" ]; then
        echo "  ERROR: project not found: $arg"
        echo "  Looked in: ${BASES[*]}"
        exit 1
    fi
    root="$found"
fi

requested_root="$root"
if ! root="$(cd -- "$requested_root" 2>/dev/null && pwd)"; then
    echo "  ERROR: cannot enter project folder: $requested_root"
    exit 1
fi
echo "  Switching workspace root to: $root"

# Prefer the local environment the launcher creates when the system Python
# cannot install websockets; otherwise fall back to the system interpreter.
PY=""
if [ -x "$SCRIPT_DIR/.venv-snapgent/bin/python" ]; then
    PY="$SCRIPT_DIR/.venv-snapgent/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PY="python3"
elif command -v python >/dev/null 2>&1; then
    PY="python"
else
    echo "  ERROR: Python 3 was not found."
    exit 1
fi

if ! "$PY" "$SCRIPT_DIR/setup-config.py" "$root"; then
    echo "  ERROR: setup-config failed."
    exit 1
fi

# Ask the running bridge to restart its MCP servers (re-reads config.json).
port="${ZS_BRIDGE_PORT:-17613}"
"$PY" - "$port" <<'PY'
import asyncio, json, sys
port = int(sys.argv[1])

async def main():
    try:
        import websockets
    except ImportError:
        print("  (websockets not available; click 'Restart' in the extension popup)")
        return
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}", open_timeout=3) as ws:
            await ws.send(json.dumps({"type": "restart_mcp", "id": "switch"}))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 30
            got = False
            while loop.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    # restart_mcp answers only after every server has started.
                    # Silence is not failure; keep waiting until the deadline.
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("type") == "mcp_status" and msg.get("id") == "switch":
                    got = True
                    servers = msg.get("servers") or []
                    repo = next(
                        (s for s in servers
                         if isinstance(s, dict) and s.get("id") == "repo"),
                        None,
                    )
                    if msg.get("ok") and (
                        (repo.get("alive") and repo.get("ready", True))
                        if repo else msg.get("alive")
                    ):
                        print("  Bridge restarted MCP servers. New project is active.")
                    else:
                        error = (
                            (repo or {}).get("error")
                            or msg.get("error")
                            or "the repo MCP server did not start"
                        )
                        print("  Bridge restart failed:", error)
                        print("  Click 'Restart' in the extension popup.")
                    break
            if not got:
                print("  No restart response from bridge; click 'Restart' in the extension popup.")
    except Exception as error:
        print(f"  Bridge not reachable ({error}); click 'Restart' in the extension popup.")

asyncio.run(main())
PY

echo
echo "  Config now points at: $root"
echo "  If the bridge is not running, start it with: bash Linux_Start.sh"
