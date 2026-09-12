"""Opt-in live Gemini + local mock MCP smoke check (never prints credentials)."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from agent_orchestrator import GeminiAgent
from gemini_tool_model import GeminiToolModel
from mcp_manager import MCPManager


async def main() -> int:
    if not config.AI_KEY or not config.AI_MODEL:
        print("live_smoke=skipped reason=gemini_not_configured")
        return 2
    manager = MCPManager()
    manager.load_config({"mcpServers": {"mock": {
        "enabled": True, "transport": "stdio", "command": sys.executable,
        "args": ["-m", "tests.mock_mcp_server"],
        "read_only_tools": ["search_issues", "get_issue"], "env": {},
    }}})
    model = GeminiToolModel(config.AI_KEY, config.AI_MODEL)
    try:
        await manager.initialize_all(timeout=10)
        result = await GeminiAgent(manager, tool_model=model).run(
            "Use the available search_issues tool to find the Wayland screenshot issue, then answer with its issue number.")
        round_trip = bool(result.success and result.tool_calls_executed and "61" in result.final_text)
        print(f"live_smoke={'passed' if round_trip else 'failed'}")
        print(f"model={config.AI_MODEL}")
        print(f"mcp_protocol={manager.get_health_status()['mock']['protocol_version']}")
        print(f"tool_calls={len(result.tool_calls_executed)}")
        print(f"function_response_role={model.last_function_response_role}")
        print(f"final_used_tool_result={bool('61' in result.final_text)}")
        return 0 if round_trip else 1
    finally:
        await model.close()
        await manager.shutdown()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
