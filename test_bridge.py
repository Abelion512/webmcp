import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bridge


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


class FakeClient:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))


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


class PromptDelegationTests(unittest.IsolatedAsyncioTestCase):
    async def _await_send(self, client):
        for _ in range(200):
            if client.sent:
                return
            await asyncio.sleep(0.01)
        self.fail("the bridge never sent the prompt")

    async def test_submit_prompt_resolves_on_prompt_result(self):
        manager = bridge.Bridge()
        client = FakeClient()
        manager.clients.add(client)
        task = asyncio.create_task(manager.submit_prompt("hello", 5))
        await self._await_send(client)
        self.assertEqual(client.sent[0]["type"], "submit_prompt")
        self.assertEqual(client.sent[0]["prompt"], "hello")
        await manager._handle_request(
            client,
            {
                "id": 7,
                "type": "prompt_result",
                "prompt_id": client.sent[0]["id"],
                "ok": True,
                "text": "world",
            },
        )
        self.assertEqual(await asyncio.wait_for(task, 2), {"ok": True, "text": "world"})

    async def test_submit_prompt_requires_extension(self):
        result = await bridge.Bridge().submit_prompt("hello", 1)
        self.assertFalse(result["ok"])
        self.assertIn("extension", result["error"])

    async def test_submit_prompt_fails_when_extension_disconnects(self):
        manager = bridge.Bridge()
        client = FakeClient()
        manager.clients.add(client)
        task = asyncio.create_task(manager.submit_prompt("hello", 5))
        await self._await_send(client)
        manager.clients.discard(client)
        manager._fail_pending_prompts("The extension disconnected.")
        result = await asyncio.wait_for(task, 2)
        self.assertFalse(result["ok"])
        self.assertIn("disconnected", result["error"])

    async def test_submit_prompt_ignores_a_foreign_socket(self):
        manager = bridge.Bridge()
        owner = FakeClient()
        rogue = FakeClient()
        manager.clients.update({owner, rogue})
        manager._last_client = owner
        task = asyncio.create_task(manager.submit_prompt("hello", 5))
        await self._await_send(owner)
        # The rogue must not be able to resolve someone else's task.
        await manager._handle_request(
            rogue,
            {
                "id": 9,
                "type": "prompt_result",
                "prompt_id": owner.sent[0]["id"],
                "ok": True,
                "text": "spoofed",
            },
        )
        self.assertFalse(task.done())
        await manager._handle_request(
            owner,
            {
                "id": 10,
                "type": "prompt_result",
                "prompt_id": owner.sent[0]["id"],
                "ok": True,
                "text": "real",
            },
        )
        self.assertEqual(await asyncio.wait_for(task, 2), {"ok": True, "text": "real"})

    def test_build_a2a_server_takes_token_from_env(self):
        with patch.dict("os.environ", {"ZS_A2A_TOKEN": "tok", "ZS_A2A_PORT": "0"}):
            server = bridge.build_a2a_server(bridge.Bridge(), asyncio.new_event_loop())
        self.assertEqual(server.token, "tok")


if __name__ == "__main__":
    unittest.main()
