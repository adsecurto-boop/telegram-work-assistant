"""
Central MCP Manager for Personal Work Assistant.
Handles MCP server lifecycle, JSON-RPC communication over stdio/HTTP,
tool discovery, health tracking, timeouts, and result normalization.
"""
import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from mcp_registry import ToolRegistry, ToolDescriptor, RiskLevel

logger = logging.getLogger("mcp_manager")


@dataclass
class ToolResult:
    tool_id: str
    gemini_name: str
    success: bool
    data: Any = None
    text: str = ""
    error: Optional[str] = None
    truncated: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_untrusted_prompt_block(self) -> str:
        """
        Format tool result wrapped in <UNTRUSTED_TOOL_RESULT> tags
        to prevent prompt injection.
        """
        content = self.text if self.text else json.dumps(self.data, indent=2, default=str)
        if self.truncated:
            content += "\n... [Output truncated for budget limit]"
        
        status_str = "SUCCESS" if self.success else f"ERROR: {self.error}"
        return (
            f'<UNTRUSTED_TOOL_RESULT tool="{self.tool_id}" status="{status_str}">\n'
            f'{content}\n'
            f'</UNTRUSTED_TOOL_RESULT>'
        )


class MCPServerProcess:
    """Manages stdio JSON-RPC process lifecycle for an MCP server."""

    def __init__(self, name: str, command: str, args: List[str], env: Dict[str, str]):
        self.name = name
        self.command = command
        self.args = args
        self.env = env
        self.process: Optional[asyncio.subprocess.Process] = None
        self.status = "disabled"
        self.last_error: Optional[str] = None
        self.last_connected: Optional[str] = None
        self.request_id = 0

    async def start(self, timeout: float = 10.0) -> bool:
        self.status = "connecting"
        try:
            full_env = os.environ.copy()
            full_env.update(self.env)

            self.process = await asyncio.create_subprocess_exec(
                self.command,
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=full_env
            )

            # Standard MCP Initialize Request
            init_req = {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "PersonalWorkAssistant",
                        "version": "1.0.0"
                    }
                }
            }

            resp = await self._send_request(init_req, timeout=timeout)
            if not resp or "error" in resp:
                err_msg = resp.get("error", {}).get("message", "Initialize failed") if resp else "No response"
                self.status = "failed"
                self.last_error = f"Initialization error: {err_msg}"
                return False

            # Send initialized notification
            init_notif = {
                "jsonrpc": "2.0",
                "method": "notifications/initialized"
            }
            await self._send_notification(init_notif)

            self.status = "ready"
            self.last_connected = datetime.now().isoformat()
            self.last_error = None
            return True

        except Exception as e:
            self.status = "failed"
            self.last_error = str(e)
            logger.warning(f"Failed to start MCP server {self.name}: {e}")
            return False

    def _next_id(self) -> int:
        self.request_id += 1
        return self.request_id

    async def _send_notification(self, notif: Dict[str, Any]):
        if not self.process or not self.process.stdin:
            return
        line = json.dumps(notif) + "\n"
        self.process.stdin.write(line.encode('utf-8'))
        await self.process.stdin.drain()

    async def _send_request(self, req: Dict[str, Any], timeout: float = 10.0) -> Optional[Dict[str, Any]]:
        if not self.process or not self.process.stdin or not self.process.stdout:
            raise RuntimeError(f"MCP server {self.name} process not running")

        line = json.dumps(req) + "\n"
        self.process.stdin.write(line.encode('utf-8'))
        await self.process.stdin.drain()

        async def _read_response():
            while True:
                line_bytes = await self.process.stdout.readline()
                if not line_bytes:
                    return None
                text = line_bytes.decode('utf-8').strip()
                if not text:
                    continue
                try:
                    data = json.loads(text)
                    if data.get("id") == req.get("id"):
                        return data
                except json.JSONDecodeError:
                    continue

        return await asyncio.wait_for(_read_response(), timeout=timeout)

    async def list_tools(self, timeout: float = 10.0) -> List[Dict[str, Any]]:
        req = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/list",
            "params": {}
        }
        resp = await self._send_request(req, timeout=timeout)
        if resp and "result" in resp:
            return resp["result"].get("tools", [])
        return []

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
        req = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments or {}
            }
        }
        resp = await self._send_request(req, timeout=timeout)
        if not resp:
            raise TimeoutError(f"Tool call {tool_name} on {self.name} timed out")
        if "error" in resp:
            raise RuntimeError(resp["error"].get("message", f"Tool call {tool_name} failed"))
        return resp.get("result", {})

    async def shutdown(self):
        if self.process:
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=3.0)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None
        self.status = "disabled"


class MCPManager:
    def __init__(self, config_path: Optional[Path] = None, registry: Optional[ToolRegistry] = None):
        self.config_path = config_path or (Path(__file__).parent / "mcp_config.json")
        self.registry = registry or ToolRegistry()
        self.servers: Dict[str, MCPServerProcess] = {}
        self.max_result_chars = 12000

    def load_config(self, config_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if config_override:
            cfg = config_override
        elif self.config_path.exists():
            with open(self.config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        else:
            cfg = {"mcpServers": {}}

        mcp_servers = cfg.get("mcpServers", {})
        for name, s_cfg in mcp_servers.items():
            if not s_cfg.get("enabled", False):
                continue

            command = s_cfg.get("command", "npx")
            args = s_cfg.get("args", [])
            raw_env = s_cfg.get("env", {})

            # Resolve environment variables
            resolved_env = {}
            for k, v in raw_env.items():
                if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
                    env_var = v[2:-1]
                    resolved_env[k] = os.environ.get(env_var, "")
                else:
                    resolved_env[k] = str(v)

            self.servers[name] = MCPServerProcess(
                name=name,
                command=command,
                args=args,
                env=resolved_env
            )
        return cfg

    async def initialize_all(self, timeout: float = 10.0):
        """Start all configured enabled servers and discover tools."""
        for name, server in list(self.servers.items()):
            ok = await server.start(timeout=timeout)
            if ok:
                await self.discover_server_tools(name, timeout=timeout)

    async def discover_server_tools(self, server_name: str, timeout: float = 10.0):
        server = self.servers.get(server_name)
        if not server or server.status != "ready":
            return

        try:
            raw_tools = await server.list_tools(timeout=timeout)
            self.registry.clear_server_tools(server_name)
            for tool_def in raw_tools:
                orig_name = tool_def.get("name")
                desc = tool_def.get("description", "")
                schema = tool_def.get("inputSchema", {})
                if orig_name:
                    self.registry.register_tool(
                        server_name=server_name,
                        original_name=orig_name,
                        description=desc,
                        input_schema=schema,
                        external=True
                    )
        except Exception as e:
            server.status = "degraded"
            server.last_error = f"Tool discovery failed: {e}"
            logger.warning(f"Error discovering tools for {server_name}: {e}")

    async def call_tool(self, gemini_or_canonical_name: str, arguments: Dict[str, Any], timeout: float = 30.0) -> ToolResult:
        tool_desc = self.registry.get_by_gemini_name(gemini_or_canonical_name)
        if not tool_desc:
            tool_desc = self.registry.get_by_canonical_id(gemini_or_canonical_name)

        if not tool_desc:
            return ToolResult(
                tool_id=gemini_or_canonical_name,
                gemini_name=gemini_or_canonical_name,
                success=False,
                error=f"Unknown tool '{gemini_or_canonical_name}' in registry"
            )

        server = self.servers.get(tool_desc.server_name)
        if not server or server.status != "ready":
            return ToolResult(
                tool_id=tool_desc.canonical_id,
                gemini_name=tool_desc.gemini_name,
                success=False,
                error=f"MCP server '{tool_desc.server_name}' is unavailable (status={server.status if server else 'not_configured'})"
            )

        try:
            raw_result = await server.call_tool(tool_desc.original_name, arguments, timeout=timeout)
            content_items = raw_result.get("content", [])
            text_chunks = []
            for item in content_items:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_chunks.append(item.get("text", ""))
                elif isinstance(item, str):
                    text_chunks.append(item)

            full_text = "\n".join(text_chunks) if text_chunks else json.dumps(raw_result, default=str)
            
            # Truncate if exceeds budget
            truncated = False
            if len(full_text) > self.max_result_chars:
                full_text = full_text[:self.max_result_chars]
                truncated = True

            return ToolResult(
                tool_id=tool_desc.canonical_id,
                gemini_name=tool_desc.gemini_name,
                success=not raw_result.get("isError", False),
                data=raw_result,
                text=full_text,
                truncated=truncated
            )

        except Exception as e:
            return ToolResult(
                tool_id=tool_desc.canonical_id,
                gemini_name=tool_desc.gemini_name,
                success=False,
                error=str(e)
            )

    def get_health_status(self) -> Dict[str, Any]:
        res = {}
        for name, server in self.servers.items():
            tool_count = len(self.registry.list_tools(server_name=name))
            res[name] = {
                "status": server.status,
                "tools": tool_count,
                "last_error": server.last_error,
                "last_connected": server.last_connected
            }
        return res

    async def shutdown(self):
        for server in self.servers.values():
            await server.shutdown()
        self.servers.clear()
