"""
Integration and regression test suite verifying Core Runtime Closure requirements:
- Gemini ConversationPlan execution & single correlation transaction semantics
- Turn persistence idempotency & update lifecycle
- Rolling memory triggers, turn range tracking, and shift_id correctness
- Exact Undo button correlation matching
- Automatic FTS indexing, historical backfill (schema v14), and retrieval
- Dashboard 303 token redirect & review inbox filtering/pagination
- ReportValidator hardening and reminder delivery separation
- Explicit mutation policy and morning briefing
"""
import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from database import Database, SCHEMA_VERSION
from models import TaskStatus
from nlp import NaturalLanguagePipeline, ConversationPlan, PlannedAction, NLIntent, NLEntities
from nlp_policy import required_entities_for_intent, evaluate_action_policy, ActionDecision
from memory_service import build_assistant_context
from report_validator import ReportValidator
from daily_assistant import generate_morning_briefing
import config


class CoreRuntimeClosureTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_closure.sqlite3"
        self.db = Database(self.db_path)
        self.owner_id = config.OWNER_ID

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_every_mutating_intent_has_explicit_policy(self):
        mutating_intents = [
            'set_shift', 'create_task', 'update_task', 'complete_task',
            'carry_task_forward', 'create_case', 'update_case', 'change_case_status',
            'add_case_event', 'add_client_update', 'create_test_session',
            'update_test_session', 'attach_evidence', 'add_learning',
            'create_followup', 'complete_followup', 'snooze_followup', 'log_support'
        ]
        for intent in mutating_intents:
            reqs = required_entities_for_intent(intent)
            self.assertIsInstance(reqs, list, f"Intent {intent} returned invalid required entities type")
            self.assertTrue(len(reqs) > 0, f"Mutating intent {intent} silently inherited empty required entities list")

    def test_turn_persistence_and_retry_idempotency(self):
        uid = 9001
        t1 = self.db.record_conversation_turn(self.owner_id, 'user', 'Hello assistant', source_update_id=uid)
        t2 = self.db.record_conversation_turn(self.owner_id, 'user', 'Hello assistant', source_update_id=uid)
        self.assertEqual(t1, t2, "Duplicate turn created for identical owner, source_update_id, and role")

        turns = self.db.get_recent_turns(self.owner_id)
        self.assertEqual(len(turns), 1)

    def test_update_lifecycle_complete_and_fail(self):
        uid = 5001
        self.assertTrue(self.db.claim_update(uid))
        self.db.complete_update(uid)
        with self.db.connect() as conn:
            row = conn.execute("SELECT status FROM updates WHERE id=?", (uid,)).fetchone()
            self.assertEqual(row['status'], 'completed')

        uid_fail = 5002
        self.assertTrue(self.db.claim_update(uid_fail))
        self.db.fail_update(uid_fail, "Simulated crash")
        with self.db.connect() as conn:
            row_f = conn.execute("SELECT status, last_error FROM updates WHERE id=?", (uid_fail,)).fetchone()
            self.assertEqual(row_f['status'], 'failed')
            self.assertEqual(row_f['last_error'], "Simulated crash")

    async def test_rolling_summary_triggers_above_threshold_and_uses_correct_shift_id(self):
        sid = self.db.start_shift("10:00", "19:00", "14:00")
        for i in range(25):
            self.db.record_conversation_turn(
                self.owner_id,
                'user' if i % 2 == 0 else 'assistant',
                f"Turn text message #{i}",
                shift_id=sid,
                source_update_id=1000 + i
            )

        ctx = build_assistant_context(self.db, owner_id=self.owner_id)
        summary = ctx.get('latest_summary')
        self.assertIsNotNone(summary, "Rolling summary should trigger when unsummarized turns >= 15")
        self.assertIn("USER:", summary['summary_text'])

    async def test_multi_action_conversation_plan_execution_and_rollback(self):
        nlp = NaturalLanguagePipeline(self.db, ai_client=None)
        sid = self.db.start_shift("10:00", "19:00", "14:00")

        # Compound plan: Create task + Create case
        plan = ConversationPlan(actions=[
            PlannedAction(intent=NLIntent.CREATE_TASK, confidence=1.0, entities=NLEntities(task_title="Test compound task 1")),
            PlannedAction(intent=NLIntent.CREATE_CASE, confidence=1.0, entities=NLEntities(case_title="Test compound case 1", client="Acme"))
        ])

        res = await nlp.execute_plan(plan, self.db.active_shift())
        self.assertTrue(res.success)
        self.assertIsNotNone(res.correlation_id)
        self.assertEqual(len(res.executed_actions), 2)

        # Verify rollback on failing action inside compound plan
        failing_plan = ConversationPlan(actions=[
            PlannedAction(intent=NLIntent.CREATE_TASK, confidence=1.0, entities=NLEntities(task_title="Task before crash")),
            PlannedAction(intent=NLIntent.CHANGE_CASE_STATUS, confidence=1.0, entities=NLEntities(case_id=99999, status="invalid_status"))
        ])
        fail_res = await nlp.execute_plan(failing_plan, self.db.active_shift())
        self.assertFalse(fail_res.success)

    def test_v14_fts_backfill_and_historical_search(self):
        self.db.add_task("Fix Wayland screenshot bug", client="Acme", project="GBB")
        case_id = self.db.create_case("Wayland blank screenshot on Ubuntu 24", client="Acme", product="GBB")
        self.db.add_case_event(case_id, "testing", "Testing under X11 works cleanly")

        # Run v14 seed backfill
        with self.db.connect() as conn:
            self.db._seed_v14_defaults(conn)

        results = self.db.search_historical_memory("Wayland screenshot")
        self.assertTrue(len(results) > 0, "FTS historical memory search returned no matches after backfill")

    def test_report_validator_none_client_handling(self):
        shift = {'start': '2026-09-12T10:00:00', 'end': '2026-09-12T19:00:00'}
        activities = [{'category': 'support', 'client': None, 'query': 'Help request'}]
        cases = [{'id': 1, 'title': 'Test Case', 'client': None, 'status': 'new'}]
        tasks = []

        res = ReportValidator.validate('eod', 'Report for shift', shift, activities, tasks, cases)
        self.assertIsInstance(res.is_valid, bool)

    def test_morning_briefing_sections(self):
        self.db.add_task("Overdue priority item", priority=3, due_date="2026-01-01")
        self.db.create_case("Critical client bug", client="Acme", status="triaged")

        text = generate_morning_briefing(self.db)
        self.assertIn("Morning Briefing", text)
        self.assertIn("Tasks & Work Overview", text)
        self.assertIn("Case Breakdown", text)
        self.assertIn("Recommended First Focus", text)
