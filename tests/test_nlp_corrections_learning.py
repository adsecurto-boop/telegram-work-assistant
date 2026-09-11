"""Regression test suite for Telegram Work Assistant NLP correction learning,
similarity retrieval, deterministic parser improvements, and carry-over handling.
"""
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import config
import handlers
from database import Database
from models import TaskStatus
from nlp import (
    DeterministicParser,
    NaturalLanguagePipeline,
    NLEntities,
    NLIntent,
    get_tz_today,
)
from nlp_policy import ActionDecision, ReasonCode, evaluate_action_policy
import reports


REF = datetime(2026, 9, 11, 12, tzinfo=ZoneInfo(config.TIMEZONE))


class NLPCorrectionsLearningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / 'work.sqlite3'
        self.db = Database(self.db_path)
        self.pipeline = NaturalLanguagePipeline(self.db, ai_client=None)
        self.tz = ZoneInfo(config.TIMEZONE)
        self.today = datetime.now(self.tz).date().isoformat()
        self.tomorrow = (datetime.now(self.tz).date() + timedelta(days=1)).isoformat()
        self.owner_mock = patch.object(config, 'OWNER_ID', 1001)
        self.owner_mock.start()

    async def asyncTearDown(self):
        self.owner_mock.stop()
        self.tmp_dir.cleanup()

    def _make_update(self, text: str):
        msg = SimpleNamespace(
            text=text,
            message_id=999,
            date=datetime.now(self.tz),
            reply_text=AsyncMock(),
        )
        update = SimpleNamespace()
        update.effective_user = SimpleNamespace(id=1001, username='tester')
        update.effective_chat = SimpleNamespace(id=1001, type='private')
        update.effective_message = msg
        update.message = msg
        update.callback_query = None
        return update

    def _make_context(self):
        ctx = SimpleNamespace()
        app = SimpleNamespace(bot_data={'db': self.db})
        ctx.application = app
        ctx.bot_data = app.bot_data
        ctx.bot = SimpleNamespace(send_message=AsyncMock())
        ctx.user_data = {}
        return ctx

    # -------------------------------------------------------------------------
    # Requirement 3: 10 Target Deterministic Examples
    # -------------------------------------------------------------------------

    def test_01_target_deterministic_examples(self):
        ref_date = REF.date().isoformat()

        # 1. Carried tasks into today
        t1 = "my carried tasks is to check empmonitor agent issue in ubuntu 22 for gbb client"
        p1 = DeterministicParser.parse(t1, REF)
        self.assertIsNotNone(p1)
        self.assertEqual(p1.intent, NLIntent.CARRY_TASK_FORWARD)
        self.assertEqual(p1.entities.date, ref_date)
        self.assertIn("empmonitor agent issue in ubuntu 22", p1.entities.task_title)
        self.assertEqual(p1.entities.client, "gbb")
        self.assertEqual(p1.entities.platform, "ubuntu 22")

        # 2. Shift with explicit date
        t2 = "my shift on today ie 11 september is from 12 pm to 9 pm"
        p2 = DeterministicParser.parse(t2, REF)
        self.assertIsNotNone(p2)
        self.assertEqual(p2.intent, NLIntent.SET_SHIFT)
        self.assertEqual(p2.entities.date, "2026-09-11")
        self.assertEqual(p2.entities.shift_start, "12:00")
        self.assertEqual(p2.entities.shift_end, "21:00")

        # 3. Retest status reported to dev (no test result/resolution invented)
        t3 = "I reported the retest test status of Airtel africa client agent 3.8.0 to the developer"
        p3 = DeterministicParser.parse(t3, REF)
        self.assertIsNotNone(p3)
        self.assertEqual(p3.intent, NLIntent.ADD_CASE_EVENT)
        self.assertEqual(p3.entities.client, "airtel africa")
        self.assertIsNone(p3.entities.test_result)
        self.assertNotEqual(p3.entities.status, "resolved")

        # 4. Planned testing work for TOD (not completed testing)
        t4 = "today i have to perform testing in ubuntu 22 system for gbb client for checking if empmonitor faces issues in ubuntu 22 version"
        p4 = DeterministicParser.parse(t4, REF)
        self.assertIsNotNone(p4)
        self.assertEqual(p4.intent, NLIntent.CREATE_TASK)
        self.assertEqual(p4.entities.client, "gbb")
        self.assertEqual(p4.entities.platform, "ubuntu 22")

        # 5. Standalone EOD
        t5 = "eod"
        p5 = DeterministicParser.parse(t5, REF)
        self.assertIsNotNone(p5)
        self.assertEqual(p5.intent, NLIntent.GENERATE_EOD)

        # 6. Addressed client (assisted, resolution not asserted)
        t6 = "Addressed cloud destinations client in teams for absent user issue in macos devices"
        p6 = DeterministicParser.parse(t6, REF)
        self.assertIsNotNone(p6)
        self.assertEqual(p6.intent, NLIntent.LOG_SUPPORT)
        self.assertEqual(p6.entities.client, "cloud destinations")
        self.assertEqual(p6.entities.channel, "teams")
        self.assertEqual(p6.entities.platform, "macos")
        self.assertEqual(p6.entities.status, "assisted")

        # 7. Resolved client (resolution asserted)
        t7 = "resolved cloud destinations client in teams for absent user issue in macos devices"
        p7 = DeterministicParser.parse(t7, REF)
        self.assertIsNotNone(p7)
        self.assertEqual(p7.intent, NLIntent.LOG_SUPPORT)
        self.assertEqual(p7.entities.client, "cloud destinations")
        self.assertEqual(p7.entities.status, "resolved")

        # 8. Assisted client (assistance)
        t8 = "assisted cloud destinations client in teams for absent user issue in macos devices"
        p8 = DeterministicParser.parse(t8, REF)
        self.assertIsNotNone(p8)
        self.assertEqual(p8.intent, NLIntent.LOG_SUPPORT)
        self.assertEqual(p8.entities.client, "cloud destinations")
        self.assertEqual(p8.entities.status, "assisted")

        # 9. Addressed client concern (assistance without assuming resolution)
        t9 = "addressed client concern about exaggerated office hours for virtual street group client"
        p9 = DeterministicParser.parse(t9, REF)
        self.assertIsNotNone(p9)
        self.assertEqual(p9.intent, NLIntent.LOG_SUPPORT)
        self.assertEqual(p9.entities.client, "virtual street group")
        self.assertEqual(p9.entities.status, "assisted")

        # 10. Shift extended to 8 pm
        t10 = "my shift has been extended to 8 pm"
        p10 = DeterministicParser.parse(t10, REF)
        self.assertIsNotNone(p10)
        self.assertEqual(p10.intent, NLIntent.SET_SHIFT)
        self.assertEqual(p10.entities.shift_end, "20:00")

    # -------------------------------------------------------------------------
    # Paraphrases of target examples
    # -------------------------------------------------------------------------

    def test_02_paraphrases_of_target_examples(self):
        # Explicit date paraphrase
        p1 = DeterministicParser.parse("todays shift ie 11 sep is from 12 pm to 9 pm", REF)
        self.assertEqual(p1.intent, NLIntent.SET_SHIFT)
        self.assertEqual(p1.entities.date, "2026-09-11")

        # Shift extension paraphrase
        p2 = DeterministicParser.parse("shift is extended till 8 pm", REF)
        self.assertEqual(p2.intent, NLIntent.SET_SHIFT)
        self.assertEqual(p2.entities.shift_end, "20:00")

        # Standalone TOD trigger
        p3 = DeterministicParser.parse("tod", REF)
        self.assertEqual(p3.intent, NLIntent.GENERATE_TOD)

        # Support assistance paraphrase
        p4 = DeterministicParser.parse("assisted virtual street group client regarding exaggerated office hours", REF)
        self.assertEqual(p4.intent, NLIntent.LOG_SUPPORT)
        self.assertEqual(p4.entities.status, "assisted")

    # -------------------------------------------------------------------------
    # Requirement 1: Retrieve relevant corrections older than the five newest
    # -------------------------------------------------------------------------

    def test_03_relevant_corrections_retrieval_older_than_five_newest(self):
        # Insert 8 corrections:
        # #1 is the oldest and matches "syncing inventory catalog with erp"
        # #2-#8 are newer but unrelated topics
        id1 = self.db.record_nl_interaction(
            raw_text="syncing inventory catalog with erp",
            intent="unknown",
            confidence=0.3
        )
        self.db.add_nl_correction(
            original_intent="unknown",
            corrected_intent="create_task",
            interaction_id=id1,
            corrected_entities={"task_title": "sync inventory catalog with erp"},
            owner_id=1001,
            notes="erp inventory sync"
        )

        for i in range(2, 9):
            uid = self.db.record_nl_interaction(
                raw_text=f"unrelated query #{i} about invoice billing payment",
                intent="unknown",
                confidence=0.2
            )
            self.db.add_nl_correction(
                original_intent="unknown",
                corrected_intent="log_support",
                interaction_id=uid,
                corrected_entities={"client": f"Client {i}"},
                owner_id=1001,
                notes="billing issue"
            )

        # Query similar to correction #1
        query = "need to sync inventory catalog with erp system"
        relevant = self.db.get_relevant_corrections(query, limit=5)

        self.assertTrue(len(relevant) >= 1)
        top = relevant[0]
        # Must be correction #1 even though it was created before the 7 newer corrections
        self.assertEqual(top["corrected_intent"], "create_task")
        self.assertIn("inventory catalog", top["raw_text"])

    # -------------------------------------------------------------------------
    # Requirement 2: Apply corrections safely without AI (offline)
    # -------------------------------------------------------------------------

    async def test_04_offline_interpretation_via_matching_correction_without_ai(self):
        # Save a correction for a custom domain phrasing
        iid = self.db.record_nl_interaction(
            raw_text="dispatch notification to warehouse supervisor",
            intent="unknown",
            confidence=0.3
        )
        self.db.add_nl_correction(
            original_intent="unknown",
            corrected_intent="create_task",
            interaction_id=iid,
            corrected_entities={"task_title": "dispatch notification to warehouse supervisor"},
            owner_id=1001
        )

        # Incoming similar message
        text = "dispatch notification to warehouse supervisor for shipment #44"
        interp = await self.pipeline.interpret_preview(text)

        self.assertIsNotNone(interp)
        self.assertEqual(interp.intent, NLIntent.CREATE_TASK)
        self.assertIn(ReasonCode.CORRECTION_MATCH.value, interp.reason_codes)
        # Safe bounded confidence: must not be blindly inflated to 1.0
        self.assertTrue(0.65 <= interp.confidence <= 0.78)

    # -------------------------------------------------------------------------
    # Requirement 2: Fresh entity extraction (never copy stale entities)
    # -------------------------------------------------------------------------

    async def test_05_fresh_entity_extraction_never_copies_stale_correction_values(self):
        # Save correction with old entities
        iid = self.db.record_nl_interaction(
            raw_text="scheduled work session for 10 january from 9 am to 5 pm for OldClient",
            intent="unknown",
            confidence=0.3
        )
        self.db.add_nl_correction(
            original_intent="unknown",
            corrected_intent="set_shift",
            interaction_id=iid,
            corrected_entities={
                "date": "2026-01-10",
                "shift_start": "09:00",
                "shift_end": "17:00",
                "client": "OldClient"
            },
            owner_id=1001
        )

        # New message with different times, dates, and clients
        new_text = "scheduled work session for 11 september from 12 pm to 9 pm for NewClient"
        interp = await self.pipeline.interpret_preview(new_text)

        self.assertEqual(interp.intent, NLIntent.SET_SHIFT)
        # Entities must be extracted FRESH from new_text, NEVER copied from old correction!
        self.assertEqual(interp.entities.shift_start, "12:00")
        self.assertEqual(interp.entities.shift_end, "21:00")
        self.assertEqual(interp.entities.date, "2026-09-11")
        self.assertNotEqual(interp.entities.shift_start, "09:00")
        self.assertNotEqual(interp.entities.client, "OldClient")

    # -------------------------------------------------------------------------
    # Requirement 2: Conflicting corrections require clarification
    # -------------------------------------------------------------------------

    async def test_06_conflicting_corrections_require_clarification(self):
        # Save two conflicting corrections with very similar wording
        id1 = self.db.record_nl_interaction(
            raw_text="quick sync with dev lead regarding agent latency",
            intent="unknown",
            confidence=0.3
        )
        self.db.add_nl_correction(
            original_intent="unknown",
            corrected_intent="create_task",
            interaction_id=id1,
            owner_id=1001
        )

        id2 = self.db.record_nl_interaction(
            raw_text="quick sync with dev lead regarding agent latency",
            intent="unknown",
            confidence=0.3
        )
        self.db.add_nl_correction(
            original_intent="unknown",
            corrected_intent="log_support",
            interaction_id=id2,
            owner_id=1001
        )

        query = "quick sync with dev lead regarding agent latency"
        reply, interp = await self.pipeline.process(query, shift=None)

        self.assertTrue(interp.needs_confirmation)
        self.assertIn(ReasonCode.CONFLICTING_CORRECTIONS.value, interp.reason_codes)
        self.assertTrue(len(interp.choices) >= 2)
        decision, would_mutate, _ = evaluate_action_policy(interp)
        self.assertEqual(decision, ActionDecision.REQUIRE_CLARIFICATION)
        self.assertFalse(would_mutate)

    # -------------------------------------------------------------------------
    # Requirement 5: Ambiguous task references for carry-over require clarification
    # -------------------------------------------------------------------------

    async def test_07_ambiguous_task_references_for_carry_forward(self):
        # Create two similar pending tasks
        self.db.add_task("Check empmonitor agent issue in ubuntu 22 for gbb client")
        self.db.add_task("Check empmonitor agent issue in macos for gbb client")

        # Ambiguous input matching both
        text = "my carried tasks is to check empmonitor agent issue for gbb client"
        reply, interp = await self.pipeline.process(text, shift=None)

        self.assertTrue(interp.needs_confirmation)
        self.assertIn(ReasonCode.AMBIGUOUS_REFERENCE.value, interp.reason_codes)
        self.assertTrue(len(interp.choices) >= 2)
        self.assertIn("Multiple tasks match", interp.clarification_question)

    # -------------------------------------------------------------------------
    # Requirement 3 & 5: Carry-over into today reuses task, prevents duplicate,
    # and appears in TOD
    # -------------------------------------------------------------------------

    async def test_08_carry_over_into_today_reuses_task_prevents_duplicate_and_appears_in_tod(self):
        # 1. Existing task from yesterday
        task = self.db.add_task("check empmonitor agent issue in ubuntu 22 for gbb client", shift_id=None)
        orig_task_id = task.id

        # 2. Start shift for today
        sid = self.db.start_shift(f"{self.today}T10:00:00+05:30", f"{self.today}T19:00:00+05:30")
        shift = self.db.shift(sid)

        # 3. Process carried task into today
        text = "my carried tasks is to check empmonitor agent issue in ubuntu 22 for gbb client"
        reply, interp = await self.pipeline.process(text, shift=shift)

        # 4. Verify task identity preserved (no duplicate created)
        all_tasks = self.db.list_tasks()
        self.assertEqual(len(all_tasks), 1)
        self.assertEqual(all_tasks[0].id, orig_task_id)

        # 5. Verify task is associated with today's shift and due today
        reused_task = self.db.get_task(orig_task_id)
        self.assertEqual(reused_task.planned_shift_id, sid)
        self.assertEqual(reused_task.due_date, self.today)
        self.assertNotEqual(reused_task.due_date, self.tomorrow)

        # 6. Verify task is included in TOD report
        shift_tasks = self.db.tasks_for_shift(sid)
        activities = self.db.activities(sid)
        tod_report = reports.generate_report('tod', shift, activities, shift_tasks)
        self.assertIn("empmonitor agent issue in ubuntu 22", tod_report)

    # -------------------------------------------------------------------------
    # Requirement 5: Carry forward to tomorrow moves to tomorrow, not today
    # -------------------------------------------------------------------------

    async def test_09_carry_forward_to_tomorrow_moves_to_tomorrow_not_today(self):
        # Create a pending task
        t = self.db.add_task("Review quarterly compliance report")

        # User explicitly carries to tomorrow
        text = "Carry the compliance task to tomorrow"
        reply, interp = await self.pipeline.process(text, shift=None)

        updated_task = self.db.get_task(t.id)
        self.assertEqual(updated_task.due_date, self.tomorrow)
        self.assertIn(f"Moved 1 task(s) to tomorrow ({self.tomorrow})", reply)

    # -------------------------------------------------------------------------
    # Requirement 4: /correct is strictly non-executing
    # -------------------------------------------------------------------------

    async def test_10_correct_command_is_strictly_non_executing(self):
        # Record unknown interaction
        iid = self.db.record_nl_interaction(
            raw_text="verify db latency on cluster",
            intent="unknown",
            confidence=0.1
        )
        task_count_before = len(self.db.list_tasks())

        # Execute /correct
        update = self._make_update(f"/correct {iid} create_task task_title='verify db latency'")
        ctx = self._make_context()
        await handlers.handle(update, ctx)

        # Verify interaction is marked corrected
        interaction = self.db.get_nl_interaction(iid)
        self.assertEqual(interaction['status'], 'corrected')

        # Verify correction record exists
        corrections = self.db.get_approved_corrections(limit=5)
        self.assertTrue(any(c['interaction_id'] == iid for c in corrections))

        # Critical: NO work action was executed (no tasks created)
        self.assertEqual(len(self.db.list_tasks()), task_count_before)

    # -------------------------------------------------------------------------
    # Requirement 4: /understand uses same pipeline and never mutates
    # -------------------------------------------------------------------------

    async def test_11_understand_command_uses_pipeline_and_never_mutates(self):
        sid = self.db.start_shift(f"{self.today}T10:00:00+05:30", f"{self.today}T19:00:00+05:30")
        before_shift = self.db.active_shift()

        # Send /understand for a shift extension request
        update = self._make_update("/understand my shift has been extended to 8 pm")
        ctx = self._make_context()
        await handlers.handle(update, ctx)

        # Verify reply was sent showing read-only preview
        update.message.reply_text.assert_called_once()
        reply_content = update.message.reply_text.call_args[0][0]
        self.assertIn("Intent: set_shift", reply_content)
        self.assertIn("Preview action: read_only (always read-only)", reply_content)
        self.assertIn('"shift_end": "20:00"', reply_content)

        # Database state must be 100% UNTOUCHED
        after_shift = self.db.active_shift()
        self.assertEqual(after_shift['end'], before_shift['end'])
        self.assertEqual(len(self.db.get_audit_log()), 0)


if __name__ == '__main__':
    unittest.main()
