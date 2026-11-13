import inspect
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bridge


def _close_code(error):
    """Close code of a ConnectionClosed, without the deprecated .code accessor.

    websockets 13.1 deprecated ConnectionClosed.code in favour of rcvd.code, and
    reading the old attribute emits a DeprecationWarning on every rotation test.
    """
    received = getattr(error, "rcvd", None)
    return received.code if received is not None else error.code


def _header_kwargs(headers):
    """websockets renamed extra_headers to additional_headers in 14."""
    if not headers or bridge.websockets is None:
        return {}
    if "additional_headers" in inspect.signature(bridge.websockets.connect).parameters:
        return {"additional_headers": headers}
    return {"extra_headers": headers}


MOCK_MCP = r"""
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": request["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "mock", "version": "1"},
        }
    elif method == "tools/list":
        result = {
            "tools": [{
                "name": "echo",
                "description": "Echo a message",
                "inputSchema": {"type": "object", "properties": {"message": {"type": "string"}}},
            }]
        }
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": request["params"]["arguments"]["message"]}]}
    else:
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": request["id"],
            "error": {"code": -32601, "message": "Method not found"},
        }), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
"""


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_mcp_start_list_call_and_stop(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            mock_path = Path(temp_dir) / "mock_mcp.py"
            mock_path.write_text(MOCK_MCP, encoding="utf-8")
            server = bridge.MCPServer(
                "mock",
                {"command": sys.executable, "args": [str(mock_path)], "env": {}},
            )
            try:
                self.assertTrue(await server.start())
                self.assertTrue(server.alive)
                self.assertEqual([tool["name"] for tool in server.tools], ["echo"])
                result = await server.call_tool("echo", {"message": "hello"}, 2)
                self.assertEqual(result["content"][0]["text"], "hello")
            finally:
                await server.stop()
            self.assertFalse(server.alive)

    async def test_large_response_line_is_received(self):
        # Regression: a single JSON line larger than asyncio's default 64 KB
        # stream limit used to kill the stdout reader while the process stayed
        # alive, so every later request timed out.
        with tempfile.TemporaryDirectory() as temp_dir:
            mock_path = Path(temp_dir) / "mock_mcp.py"
            mock_path.write_text(MOCK_MCP, encoding="utf-8")
            server = bridge.MCPServer(
                "mock",
                {"command": sys.executable, "args": [str(mock_path)], "env": {}},
            )
            try:
                self.assertTrue(await server.start())
                big_message = "x" * 200_000
                result = await server.call_tool("echo", {"message": big_message}, 10)
                self.assertEqual(result["content"][0]["text"], big_message)
                self.assertTrue(server.alive)
                follow_up = await server.call_tool("echo", {"message": "still works"}, 10)
                self.assertEqual(follow_up["content"][0]["text"], "still works")
            finally:
                await server.stop()

    @unittest.skipIf(bridge.websockets is None, "websockets package is not installed")
    async def test_websocket_extension_protocol(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            mock_path = root / "mock_mcp.py"
            mock_path.write_text(MOCK_MCP, encoding="utf-8")
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "mock": {
                                "command": sys.executable,
                                "args": [str(mock_path)],
                                "env": {},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            manager = bridge.Bridge()
            with patch.object(bridge, "CONFIG_PATH", config_path):
                await manager.start_servers()
                try:
                    async with bridge.websockets.serve(
                        manager.handle_client, "127.0.0.1", 0
                    ) as server:
                        port = server.sockets[0].getsockname()[1]
                        async with bridge.websockets.connect(f"ws://127.0.0.1:{port}") as client:
                            connected = json.loads(await client.recv())
                            self.assertEqual(connected["type"], "connected")
                            self.assertEqual(connected["servers"][0]["id"], "mock")

                            await client.send(json.dumps({"id": 1, "type": "list_tools"}))
                            tools = json.loads(await client.recv())
                            self.assertEqual(tools["type"], "tools")
                            self.assertEqual(tools["tools"][0]["server"], "mock")

                            await client.send(
                                json.dumps(
                                    {
                                        "id": 2,
                                        "type": "call_tool",
                                        "name": "echo",
                                        "arguments": {"message": "round trip"},
                                        "timeout": 2000,
                                    }
                                )
                            )
                            result = json.loads(await client.recv())
                            self.assertTrue(result["ok"])
                            self.assertEqual(result["text"], "round trip")
                finally:
                    await manager.stop_servers()

    def test_duplicate_tool_names_are_server_qualified(self):
        first = bridge.MCPServer("alpha", {})
        second = bridge.MCPServer("beta", {})
        first.process = SimpleNamespace(returncode=None)
        second.process = SimpleNamespace(returncode=None)
        first.ready = second.ready = True
        first.tools = [{"name": "get_state"}]
        second.tools = [{"name": "get_state"}]

        manager = bridge.Bridge()
        manager.servers = {"alpha": first, "beta": second}
        manager._rebuild_catalog()

        self.assertEqual(
            [tool["name"] for tool in manager._catalog],
            ["alpha/get_state", "beta/get_state"],
        )
        self.assertIs(manager._resolve_tool("beta/get_state")[0], second)
        self.assertIsNone(manager._resolve_tool("get_state")[0])

    def test_restart_backoff_is_exponential_and_capped(self):
        server = bridge.MCPServer("mock", {})
        delays = [server.record_restart_failure() for _ in range(12)]
        self.assertEqual(delays[:4], [5.0, 10.0, 20.0, 40.0])
        self.assertTrue(all(d <= bridge.RESTART_MAX_DELAY_SECONDS for d in delays))
        self.assertEqual(server.restart_attempts, 12)
        self.assertGreater(server.next_restart_at, 0.0)
        server.record_restart_success()
        self.assertEqual(server.restart_attempts, 0)
        self.assertEqual(server.next_restart_at, 0.0)

    def test_get_bridge_status_shape(self):
        manager = bridge.Bridge()
        status = manager.get_bridge_status(log_tail=["line one", "line two"])
        self.assertEqual(status["bridge_version"], bridge.BRIDGE_VERSION)
        self.assertEqual(status["host"], bridge.HOST)
        self.assertEqual(status["port"], bridge.PORT)
        self.assertEqual(status["clients_connected"], 0)
        self.assertEqual(status["servers"], [])
        self.assertEqual(status["log_tail"], ["line one", "line two"])
        self.assertGreaterEqual(status["uptime_seconds"], 0.0)

    def test_json_log_formatter_emits_parseable_json(self):
        import logging

        formatter = bridge._JsonLogFormatter()
        record = logging.LogRecord(
            "test", logging.INFO, __file__, 1, "hello %s", ("world",), None
        )
        parsed = json.loads(formatter.format(record))
        self.assertEqual(parsed["level"], "INFO")
        self.assertEqual(parsed["message"], "hello world")
        self.assertIn("ts", parsed)

    @unittest.skipIf(bridge.websockets is None, "websockets package is not installed")
    async def test_websocket_get_bridge_status_request(self):
        manager = bridge.Bridge()
        async with bridge.websockets.serve(manager.handle_client, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with bridge.websockets.connect(f"ws://127.0.0.1:{port}") as client:
                connected = json.loads(await client.recv())
                self.assertEqual(connected["type"], "connected")
                await client.send(json.dumps({"id": 9, "type": "get_bridge_status"}))
                reply = json.loads(await client.recv())
                self.assertEqual(reply["type"], "bridge_status")
                self.assertEqual(reply["id"], 9)
                self.assertEqual(reply["status"]["clients_connected"], 1)
                self.assertIn("log_tail", reply["status"])


class BridgeAuthTests(unittest.IsolatedAsyncioTestCase):
    def test_origin_policy_allows_extensions_and_native_clients(self):
        self.assertTrue(bridge.origin_allowed(None))
        self.assertTrue(bridge.origin_allowed("chrome-extension://abcdefghijklmnop"))
        for rejected in ("https://evil.example", "http://127.0.0.1:8080", "null", "file://"):
            self.assertFalse(bridge.origin_allowed(rejected), rejected)

    def test_token_is_generated_once_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "nested" / "bridge_token"
            with patch.dict(os.environ, {bridge.TOKEN_PATH_ENV: str(path)}, clear=False):
                os.environ.pop(bridge.TOKEN_ENV, None)
                os.environ.pop(bridge.AUTH_DISABLE_ENV, None)
                first, created = bridge.load_auth_token()
                self.assertTrue(created)
                second, created_again = bridge.load_auth_token()
            self.assertFalse(created_again)
            self.assertEqual(first, second)
            self.assertGreaterEqual(len(first or ""), 32)
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_repeated_rejections_are_logged_once_per_interval(self):
        manager = bridge.Bridge("t")
        with patch.object(bridge, "log") as logged:
            for _ in range(3):
                manager._log_rejection("token", "rejected client")
            self.assertEqual(logged.call_count, 1)
            # Pretend the reporting interval has elapsed.
            last, suppressed = manager._reject_log["token"]
            manager._reject_log["token"] = (last - bridge.REJECT_LOG_INTERVAL - 1, suppressed)
            manager._log_rejection("token", "rejected client")
        self.assertEqual(logged.call_count, 2)
        self.assertIn("2 more since the last report", logged.call_args[0][0])

    def test_token_env_overrides_file_and_disable_switch_turns_auth_off(self):
        with patch.dict(os.environ, {bridge.TOKEN_ENV: "env-token", bridge.AUTH_DISABLE_ENV: ""}):
            self.assertEqual(bridge.load_auth_token(), ("env-token", False))
        with patch.dict(os.environ, {bridge.AUTH_DISABLE_ENV: "0"}):
            self.assertEqual(bridge.load_auth_token(), (None, False))

    def test_token_matches_is_constant_time_and_tolerates_any_input(self):
        self.assertTrue(bridge.token_matches("secret", "secret"))
        self.assertFalse(bridge.token_matches("secret", "secrets"))
        self.assertFalse(bridge.token_matches("", "secret"))
        self.assertFalse(bridge.token_matches(None, "secret"))
        self.assertFalse(bridge.token_matches("secret", None))
        # Non-ASCII must be a plain mismatch: hmac.compare_digest raises
        # TypeError on non-ASCII str, which used to crash the connection handler.
        self.assertFalse(bridge.token_matches("\u00e9", "secret"))
        # Non-ASCII still compares correctly against itself; only the crash goes away.
        self.assertTrue(bridge.token_matches("\u00e9", "\u00e9"))
        self.assertFalse(bridge.token_matches("\ud800", "secret"))  # lone surrogate

    def test_rotate_token_refuses_when_pinned_or_disabled(self):
        with patch.dict(os.environ, {bridge.TOKEN_ENV: "pinned", bridge.AUTH_DISABLE_ENV: ""}):
            result = bridge.Bridge("pinned").rotate_token("r1")
        self.assertFalse(result["ok"])
        self.assertIn(bridge.TOKEN_ENV, result["error"])
        self.assertFalse(bridge.Bridge(None).rotate_token("r1")["ok"])

    def test_rotate_token_persists_and_updates_the_instance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bridge_token"
            with patch.dict(os.environ, {bridge.TOKEN_PATH_ENV: str(path)}, clear=False):
                os.environ.pop(bridge.TOKEN_ENV, None)
                os.environ.pop(bridge.AUTH_DISABLE_ENV, None)
                manager = bridge.Bridge("old-token")
                result = manager.rotate_token(7)
            self.assertTrue(result["ok"])
            self.assertEqual(result["id"], 7)
            self.assertNotEqual(result["token"], "old-token")
            self.assertEqual(manager.token, result["token"])
            self.assertEqual(path.read_text(encoding="utf-8").strip(), result["token"])
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual([entry.name for entry in Path(temp_dir).iterdir()], ["bridge_token"])

    @unittest.skipIf(bridge.websockets is None, "websockets package is not installed")
    async def test_rotated_token_replaces_the_old_one_for_new_connections(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bridge_token"
            with patch.dict(os.environ, {bridge.TOKEN_PATH_ENV: str(path)}, clear=False):
                os.environ.pop(bridge.TOKEN_ENV, None)
                os.environ.pop(bridge.AUTH_DISABLE_ENV, None)
                manager = bridge.Bridge("old-token")
                async with bridge.websockets.serve(manager.handle_client, "127.0.0.1", 0) as server:
                    port = server.sockets[0].getsockname()[1]
                    base = f"ws://127.0.0.1:{port}"
                    async with bridge.websockets.connect(f"{base}/?token=old-token") as client:
                        self.assertEqual(json.loads(await client.recv())["type"], "connected")
                        await client.send(json.dumps({"type": "rotate_token", "id": 7}))
                        reply = json.loads(await client.recv())
                        # The reply arrives first, then the rotation revokes the
                        # session that asked for it.
                        with self.assertRaises(bridge.websockets.exceptions.ConnectionClosed) as closed:
                            await client.recv()
                        self.assertEqual(_close_code(closed.exception), bridge.ROTATE_CLOSE_CODE)
                    self.assertTrue(reply["ok"], reply)
                    new_token = reply["token"]
                    self.assertEqual(path.read_text(encoding="utf-8").strip(), new_token)

                    async with bridge.websockets.connect(f"{base}/?token=old-token") as stale:
                        with self.assertRaises(bridge.websockets.exceptions.ConnectionClosed) as ctx:
                            await stale.recv()
                        self.assertEqual(_close_code(ctx.exception), 1008)
                    async with bridge.websockets.connect(f"{base}/?token={new_token}") as fresh:
                        self.assertEqual(json.loads(await fresh.recv())["type"], "connected")

    @unittest.skipIf(bridge.websockets is None, "websockets package is not installed")
    async def test_rotation_refuses_the_old_token_even_for_the_client_that_rotated(self):
        # The session that rotates is closed too (1012), so a token leaked to that
        # client must not survive: dialing back in with the old value is a 1008.
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bridge_token"
            with patch.dict(os.environ, {bridge.TOKEN_PATH_ENV: str(path)}, clear=False):
                os.environ.pop(bridge.TOKEN_ENV, None)
                os.environ.pop(bridge.AUTH_DISABLE_ENV, None)
                manager = bridge.Bridge("leaked-token")
                async with bridge.websockets.serve(manager.handle_client, "127.0.0.1", 0) as server:
                    port = server.sockets[0].getsockname()[1]
                    base = f"ws://127.0.0.1:{port}"
                    async with bridge.websockets.connect(f"{base}/?token=leaked-token") as client:
                        self.assertEqual(json.loads(await client.recv())["type"], "connected")
                        await client.send(json.dumps({"type": "rotate_token", "id": 4}))
                        reply = json.loads(await client.recv())
                        self.assertTrue(reply["ok"], reply)
                        with self.assertRaises(bridge.websockets.exceptions.ConnectionClosed) as closed:
                            await client.recv()
                        self.assertEqual(_close_code(closed.exception), bridge.ROTATE_CLOSE_CODE)
                    new_token = reply["token"]
                    self.assertNotEqual(new_token, "leaked-token")
                    self.assertEqual(path.read_text(encoding="utf-8").strip(), new_token)

                    async with bridge.websockets.connect(f"{base}/?token=leaked-token") as stale:
                        with self.assertRaises(bridge.websockets.exceptions.ConnectionClosed) as ctx:
                            await stale.recv()
                        self.assertEqual(_close_code(ctx.exception), 1008)
                    async with bridge.websockets.connect(f"{base}/?token={new_token}") as fresh:
                        self.assertEqual(json.loads(await fresh.recv())["type"], "connected")

    @unittest.skipIf(bridge.websockets is None, "websockets package is not installed")
    async def test_rotation_revokes_every_authenticated_connection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bridge_token"
            with patch.dict(os.environ, {bridge.TOKEN_PATH_ENV: str(path)}, clear=False):
                os.environ.pop(bridge.TOKEN_ENV, None)
                os.environ.pop(bridge.AUTH_DISABLE_ENV, None)
                manager = bridge.Bridge("old-token")
                async with bridge.websockets.serve(manager.handle_client, "127.0.0.1", 0) as server:
                    port = server.sockets[0].getsockname()[1]
                    base = f"ws://127.0.0.1:{port}"
                    async with bridge.websockets.connect(f"{base}/?token=old-token") as owner:
                        async with bridge.websockets.connect(f"{base}/?token=old-token") as bystander:
                            self.assertEqual(json.loads(await owner.recv())["type"], "connected")
                            self.assertEqual(json.loads(await bystander.recv())["type"], "connected")
                            await owner.send(json.dumps({"type": "rotate_token", "id": 1}))
                            reply = json.loads(await owner.recv())
                            self.assertTrue(reply["ok"], reply)
                            # Both the caller and the idle session lose their socket:
                            # a leaked token must not keep any connection alive.
                            for client in (owner, bystander):
                                with self.assertRaises(bridge.websockets.exceptions.ConnectionClosed) as closed:
                                    await client.recv()
                                self.assertEqual(_close_code(closed.exception), bridge.ROTATE_CLOSE_CODE)

                    new_token = reply["token"]
                    async with bridge.websockets.connect(f"{base}/?token=old-token") as stale:
                        with self.assertRaises(bridge.websockets.exceptions.ConnectionClosed) as closed:
                            await stale.recv()
                        self.assertEqual(_close_code(closed.exception), 1008)
                    async with bridge.websockets.connect(f"{base}/?token={new_token}") as fresh:
                        self.assertEqual(json.loads(await fresh.recv())["type"], "connected")

    @unittest.skipIf(bridge.websockets is None, "websockets package is not installed")
    async def test_non_ascii_token_is_refused_with_1008(self):
        manager = bridge.Bridge("secret-token")
        async with bridge.websockets.serve(manager.handle_client, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            base = f"ws://127.0.0.1:{port}"

            async def close_code(uri):
                async with bridge.websockets.connect(uri) as client:
                    try:
                        await client.recv()
                    except bridge.websockets.exceptions.ConnectionClosed as error:
                        return _close_code(error)
                return None

            # hmac.compare_digest() raised TypeError on non-ASCII input, so this
            # closed with 1011 and printed a traceback instead of rejecting.
            self.assertEqual(await close_code(f"{base}/?token=%C3%A9"), 1008)
            self.assertEqual(await close_code(base + "/?token=%FF%E0"), 1008)
            # The handler survived, so a valid client still gets in.
            async with bridge.websockets.connect(f"{base}/?token=secret-token") as client:
                self.assertEqual(json.loads(await client.recv())["type"], "connected")

    @unittest.skipIf(bridge.websockets is None, "websockets package is not installed")
    async def test_handshake_requires_origin_and_token(self):
        manager = bridge.Bridge("secret-token")
        async with bridge.websockets.serve(manager.handle_client, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            base = f"ws://127.0.0.1:{port}"

            async def close_code(uri, headers=None):
                async with bridge.websockets.connect(uri, **_header_kwargs(headers or {})) as client:
                    try:
                        await client.recv()
                    except bridge.websockets.exceptions.ConnectionClosed as error:
                        return _close_code(error)
                return None

            self.assertEqual(await close_code(base), 1008, "missing token must be refused")
            self.assertEqual(await close_code(f"{base}/?token=wrong"), 1008, "wrong token must be refused")
            self.assertEqual(
                await close_code(f"{base}/?token=secret-token", {"Origin": "https://evil.example"}),
                1008,
                "a website origin must be refused even with a valid token",
            )
            async with bridge.websockets.connect(
                f"{base}/?token=secret-token",
                **_header_kwargs({"Origin": "chrome-extension://abcdefghijklmnop"}),
            ) as client:
                self.assertEqual(json.loads(await client.recv())["type"], "connected")

            # Native clients may authenticate with a header instead of the URL.
            async with bridge.websockets.connect(
                base, **_header_kwargs({"X-Bridge-Token": "secret-token"})
            ) as client:
                self.assertEqual(json.loads(await client.recv())["type"], "connected")


if __name__ == "__main__":
    unittest.main()
