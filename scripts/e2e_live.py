#!/usr/bin/env python3
"""Live e2e for snapgent bridge + workspace MCP server.

Boots a REAL bridge (bridge.py) on an ephemeral port with a REAL
workspace_mcp.py subprocess, then drives it over a real WebSocket:
tool catalog, file tools, symlink containment, run_command policy,
write/edit/delete roundtrip, and kill -> restart recovery (both the
inline restart on the next call and the 5 s monitor restart).

Usage:
    /usr/bin/python3 scripts/e2e_live.py

Exit code 0 = all checks passed, 1 = at least one failed.
Never touches port 17613 (a user's own bridge may live there): the port
is picked by binding port 0 and handing it to the bridge subprocess.
Cleans up every file and process it creates.
"""
import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import websockets
except ImportError:
    print("FAIL  websockets package is required: "
          f"{sys.executable} -m pip install websockets")
    sys.exit(1)

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {str(detail)[:160]}", flush=True)
    return cond


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def rpc(ws, mid, payload):
    await ws.send(json.dumps({"id": mid, **payload}))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get("id") == mid:
            return msg


async def call(ws, mid, name, args=None, timeout=10000):
    return await rpc(ws, mid, {"type": "call_tool", "name": name,
                                "arguments": args or {}, "timeout": timeout})


async def wait_listening(port, proc, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"bridge exited early with code {proc.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return
        except OSError:
            await asyncio.sleep(0.2)
    raise RuntimeError(f"bridge did not listen on port {port} within {timeout}s")


async def run_checks(port):
    outside = Path("/tmp") / f"e2e_outside_secret_{os.getpid()}.txt"
    outside.write_text("E2E_TOPSECRET_OUTSIDE\n", encoding="utf-8")
    link = ROOT / "e2e_link_secret.txt"
    tmpfile = ROOT / "e2e_live_tmp.txt"
    for p in (link, tmpfile):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    try:
        # The bridge listens before its MCP subprocesses finish starting
        # (async startup), so the first `connected` may advertise 0 tools.
        # Reconnect until the catalog is populated (timeout 15 s).
        ws = None
        conn = None
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            try:
                ws = await websockets.connect(f"ws://127.0.0.1:{port}")
                conn = json.loads(await ws.recv())
            except Exception:
                if ws is not None:
                    await ws.close()
                    ws = None
                await asyncio.sleep(0.5)
                continue
            if len(conn.get("tools", [])) == 28:
                break
            await ws.close()
            ws = None
            await asyncio.sleep(0.5)
        if ws is None or conn is None:
            check("connected_28_tools", False, "no connection with 28 tools in 15s")
            return
        async with ws:
            check("connected_28_tools",
                  conn.get("type") == "connected" and len(conn.get("tools", [])) == 28,
                  f"{len(conn.get('tools', []))} tools")

            tools = await rpc(ws, 1, {"type": "list_tools"})
            check("list_tools", tools.get("type") == "tools"
                  and len(tools.get("tools", [])) == 28)

            st = await rpc(ws, 2, {"type": "get_bridge_status"})
            s = st["status"]
            pid1 = s["servers"][0]["pid"]
            check("bridge_status",
                  st.get("type") == "bridge_status" and s["clients_connected"] == 1
                  and s["servers"][0]["alive"] and pid1
                  and s["servers"][0]["rss_kib"] is not None,
                  f"pid={pid1} port={s['port']}")

            r = await call(ws, 3, "get_workspace_info")
            check("get_workspace_info", r.get("ok") and ROOT.name in r.get("text", ""))

            r = await call(ws, 4, "list_dir", {"path": "."})
            check("list_dir", r.get("ok") and "bridge.py" in r.get("text", ""))

            r = await call(ws, 5, "search_files",
                           {"path": ".", "query": "BLOCKED_COMMANDS",
                            "file_pattern": "workspace_mcp.py"})
            check("search_files", r.get("ok") and "BLOCKED_COMMANDS" in r.get("text", ""))

            r = await call(ws, 6, "read_file_lines",
                           {"path": "bridge.py", "start_line": 1, "end_line": 3})
            check("read_file_lines", r.get("ok") and "Cross-platform" in r.get("text", ""))

            # Symlink containment: a link inside the workspace pointing
            # outside must not leak through search/find, and read must fail.
            os.symlink(outside, link)
            r = await call(ws, 7, "search_files",
                           {"path": ".", "query": "E2E_TOPSECRET_OUTSIDE",
                            "file_pattern": "e2e_link_secret.txt"})
            check("symlink_search_no_leak",
                  r.get("ok") and "E2E_TOPSECRET" not in r.get("text", ""), r.get("text", ""))
            r = await call(ws, 8, "find_files",
                           {"path": ".", "pattern": "e2e_link_secret.txt"})
            check("symlink_find_no_leak",
                  r.get("ok") and "e2e_link_secret" not in r.get("text", ""), r.get("text", ""))
            r = await call(ws, 9, "read_file", {"path": "e2e_link_secret.txt"})
            check("symlink_read_blocked",
                  r.get("ok") is False and "escapes" in r.get("error", ""), r.get("error", ""))

            # run_command policy
            r = await call(ws, 10, "run_command", {"command": "sudo", "args": ["-h"]})
            check("run_command_sudo_blocked",
                  r.get("ok") is False and "blocked by policy" in r.get("error", ""),
                  r.get("error", ""))
            r = await call(ws, 11, "run_command",
                           {"command": "echo", "args": ["e2e-hello"], "timeout": 10})
            check("run_command_echo_timeout",
                  r.get("ok") and r.get("text", "").strip() == "e2e-hello")
            r = await call(ws, 12, "run_command",
                           {"command": "echo", "args": ["x"], "timeout": 9999})
            check("run_command_timeout_rejected",
                  r.get("ok") is False and "timeout" in r.get("error", ""), r.get("error", ""))

            # write / edit / read / delete roundtrip
            r = await call(ws, 13, "write_file",
                           {"path": "e2e_live_tmp.txt", "content": "alpha beta"})
            check("write_file", r.get("ok"))
            r = await call(ws, 14, "edit_file",
                           {"path": "e2e_live_tmp.txt", "old_text": "beta",
                            "new_text": "gamma"})
            check("edit_file", r.get("ok"))
            r = await call(ws, 15, "read_file", {"path": "e2e_live_tmp.txt"})
            check("read_back_edited",
                  r.get("ok") and r.get("text", "").strip() == "alpha gamma",
                  r.get("text", ""))
            r = await call(ws, 16, "delete_file", {"path": "e2e_live_tmp.txt"})
            check("delete_file", r.get("ok") and not tmpfile.exists())

            r = await call(ws, 17, "git_status")
            # Branch-agnostic: git status --short --branch first line is
            # "## <branch>..."; hardcoding a branch name breaks on any
            # other branch or detached HEAD in CI.
            check("git_status",
                  r.get("ok") and r.get("text", "").startswith("##"))

            # Restart #1: kill the subprocess; the next call restarts it inline.
            os.kill(pid1, signal.SIGKILL)
            await asyncio.sleep(0.5)
            r = await call(ws, 18, "get_workspace_info")
            check("inline_restart_after_kill", r.get("ok") and ROOT.name in r.get("text", ""))
            st = await rpc(ws, 19, {"type": "get_bridge_status"})
            pid2 = st["status"]["servers"][0]["pid"]
            check("new_pid_after_inline_restart",
                  pid2 and pid2 != pid1 and st["status"]["servers"][0]["alive"],
                  f"pid1={pid1} pid2={pid2}")

            # Restart #2: kill again; the 5 s monitor restarts it with no call.
            # Wait for ready + full tool catalog, not just a new pid: `alive`
            # is true while initialize/tools-list are still in flight, and a
            # call in that window resolves to "unknown tool".
            os.kill(pid2, signal.SIGKILL)
            pid3 = None
            for _ in range(15):
                await asyncio.sleep(1)
                st = await rpc(ws, 20, {"type": "get_bridge_status"})
                cur = st["status"]["servers"][0]
                if (
                    cur["alive"] and cur["ready"] and cur["pid"]
                    and cur["pid"] != pid2 and cur["tools"] == 28
                ):
                    pid3 = cur["pid"]
                    break
            check("monitor_restart_after_kill", pid3 is not None, f"pid2={pid2} pid3={pid3}")
            r = await call(ws, 21, "get_workspace_info")
            check("call_after_monitor_restart", r.get("ok"))
    finally:
        for p in (link, tmpfile, outside):
            try:
                p.unlink()
            except FileNotFoundError:
                pass


async def main():
    import tempfile

    port = free_port()
    # config.json is gitignored (generated by launchers), so a fresh CI
    # checkout has no MCP servers configured and the bridge advertises 0
    # tools. Point the bridge at a temp config instead of mutating the
    # repo's config.json (which a developer may rely on locally).
    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / "config.json"
        config_path.write_text(json.dumps({
            "mcpServers": {
                "repo": {
                    "command": sys.executable,
                    "args": [str(ROOT / "workspace_mcp.py")],
                    "env": {"ZS_WORKSPACE_ROOT": str(ROOT)},
                }
            }
        }), encoding="utf-8")
        env = {**os.environ, "ZS_BRIDGE_PORT": str(port),
               "ZS_BRIDGE_CONFIG": str(config_path)}
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "bridge.py")],
            cwd=str(ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            await wait_listening(port, proc)
            await run_checks(port)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
    failed = [name for name, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} PASS"
          + (f"  FAILED={failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
