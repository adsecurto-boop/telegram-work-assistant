"""Opt-in harmless read smoke for the configured official GitHub MCP endpoint."""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_manager import MCPManager
from mcp_registry import RiskLevel


async def main() -> int:
    if not os.environ.get("GITHUB_TOKEN"):
        print("live_github_smoke=skipped")
        print("reason=GITHUB_TOKEN_not_configured")
        return 0
    manager = MCPManager()
    config = manager.load_config()
    config["mcpServers"]["github"]["enabled"] = True
    manager.load_config(config)
    try:
        await manager.initialize_all(timeout=20)
        health = manager.get_health_status()["github"]
        if health["status"] != "ready":
            print("live_github_smoke=failed")
            print(f"status={health['status']}")
            return 1
        tool = next((item for item in manager.registry.list_tools("github")
                     if item.original_name == "get_me" and item.risk_level == RiskLevel.READ_ONLY), None)
        if not tool:
            print("live_github_smoke=failed")
            print("reason=harmless_read_tool_unavailable")
            return 1
        result = await manager.call_tool(tool.gemini_name, {})
        print(f"live_github_smoke={'passed' if result.success else 'failed'}")
        print(f"mcp_protocol={health['protocol_version']}")
        print("tool=get_me")
        return 0 if result.success else 1
    finally:
        await manager.shutdown()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
