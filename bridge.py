#!/usr/bin/env python3
"""Cross-platform local WebSocket bridge for Snapgent MCP servers."""

import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import websockets
except ImportError:
    websockets = None


if getattr(sys, "frozen", False):
    # PyInstaller build: config.json and logs live next to the .exe, not in the temp extraction dir.
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
LOG_PATH = ROOT / "logs" / "bridge_debug.log"
HOST = "127.0.0.1"
PORT = int(os.environ.get("ZS_BRIDGE_PORT", "17613"))
MCP_PROTOCOL_VERSION = "2024-11-05"
BRIDGE_VERSION = "3.9.4"
MAX_LOG_CHARS = 1000

# Handshake policy. Web browsers do not apply CORS to WebSockets, so without an
# Origin allowlist any website could open ws://127.0.0.1:PORT and call every MCP
# tool. Only browser-extension origins and non-browser clients (no Origin header
# at all) are accepted.
ALLOWED_ORIGIN_PREFIXES = (
    "chrome-extension://",
    "moz-extension://",
    "safari-web-extension://",
)
# A per-install shared secret, sent by the extension as ?token=... on connect (or
# by native clients as the X-Bridge-Token header). Auto-generated on first run.
TOKEN_ENV = "ZS_BRIDGE_TOKEN"
TOKEN_PATH_ENV = "ZS_BRIDGE_TOKEN_FILE"
AUTH_DISABLE_ENV = "ZS_BRIDGE_AUTH"  # set to 0/off/false/no to run without a token
AUTH_DISABLED_VALUES = {"0", "off", "false", "no"}
# An unpaired extension retries every few seconds, so repeat rejections are only
# reported once per interval to keep bridge_debug.log readable.
REJECT_LOG_INTERVAL = 60.0

LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def log(message: str) -> None:
    logging.info(message)
    print(message, flush=True)


def _json_line(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def token_path() -> Path:
    override = (os.environ.get(TOKEN_PATH_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".snapgent" / "bridge_token"


def auth_disabled() -> bool:
    return (os.environ.get(AUTH_DISABLE_ENV) or "").strip().lower() in AUTH_DISABLED_VALUES


def load_auth_token() -> Tuple[Optional[str], bool]:
    """Return (token, created). A token of None means auth is turned off."""
    if auth_disabled():
        return None, False
    env_token = (os.environ.get(TOKEN_ENV) or "").strip()
    if env_token:
        return env_token, False

    path = token_path()
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""
    except OSError as error:
        log(f"WARNING: could not read the bridge token ({error}); token auth is off.")
        return None, False
    if existing:
        return existing, False

    token = secrets.token_urlsafe(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        # O_EXCL keeps two bridges racing on first run from overwriting a token
        # the other one already handed out.
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(token + "\n")
    except FileExistsError:
        try:
            return path.read_text(encoding="utf-8").strip() or None, False
        except OSError:
            return None, False
    except OSError as error:
        log(f"WARNING: could not create the bridge token ({error}); token auth is off.")
        return None, False
    return token, True


def _write_token_file(path: Path, token: str) -> None:
    """Write the token atomically with mode 0600, replacing any previous value."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    temp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(token + "\n")
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise
    os.replace(temp, path)


def origin_allowed(origin: Optional[str]) -> bool:
    if not origin:
        return True  # native clients (tests, curl, desktop app) send no Origin
    return origin.startswith(ALLOWED_ORIGIN_PREFIXES)


def request_headers(websocket: Any) -> Any:
    headers = getattr(getattr(websocket, "request", None), "headers", None)
    if headers is not None:
        return headers
    return getattr(websocket, "request_headers", None)  # websockets < 14


def request_path(websocket: Any) -> str:
    path = getattr(getattr(websocket, "request", None), "path", None)
    if isinstance(path, str):
        return path
    legacy = getattr(websocket, "path", None)  # websockets < 14
    return legacy if isinstance(legacy, str) else ""


def _header_value(websocket: Any, name: str) -> Optional[str]:
    headers = request_headers(websocket)
    if headers is None:
        return None
    try:
        value = headers.get(name)
    except AttributeError:
        return None
    return value if isinstance(value, str) else None


def presented_token(websocket: Any) -> Optional[str]:
    header_token = (_header_value(websocket, "X-Bridge-Token") or "").strip()
    if header_token:
        return header_token
    query = urllib.parse.parse_qs(urllib.parse.urlparse(request_path(websocket)).query)
    values = query.get("token") or []
    return values[0].strip() if values else None


class MCPServer:
    def __init__(self, server_id: str, config: Dict[str, Any]) -> None:
        self.server_id = server_id
        self.config = config
        self.process: Optional[asyncio.subprocess.Process] = None
        self.tools: List[Dict[str, Any]] = []
        self._next_id = 1
        self._pending: Dict[int, asyncio.Future] = {}
        self._write_lock = asyncio.Lock()
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._start_lock = asyncio.Lock()
        self.error: Optional[str] = None
        self.ready = False

    @property
    def alive(self) -> bool:
        reader_dead = bool(self._reader_task and self._reader_task.done())
        return bool(self.process and self.process.returncode is None and not reader_dead)

    async def start(self) -> bool:
        async with self._start_lock:
            if self.alive and self.ready:
                return True
            await self.stop()
            command = self.config.get("command")
            args = self.config.get("args", [])
            if not isinstance(command, str) or not command.strip():
                self.error = "MCP server command is missing."
                log(f"[{self.server_id}] {self.error}")
                return False
            if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
                self.error = "MCP server args must be an array of strings."
                log(f"[{self.server_id}] {self.error}")
                return False

            env = os.environ.copy()
            server_env = self.config.get("env", {})
            if not isinstance(server_env, dict):
                self.error = "MCP server env must be an object."
                log(f"[{self.server_id}] {self.error}")
                return False
            env.update({str(key): str(value) for key, value in server_env.items()})
            try:
                self.process = await asyncio.create_subprocess_exec(
                    command,
                    *args,
                    cwd=str(ROOT),
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=64 * 1024 * 1024,
                )
            except (OSError, ValueError) as error:
                self.error = str(error)
                log(f"[{self.server_id}] failed to launch: {self.error}")
                return False

            self.tools = []
            self.error = None
            self._reader_task = asyncio.create_task(self._read_stdout())
            self._stderr_task = asyncio.create_task(self._read_stderr())
            try:
                await self._request(
                    "initialize",
                    {
                        "protocolVersion": MCP_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "snapgent-bridge", "version": BRIDGE_VERSION},
                    },
                    timeout=30,
                )
                await self._notify("notifications/initialized")
                result = await self._request("tools/list", {}, timeout=30)
                raw_tools = result.get("tools", []) if isinstance(result, dict) else []
                if not isinstance(raw_tools, list):
                    raise RuntimeError("MCP tools/list returned an invalid tool list.")
                self.tools = [
                    tool for tool in raw_tools
                    if isinstance(tool, dict) and isinstance(tool.get("name"), str)
                ]
                self.ready = True
                log(f"[{self.server_id}] MCP server up ({len(self.tools)} tools advertised)")
                return True
            except (asyncio.TimeoutError, RuntimeError, OSError, ValueError) as error:
                self.error = str(error)
                log(f"[{self.server_id}] MCP startup failed: {self.error}")
                await self.stop()
                return False

    async def stop(self) -> None:
        process = self.process
        self.process = None
        self.tools = []
        self.ready = False
        for task in (self._reader_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
        tasks = [task for task in (self._reader_task, self._stderr_task) if task]
        self._reader_task = None
        self._stderr_task = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RuntimeError(f"{self.server_id} MCP server stopped."))
        self._pending.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if not process or process.returncode is not None:
            return
        if process.stdin:
            try:
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except asyncio.TimeoutError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

    async def _send(self, payload: Dict[str, Any]) -> None:
        process = self.process
        if not process or process.returncode is not None or not process.stdin:
            raise RuntimeError(f"{self.server_id} MCP server is offline.")
        data = (_json_line(payload) + "\n").encode("utf-8")
        async with self._write_lock:
            process.stdin.write(data)
            await process.stdin.drain()

    async def _request(
        self, method: str, params: Dict[str, Any], timeout: float = 30
    ) -> Any:
        request_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )
            response = await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(request_id, None)
        if "error" in response:
            detail = response["error"]
            if isinstance(detail, dict):
                raise RuntimeError(str(detail.get("message") or detail))
            raise RuntimeError(str(detail))
        return response.get("result")

    async def _notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        await self._send(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params or {},
            }
        )

    async def call_tool(
        self, tool_name: str, arguments: Dict[str, Any], timeout: float
    ) -> Dict[str, Any]:
        if not self.alive:
            raise RuntimeError(self.error or f"{self.server_id} MCP server is offline.")
        result = await self._request(
            "tools/call",
            {"name": tool_name, "arguments": arguments},
            timeout=timeout,
        )
        return result if isinstance(result, dict) else {"content": [{"text": str(result)}]}

    async def _read_stdout(self) -> None:
        process = self.process
        if not process or not process.stdout:
            return
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    log(f"[{self.server_id}] ignored non-JSON MCP stdout.")
                    continue
                request_id = message.get("id")
                if isinstance(request_id, int):
                    future = self._pending.get(request_id)
                    if future and not future.done():
                        future.set_result(message)
        except asyncio.CancelledError:
            raise
        except (ValueError, asyncio.LimitOverrunError) as error:
            log(f"[{self.server_id}] MCP stdout read failed: {error}")
        finally:
            if self.process is process and process.returncode is None:
                log(f"[{self.server_id}] MCP stdout closed.")
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(RuntimeError(f"{self.server_id} MCP server disconnected."))

    async def _read_stderr(self) -> None:
        process = self.process
        if not process or not process.stderr:
            return
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    log(f"[{self.server_id}] stderr: {text[:MAX_LOG_CHARS]}")
        except asyncio.CancelledError:
            raise


class Bridge:
    def __init__(self, token: Optional[str] = None) -> None:
        self.servers: Dict[str, MCPServer] = {}
        self.clients = set()
        self._client_lock = asyncio.Lock()
        self._catalog: List[Dict[str, Any]] = []
        self._last_studio: Optional[bool] = None
        self.token = token
        self._reject_log: Dict[str, Tuple[float, int]] = {}

    def load_config(self) -> Dict[str, Dict[str, Any]]:
        if not CONFIG_PATH.exists():
            return {}
        try:
            config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Could not read {CONFIG_PATH.name}: {error}") from error
        servers = config.get("mcpServers") if isinstance(config, dict) else None
        if not isinstance(servers, dict):
            raise RuntimeError(f"{CONFIG_PATH.name} must contain an object named mcpServers.")
        return {
            server_id: value
            for server_id, value in servers.items()
            if isinstance(server_id, str)
            and isinstance(value, dict)
            and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", server_id)
        }

    async def start_servers(self) -> None:
        config = self.load_config()
        self.servers = {
            server_id: MCPServer(server_id, server_config)
            for server_id, server_config in config.items()
        }
        async def _start_one(server: MCPServer) -> None:
            try:
                await server.start()
            except Exception as error:
                server.error = str(error)
                log(f"[{server.server_id}] unexpected startup error: {error}")
            # Publish each server as soon as it is ready so slow ones do not block the rest.
            self._rebuild_catalog()
            await self.broadcast(self._connected_message())

        await asyncio.gather(*(_start_one(server) for server in self.servers.values()))
        self._rebuild_catalog()
        self._log_health()

    async def stop_servers(self) -> None:
        await asyncio.gather(*(server.stop() for server in self.servers.values()))
        self.servers.clear()
        self._catalog = []

    def _rebuild_catalog(self) -> None:
        entries = []
        for server in self.servers.values():
            if server.alive:
                entries.extend((server.server_id, tool) for tool in server.tools)
        name_counts: Dict[str, int] = {}
        for _, tool in entries:
            name_counts[tool["name"]] = name_counts.get(tool["name"], 0) + 1
        self._catalog = []
        for server_id, tool in entries:
            bare_name = tool["name"]
            name = f"{server_id}/{bare_name}" if name_counts[bare_name] > 1 else bare_name
            self._catalog.append(
                {
                    "name": name,
                    "server": server_id,
                    "description": tool.get("description", ""),
                    "inputSchema": tool.get("inputSchema", {"type": "object", "properties": {}}),
                }
            )

    def _server_status(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": server.server_id,
                "alive": server.alive,
                "tools": len(server.tools) if server.alive else 0,
            }
            for server in self.servers.values()
        ]

    def _log_health(self) -> None:
        ids = ", ".join(self.servers) or "none"
        active = ", ".join(server.server_id for server in self.servers.values() if server.alive) or "none"
        log(f"configured {len(self.servers)} MCP server(s): {ids}  (active: {active})")
        total = len(self._catalog)
        log(f"ready {total} tools available ({len(self.servers)} MCP server(s))")

    def _connected_message(self) -> Dict[str, Any]:
        return {
            "type": "connected",
            "bridge_version": BRIDGE_VERSION,
            "mcp_alive": any(server.alive for server in self.servers.values()),
            "tools": self._catalog,
            "servers": self._server_status(),
        }

    async def broadcast(self, message: Dict[str, Any]) -> None:
        async with self._client_lock:
            clients = list(self.clients)
        if not clients:
            return
        await asyncio.gather(
            *(self._send_client(client, message) for client in clients),
            return_exceptions=True,
        )

    async def _send_client(self, client: Any, message: Dict[str, Any]) -> None:
        try:
            await client.send(_json_line(message))
        except Exception:
            pass

    def _log_rejection(self, key: str, message: str) -> None:
        """Log the first rejection, then at most one summary per interval."""
        now = time.monotonic()
        last, suppressed = self._reject_log.get(key, (0.0, 0))
        if last and now - last < REJECT_LOG_INTERVAL:
            self._reject_log[key] = (last, suppressed + 1)
            return
        if suppressed:
            message = f"{message} ({suppressed} more since the last report)"
        log(message)
        self._reject_log[key] = (now, 0)

    def rotate_token(self, request_id: Any = None) -> Dict[str, Any]:
        """Issue a fresh token that applies to future connections, no restart needed.

        The caller is already authenticated by the current token, and existing
        connections keep working; only new handshakes use the new value.
        """
        response: Dict[str, Any] = {"type": "token_rotated", "id": request_id}
        if self.token is None:
            return {**response, "ok": False, "error": f"token auth is disabled ({AUTH_DISABLE_ENV})"}
        if (os.environ.get(TOKEN_ENV) or "").strip():
            return {
                **response,
                "ok": False,
                "error": f"the token is pinned by {TOKEN_ENV}; change that variable instead",
            }
        token = secrets.token_urlsafe(32)
        path = token_path()
        try:
            _write_token_file(path, token)
        except OSError as error:
            log(f"could not rotate the bridge token ({error})")
            return {**response, "ok": False, "error": f"could not write {path}: {error}"}
        self.token = token
        log(f"bridge token rotated; the new token is saved to {path}")
        return {**response, "ok": True, "token": token}

    async def _reject_handshake(self, websocket: Any, reason: str) -> None:
        try:
            await websocket.close(code=1008, reason=reason)
        except Exception:
            pass

    async def handle_client(self, websocket: Any, _path: Optional[str] = None) -> None:
        origin = _header_value(websocket, "Origin")
        if not origin_allowed(origin):
            self._log_rejection(
                f"origin:{origin}", f"rejected client from origin {origin!r}"
            )
            await self._reject_handshake(websocket, "origin not allowed")
            return
        if self.token:
            presented = presented_token(websocket)
            if not presented or not hmac.compare_digest(presented, self.token):
                self._log_rejection(
                    "token", "rejected client with a missing or invalid bridge token"
                )
                await self._reject_handshake(websocket, "bridge token required")
                return
        async with self._client_lock:
            self.clients.add(websocket)
        log("extension connected")
        await self._send_client(websocket, self._connected_message())
        try:
            async for raw in websocket:
                try:
                    request = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    await self._send_client(websocket, {"type": "error", "error": "Invalid JSON request."})
                    continue
                if not isinstance(request, dict):
                    await self._send_client(websocket, {"type": "error", "error": "Request must be a JSON object."})
                    continue
                await self._handle_request(websocket, request)
        finally:
            async with self._client_lock:
                self.clients.discard(websocket)
            log("extension disconnected")

    async def _handle_request(self, websocket: Any, request: Dict[str, Any]) -> None:
        request_id = request.get("id")
        kind = request.get("type")
        if kind == "ping":
            await self._send_client(websocket, {"type": "pong", "id": request_id})
        elif kind == "studio_status":
            studio, studio_app, studio_proc = await self._studio_status()
            await self._send_client(
                websocket,
                {
                    "type": "studio_status",
                    "id": request_id,
                    "studio": studio,
                    "studio_app": studio_app,
                    "studio_proc": studio_proc,
                },
            )
        elif kind == "list_tools":
            await self._send_client(
                websocket,
                {
                    **self._connected_message(),
                    "type": "tools",
                    "id": request_id,
                },
            )
        elif kind == "call_tool":
            await self._call_tool(websocket, request)
        elif kind == "restart_mcp":
            try:
                await self.stop_servers()
                await self.start_servers()
                response = {
                    "type": "mcp_status",
                    "id": request_id,
                    "ok": True,
                    "alive": any(server.alive for server in self.servers.values()),
                    "tools": self._catalog,
                    "servers": self._server_status(),
                }
            except (OSError, RuntimeError) as error:
                response = {"type": "mcp_status", "id": request_id, "ok": False, "alive": False, "error": str(error)}
            await self._send_client(websocket, response)
            await self.broadcast(self._connected_message())
        elif kind == "rotate_token":
            await self._send_client(websocket, self.rotate_token(request_id))
        elif kind == "add_server":
            await self._change_server(websocket, request, remove=False)
        elif kind == "remove_server":
            await self._change_server(websocket, request, remove=True)
        else:
            await self._send_client(
                websocket,
                {"type": "error", "id": request_id, "error": f"Unknown bridge request: {kind}"},
            )

    async def _call_tool(self, websocket: Any, request: Dict[str, Any]) -> None:
        request_id = request.get("id")
        name = request.get("name")
        arguments = request.get("arguments") or {}
        timeout = request.get("timeout", 120000)
        if not isinstance(name, str) or not isinstance(arguments, dict):
            await self._send_client(
                websocket,
                {"type": "tool_result", "id": request_id, "ok": False, "error": "Invalid tool call."},
            )
            return
        try:
            timeout_seconds = max(1.0, min(float(timeout) / 1000, 300.0))
        except (TypeError, ValueError):
            timeout_seconds = 120.0
        server, tool_name = self._resolve_tool(name)
        # Isolation: the extension states which server is selected. The bridge refuses to
        # run a tool that belongs to any other server (requests without the field keep
        # the old behaviour).
        allowed = request.get("server")
        if isinstance(allowed, str) and allowed:
            if server is None and "/" not in name:
                allowed_server = self.servers.get(allowed)
                if allowed_server and allowed_server.alive and any(t["name"] == name for t in allowed_server.tools):
                    server, tool_name = allowed_server, name
            if server is not None and server.server_id != allowed:
                await self._send_client(
                    websocket,
                    {
                        "type": "tool_result",
                        "id": request_id,
                        "ok": False,
                        "kind": "unknown_tool",
                        "error": f"MCP tool not available for the selected server: {name}",
                    },
                )
                return
        if not server:
            await self._send_client(
                websocket,
                {
                    "type": "tool_result",
                    "id": request_id,
                    "ok": False,
                    "kind": "unknown_tool",
                    "error": f"Unknown or unavailable MCP tool: {name}",
                },
            )
            return
        try:
            result = await server.call_tool(tool_name, arguments, timeout_seconds)
            text_parts = []
            images = []
            for item in result.get("content", []):
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    text_parts.append(item["text"])
                elif item.get("type") == "image" and isinstance(item.get("data"), str):
                    images.append(
                        {
                            "data": item["data"],
                            "mimeType": item.get("mimeType", "image/png"),
                        }
                    )
            structured = result.get("structuredContent")
            if not text_parts and structured is not None:
                text_parts.append(json.dumps(structured, ensure_ascii=False))
            text = "\n".join(text_parts)
            if result.get("isError"):
                await self._send_client(
                    websocket,
                    {"type": "tool_result", "id": request_id, "ok": False, "error": text or "MCP tool failed."},
                )
            else:
                await self._send_client(
                    websocket,
                    {"type": "tool_result", "id": request_id, "ok": True, "text": text, "images": images},
                )
            log(f"<- {name} ({text[:120].replace(chr(10), ' ')})")
        except (asyncio.TimeoutError, RuntimeError, OSError, ValueError) as error:
            message = str(error)
            await self._send_client(
                websocket,
                {
                    "type": "tool_result",
                    "id": request_id,
                    "ok": False,
                    "kind": "timeout" if isinstance(error, asyncio.TimeoutError) else "tool_error",
                    "error": message,
                },
            )
            log(f"<- {name} ERROR: {message[:MAX_LOG_CHARS]}")

    def _resolve_tool(self, name: str) -> Tuple[Optional[MCPServer], str]:
        if "/" in name:
            server_id, bare_name = name.split("/", 1)
            server = self.servers.get(server_id)
            if server and server.alive and any(tool["name"] == bare_name for tool in server.tools):
                return server, bare_name
        matches = [
            (server, tool)
            for server in self.servers.values()
            if server.alive
            for tool in server.tools
            if tool["name"] == name
        ]
        if len(matches) == 1:
            return matches[0][0], name
        return None, name

    async def _studio_status(self) -> Tuple[Optional[bool], Optional[bool], Optional[bool]]:
        studio_server = self.servers.get("roblox")
        studio_proc = _process_running("RobloxStudioBeta.exe" if os.name == "nt" else "RobloxStudio")
        if not studio_server or not studio_server.alive:
            return None, None, studio_proc
        if not any(tool["name"] == "get_studio_state" for tool in studio_server.tools):
            return None, None, studio_proc
        try:
            result = await studio_server.call_tool("get_studio_state", {}, 8)
        except (asyncio.TimeoutError, RuntimeError, OSError, ValueError):
            return False, studio_proc, studio_proc
        text = " ".join(
            item.get("text", "")
            for item in result.get("content", [])
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
        if result.get("isError") or re.search(
            r"Unable to find an active Studio instance|previously active Studio has disconnected",
            text,
            re.IGNORECASE,
        ):
            return False, studio_proc, studio_proc
        return True, True, studio_proc

    async def _change_server(
        self, websocket: Any, request: Dict[str, Any], remove: bool
    ) -> None:
        request_id = request.get("id")
        server_id = request.get("server_id")
        if (
            not isinstance(server_id, str)
            or server_id == "roblox"
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", server_id)
        ):
            await self._send_client(
                websocket,
                {"type": "server_changed", "id": request_id, "ok": False, "error": "Invalid or protected MCP server id."},
            )
            return
        try:
            config = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
            servers = config.setdefault("mcpServers", {})
            if not isinstance(servers, dict):
                raise RuntimeError("mcpServers must be a JSON object.")
            if remove:
                if server_id == "vscode":
                    raise RuntimeError("The VS Code MCP server cannot be removed.")
                servers.pop(server_id, None)
            else:
                command = request.get("command")
                args = request.get("args", [])
                env = request.get("env", {})
                if not isinstance(command, str) or not command.strip():
                    raise RuntimeError("MCP server command is required.")
                if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
                    raise RuntimeError("MCP server args must be an array of strings.")
                if not isinstance(env, dict) or not all(isinstance(k, str) for k in env):
                    raise RuntimeError("MCP server env must be an object.")
                servers[server_id] = {"command": command, "args": args, "env": env}
            temp_path = CONFIG_PATH.with_suffix(".json.tmp")
            temp_path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            temp_path.replace(CONFIG_PATH)
            await self.stop_servers()
            await self.start_servers()
            await self._send_client(
                websocket,
                {"type": "server_changed", "id": request_id, "ok": True, "restarting": False},
            )
            await self.broadcast(self._connected_message())
        except (OSError, json.JSONDecodeError, RuntimeError) as error:
            await self._send_client(
                websocket,
                {"type": "server_changed", "id": request_id, "ok": False, "error": str(error)},
            )


def _process_running(process_name: str) -> Optional[bool]:
    try:
        if os.name == "nt":
            result = subprocess_run(
                ["tasklist", "/FI", f"IMAGENAME eq {process_name}", "/NH"],
                timeout=5,
            )
            return process_name.lower() in result.lower()
        result = subprocess_run(["pgrep", "-f", process_name], timeout=5)
        return bool(result.strip())
    except (OSError, subprocess.TimeoutExpired):
        return None


def subprocess_run(command: List[str], timeout: int) -> str:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    return result.stdout


async def run() -> None:
    if websockets is None:
        raise RuntimeError(
            "The 'websockets' package is required. Install it with "
            f"'{sys.executable} -m pip install websockets'."
        )
    token, created = load_auth_token()
    bridge = Bridge(token)
    server = await websockets.serve(
        bridge.handle_client,
        HOST,
        PORT,
        max_size=50 * 1024 * 1024,
        ping_interval=20,
        ping_timeout=20,
    )
    log(f"snapgent bridge v{BRIDGE_VERSION}")
    log(f"listening on ws://{HOST}:{PORT} - load the extension and open a supported AI chat")
    if token:
        if (os.environ.get(TOKEN_ENV) or "").strip():
            log(f"auth: token required ({TOKEN_ENV}); origin allowlist enforced")
        else:
            log(f"auth: token required from {token_path()}; origin allowlist enforced")
        if created:
            log("first run: paste this token into the Snapgent popup once (it is saved by the extension)")
            log(f"bridge token: {token}")
    else:
        log(
            "WARNING: bridge token auth is disabled - any local process can call MCP tools. "
            f"Unset {AUTH_DISABLE_ENV} and restart the bridge to require a token."
        )
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    async def _startup_then_monitor() -> None:
        # Servers start in parallel after the websocket is already listening;
        # the monitor only begins once the initial startup has finished.
        try:
            await bridge.start_servers()
        except RuntimeError as error:
            log(f"config error: {error}")
        await _monitor_servers(bridge)

    monitor_task = asyncio.create_task(_startup_then_monitor())
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await stop_event.wait()
    finally:
        monitor_task.cancel()
        await asyncio.gather(monitor_task, return_exceptions=True)
        server.close()
        await server.wait_closed()
        await bridge.stop_servers()
        log("bridge stopped")


async def _monitor_servers(bridge: Bridge) -> None:
    while True:
        await asyncio.sleep(5)
        changed = False
        for server in bridge.servers.values():
            if not server.alive:
                log(f"[{server.server_id}] MCP server disconnected; restarting.")
                await server.start()
                changed = True
        if changed:
            bridge._rebuild_catalog()
            bridge._log_health()
            await bridge.broadcast(bridge._connected_message())


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except (OSError, RuntimeError, ValueError) as error:
        log(f"FATAL: {error}")
        raise SystemExit(1)
