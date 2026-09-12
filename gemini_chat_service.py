"""
Gemini Multi-Turn Chat Service for Personal Work Assistant.
Provides multi-turn conversation handling, intelligent model routing,
role-based system instructions, and privacy-bounded context injection.

Models:
- Complex Tasks: gemini-3.1-pro-preview
- General Tasks: gemini-3.5-flash
- Fast Tasks: gemini-3.1-flash-lite
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import config
from database import Database, now_iso
from memory_service import build_assistant_context, format_context_for_prompt
from telegram_import import redact

logger = logging.getLogger("gemini_chat_service")

# Model definitions per requirements
MODEL_COMPLEX = "gemini-3.1-pro-preview"
MODEL_GENERAL = "gemini-3.5-flash"
MODEL_FAST = "gemini-3.1-flash-lite"

ROUTING_MODES = {
    "auto": "Auto-Detect Complexity",
    "complex": f"Complex Tasks ({MODEL_COMPLEX})",
    "general": f"General Tasks ({MODEL_GENERAL})",
    "fast": f"Fast Tasks ({MODEL_FAST})",
}

# Role definitions with tailored system instructions
ROLE_DEFINITIONS = {
    "qa_lead": {
        "title": "Senior QA & Test Engineer",
        "description": "Specializes in test strategies, test conditions, regression suites, defect triage, and test coverage.",
        "icon": "shield-check",
        "system_instruction": (
            "You are a Senior QA Engineer and Test Lead embedded in the Work Operations Hub. "
            "Your expertise spans test strategy, functional and non-functional test condition design, "
            "boundary value analysis, defect lifecycle management, regression impact analysis, and exploratory testing. "
            "When analyzing requirements or bugs: "
            "1. Identify clear preconditions, test steps, expected vs actual behaviors, and acceptance criteria. "
            "2. Highlight potential edge cases, data dependencies, and regression risks. "
            "3. Structure defect reports with clear severity, reproduction steps, and root-cause hypotheses. "
            "Be precise, structured, and factual. Do not hallucinate or assume unverified system behaviors."
        ),
    },
    "support_specialist": {
        "title": "Customer Support Lead",
        "description": "Specializes in client communication, incident triage, SLA adherence, and customer updates.",
        "icon": "headphones",
        "system_instruction": (
            "You are an empathetic, highly technical Customer Support Lead. "
            "You manage client communication, query resolution, incident escalations, and SLA compliance. "
            "When responding: "
            "1. Maintain a professional, reassuring, and solution-oriented tone. "
            "2. Structure client updates with clarity: acknowledgment, current status, investigation findings, and next steps. "
            "3. Draft escalation notes for engineering that include all technical details, logs, and client impact. "
            "4. Never mark an issue resolved unless explicit resolution evidence exists."
        ),
    },
    "project_manager": {
        "title": "Technical Project Manager & Scrum Master",
        "description": "Specializes in sprint planning, task breakdown, blocker resolution, and delivery tracking.",
        "icon": "clipboard-list",
        "system_instruction": (
            "You are an Agile Technical Project Manager and Scrum Master. "
            "You specialize in breaking down complex initiatives into actionable work items, identifying blocker dependencies, "
            "resource allocation, and tracking timeline commitments. "
            "When helping the user: "
            "1. Organize tasks with explicit priorities, assignees, and estimated durations. "
            "2. Identify potential critical path bottlenecks and cross-team dependencies. "
            "3. Summarize status updates with executive brevity and actionable next steps."
        ),
    },
    "tech_architect": {
        "title": "Software Architect & DevOps Lead",
        "description": "Specializes in system design, root cause analysis, code review, performance, and deployment.",
        "icon": "server",
        "system_instruction": (
            "You are a Principal Software Architect and DevOps Lead. "
            "You excel in distributed systems, clean architecture, performance optimization, root cause analysis, and CI/CD pipelines. "
            "When analyzing technical problems: "
            "1. Formulate rigorous hypotheses for system failures or unexpected behaviors. "
            "2. Propose modular, maintainable, and resilient architectural solutions. "
            "3. Provide clean, secure, and production-ready code snippets or configuration examples when appropriate."
        ),
    },
    "general_assistant": {
        "title": "Operations & Workspace Assistant",
        "description": "All-purpose assistant for work management, note drafting, summarization, and task coordination.",
        "icon": "sparkles",
        "system_instruction": (
            "You are the intelligent Operations & Workspace Assistant for the Personal Work Assistant platform. "
            "You assist the user across their daily shifts, tasks, support cases, test sessions, and workspace documentation. "
            "Be concise, highly organized, and helpful. Format your responses with clean Markdown bullet points and bold headers."
        ),
    },
    "custom": {
        "title": "Custom System Role",
        "description": "User-defined system instructions tailored to specific workflows.",
        "icon": "settings",
        "system_instruction": "You are a helpful and knowledgeable AI assistant.",
    },
}


def select_model(task_mode: str, message: str) -> Tuple[str, str]:
    """
    Select appropriate Gemini model based on user selection or auto-detection.
    Returns (model_name, detected_complexity).
    """
    if task_mode == "complex":
        return MODEL_COMPLEX, "complex"
    if task_mode == "general":
        return MODEL_GENERAL, "general"
    if task_mode == "fast":
        return MODEL_FAST, "fast"

    # Auto-detection heuristic
    msg_len = len(message.strip())
    low = message.lower()

    # Complex indicators: architecture, deep analysis, root cause, code generation, multi-step math/logic
    complex_keywords = [
        "architecture", "root cause", "deep analysis", "refactor", "investigate defect",
        "test strategy", "security audit", "optimize", "race condition", "memory leak",
        "design pattern", "database schema", "system failure", "algorithm", "complex"
    ]
    if any(re.search(r'\b' + re.escape(kw) + r'\b', low) for kw in complex_keywords) or msg_len > 600 or low.count("\n") > 5:
        return MODEL_COMPLEX, "complex (auto)"

    # Fast indicators: simple lookup, quick reword, short questions, greeting, yes/no
    fast_keywords = [
        "quick", "fast", "check spelling", "rephrase", "summarize in one sentence",
        "hello", "hi", "help", "what time", "status check"
    ]
    if (any(re.search(r'\b' + re.escape(kw) + r'\b', low) for kw in fast_keywords) or msg_len < 40) and not any(k in low for k in ("why", "explain in detail", "analyze")):
        return MODEL_FAST, "fast (auto)"

    # Default to general
    return MODEL_GENERAL, "general (auto)"


class GeminiChatService:
    def __init__(self, db: Database):
        self.db = db
        self.api_key = os.environ.get("GEMINI_API_KEY", "") or getattr(config, "GEMINI_API_KEY", "")

    def _get_client(self):
        if not self.api_key:
            return None
        try:
            from google import genai
            from google.genai import types
            return genai.Client(
                api_key=self.api_key,
                http_options=types.HttpOptions(
                    timeout=45000,
                    headers={"User-Agent": "aistudio-build"}
                )
            )
        except Exception as e:
            logger.error("Failed to initialize Google GenAI Client: %s", e)
            return None

    def get_conversation_history(self, owner_id: int = 1, limit: int = 50) -> List[Dict[str, Any]]:
        """Retrieve recent conversation turns from SQLite database."""
        try:
            turns = self.db.get_recent_turns(owner_id=owner_id, limit=limit)
            results = []
            for t in turns:
                meta = {}
                if t.get("entities_json"):
                    try:
                        meta = json.loads(t["entities_json"])
                    except Exception:
                        meta = {}
                results.append({
                    "id": t["id"],
                    "role": t["role"],
                    "text": t["text"],
                    "intent": t.get("intent"),
                    "created_at": t.get("created_at"),
                    "metadata": meta,
                })
            return results
        except Exception as e:
            logger.error("Failed to get conversation history: %s", e)
            return []

    def clear_conversation_history(self, owner_id: int = 1) -> bool:
        """Clear conversation turns for the user."""
        try:
            with self.db.connect() as conn:
                conn.execute("DELETE FROM conversation_turns WHERE owner_id=?", (owner_id,))
            return True
        except Exception as e:
            logger.error("Failed to clear conversation history: %s", e)
            return False

    async def send_message(
        self,
        message: str,
        role_key: str = "general_assistant",
        task_mode: str = "auto",
        custom_system_instruction: Optional[str] = None,
        owner_id: int = 1,
        include_workspace_context: bool = True,
    ) -> Dict[str, Any]:
        """
        Processes a multi-turn chat message:
        1. Selects the appropriate Gemini model (pro / flash / flash-lite).
        2. Retrieves and bounds conversation history.
        3. Injects live workspace context (shifts, cases, tasks, test sessions).
        4. Calls Gemini via @google/genai SDK.
        5. Saves user and assistant turns into conversation_turns.
        """
        clean_user_message = redact(message.strip())
        if not clean_user_message:
            return {"error": "Message cannot be empty."}

        # 1. Determine model and role
        model_name, detected_mode = select_model(task_mode, clean_user_message)
        role_info = ROLE_DEFINITIONS.get(role_key, ROLE_DEFINITIONS["general_assistant"])
        
        base_instruction = role_info["system_instruction"]
        if role_key == "custom" and custom_system_instruction:
            base_instruction = custom_system_instruction.strip()

        # 2. Build system instruction with bounded context
        context_block = ""
        if include_workspace_context:
            try:
                raw_context = build_assistant_context(self.db, owner_id=owner_id, conversation_limit=10)
                context_block = format_context_for_prompt(raw_context, max_chars=3000)
            except Exception as e:
                logger.warning("Could not assemble workspace context: %s", e)
                context_block = "Workspace context currently unavailable."

        full_system_instruction = (
            f"{base_instruction}\n\n"
            f"=== CURRENT WORKSPACE CONTEXT (READ-ONLY REFERENCE) ===\n"
            f"{context_block}\n"
            f"======================================================\n\n"
            f"Guidelines:\n"
            f"- Answer the user's questions clearly, factually, and concisely.\n"
            f"- Use Markdown formatting (bold, bullet points, code blocks) to make your reply easy to read.\n"
            f"- Maintain awareness of past turns in this conversation."
        )

        # 3. Retrieve recent history for multi-turn thread
        past_turns = self.get_conversation_history(owner_id=owner_id, limit=20)
        
        # 4. Save User Turn to Database
        user_turn_id = self.db.record_conversation_turn(
            owner_id=owner_id,
            role="user",
            text=clean_user_message,
            intent=f"chat_{role_key}",
            metadata={
                "role_key": role_key,
                "task_mode": task_mode,
                "model": model_name,
            }
        )

        # 5. Call Gemini
        client = self._get_client()
        assistant_reply = ""
        error_msg = None

        if not client:
            # Deterministic Fallback if API key not available
            assistant_reply = (
                f"**[Offline Assistant Mode - {role_info['title']}]**\n\n"
                f"I received your message: *\"{clean_user_message}\"*\n\n"
                f"To enable live AI generation with Gemini `{model_name}`, please configure your `GEMINI_API_KEY` in settings. "
                f"In the meantime, your conversation turn has been safely recorded in the local operations database."
            )
        else:
            try:
                from google.genai import types

                # Build multi-turn content parts
                contents = []
                # Add past turns (up to 12 turns)
                for pt in past_turns[-12:]:
                    r = pt.get("role")
                    t = pt.get("text", "")
                    if not t or r == "system":
                        continue
                    genai_role = "user" if r == "user" else "model"
                    contents.append(
                        types.Content(
                            role=genai_role,
                            parts=[types.Part.from_text(text=t)]
                        )
                    )

                # Add current user message
                contents.append(
                    types.Content(
                        role="user",
                        parts=[types.Part.from_text(text=clean_user_message)]
                    )
                )

                config_obj = types.GenerateContentConfig(
                    system_instruction=full_system_instruction,
                    temperature=0.4 if task_mode == "complex" else 0.7,
                    max_output_tokens=3000,
                )

                # Execute call
                response = await client.aio.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=config_obj
                )

                if response and response.text:
                    assistant_reply = response.text.strip()
                else:
                    assistant_reply = "I processed your request, but the model did not produce a text response."

            except Exception as e:
                logger.error("Gemini API call failed: %s", e)
                error_msg = str(e)
                assistant_reply = (
                    f"**[Service Notice]**\n\n"
                    f"Encountered an issue communicating with Gemini `{model_name}`: {error_msg}.\n\n"
                    f"Your message has been stored in conversation history. Please try again or switch to another model."
                )

        # 6. Save Assistant Turn to Database
        assistant_turn_id = self.db.record_conversation_turn(
            owner_id=owner_id,
            role="assistant",
            text=assistant_reply,
            intent=f"chat_reply_{role_key}",
            metadata={
                "role_key": role_key,
                "model_used": model_name,
                "detected_mode": detected_mode,
                "user_turn_id": user_turn_id,
                "error": error_msg,
            }
        )

        return {
            "success": True,
            "user_turn_id": user_turn_id,
            "assistant_turn_id": assistant_turn_id,
            "reply": assistant_reply,
            "model_used": model_name,
            "detected_mode": detected_mode,
            "role_key": role_key,
            "role_title": role_info["title"],
            "created_at": now_iso(),
            "error": error_msg,
        }
