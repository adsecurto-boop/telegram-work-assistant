import asyncio
import socket
import threading
import time
import unittest

import uvicorn
from mcp.server import MCPServer
from pydantic import BaseModel

from mcp_manager import MCPManager


class StateResult(BaseModel):
    number: int
    state: str


class StreamableHTTPTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        server = MCPServer("hardening-http-test")

        @server.tool(structured_output=True)
        def current_state(issue_number: int) -> StateResult:
            """Read current issue state."""
            return StateResult(number=issue_number, state="open")

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        app = server.streamable_http_app(stateless_http=True, host="127.0.0.1")
        self.http_server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="critical"))
        self.thread = threading.Thread(target=self.http_server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 5
        while not self.http_server.started and time.time() < deadline:
            await asyncio.sleep(0.02)
        self.assertTrue(self.http_server.started)

        self.manager = MCPManager()
        self.manager.load_config({"mcpServers": {"http_mock": {
            "enabled": True, "transport": "streamable_http",
            "url": f"http://127.0.0.1:{self.port}/mcp",
        }}})
        await self.manager.initialize_all(timeout=5)

    async def asyncTearDown(self):
        await self.manager.shutdown()
        self.http_server.should_exit = True
        self.thread.join(timeout=5)

    async def test_streamable_http_lifecycle_discovery_and_call(self):
        health = self.manager.get_health_status()["http_mock"]
        self.assertEqual(health["status"], "ready")
        self.assertEqual(health["transport"], "streamable_http")
        self.assertTrue(str(health["protocol_version"]).startswith("2026-"))
        result = await self.manager.call_tool("http_mock.current_state", {"issue_number": 61})
        self.assertTrue(result.success)
        self.assertEqual(result.data["number"], 61)
        self.assertEqual(result.data["state"], "open")
        self.assertIsNotNone(self.manager.get_health_status()["http_mock"]["last_successful_tool_call"])


if __name__ == "__main__":
    unittest.main()
