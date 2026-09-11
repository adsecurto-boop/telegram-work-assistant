"""
Action policy, confidence scoring, and reason-code evaluation for natural language interpretation.
Separates recognition confidence from execution authorization.
"""
from __future__ import annotations

from enum import Enum
from typing import Any


class ReasonCode(str, Enum):
    EXACT_INTENT_PHRASE = 'exact_intent_phrase'
    COMPLETE_REQUIRED_ENTITIES = 'complete_required_entities'
    VALIDATED_DATETIME = 'validated_datetime'
    UNAMBIGUOUS_ENTITY_ID = 'unambiguous_entity_id'
    UNIQUE_CONTEXT_MATCH = 'unique_context_match'
    EXACT_DETERMINISTIC_RULE = 'exact_deterministic_rule'
    NORMALIZED_TIME_RANGE = 'normalized_time_range'
    UNCERTAIN_WORDING = 'uncertain_wording'
    NEGATION_DETECTED = 'negation_detected'
    MISSING_REQUIRED_ENTITY = 'missing_required_entity'
    INVALID_TIME = 'invalid_time'
    AMBIGUOUS_REFERENCE = 'ambiguous_reference'
    CONFLICTING_ENTITIES = 'conflicting_entities'
    INCOMPLETE_INPUT = 'incomplete_input'
    GEMINI_FALLBACK = 'gemini_fallback'
    PARSER_DISAGREEMENT = 'parser_disagreement'
    CORRECTION_MATCH = 'correction_match'
    CONFLICTING_CORRECTIONS = 'conflicting_corrections'


class ActionDecision(str, Enum):
    EXECUTE_IMMEDIATELY = 'execute_immediately'
    PROPOSE_CONFIRMATION = 'propose_confirmation'
    REQUIRE_CLARIFICATION = 'require_clarification'
    REJECT = 'reject'
    READ_ONLY = 'read_only'


READ_ONLY_INTENTS = {
    'show_shift',
    'show_today',
    'show_pending',
    'show_cases',
    'show_case_summary',
    'analyze_test',
}


def required_entities_for_intent(intent: str) -> list[str]:
    """Returns essential entity names required for mutating operations."""
    if intent == 'set_shift':
        return ['shift_start', 'shift_end']
    if intent == 'create_task':
        return ['task_title']
    if intent == 'complete_task':
        return ['reference']
    if intent == 'create_case':
        return ['case_title']
    if intent == 'change_case_status':
        return ['status']
    if intent == 'create_test_session':
        return ['test_scenario', 'test_result']
    if intent == 'update_test_session':
        return ['test_result']
    if intent == 'add_learning':
        return ['learning_topic']
    if intent == 'create_followup':
        return ['notes']
    if intent == 'log_support':
        return ['client', 'query']
    return []


def evaluate_action_policy(
    interpretation: Any,
    is_understand: bool = False,
    has_active_shift: bool = False
) -> tuple[ActionDecision, bool, list[str]]:
    """
    Evaluates whether an interpretation can execute, requires confirmation, or is rejected.
    Returns (decision, would_mutate, reason_codes).
    """
    reasons = list(getattr(interpretation, 'reason_codes', []) or [])
    intent_val = getattr(interpretation.intent, 'value', str(interpretation.intent))

    # 1. /understand is strictly read-only
    if is_understand:
        if ReasonCode.UNCERTAIN_WORDING not in reasons and any(m in getattr(interpretation, 'explanation', '').lower() for m in ('can be', 'might', 'maybe')):
            reasons.append(ReasonCode.UNCERTAIN_WORDING.value)
        return ActionDecision.READ_ONLY, False, reasons

    # 2. Negation detected -> Reject immediately
    if ReasonCode.NEGATION_DETECTED in reasons or getattr(interpretation, 'has_negation', False):
        if ReasonCode.NEGATION_DETECTED.value not in reasons:
            reasons.append(ReasonCode.NEGATION_DETECTED.value)
        return ActionDecision.REJECT, False, reasons

    # 3. Invalid time -> Reject immediately
    if ReasonCode.INVALID_TIME in reasons or ReasonCode.INVALID_TIME.value in reasons:
        return ActionDecision.REJECT, False, reasons

    # 4. Ambiguous reference, conflicting corrections, or choices present -> Require clarification
    if (getattr(interpretation, 'choices', None)
            or ReasonCode.AMBIGUOUS_REFERENCE in reasons or ReasonCode.AMBIGUOUS_REFERENCE.value in reasons
            or ReasonCode.CONFLICTING_CORRECTIONS in reasons or ReasonCode.CONFLICTING_CORRECTIONS.value in reasons):
        return ActionDecision.REQUIRE_CLARIFICATION, False, reasons

    # 5. Unknown intent without clarification choices -> Reject / no mutation
    if intent_val == 'unknown':
        return ActionDecision.REJECT, False, reasons

    # 6. Read-only query intents -> READ_ONLY, never mutate
    if intent_val in READ_ONLY_INTENTS:
        return ActionDecision.READ_ONLY, False, reasons

    # 7. Missing required entities -> Cannot execute directly
    missing = getattr(interpretation, 'missing_fields', []) or []
    if missing or ReasonCode.MISSING_REQUIRED_ENTITY in reasons or ReasonCode.MISSING_REQUIRED_ENTITY.value in reasons:
        return ActionDecision.REQUIRE_CLARIFICATION, False, reasons

    # 8. Uncertain wording ("can be", "might", "maybe", etc.) -> Must propose confirmation before mutation!
    if ReasonCode.UNCERTAIN_WORDING in reasons or ReasonCode.UNCERTAIN_WORDING.value in reasons:
        return ActionDecision.PROPOSE_CONFIRMATION, True, reasons

    # 9. Active shift timing change -> Must propose confirmation
    if intent_val == 'set_shift' and has_active_shift:
        entities = getattr(interpretation, 'entities', None)
        if (entities and not getattr(entities, 'is_day_off', False)
                and not getattr(entities, 'start_date', None)
                and getattr(entities, 'notes', None) != 'preview_calendar'):
            # If target is today or unstated date, it modifies active shift
            target_date = getattr(entities, 'date', None)
            if not target_date or target_date == getattr(interpretation, 'current_date', None):
                return ActionDecision.PROPOSE_CONFIRMATION, True, reasons

    # 10. Needs confirmation flag explicitly set on interpretation
    if getattr(interpretation, 'needs_confirmation', False):
        return ActionDecision.PROPOSE_CONFIRMATION, True, reasons

    # 11. Low confidence (< 0.80) on a mutating intent -> Propose confirmation
    conf = getattr(interpretation, 'confidence', 0.0)
    if conf < 0.80:
        return ActionDecision.PROPOSE_CONFIRMATION, True, reasons

    # 12. High confidence, validated, clear -> Execute immediately (with Undo)
    return ActionDecision.EXECUTE_IMMEDIATELY, True, reasons
