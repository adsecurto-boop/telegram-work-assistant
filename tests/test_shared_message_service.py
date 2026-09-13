import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
