"""Official MCP SDK based client manager with long-lived server sessions."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

from mcp_registry import ToolRegistry

logger = logging.getLogger("mcp_manager")
SUPPORTED_TRANSPORTS = {"stdio", "streamable_http"}
TRUSTED_ANNOTATION_SERVERS = {"github"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_env_value(value: Any) -> str:
    text = str(value)
    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}",
                  lambda match: os.environ.get(match.group(1), ""), text)


def _as_dict(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(by_alias=True, exclude_none=True)
    return {}


def _redact_model_value(value: Any) -> Any:
    from ai import AIPayloadBuilder
    if isinstance(value, str):
        return AIPayloadBuilder.redact_text(value)
    if isinstance(value, dict):
        return {str(key): _redact_model_value(item) for key, item in value.items()
                if str(key).casefold() not in {"authorization", "access_token", "refresh_token"}}
    if isinstance(value, list):
        return [_redact_model_value(item) for item in value]
    return value


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

    def model_payload(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "tool": self.tool_id,
            "data": _redact_model_value(self.data),
            "text": _redact_model_value(self.text),
            "truncated": self.truncated,
            **({"error": _redact_model_value(self.error)} if self.error else {}),
        }

    def to_untrusted_prompt_block(self) -> str:
        return (
            f'<UNTRUSTED_TOOL_RESULT tool="{self.tool_id}">\n'
            f'{json.dumps(self.model_payload(), default=str, ensure_ascii=False)}\n'
            f'</UNTRUSTED_TOOL_RESULT>'
        )


class MCPServerConnection:
    """One configured SDK client and its transport/resource lifecycle."""

    def __init__(self, name: str, config: Dict[str, Any]):
        self.name = name
        self.config = dict(config)
        self.enabled = bool(config.get("enabled", False))
        self.transport = config.get("transport", "stdio")
        self.status = "disabled"
        self.protocol_version: Optional[str] = None
        self.last_error: Optional[str] = None
        self.last_connected: Optional[str] = None
        self.last_successful_tool_call: Optional[str] = None
        self.client: Optional[Client] = None
        self._http_client: Any = None

    async def start(self, timeout: float = 10.0) -> bool:
        if not self.enabled:
            return False
        self.status = "connecting"
        try:
            if self.transport == "stdio":
                raw_env = self.config.get("env", {})
                params = StdioServerParameters(
                    command=self.config["command"],
                    args=list(self.config.get("args", [])),
                    env={k: _resolve_env_value(v) for k, v in raw_env.items()},
                    cwd=self.config.get("cwd"),
                )
                self.client = Client(params)
            elif self.transport == "streamable_http":
                import httpx2
                url = self.config.get("url")
                if not url:
                    raise ValueError(f"MCP server '{self.name}' requires a url")
                raw_headers = self.config.get("headers", {})
                missing_env = [
                    var for value in raw_headers.values()
                    for var in re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", str(value))
                    if not os.environ.get(var)
                ]
                headers = {k: _resolve_env_value(v) for k, v in raw_headers.items()}
                if missing_env or any(not value for value in headers.values()):
                    raise RuntimeError("required credential environment variable is unavailable")
                self._http_client = httpx2.AsyncClient(headers=headers)
                transport = streamable_http_client(url, http_client=self._http_client)
                self.client = Client(transport)
            else:
                raise ValueError(f"Unsupported MCP transport: {self.transport}")

            await asyncio.wait_for(self.client.__aenter__(), timeout=timeout)
            self.protocol_version = str(self.client.protocol_version)
            self.status = "ready"
            self.last_connected = _utc_now()
            self.last_error = None
            return True
        except Exception as exc:
            self.status = "failed"
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("Failed to start MCP server %s: %s", self.name, exc)
            await self.shutdown(final_status="failed")
            return False

    async def list_tools(self, timeout: float = 10.0) -> List[Any]:
        if not self.client:
            return []
        result = await asyncio.wait_for(self.client.list_tools(), timeout=timeout)
        return list(result.tools)

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any], timeout: float = 30.0) -> Any:
        if not self.client:
            raise RuntimeError(f"MCP server {self.name} is not connected")
        return await asyncio.wait_for(
            self.client.call_tool(tool_name, arguments, read_timeout_seconds=timeout),
            timeout=timeout + 1,
        )

    async def shutdown(self, final_status: str = "disabled") -> None:
        client, self.client = self.client, None
        if client:
            try:
                await client.__aexit__(None, None, None)
            except Exception as exc:
                logger.debug("MCP client close failed for %s: %s", self.name, exc)
        if self._http_client:
            try:
                await self._http_client.aclose()
            except Exception:
                pass
            self._http_client = None
        self.status = final_status


MCPServerProcess = MCPServerConnection


class MCPManager:
    def __init__(self, config_path: Optional[Path] = None, registry: Optional[ToolRegistry] = None):
        self.config_path = config_path or (Path(__file__).parent / "mcp_config.json")
        self.registry = registry or ToolRegistry()
        self.servers: Dict[str, MCPServerConnection] = {}
        self.max_result_chars = 12000

    def load_config(self, config_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if config_override is not None:
            cfg = config_override
        elif self.config_path.exists():
            cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        else:
            cfg = {"mcpServers": {}}
        self.servers.clear()
        for name, server_config in cfg.get("mcpServers", {}).items():
            transport = server_config.get("transport", "stdio")
            if transport not in SUPPORTED_TRANSPORTS:
                raise ValueError(f"Unsupported MCP transport '{transport}' for server '{name}'")
            if transport == "stdio" and server_config.get("enabled") and not server_config.get("command"):
                raise ValueError(f"MCP stdio server '{name}' requires a command")
            self.servers[name] = MCPServerConnection(name, server_config)
        return cfg

    async def initialize_all(self, timeout: float = 10.0) -> None:
        for name, server in self.servers.items():
            if await server.start(timeout=timeout):
                await self.discover_server_tools(name, timeout=timeout)

    async def discover_server_tools(self, server_name: str, timeout: float = 10.0) -> None:
        server = self.servers.get(server_name)
        if not server or server.status != "ready":
            return
        try:
            raw_tools = await server.list_tools(timeout=timeout)
            self.registry.clear_server_tools(server_name)
            for tool in raw_tools:
                annotations = _as_dict(getattr(tool, "annotations", None))
                self.registry.register_tool(
                    server_name=server_name,
                    original_name=tool.name,
                    description=getattr(tool, "description", "") or "",
                    input_schema=getattr(tool, "input_schema", None) or {},
                    external=True,
                    annotations=annotations,
                    trusted_annotations=server_name.casefold() in TRUSTED_ANNOTATION_SERVERS,
                )
        except Exception as exc:
            server.status = "degraded"
            server.last_error = f"Tool discovery failed: {type(exc).__name__}: {exc}"
            logger.warning("Error discovering tools for %s: %s", server_name, exc)

    async def call_tool(self, name: str, arguments: Dict[str, Any], timeout: float = 30.0) -> ToolResult:
        desc = self.registry.get_by_gemini_name(name) or self.registry.get_by_canonical_id(name)
        if not desc:
            return ToolResult(name, name, False, error=f"Unknown tool '{name}' in registry")
        server = self.servers.get(desc.server_name)
        if not server or server.status != "ready":
            status = server.status if server else "not_configured"
            return ToolResult(desc.canonical_id, desc.gemini_name, False,
                              error=f"MCP server '{desc.server_name}' is unavailable (status={status})")
        try:
            result = await server.call_tool(desc.original_name, arguments, timeout)
            structured = getattr(result, "structured_content", None)
            chunks = []
            content_data = []
            for item in getattr(result, "content", []) or []:
                item_text = getattr(item, "text", None)
                if item_text:
                    chunks.append(item_text)
                    content_data.append({"type": "text", "text": item_text})
            data = structured if structured is not None else {"content": content_data}
            full_text = "\n".join(chunks) or json.dumps(data, default=str, ensure_ascii=False)
            truncated = len(full_text) > self.max_result_chars
            if truncated:
                full_text = full_text[:self.max_result_chars]
            success = not bool(getattr(result, "is_error", False))
            if success:
                server.last_successful_tool_call = _utc_now()
            return ToolResult(desc.canonical_id, desc.gemini_name, success,
                              data=data, text=full_text, truncated=truncated)
        except Exception as exc:
            server.last_error = f"Tool call failed: {type(exc).__name__}: {exc}"
            return ToolResult(desc.canonical_id, desc.gemini_name, False,
                              error="External tool call failed")

    def validate_tool_call(self, name: str, arguments: Dict[str, Any]):
        from jsonschema import validate
        desc = self.registry.get_by_gemini_name(name) or self.registry.get_by_canonical_id(name)
        if not desc:
            raise ValueError("Persisted MCP tool is no longer available.")
        server = self.servers.get(desc.server_name)
        if not server or server.status != "ready":
            raise ValueError(f"MCP server '{desc.server_name}' is not ready.")
        validate(instance=arguments, schema=desc.input_schema or {"type": "object"})
        return desc

    def get_health_status(self) -> Dict[str, Any]:
        return {
            name: {
                "enabled": server.enabled,
                "transport": server.transport,
                "status": server.status,
                "protocol_version": server.protocol_version,
                "tool_count": len(self.registry.list_tools(server_name=name)),
                "tools": len(self.registry.list_tools(server_name=name)),
                "last_connected": server.last_connected,
                "last_error": server.last_error,
                "last_successful_tool_call": server.last_successful_tool_call,
            }
            for name, server in self.servers.items()
        }

    async def shutdown(self) -> None:
        for server in self.servers.values():
            await server.shutdown()
        self.registry = ToolRegistry()
