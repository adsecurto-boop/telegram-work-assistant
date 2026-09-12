"""
Gemini Test Condition Generator Service.
Invokes server-side Gemini to generate comprehensive, standards-compliant test conditions
for a specific Requirement (functional, boundary, negative, security, regression).
Outputs conditions as a draft for user inspection, customization, and approval.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

import config
from database import Database, now_iso

logger = logging.getLogger("gemini_test_generator")

MODEL_PRIMARY = "gemini-3.5-flash"
MODEL_COMPLEX = "gemini-3.1-pro-preview"

VALID_CATEGORIES = {"functional", "boundary", "negative", "security", "regression", "validation"}
VALID_RISK_LEVELS = {"high", "medium", "low"}


class GeminiTestConditionGenerator:
    """Generates structured test condition drafts for requirements using server-side Gemini."""

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

    def _generate_fallback_conditions(self, req: Dict[str, Any], focus_area: str = "comprehensive") -> List[Dict[str, Any]]:
        """
        Deterministic, rule-based fallback condition generator when offline or API key is absent.
        Extracts key criteria from requirement fields to build high-quality test condition drafts.
        """
        title = req.get("title") or "Requirement"
        user_story = req.get("user_story") or ""
        criteria = req.get("acceptance_criteria") or ""
        desc = req.get("description") or req.get("requirement_text") or ""
        client = req.get("client") or req.get("product") or "System"

        conditions = []

        # 1. Primary Happy Path / Functional
        conditions.append({
            "title": f"Verify standard end-to-end execution of {title[:55]}",
            "category": "functional",
            "risk_level": "high",
            "description": f"Validate that {title} functions successfully under normal valid parameters for {client}. Confirm user story goals are satisfied.",
            "rationale": "Baseline verification of the primary user story workflow."
        })

        # 2. Acceptance Criteria specific conditions
        if criteria:
            lines = [line.strip().lstrip("-*1234567890. ") for line in criteria.split("\n") if line.strip()]
            for idx, line in enumerate(lines[:3]):
                if len(line) > 5:
                    cat = "functional"
                    risk = "medium"
                    if any(w in line.lower() for w in ("limit", "max", "min", "exceed", "boundary", "threshold")):
                        cat = "boundary"
                        risk = "high"
                    elif any(w in line.lower() for w in ("error", "invalid", "fail", "reject", "timeout", "exception")):
                        cat = "negative"
                        risk = "high"
                    elif any(w in line.lower() for w in ("auth", "permission", "security", "role", "sanitize", "token")):
                        cat = "security"
                        risk = "high"

                    conditions.append({
                        "title": f"Verify AC #{idx+1}: {line[:60]}",
                        "category": cat,
                        "risk_level": risk,
                        "description": f"Ensure acceptance criteria is strictly satisfied: {line}. Verify expected system feedback and persistent state.",
                        "rationale": f"Directly verifies requirement acceptance criterion #{idx+1}."
                    })

        # 3. Boundary & Input Limits
        conditions.append({
            "title": f"Boundary & Threshold Limits for {title[:45]}",
            "category": "boundary",
            "risk_level": "medium",
            "description": f"Test edge boundaries, maximum field capacities, zero/null payloads, and volume thresholds during {title} processing.",
            "rationale": "Prevents buffer issues, precision truncation, or threshold failures at operating limits."
        })

        # 4. Negative & Error Handling
        conditions.append({
            "title": f"Negative Flow & Exception Recovery for {title[:45]}",
            "category": "negative",
            "risk_level": "high",
            "description": f"Simulate invalid inputs, network disconnects, server timeout responses, and unauthorized state transitions. Verify graceful user error messages and zero data corruption.",
            "rationale": "Ensures system resilience, idempotency, and user-friendly failure modes."
        })

        # 5. Security & Access Control
        conditions.append({
            "title": f"Access Control & Authorization Verification for {title[:40]}",
            "category": "security",
            "risk_level": "high",
            "description": f"Verify that only authenticated and authorized users/roles can execute {title}. Validate input sanitization and audit logging.",
            "rationale": "Protects against privilege escalation, unauthorized modifications, and injection vectors."
        })

        # 6. Regression & Cross-Module Impact
        conditions.append({
            "title": f"Regression & Downstream Integration for {title[:42]}",
            "category": "regression",
            "risk_level": "medium",
            "description": f"Verify that changes for {title} do not degrade existing workflows, database schemas, or reporting downstream for {client}.",
            "rationale": "Protects existing production stability and client backward compatibility."
        })

        return conditions

    async def generate_draft_conditions(
        self,
        requirement_id: int,
        focus_area: str = "comprehensive",
        model_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Calls Gemini to generate structured test conditions for the specified requirement.
        Returns a draft proposal ready for user review and approval.
        """
        req = self.db.get_requirement(requirement_id)
        if not req:
            return {
                "success": False,
                "error": f"Requirement REQ-{requirement_id} not found."
            }

        title = req.get("title") or "Untitled Requirement"
        user_story = req.get("user_story") or "Not specified"
        criteria = req.get("acceptance_criteria") or "Not specified"
        description = req.get("description") or req.get("requirement_text") or "Not specified"
        client = req.get("client") or req.get("product") or "Internal"
        ticket = req.get("ticket") or "None"
        priority = req.get("priority", 2)

        existing_conditions = self.db.list_test_conditions(requirement_id=requirement_id)
        existing_summary = [c.get("title") for c in existing_conditions if c.get("title")]

        selected_model = model_name or (MODEL_COMPLEX if len(criteria) > 400 or len(description) > 500 else MODEL_PRIMARY)

        system_instruction = (
            "You are an expert QA Test Architect and Quality Engineer. "
            "Your task is to analyze software requirements and generate rigorous, comprehensive test conditions "
            "following ISO/IEC/IEEE 29119 standards. "
            "For each test condition, provide:\n"
            "- title: concise, unambiguous condition title\n"
            "- category: one of ['functional', 'boundary', 'negative', 'security', 'regression', 'validation']\n"
            "- risk_level: one of ['high', 'medium', 'low']\n"
            "- description: detailed testing scope, preconditions, inputs, and expected validation behavior\n"
            "- rationale: why this test condition is required for verification\n\n"
            "Ensure full test coverage across positive business logic, edge boundaries, error handling, "
            "security/permissions, and regression impact. Avoid duplicate conditions."
        )

        user_prompt = f"""
Requirement ID: REQ-{requirement_id}
Title: {title}
Client / Product: {client} (Ticket: {ticket}, Priority: P{priority})

User Story:
{user_story}

Acceptance Criteria:
{criteria}

Specification / Description:
{description}

Existing Conditions Already Defined:
{json.dumps(existing_summary, indent=2)}

Focus Area: {focus_area}

Please generate 4 to 8 high-quality, non-redundant test conditions covering all critical angles.
Return ONLY valid JSON with this exact structure:
{{
  "rationale_summary": "High-level summary of test condition strategy and coverage coverage rationale",
  "conditions": [
    {{
      "title": "Clear condition title",
      "category": "functional|boundary|negative|security|regression|validation",
      "risk_level": "high|medium|low",
      "description": "Scope, preconditions, actions, and verification criteria",
      "rationale": "Why this test condition is critical"
    }}
  ]
}}
"""

        client_instance = self._get_client()
        if not client_instance:
            logger.info("Gemini API key not configured. Using deterministic test condition generator.")
            fallback_conditions = self._generate_fallback_conditions(req, focus_area)
            return {
                "success": True,
                "requirement": {
                    "id": req["id"],
                    "title": req["title"],
                    "client": req.get("client"),
                    "ticket": req.get("ticket"),
                    "user_story": req.get("user_story"),
                    "acceptance_criteria": req.get("acceptance_criteria"),
                },
                "focus_area": focus_area,
                "model_used": "deterministic-qa-engine (offline/no key)",
                "rationale_summary": f"Generated {len(fallback_conditions)} standard test coverage conditions based on requirement specification and acceptance criteria.",
                "draft_conditions": fallback_conditions,
                "is_fallback": True,
            }

        try:
            from google.genai import types

            response = await client_instance.aio.models.generate_content(
                model=selected_model,
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=0.2,
                    response_mime_type="application/json",
                )
            )

            raw_text = response.text or "{}"
            # Clean possible markdown code fence
            raw_text = re.sub(r"^```json\s*", "", raw_text.strip())
            raw_text = re.sub(r"\s*```$", "", raw_text.strip())

            parsed_data = json.loads(raw_text)
            conditions_list = parsed_data.get("conditions", [])
            rationale_summary = parsed_data.get("rationale_summary", "AI-generated comprehensive test condition suite.")

            # Validate and clean conditions
            sanitized_conditions = []
            for cond in conditions_list:
                c_title = (cond.get("title") or "").strip()
                if not c_title:
                    continue
                cat = (cond.get("category") or "functional").lower().strip()
                if cat not in VALID_CATEGORIES:
                    cat = "functional"
                risk = (cond.get("risk_level") or "medium").lower().strip()
                if risk not in VALID_RISK_LEVELS:
                    risk = "medium"

                sanitized_conditions.append({
                    "title": c_title,
                    "category": cat,
                    "risk_level": risk,
                    "description": (cond.get("description") or "").strip(),
                    "rationale": (cond.get("rationale") or "").strip()
                })

            if not sanitized_conditions:
                sanitized_conditions = self._generate_fallback_conditions(req, focus_area)

            return {
                "success": True,
                "requirement": {
                    "id": req["id"],
                    "title": req["title"],
                    "client": req.get("client"),
                    "ticket": req.get("ticket"),
                    "user_story": req.get("user_story"),
                    "acceptance_criteria": req.get("acceptance_criteria"),
                },
                "focus_area": focus_area,
                "model_used": selected_model,
                "rationale_summary": rationale_summary,
                "draft_conditions": sanitized_conditions,
                "is_fallback": False,
            }

        except Exception as e:
            logger.warning("Gemini test condition generation call encountered error: %s. Using fallback.", e)
            fallback_conditions = self._generate_fallback_conditions(req, focus_area)
            return {
                "success": True,
                "requirement": {
                    "id": req["id"],
                    "title": req["title"],
                    "client": req.get("client"),
                    "ticket": req.get("ticket"),
                    "user_story": req.get("user_story"),
                    "acceptance_criteria": req.get("acceptance_criteria"),
                },
                "focus_area": focus_area,
                "model_used": f"{selected_model} (fallback triggered: {type(e).__name__})",
                "rationale_summary": f"Fallback test conditions generated due to service response: {str(e)[:100]}",
                "draft_conditions": fallback_conditions,
                "is_fallback": True,
            }

    def approve_draft_conditions(
        self,
        requirement_id: int,
        approved_conditions: List[Dict[str, Any]],
        status: str = "approved"
    ) -> Dict[str, Any]:
        """
        Persists approved draft test conditions to the SQLite database for the specified requirement.
        """
        req = self.db.get_requirement(requirement_id)
        if not req:
            return {
                "success": False,
                "error": f"Requirement REQ-{requirement_id} not found."
            }

        created_ids = []
        for cond in approved_conditions:
            title = (cond.get("title") or "").strip()
            if not title:
                continue
            cat = (cond.get("category") or "functional").lower().strip()
            if cat not in VALID_CATEGORIES:
                cat = "functional"
            risk = (cond.get("risk_level") or "medium").lower().strip()
            if risk not in VALID_RISK_LEVELS:
                risk = "medium"
            desc = (cond.get("description") or "").strip() or None
            notes = (cond.get("rationale") or "").strip() or None

            cond_id = self.db.add_test_condition(
                requirement_id=requirement_id,
                title=title,
                description=desc,
                category=cat,
                risk_level=risk,
                status=status,
                notes=notes
            )
            created_ids.append(cond_id)

        return {
            "success": True,
            "requirement_id": requirement_id,
            "created_count": len(created_ids),
            "condition_ids": created_ids,
            "message": f"Successfully approved and created {len(created_ids)} test condition(s) for REQ-{requirement_id}."
        }
