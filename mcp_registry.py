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
    return RiskLevel.UNKNOWN_EXTERNAL


class ToolRegistry:
    def __init__(self):
        self._tools_by_gemini_name: Dict[str, ToolDescriptor] = {}
        self._tools_by_canonical_id: Dict[str, ToolDescriptor] = {}
        self._capability_cache: Optional[Dict[str, tuple[str, ...]]] = None

    @staticmethod
    def capabilities_for_tool(tool: ToolDescriptor) -> frozenset[str]:
        """Derive stable routing/authorization capabilities from live tool metadata."""
        service = tool.server_name.casefold().replace('-', '_')
        name = tool.original_name.casefold().replace('-', '_')
        identity = f"{tool.server_name} {tool.original_name} {tool.description}".casefold()
        if any(token in identity for token in ("github", "repository", "pull request", "issue")):
            service = "github"
        elif any(token in identity for token in ("gmail", "email", "inbox")):
            service = "gmail"
        elif any(token in identity for token in ("calendar", "meeting")):
            service = "calendar"
        elif any(token in identity for token in ("google drive", "document", "drive file")):
            service = "drive"
        caps = {f"{service}.available"}
        if service == "github":
            if any(word in name for word in ("issue", "pull", "repo", "commit", "branch")):
                caps.add("github.read")
            mappings = {
                "create_issue": "github.issue.create",
                "update_issue": "github.issue.update",
                "close_issue": "github.issue.update",
                "reopen_issue": "github.issue.update",
                "add_issue_comment": "github.issue.comment",
                "add_comment": "github.issue.comment",
                "merge_pull_request": "github.pr.merge",
                "add_issue_label": "github.issue.label",
                "add_label": "github.issue.label",
                "remove_label": "github.issue.label",
                "assign": "github.issue.assign",
            }
            for marker, capability in mappings.items():
                if marker in name:
                    caps.add(capability)
        elif service in {"gmail", "email", "mail"}:
            caps.update({"gmail.available", "gmail.read"})
            if any(word in name for word in ("send", "reply", "draft")):
                caps.add("gmail.message.send")
        elif "calendar" in service:
            caps.update({"calendar.available", "calendar.read"})
            if any(word in name for word in ("create", "schedule", "insert")):
                caps.add("calendar.event.create")
        elif "drive" in service:
            caps.update({"drive.available", "drive.read"})
            if any(word in name for word in ("create", "update", "edit", "move", "write")):
                caps.add("drive.file.write")
        if tool.risk_level in (RiskLevel.DESTRUCTIVE, RiskLevel.PRIVILEGED):
            caps.add("external.delete")
        if tool.risk_level == RiskLevel.READ_ONLY:
            caps.add(f"{service}.read")
        return frozenset(caps)

    def capability_index(self) -> Dict[str, tuple[str, ...]]:
        if self._capability_cache is None:
            index: Dict[str, List[str]] = {}
            for tool in self.list_tools():
                for capability in self.capabilities_for_tool(tool):
                    index.setdefault(capability, []).append(tool.canonical_id)
            self._capability_cache = {
                capability: tuple(sorted(tool_ids))
                for capability, tool_ids in sorted(index.items())
            }
        return dict(self._capability_cache)

    def supports(self, *capabilities: str) -> bool:
        index = self.capability_index()
        return any(capability in index for capability in capabilities)

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
        self._capability_cache = None
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
        self._capability_cache = None
