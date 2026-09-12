"""
Production Assistant Orchestrator for Personal Work Assistant.
Seamlessly routes incoming natural language requests between:
1. Deterministic NLP (unambiguous commands / single simple actions)
2. Gemini ConversationPlan execution (contextual / multi-action work updates)
3. GeminiAgent MCP Tool Execution (external tools, GitHub, Filesystem, etc.)

Maintains active external entity references across conversation turns.
"""
import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from database import Database
from models import TaskStatus
from nlp import NaturalLanguagePipeline, ConversationPlan, PlannedAction, NLIntent, NLEntities, PlanExecutionResult
from nlp_policy import evaluate_action_policy, ActionDecision
from memory_service import build_assistant_context
from mcp_manager import MCPManager, ToolResult
from mcp_registry import ToolRegistry, RiskLevel
from mcp_policy import ToolPolicy, PolicyDecision
from agent_orchestrator import GeminiAgent, AgentRunResult, SYSTEM_INSTRUCTION_MCP_AGENT
from gemini_tool_model import ToolCallingModel
import config

logger = logging.getLogger("assistant_orchestrator")


@dataclass
class OrchestrationResult:
    reply_text: str
    actions_executed: List[Dict[str, Any]] = field(default_factory=list)
    proposal_id: Optional[str] = None
    choices: List[Any] = field(default_factory=list)
    correlation_id: Optional[str] = None
    agent_run_id: Optional[str] = None
    active_external_refs: Optional[Dict[str, Any]] = None
    success: bool = True
    error: Optional[str] = None


@dataclass
class AgentContinuationResult:
    final_answer: str
    external_facts: List[Dict[str, Any]] = field(default_factory=list)
    local_plan_required: bool = False
    local_action_executed: Optional[Dict[str, Any]] = None


class AssistantOrchestrator:

    def __init__(self, db: Database, mcp_manager: Optional[MCPManager] = None,
                 ai_client: Any = None, tool_model: Optional[ToolCallingModel] = None):
        self.db = db
        self.mcp_manager = mcp_manager
        self.ai_client = ai_client
        self.tool_model = tool_model
        self.nlp = NaturalLanguagePipeline(db, ai_client=ai_client)

    async def route_and_process(
        self,
        user_message: str,
        owner_id: int = config.OWNER_ID,
        source_update_id: Optional[int] = None
    ) -> OrchestrationResult:
        text = user_message.strip()

        # 1. Fetch recent conversation turns and active external entity references
        turns = await asyncio.to_thread(self.db.get_recent_turns, owner_id, 10)
        active_refs = self._extract_active_external_refs(turns)

        # 2. Check if external integration tool call is needed
        needs_mcp = self._detect_external_tool_need(text, active_refs)
        if needs_mcp and self.mcp_manager:
            active_tools = self.mcp_manager.registry.list_tools()
            if active_tools:
                return await self._process_mcp_agent_path(text, owner_id, active_refs, source_update_id)
            integration = "GitHub" if any(word in text.casefold() for word in ("github", "issue", "pull request", "repo")) else "the external integration"
            return OrchestrationResult(
                reply_text=(f"I couldn't check {integration} right now. Your local work data "
                            f"and commands are still available. Reference: ERR-{uuid.uuid4().hex[:6].upper()}"),
                active_external_refs=active_refs, success=False, error="integration_unavailable",
            )

        # 3. Check if compound multi-action or contextual plan is needed
        needs_plan = self._detect_plan_need(text)
        has_ai = bool(self.ai_client or getattr(self.nlp, 'ai', None) or getattr(self.nlp, 'gemini_interpreter', None))
        if needs_plan and has_ai:
            return await self._process_conversation_plan_path(text, owner_id, source_update_id)

        # 4. Standard NLP processing (deterministic or single Gemini fallback)
        shift = await asyncio.to_thread(self.db.active_shift)
        reply_txt, interp = await self.nlp.process(text, shift, source_update_id=source_update_id)
        
        prop_id = getattr(interp, 'proposal_id', None) if interp else None
        choices = getattr(interp, 'choices', []) if interp else []
        return OrchestrationResult(
            reply_text=reply_txt,
            actions_executed=[{"intent": interp.intent.value, "summary": interp.proposed_summary}] if interp else [],
            proposal_id=prop_id,
            choices=choices,
            success=True
        )

    def _detect_external_tool_need(self, text: str, active_refs: Optional[Dict[str, Any]] = None) -> bool:
        low = text.lower()
        tool_keywords = [
            "github", "issue", "pull request", "pr", "repo", "repository",
            "gmail", "email", "mail", "inbox",
            "calendar", "meeting", "event",
            "drive", "google document", "google doc"
        ]
        if any(k in low for k in tool_keywords):
            return True
        if active_refs and any(w in low for w in [
            "who created", "who opened", "what labels", "assignee", "comment",
            "is it", "does it", "still open", "closed now", "its state", "its status",
        ]):
            return True
        return False

    def _detect_plan_need(self, text: str) -> bool:
        low = text.lower()
        if any(k in low for k in ["and then", "also", "after that", "remind me", "add that", "wayland", "x11"]):
            return True
        if len(re.split(r'[;.]\s+', text)) > 1:
            return True
        return False

    def _extract_active_external_refs(self, turns: List[Dict[str, Any]]) -> Dict[str, Any]:
        refs = {}
        for turn in turns:
            meta = turn.get("metadata") or turn.get("entities_json") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            ext = meta.get("active_external_refs")
            if isinstance(ext, dict):
                refs.update(ext)
        return refs

    async def _process_mcp_agent_path(
        self,
        text: str,
        owner_id: int,
        active_refs: Dict[str, Any],
        source_update_id: Optional[int]
    ) -> OrchestrationResult:
        agent = GeminiAgent(self.mcp_manager, ai_client=self.ai_client, tool_model=self.tool_model)

        context_data = build_assistant_context(self.db, owner_id=owner_id)
        context_prompt = (
            f"Active Shift: {context_data.get('active_shift')}\n"
            f"Active Case: {context_data.get('active_case')}\n"
            f"Active External References: {json.dumps(active_refs)}\n"
            f"Recent Context:\n{context_data.get('recent_turns')}"
        )

        res: AgentRunResult = await agent.run(user_message=text, context_prompt=context_prompt)

        # Handle write confirmation required
        if res.pending_confirmation:
            prop_data = res.pending_confirmation.proposal_data or {}
            prop_id = f"prop_{uuid.uuid4().hex[:8]}"
            await asyncio.to_thread(
                self.db.create_proposal,
                prop_id=prop_id,
                owner_id=owner_id,
                action_type="mcp_external_write",
                payload_json=json.dumps(prop_data),
                source_update_id=source_update_id
            )
            return OrchestrationResult(
                reply_text=res.final_text,
                proposal_id=prop_id,
                agent_run_id=res.run_id,
                success=True
            )

        new_refs = self._external_refs_from_results(active_refs, res.tool_results, res.final_text)
        continuation = await self._continue_from_external_facts(text, owner_id, res, context_data)
        if continuation.local_action_executed:
            res.tool_calls_executed.append(continuation.local_action_executed)

        return OrchestrationResult(
            reply_text=continuation.final_answer,
            actions_executed=res.tool_calls_executed,
            agent_run_id=res.run_id,
            active_external_refs=new_refs,
            success=res.success,
            error=res.error
        )

    def _external_refs_from_results(self, existing: Dict[str, Any], results: List[ToolResult],
                                    final_text: str) -> Dict[str, Any]:
        refs = dict(existing)
        for result in results:
            candidates: List[Dict[str, Any]] = []
            data = result.data
            if isinstance(data, dict):
                candidates.append(data)
                for key in ("items", "issues", "data", "result"):
                    value = data.get(key)
                    if isinstance(value, list):
                        candidates.extend(item for item in value if isinstance(item, dict))
                    elif isinstance(value, dict):
                        candidates.append(value)
            for item in candidates:
                number = item.get("number") or item.get("issue_number")
                repository = item.get("repository") or item.get("repo")
                url = item.get("html_url") or item.get("url")
                if not repository and isinstance(url, str):
                    match = re.search(r"github\.com/([^/]+/[^/]+)/(?:issues|pull)/\d+", url)
                    repository = match.group(1) if match else None
                if number and repository:
                    refs["github_issue"] = {
                        "provider": "github", "repository": repository,
                        "number": int(number), "url": url,
                        "title": item.get("title"),
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                        "source_tool": result.tool_id,
                    }
                    return refs
        match = re.search(r'issue\s+#?(\d+)', final_text, re.IGNORECASE)
        old_repo = (existing.get("github_issue") or {}).get("repository")
        if match and old_repo:
            refs["github_issue"] = {**existing.get("github_issue", {}),
                                    "number": int(match.group(1)),
                                    "fetched_at": datetime.now(timezone.utc).isoformat()}
        return refs

    async def _continue_from_external_facts(self, text: str, owner_id: int,
                                            result: AgentRunResult,
                                            context_data: Dict[str, Any]) -> AgentContinuationResult:
        facts = [r.model_payload() for r in result.tool_results if r.success]
        low = text.casefold()
        conditional_reminder = ("remind me" in low or "follow up" in low) and any(
            phrase in low for phrase in ("if it is", "if it's", "if still", "if the issue")
        )
        continuation = AgentContinuationResult(result.final_text, facts, conditional_reminder)
        if not conditional_reminder:
            return continuation

        state: Optional[str] = None
        for tool_result in result.tool_results:
            if not tool_result.success:
                continue
            if isinstance(tool_result.data, dict):
                value = tool_result.data.get("state") or tool_result.data.get("status")
                if isinstance(value, str):
                    state = value.casefold()
            if not state:
                match = re.search(r"\b(open|closed)\b", tool_result.text, re.IGNORECASE)
                state = match.group(1).casefold() if match else None
        if state != "open":
            if state:
                continuation.final_answer += "\n\nThe condition was not met, so I did not create a reminder."
            return continuation

        active_case = context_data.get("active_case") or {}
        case_id = active_case.get("id") if isinstance(active_case, dict) else None
        if not case_id:
            cases = await asyncio.to_thread(self.db.list_cases, None, 1)
            case_id = cases[0]["id"] if cases else None
        if not case_id:
            continuation.final_answer += "\n\nThe issue is open, but no active case exists to attach a reminder to."
            return continuation
        due_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        correlation_id = f"external_cont_{uuid.uuid4().hex[:10]}"
        followup_id = await asyncio.to_thread(
            self.db.add_followup, case_id, due_at,
            "Re-check external issue state and follow up with development",
            "development", None, "case", correlation_id, "assistant_orchestrator",
        )
        continuation.local_action_executed = {
            "intent": "create_followup", "followup_id": followup_id,
            "case_id": case_id, "condition": "external issue state == open",
        }
        continuation.final_answer += f"\n\nThe issue is open, so I created follow-up #{followup_id} for tomorrow."
        return continuation

    async def _process_conversation_plan_path(
        self,
        text: str,
        owner_id: int,
        source_update_id: Optional[int]
    ) -> OrchestrationResult:
        context_data = build_assistant_context(self.db, owner_id=owner_id)
        plan = None

        if self.ai_client and hasattr(self.ai_client, 'interpret_plan'):
            plan = await self.ai_client.interpret_plan(text, context_data)
        elif getattr(self.nlp, 'ai', None) and hasattr(self.nlp.ai, 'interpret_plan'):
            plan = await self.nlp.ai.interpret_plan(text, context_data)
        elif getattr(self.nlp, 'gemini_interpreter', None) and hasattr(self.nlp.gemini_interpreter, 'interpret_plan'):
            plan = await self.nlp.gemini_interpreter.interpret_plan(text, context_data)

        if not plan or not plan.actions:
            shift = await asyncio.to_thread(self.db.active_shift)
            reply_txt, interp = await self.nlp.process(text, shift)
            return OrchestrationResult(reply_text=reply_txt, success=True)

        shift = await asyncio.to_thread(self.db.active_shift)
        
        # Policy check every planned action for high-risk write confirmation
        high_risk_actions = []
        for action in plan.actions:
            decision, would_mutate, reason = evaluate_action_policy(action, has_active_shift=bool(shift))
            if decision in (ActionDecision.PROPOSE_CONFIRMATION, ActionDecision.REQUIRE_CLARIFICATION):
                high_risk_actions.append(action)

        if high_risk_actions:
            # Generate proposal requiring Telegram confirmation without partial execution
            prop_id = f"prop_{uuid.uuid4().hex[:8]}"
            preview = (
                f"⚠️ **Compound Plan Requires Confirmation**\n\n"
                f"Actions proposed:\n" +
                "\n".join(f"• {a.intent.value}: {a.entities}" for a in plan.actions) +
                f"\n\nConfirm execution?"
            )
            await asyncio.to_thread(
                self.db.create_proposal,
                prop_id=prop_id,
                owner_id=owner_id,
                action_type="compound_plan",
                payload_json=json.dumps(plan.model_dump()),
                source_update_id=source_update_id
            )
            return OrchestrationResult(
                reply_text=preview,
                proposal_id=prop_id,
                success=True
            )

        # Execute compound multi-action plan atomically
        res: PlanExecutionResult = await self.nlp.execute_plan(plan, shift)
        return OrchestrationResult(
            reply_text=res.reply,
            actions_executed=res.executed_actions,
            correlation_id=res.correlation_id,
            success=res.success,
            error=res.error
        )
