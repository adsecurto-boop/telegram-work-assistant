"""
MCP Tool Policy for Security and Confirmation Enforcements.
Determines whether a tool execution requires user confirmation,
and formats confirmation previews for external write operations.
"""
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from mcp_registry import ToolDescriptor, RiskLevel, effective_risk_for_call, required_capabilities_for_call


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


@dataclass
class ExternalActionPolicyResult:
    decision: str
    arguments_hash: str
    risk_level: RiskLevel
    required_capabilities: list[str]


class ExternalActionPolicy:
    """Canonical confirmation and integrity policy for every external adapter.

    MCP tools and first-party adapters may classify their own operation, but the
    confirmation decision and immutable argument fingerprint live in one place.
    """
    @staticmethod
    def evaluate(arguments: Dict[str, Any], risk_level: RiskLevel,
                 required_capabilities: list[str] | tuple[str, ...] | set[str]) -> ExternalActionPolicyResult:
        required = sorted(required_capabilities)
        decision = (PolicyDecision.CONFIRMATION_REQUIRED if risk_level in {
            RiskLevel.EXTERNAL_WRITE, RiskLevel.DESTRUCTIVE,
            RiskLevel.PRIVILEGED, RiskLevel.UNKNOWN_EXTERNAL,
        } else PolicyDecision.ALLOWED)
        return ExternalActionPolicyResult(
            decision=decision,
            arguments_hash=hashlib.sha256(json.dumps(arguments, sort_keys=True, default=str).encode()).hexdigest(),
            risk_level=risk_level,
            required_capabilities=required)


class ToolPolicy:
    """Enforces safety rules on tool calls."""

    @staticmethod
    def evaluate(tool_desc: ToolDescriptor, arguments: Dict[str, Any]) -> ToolPolicyResult:
        effective_risk = effective_risk_for_call(tool_desc, arguments)
        required = sorted(required_capabilities_for_call(tool_desc, arguments))
        external_policy = ExternalActionPolicy.evaluate(arguments, effective_risk, required)
        # READ_ONLY tools are automatically allowed
        if effective_risk == RiskLevel.READ_ONLY:
            return ToolPolicyResult(
                decision=PolicyDecision.ALLOWED,
                tool_desc=tool_desc,
                reason="Read-only operation permitted automatically"
            )

        # EXTERNAL_WRITE and DESTRUCTIVE tools require explicit confirmation
        if external_policy.decision == PolicyDecision.CONFIRMATION_REQUIRED:
            preview = ToolPolicy.format_confirmation_preview(tool_desc, arguments)
            proposal = {
                "tool_id": tool_desc.canonical_id,
                "gemini_name": tool_desc.gemini_name,
                "server": tool_desc.server_name,
                "arguments": arguments,
                "risk_level": effective_risk.value,
                "required_capabilities": required,
                "arguments_hash": external_policy.arguments_hash,
            }
            return ToolPolicyResult(
                decision=PolicyDecision.CONFIRMATION_REQUIRED,
                tool_desc=tool_desc,
                reason=f"Action requires explicit user confirmation ({effective_risk.value})",
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
