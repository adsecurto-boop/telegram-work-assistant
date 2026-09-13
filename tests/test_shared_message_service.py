import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from application.message_service import AssistantMessageService
from assistant_orchestrator import AssistantOrchestrator
from database import Database


class SharedMessageServiceTests(unittest.TestCase):
    def test_web_replay_and_channel_provenance(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            db = Database(Path(temp) / 'work.sqlite3')
            service = AssistantMessageService(db)
            first = asyncio.run(service.process_user_message(owner_id=1, text='What is pending today?',
                source_channel='web', client_message_id='web:one'))
            second = asyncio.run(service.process_user_message(owner_id=1, text='What is pending today?',
                source_channel='web', client_message_id='web:one'))
            self.assertTrue(first['success'])
            self.assertTrue(second['success'])
            self.assertTrue(second['idempotent_replay'])
            turns = db.get_recent_turns(1)
            self.assertEqual(len(turns), 2)
            self.assertEqual({turn['source_channel'] for turn in turns}, {'web'})

    def test_new_thread_preserves_previous_turns(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            db = Database(Path(temp) / 'work.sqlite3')
            db.record_conversation_turn(1, 'user', 'old', source_channel='telegram')
            old = db.get_active_conversation_thread(1)
            new = db.start_new_conversation_thread(1)
            self.assertNotEqual(old, new)
            self.assertEqual(db.get_recent_turns(1, thread_id=new), [])
            self.assertEqual(db.get_recent_turns(1, thread_id='primary')[0]['text'], 'old')

    def test_orchestrator_short_term_context_uses_active_thread(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            db = Database(Path(temp) / 'work.sqlite3')
            db.record_conversation_turn(1, 'user', 'old-thread context')
            active_thread = db.start_new_conversation_thread(1)
            orchestrator = AssistantOrchestrator(db)
            with patch.object(db, 'get_recent_turns', wraps=db.get_recent_turns) as recent:
                asyncio.run(orchestrator.route_and_process('What is pending today?', owner_id=1))
            self.assertTrue(any(call.kwargs.get('thread_id') == active_thread
                                for call in recent.call_args_list))

    def test_concurrent_web_retry_claims_one_orchestrator_execution(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            db = Database(Path(temp) / 'work.sqlite3')
            service = AssistantMessageService(db)
            calls = 0

            async def delayed_route(*_args, **_kwargs):
                nonlocal calls
                calls += 1
                await asyncio.sleep(0.05)
                return SimpleNamespace(reply_text='done', proposal_id=None, choices=[],
                    correlation_id=None, active_external_refs=None, success=True, error=None)

            service.orchestrator.route_and_process = delayed_route

            async def submit_twice():
                return await asyncio.gather(*[
                    service.process_user_message(owner_id=1, text='Create a task',
                        source_channel='web', client_message_id='web:concurrent')
                    for _ in range(2)
                ])

            first, second = asyncio.run(submit_twice())
            self.assertEqual(calls, 1)
            self.assertEqual(sum(bool(item['success']) for item in (first, second)), 1)
            replay = asyncio.run(service.process_user_message(owner_id=1, text='Create a task',
                source_channel='web', client_message_id='web:concurrent'))
            self.assertTrue(replay['success'])
            self.assertTrue(replay['idempotent_replay'])

    def test_unknown_message_uses_non_mutating_conversation_route(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            db = Database(Path(temp) / 'work.sqlite3')
            orchestrator = AssistantOrchestrator(db)
            with patch('gemini_chat_service.GeminiChatService.send_message', new=AsyncMock(
                       return_value={'reply': 'Hello — I can help with parity testing.', 'error': None})) as chat:
                result = asyncio.run(orchestrator.route_and_process(
                    'Hello, I am doing parity testing between channels.', owner_id=1))
            self.assertTrue(result.success)
            self.assertIn('parity testing', result.reply_text)
            self.assertTrue(chat.called)

    def test_web_retry_preserves_proposal_and_choices(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            db = Database(Path(temp) / 'work.sqlite3')
            service = AssistantMessageService(db)

            async def mock_route_with_proposal(*_args, **_kwargs):
                return SimpleNamespace(
                    reply_text='Confirmation required for deleting file.',
                    proposal_id='prop_ABC123',
                    choices=[{'label': 'Confirm', 'value': 'confirm'}, {'label': 'Cancel', 'value': 'cancel'}],
                    correlation_id='corr_XYZ789',
                    active_external_refs=None,
                    success=True,
                    error=None
                )

            service.orchestrator.route_and_process = mock_route_with_proposal

            first = asyncio.run(service.process_user_message(
                owner_id=1, text='Delete that external file.',
                source_channel='web', client_message_id='web:delete_file'
            ))
            self.assertTrue(first['success'])
            self.assertFalse(first['idempotent_replay'])
            self.assertEqual(first['proposal_id'], 'prop_ABC123')
            self.assertEqual(len(first['choices']), 2)
            self.assertEqual(first['correlation_id'], 'corr_XYZ789')
            self.assertEqual(first['reply'], 'Confirmation required for deleting file.')

            # Simulate network retry with the same client_message_id
            retry = asyncio.run(service.process_user_message(
                owner_id=1, text='Delete that external file.',
                source_channel='web', client_message_id='web:delete_file'
            ))
            self.assertTrue(retry['success'])
            self.assertTrue(retry['idempotent_replay'])
            self.assertEqual(retry['proposal_id'], 'prop_ABC123')
            self.assertEqual(retry['choices'], [{'label': 'Confirm', 'value': 'confirm'}, {'label': 'Cancel', 'value': 'cancel'}])
            self.assertEqual(retry['correlation_id'], 'corr_XYZ789')
            self.assertEqual(retry['reply'], 'Confirmation required for deleting file.')
