"""Channel-neutral adapter for the single assistant orchestration pipeline."""
from __future__ import annotations

from typing import Any
from assistant_orchestrator import AssistantOrchestrator


class AssistantMessageService:
    def __init__(self, db, mcp_manager=None, ai_client=None, tool_model=None):
        self.db = db
        self.orchestrator = AssistantOrchestrator(db, mcp_manager=mcp_manager,
                                                  ai_client=ai_client, tool_model=tool_model)

    def configure_runtime(self, *, mcp_manager=None, ai_client=None, tool_model=None):
        self.orchestrator = AssistantOrchestrator(self.db, mcp_manager=mcp_manager,
                                                  ai_client=ai_client, tool_model=tool_model)

    async def process_user_message(self, *, owner_id: int, text: str,
                                   source_channel: str, source_message_id: str | None = None,
                                   client_message_id: str | None = None,
                                   correlation_id: str | None = None,
                                   metadata: dict[str, Any] | None = None):
        thread_id = self.db.get_active_conversation_thread(owner_id)
        existing = None
        if client_message_id:
            with self.db.connect() as conn:
                existing = conn.execute('SELECT * FROM conversation_turns WHERE owner_id=? AND client_message_id=? AND role=?',
                                        (owner_id, client_message_id, 'assistant')).fetchone()
        if source_channel == 'telegram' and source_message_id and source_message_id.isdigit():
            with self.db.connect() as conn:
                existing = conn.execute('SELECT * FROM conversation_turns WHERE owner_id=? AND source_update_id=? AND role=?',
                                        (owner_id, int(source_message_id), 'assistant')).fetchone() or existing
        if existing:
            return {'success': True, 'reply': existing['text'], 'reply_text': existing['text'],
                    'idempotent_replay': True, 'proposal_id': None, 'choices': []}
        shift = self.db.active_shift()
        self.db.record_conversation_turn(owner_id, 'user', text, shift_id=shift['id'] if shift else None,
            source_channel=source_channel, source_message_id=source_message_id,
            source_update_id=int(source_message_id) if source_channel == 'telegram' and source_message_id and source_message_id.isdigit() else None,
            client_message_id=client_message_id, correlation_id=correlation_id, metadata=metadata, thread_id=thread_id)
        result = await self.orchestrator.route_and_process(text, owner_id=owner_id,
            source_update_id=int(source_message_id) if source_channel == 'telegram' and source_message_id and source_message_id.isdigit() else None)
        self.db.record_conversation_turn(owner_id, 'assistant', result.reply_text, shift_id=shift['id'] if shift else None,
            source_channel=source_channel, source_message_id=source_message_id,
            source_update_id=int(source_message_id) if source_channel == 'telegram' and source_message_id and source_message_id.isdigit() else None,
            client_message_id=client_message_id, correlation_id=result.correlation_id or correlation_id,
            metadata={'active_external_refs': result.active_external_refs} if result.active_external_refs else None, thread_id=thread_id)
        return {'success': result.success, 'reply': result.reply_text, 'reply_text': result.reply_text,
                'proposal_id': result.proposal_id, 'choices': result.choices,
                'correlation_id': result.correlation_id, 'error': result.error,
                'idempotent_replay': False}
