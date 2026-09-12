"""
MCP Tool Policy for Security and Confirmation Enforcements.
Determines whether a tool execution requires user confirmation,
and formats confirmation previews for external write operations.
"""
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from mcp_registry import ToolDescriptor, RiskLevel


class PolicyDecision:
    ALLOWED = "ALLOWED"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    DENIED = "DENIED"


@dataclass
class ToolPolicyResult:
    decision: str
    tool_desc: ToolDescriptor
    reason: str
    confirmation_preview: Optional[str] = None
    proposal_data: Optional[Dict[str, Any]] = None


class ToolPolicy:
    """Enforces safety rules on tool calls."""

    @staticmethod
    def evaluate(tool_desc: ToolDescriptor, arguments: Dict[str, Any]) -> ToolPolicyResult:
        # READ_ONLY tools are automatically allowed
        if tool_desc.risk_level == RiskLevel.READ_ONLY:
            return ToolPolicyResult(
                decision=PolicyDecision.ALLOWED,
                tool_desc=tool_desc,
                reason="Read-only operation permitted automatically"
            )

        # EXTERNAL_WRITE and DESTRUCTIVE tools require explicit confirmation
        if tool_desc.risk_level in [
            RiskLevel.EXTERNAL_WRITE, RiskLevel.DESTRUCTIVE,
            RiskLevel.PRIVILEGED, RiskLevel.UNKNOWN_EXTERNAL,
        ]:
            preview = ToolPolicy.format_confirmation_preview(tool_desc, arguments)
            proposal = {
                "tool_id": tool_desc.canonical_id,
                "gemini_name": tool_desc.gemini_name,
                "server": tool_desc.server_name,
                "arguments": arguments,
                "risk_level": tool_desc.risk_level.value
            }
            return ToolPolicyResult(
                decision=PolicyDecision.CONFIRMATION_REQUIRED,
                tool_desc=tool_desc,
                reason=f"Action requires explicit user confirmation ({tool_desc.risk_level.value})",
                confirmation_preview=preview,
                proposal_data=proposal
            )

        # LOCAL_WRITE
        return ToolPolicyResult(
            decision=PolicyDecision.ALLOWED,
            tool_desc=tool_desc,
            reason="Local write operation permitted"
        )

    @staticmethod
    def format_confirmation_preview(tool_desc: ToolDescriptor, arguments: Dict[str, Any]) -> str:
        s_name = tool_desc.server_name.upper()
        o_name = tool_desc.original_name.replace('_', ' ').title()
        
        args_str = ""
        for k, v in arguments.items():
            args_str += f"  - **{k}**: `{v}`\n"

        return (
            f"⚠️ **External Tool Action Requires Confirmation**\n\n"
            f"**Integration**: {s_name}\n"
            f"**Action**: {o_name}\n"
            f"**Details**:\n{args_str}\n"
            f"Confirm execution?"
        )
