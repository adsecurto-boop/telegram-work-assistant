import asyncio
import tempfile
import unittest
from pathlib import Path

from application.message_service import AssistantMessageService
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

