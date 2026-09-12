"""
Tool Registry module for Personal Work Assistant MCP Integration.
Maintains namespaced tool definitions, Gemini function names, and security risk levels.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class RiskLevel(Enum):
    READ_ONLY = "READ_ONLY"
    LOCAL_WRITE = "LOCAL_WRITE"
    EXTERNAL_WRITE = "EXTERNAL_WRITE"
    DESTRUCTIVE = "DESTRUCTIVE"
    PRIVILEGED = "PRIVILEGED"
    UNKNOWN_EXTERNAL = "UNKNOWN_EXTERNAL"


@dataclass
class ToolDescriptor:
    canonical_id: str          # e.g., "github.search_issues"
    gemini_name: str           # e.g., "mcp__github__search_issues"
    server_name: str           # e.g., "github"
    original_name: str         # e.g., "search_issues"
    description: str
    input_schema: Dict[str, Any]
    risk_level: RiskLevel = RiskLevel.UNKNOWN_EXTERNAL
    external: bool = True
    enabled: bool = True
    read_only_hint: Optional[bool] = None
    destructive_hint: Optional[bool] = None
    idempotent_hint: Optional[bool] = None
    open_world_hint: Optional[bool] = None


def format_gemini_name(server_name: str, tool_name: str) -> str:
    """Format a valid Gemini function name from server and tool names."""
    clean_server = server_name.replace('-', '_').replace('.', '_')
    clean_tool = tool_name.replace('-', '_').replace('.', '_')
    return f"mcp__{clean_server}__{clean_tool}"


_GITHUB_WRITES = {
    "merge_pull_request", "create_issue", "update_issue", "add_issue_comment",
    "create_pull_request", "request_review", "mark_notifications_read",
    "assign_copilot_to_issue", "add_sub_issue", "reprioritize_sub_issue",
    "push_files", "create_or_update_file", "create_branch", "fork_repository",
}
_READ_PREFIXES = ("get_", "list_", "search_", "read_", "fetch_", "show_", "view_")


def classify_tool_risk(
    server_name: str,
    tool_name: str,
    *,
    external: bool = True,
    read_only_hint: Optional[bool] = None,
    destructive_hint: Optional[bool] = None,
    trusted_annotations: bool = False,
) -> RiskLevel:
    """Classify risk conservatively; unknown external operations never auto-run."""
    name_lower = tool_name.lower()

    if server_name.casefold() == "github" and name_lower in _GITHUB_WRITES:
        return RiskLevel.EXTERNAL_WRITE
    if destructive_hint is True:
        return RiskLevel.DESTRUCTIVE
    if any(k in name_lower for k in ['delete', 'destroy', 'drop', 'purge', 'remove', 'unlink']):
        return RiskLevel.DESTRUCTIVE
    if any(k in name_lower for k in [
        'create', 'add', 'insert', 'update', 'edit', 'patch', 'send', 'post',
        'put', 'write', 'move', 'merge', 'assign', 'label', 'approve', 'deploy',
        'publish', 'close', 'reopen', 'cancel', 'trigger', 'dispatch', 'invite',
    ]):
        if not external or server_name in ['local', 'assistant', 'system']:
            return RiskLevel.LOCAL_WRITE
        return RiskLevel.EXTERNAL_WRITE
    if not external:
        return RiskLevel.READ_ONLY
    if trusted_annotations and read_only_hint is True:
        return RiskLevel.READ_ONLY
    if name_lower.startswith(_READ_PREFIXES):
        return RiskLevel.READ_ONLY
    return RiskLevel.UNKNOWN_EXTERNAL


class ToolRegistry:
    def __init__(self):
        self._tools_by_gemini_name: Dict[str, ToolDescriptor] = {}
        self._tools_by_canonical_id: Dict[str, ToolDescriptor] = {}

    def register_tool(
        self,
        server_name: str,
        original_name: str,
        description: str,
        input_schema: Dict[str, Any],
        risk_level: Optional[RiskLevel] = None,
        external: bool = True,
        annotations: Optional[Dict[str, Any]] = None,
        trusted_annotations: bool = False,
    ) -> ToolDescriptor:
        canonical_id = f"{server_name}.{original_name}"
        gemini_name = format_gemini_name(server_name, original_name)
        
        annotations = annotations or {}
        read_only_hint = annotations.get("readOnlyHint", annotations.get("read_only_hint"))
        destructive_hint = annotations.get("destructiveHint", annotations.get("destructive_hint"))
        idempotent_hint = annotations.get("idempotentHint", annotations.get("idempotent_hint"))
        open_world_hint = annotations.get("openWorldHint", annotations.get("open_world_hint"))
        if risk_level is None:
            risk_level = classify_tool_risk(
                server_name, original_name, external=external,
                read_only_hint=read_only_hint,
                destructive_hint=destructive_hint,
                trusted_annotations=trusted_annotations,
            )

        desc = ToolDescriptor(
            canonical_id=canonical_id,
            gemini_name=gemini_name,
            server_name=server_name,
            original_name=original_name,
            description=description,
            input_schema=input_schema or {},
            risk_level=risk_level,
            external=external,
            enabled=True,
            read_only_hint=read_only_hint,
            destructive_hint=destructive_hint,
            idempotent_hint=idempotent_hint,
            open_world_hint=open_world_hint,
        )

        self._tools_by_gemini_name[gemini_name] = desc
        self._tools_by_canonical_id[canonical_id] = desc
        return desc

    def get_by_gemini_name(self, gemini_name: str) -> Optional[ToolDescriptor]:
        return self._tools_by_gemini_name.get(gemini_name)

    def get_by_canonical_id(self, canonical_id: str) -> Optional[ToolDescriptor]:
        return self._tools_by_canonical_id.get(canonical_id)

    def list_tools(self, server_name: Optional[str] = None) -> List[ToolDescriptor]:
        tools = list(self._tools_by_gemini_name.values())
        if server_name:
            tools = [t for t in tools if t.server_name == server_name]
        return [t for t in tools if t.enabled]

    def clear_server_tools(self, server_name: str):
        to_remove = [k for k, v in self._tools_by_gemini_name.items() if v.server_name == server_name]
        for k in to_remove:
            desc = self._tools_by_gemini_name.pop(k, None)
            if desc:
                self._tools_by_canonical_id.pop(desc.canonical_id, None)
