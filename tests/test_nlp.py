"""Unit tests for Natural Language Processing pipeline and deterministic parser."""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from database import Database
from models import TaskStatus
from nlp import DeterministicParser, NLIntent, NaturalLanguagePipeline


class TestNaturalLanguageEngine(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / 'work.sqlite3')
        self.pipeline = NaturalLanguagePipeline(self.db)
        self.sid = self.db.start_shift('2026-09-09T10:00:00+05:30', '2026-09-09T19:00:00+05:30')
        self.shift = self.db.active_shift()

    def test_deterministic_shift_parsing(self):
        text = "My shift today is 10 to 7 and I'll take lunch around 2."
        res = DeterministicParser.parse(text)
        self.assertIsNotNone(res)
        self.assertEqual(res.intent, NLIntent.SET_SHIFT)
        self.assertEqual(res.entities.shift_start, '10:00')
        self.assertEqual(res.entities.shift_end, '19:00')
        self.assertEqual(res.entities.shift_lunch, '14:00')

    def test_deterministic_task_planning(self):
        text = "Today I need to test the idle-time issue and follow up with Rahul"
        res = DeterministicParser.parse(text)
        self.assertIsNotNone(res)
        self.assertEqual(res.intent, NLIntent.CREATE_TASK)
        self.assertEqual(len(res.entities.task_titles), 2)
        self.assertIn('test the idle-time issue', res.entities.task_titles)
        self.assertIn('follow up with rahul', res.entities.task_titles)

    def test_deterministic_testing_record(self):
        text = "I checked it on Windows 11 and reproduced the issue"
        res = DeterministicParser.parse(text)
        self.assertIsNotNone(res)
        self.assertEqual(res.intent, NLIntent.CREATE_TEST_SESSION)
        self.assertEqual(res.entities.test_result, 'failed')
        self.assertIn('Windows 11', res.entities.test_environment)

    def test_deterministic_learning_record(self):
        text = "I learned how idle-time calculation works"
        res = DeterministicParser.parse(text)
        self.assertIsNotNone(res)
        self.assertEqual(res.intent, NLIntent.ADD_LEARNING)
        self.assertEqual(res.entities.learning_topic, 'idle-time calculation works')

    def test_deterministic_followup_record(self):
        text = "Remind me tomorrow at 11 to ask Rahul for fresh logs"
        res = DeterministicParser.parse(text)
        self.assertIsNotNone(res)
        self.assertEqual(res.intent, NLIntent.CREATE_FOLLOWUP)
        self.assertEqual(res.entities.waiting_on, 'Rahul')
        self.assertIn('11:00:00', res.entities.followup_due)

    async def test_end_to_end_task_creation_and_undo(self):
        reply, interp = await self.pipeline.process(
            "Today I need to test the idle-time issue and follow up with Rahul",
            self.shift
        )
        self.assertIn('Planned 2 task(s)', reply)
        tasks = self.db.list_pending()
        self.assertEqual(len(tasks), 2)

        # Audit should have recorded these operations
        audits = self.db.get_audit_log(limit=5)
        self.assertGreaterEqual(len(audits), 2)

        # Undo the last action
        undo_reply, undo_interp = await self.pipeline.process("undo", self.shift)
        self.assertIn('Reverted', undo_reply)

    async def test_case_resolution_and_context_resolution(self):
        # Create a case
        case_id = self.db.create_case('Idle time discrepancy', client='Acme', shift_id=self.sid)
        self.db.update_conversation_context('owner', active_case_id=case_id)

        # Reference by context "that case"
        reply, interp = await self.pipeline.process("Mark that case resolved", self.shift)
        self.assertIn(f'CASE-{case_id}', reply)
        self.assertIn('resolved', reply)

        c = self.db.case(case_id)
        self.assertEqual(c['status'], 'resolved')

    async def test_ambiguity_detection(self):
        # Create two cases for same client
        self.db.create_case('Acme attendance issue', client='Acme', shift_id=self.sid)
        self.db.create_case('Acme billing discrepancy', client='Acme', shift_id=self.sid)
        self.db.update_conversation_context('owner', active_case_id=None)

        reply, interp = await self.pipeline.process("Mark the Acme issue resolved", self.shift)
        self.assertTrue(interp.needs_confirmation)
        self.assertIn('Multiple cases match', reply)
        self.assertGreaterEqual(len(interp.choices), 2)


if __name__ == '__main__':
    unittest.main()
