"""Memory and Assistant Context Service.

Assembles bounded, privacy-aware context for conversational reasoning and AI prompts.
Adheres to Product Invariant 4.1 Truthfulness and bounded context constraints:
- Strictly bounded history (never dumps entire database tables)
- Secrets, tokens, and PII are redacted via telegram_import.redact
- Structured and deterministic context assembly
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import config
from telegram_import import redact


def build_assistant_context(
    db: Any,
    owner_id: int,
    shift: dict | None = None,
    conversation_limit: int = 12,
    retrieval_limit: int = 8,
    max_turn_text_len: int = 500,
) -> dict[str, Any]:
    """Assemble a bounded, truthful context dictionary for the assistant.

    Parameters:
        db: Database instance.
        owner_id: Telegram user ID of the owner.
        shift: Optional active shift dict. If None, queries db.active_shift().
        conversation_limit: Max number of recent conversation turns to include.
        retrieval_limit: Max number of pending tasks / followups to retrieve.
        max_turn_text_len: Maximum length of text per turn before truncation.

    Returns:
        Structured dict containing bounded shift, conversation, task, and followup context.
    """
    tz_name = getattr(config, 'TIMEZONE', 'Asia/Kolkata')
    now = datetime.now(ZoneInfo(tz_name))

    # 1. Shift context
    active_shift = shift or db.active_shift()
    shift_ctx = None
    if active_shift:
        shift_ctx = {
            'id': active_shift.get('id'),
            'start': active_shift.get('start'),
            'lunch': active_shift.get('lunch'),
            'end': active_shift.get('end'),
            'is_active': active_shift.get('closed_at') is None,
        }

    # 2. Ephemeral conversation context (active pointers)
    raw_ctx = db.get_conversation_context(context_key='owner')
    active_ctx = {
        'active_case_id': raw_ctx.get('active_case_id'),
        'active_task_id': raw_ctx.get('active_task_id'),
        'active_test_session_id': raw_ctx.get('active_test_session_id'),
        'active_client': raw_ctx.get('active_client'),
        'last_intent': raw_ctx.get('last_intent'),
        'data': raw_ctx.get('data', {}),
    }

    # 3. Durable conversation turns (bounded)
    shift_id = active_shift.get('id') if active_shift else None
    raw_turns = db.get_recent_turns(owner_id, limit=conversation_limit, shift_id=shift_id)
    recent_turns = []
    for turn in raw_turns:
        text = turn.get('text', '')
        if len(text) > max_turn_text_len:
            text = text[:max_turn_text_len] + '... [truncated]'
        recent_turns.append({
            'id': turn.get('id'),
            'role': turn.get('role'),
            'text': redact(text),
            'intent': turn.get('intent'),
            'created_at': turn.get('created_at'),
        })

    # 4. Pending tasks (bounded)
    all_tasks = db.list_tasks() if hasattr(db, 'list_tasks') else []
    pending_tasks = []
    for t in all_tasks:
        status_val = t.status.value if hasattr(t.status, 'value') else str(t.status)
        if status_val in ('pending', 'in_progress', 'blocked'):
            pending_tasks.append({
                'id': t.id,
                'title': redact(t.title),
                'status': status_val,
                'client': t.client,
                'priority': getattr(t, 'priority', 0),
            })
            if len(pending_tasks) >= retrieval_limit:
                break

    # 5. Due followups (bounded)
    due_followups = []
    if hasattr(db, 'due_followups'):
        raw_followups = db.due_followups(now.isoformat())
        for f in raw_followups[:retrieval_limit]:
            due_followups.append({
                'id': f.get('id') if isinstance(f, dict) else getattr(f, 'id', None),
                'text': redact(f.get('text', '') if isinstance(f, dict) else getattr(f, 'text', '')),
                'due_at': f.get('due_at') if isinstance(f, dict) else getattr(f, 'due_at', None),
            })

    # 6. Shift summary stats (if active)
    shift_stats = {}
    if active_shift and hasattr(db, 'activities'):
        acts = db.activities(active_shift['id'])
        shift_stats = {
            'activities_count': len(acts),
            'support_count': sum(1 for a in acts if a.get('category') == 'support'),
            'testing_count': sum(1 for a in acts if a.get('category') == 'testing'),
        }

    return {
        'timestamp': now.isoformat(),
        'timezone': tz_name,
        'shift': shift_ctx,
        'active_context': active_ctx,
        'recent_turns': recent_turns,
        'pending_tasks': pending_tasks,
        'due_followups': due_followups,
        'shift_stats': shift_stats,
    }


def format_context_for_prompt(context: dict[str, Any], max_chars: int = 3500) -> str:
    """Format structured context into a concise markdown text block for LLM prompts."""
    lines: list[str] = [
        f"Time: {context.get('timestamp')} ({context.get('timezone')})",
    ]

    shift = context.get('shift')
    if shift and shift.get('is_active'):
        lines.append(f"Active Shift: #{shift['id']} ({shift['start']} to {shift['end']})")
    else:
        lines.append("Active Shift: None")

    act_ctx = context.get('active_context', {})
    ptrs = []
    if act_ctx.get('active_client'):
        ptrs.append(f"Client={act_ctx['active_client']}")
    if act_ctx.get('active_task_id'):
        ptrs.append(f"Task=#{act_ctx['active_task_id']}")
    if act_ctx.get('active_case_id'):
        ptrs.append(f"Case=#{act_ctx['active_case_id']}")
    if ptrs:
        lines.append("Active Focus: " + ", ".join(ptrs))

    # Pending tasks
    tasks = context.get('pending_tasks', [])
    if tasks:
        lines.append("Pending Tasks:")
        for t in tasks:
            client_tag = f" [{t['client']}]" if t.get('client') else ""
            lines.append(f"  - #{t['id']} {t['title']} ({t['status']}){client_tag}")

    # Due followups
    followups = context.get('due_followups', [])
    if followups:
        lines.append("Due Follow-ups:")
        for f in followups:
            lines.append(f"  - #{f['id']} {f['text']} (due: {f.get('due_at')})")

    # Recent turns
    turns = context.get('recent_turns', [])
    if turns:
        lines.append("Recent Conversation:")
        for turn in turns:
            lines.append(f"  {turn['role'].upper()}: {turn['text']}")

    result = "\n".join(lines)
    if len(result) > max_chars:
        result = result[:max_chars] + "\n... [Context truncated to token budget]"
    return result
