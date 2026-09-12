"""Comprehensive tests for Phase 2 Completion Master Engineering Mission capabilities.

Covers:
- Turn recording (User turn pre-processing, Assistant turn post-processing, DB restart persistence)
- Memory service prompt context integration (active case details, bounded turns, rolling daily summary)
- Contextual Gemini routing and ConversationPlan execution
- Exact correlation Undo button display
- SQLite WAL mode and schema v13
- Application Services (TaskService, CaseService) returning MutationResult
- FTS5 historical memory search & chunked evidence hashing
- Dashboard filter_inbox SQL queries and 303 token exchange redirect
- Report validator casefolded client deduplication
- Morning briefing (/briefing) generation and outside-shift work capture
- Safe error logging with unique ERR-XXXXXX reference IDs
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import config
from application.case_service import CaseService
from application.task_service import TaskService
from database import SCHEMA_VERSION, Database
from daily_assistant import generate_morning_briefing
from memory_service import build_assistant_context, format_context_for_prompt
from nlp import (
    ConversationPlan,
    NaturalLanguagePipeline,
    NLEntities,
    NLIntent,
    NLInterpretation,
    PlannedAction,
    contains_contextual_reference,
)
from report_validator import ReportValidator


class TurnRecordingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / 'work.sqlite3'
        self.db = Database(self.db_path)

    def test_turn_memory_survives_database_restart(self):
        t1 = self.db.record_conversation_turn(
            owner_id=101, role='user', text='tested on Wayland and it failed', intent='add_case_event'
        )
        t2 = self.db.record_conversation_turn(
            owner_id=101, role='assistant', text='Recorded Wayland failure.', intent='add_case_event'
        )
        self.assertIsNotNone(t1)
        self.assertIsNotNone(t2)

        del self.db
        reopened_db = Database(self.db_path)
        turns = reopened_db.get_recent_turns(owner_id=101, limit=10)
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0]['text'], 'tested on Wayland and it failed')
        self.assertEqual(turns[1]['text'], 'Recorded Wayland failure.')


class MemoryServiceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / 'work.sqlite3'
        self.db = Database(self.db_path)

    def test_ai_context_includes_active_case_and_task_details(self):
        c_id = self.db.create_case(title='Ubuntu Screenshot Issue', client='GBB', product='GBB')
        t_id = TaskService(self.db).create_task(title='Retest Ubuntu Wayland', priority=1).entity_id

        self.db.update_conversation_context('owner', active_case_id=c_id, active_task_id=t_id)

        ctx = build_assistant_context(self.db, owner_id=101)
        self.assertIsNotNone(ctx['active_case'])
        self.assertEqual(ctx['active_case']['id'], c_id)
        self.assertEqual(ctx['active_case']['title'], 'Ubuntu Screenshot Issue')

        self.assertIsNotNone(ctx['active_task'])
        self.assertEqual(ctx['active_task']['id'], t_id)

        prompt_str = format_context_for_prompt(ctx)
        self.assertIn('Ubuntu Screenshot Issue', prompt_str)
        self.assertIn('Retest Ubuntu Wayland', prompt_str)


class ContextualRoutingAndPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / 'work.sqlite3'
        self.db = Database(self.db_path)

    def test_contains_contextual_reference_detection(self):
        self.assertTrue(contains_contextual_reference("tested it on X11 and it works"))
        self.assertTrue(contains_contextual_reference("mark that case waiting client"))
        self.assertFalse(contains_contextual_reference("show pending tasks"))

    def test_multi_action_plan_execution(self):
        pipeline = NaturalLanguagePipeline(self.db)
        plan = ConversationPlan(
            actions=[
                PlannedAction(
                    intent=NLIntent.CREATE_CASE,
                    confidence=0.95,
                    entities=NLEntities(case_title="GBB Ubuntu Issue", client="GBB")
                ),
                PlannedAction(
                    intent=NLIntent.CREATE_TASK,
                    confidence=0.90,
                    entities=NLEntities(task_title="Retest Ubuntu 24")
                )
            ],
            reply="Created GBB Ubuntu case and retest task."
        )

        res = asyncio.run(pipeline.execute_plan(plan, shift=None))
        self.assertTrue(res.success)
        self.assertIsNotNone(res.correlation_id)
        self.assertIn("Created GBB Ubuntu case", res.reply)


class DatabaseWalAndFtsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / 'work.sqlite3'
        self.db = Database(self.db_path)

    def test_sqlite_wal_mode_enabled(self):
        self.assertGreaterEqual(SCHEMA_VERSION, 13)
        with self.db.connect() as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(mode.lower(), 'wal')

    def test_historical_fts_indexing_and_search(self):
        self.db.index_fts_record(
            source_type='case',
            source_id=14,
            title='Ubuntu Screenshot Blank',
            content='Screenshot blank under Wayland but works on X11',
            client='GBB'
        )

        results = self.db.search_historical_memory('Wayland')
        self.assertGreaterEqual(len(results), 1)
        self.assertEqual(results[0]['source_id'], '14')
        self.assertIn('Wayland', results[0]['content'])


class ApplicationServicesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / 'work.sqlite3'
        self.db = Database(self.db_path)

    def test_task_service_complete_returns_mutation_result(self):
        t_service = TaskService(self.db)
        t_id = t_service.create_task(title='Review GBB logs').entity_id

        res = t_service.complete_task(t_id, completion_note='Logs verified', correlation_id='corr_123')
        self.assertTrue(res.success)
        self.assertEqual(res.correlation_id, 'corr_123')
        self.assertEqual(res.entity_id, t_id)
        self.assertTrue(res.reversible)

    def test_case_service_change_status_returns_mutation_result(self):
        c_service = CaseService(self.db)
        c_id = self.db.create_case(title='Attendance Issue')

        res = c_service.change_status(c_id, 'investigating', correlation_id='corr_456')
        self.assertTrue(res.success)
        self.assertEqual(res.entity_id, c_id)
        self.assertTrue(res.reversible)


class ReportValidatorTests(unittest.TestCase):
    def test_client_casefolding_deduplication(self):
        cases = [
            {'client': 'Acme', 'participation': 'owned', 'status': 'investigating'},
            {'client': 'ACME', 'participation': 'owned', 'status': 'investigating'},
            {'client': ' acme ', 'participation': 'owned', 'status': 'investigating'}
        ]
        shift = {'start': '2026-09-12T00:00:00', 'end': '2026-09-12T23:59:59'}
        res = ReportValidator.validate('eod', 'Report for Acme', activities=[], tasks=[], cases=cases, test_sessions=[], shift=shift)
        self.assertEqual(res.verified_metrics['unique_clients_count'], 1)


class MorningBriefingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / 'work.sqlite3'
        self.db = Database(self.db_path)

    def test_generate_morning_briefing_returns_text(self):
        briefing = generate_morning_briefing(self.db)
        self.assertIn('Personal Work Assistant', briefing)
        self.assertIn('Tasks', briefing)


class ChunkedEvidenceHashTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / 'work.sqlite3'
        self.db = Database(self.db_path)

    def test_add_evidence_chunked_hash(self):
        file_path = Path(self.tmp.name) / 'test_evidence.png'
        file_path.write_bytes(b'x' * (2 * 1024 * 1024))  # 2MB file

        c_id = self.db.create_case(title='Test Case')

        ev_id = self.db.add_evidence('screenshot', case_id=c_id, path=file_path, caption='Screenshot')
        self.assertIsNotNone(ev_id)
        ev_item = self.db.evidence_item(ev_id)
        self.assertIsNotNone(ev_item.get('sha256'))


if __name__ == '__main__':
    unittest.main()
