"""Stage I — Integration and Conversational Planning Test Suite.
Tests 25 complete conversational and operational workflows:
1. Startday with all details.
2. Missing lunch time and a follow-up answer.
3. Restart during planning.
4. Duplicate Telegram update.
5. Stale confirmation button.
6. Exact and similar task titles.
7. Ambiguous task/client references.
8. Linked task completed without closing its request.
9. Failed retest after a developer reports a fix.
10. Correction followed by Undo.
11. Late work during an overnight shift.
12. Late work affecting a finalized EOD.
13. Planned versus unplanned checkpoint comparison.
14. Multiple interactions for one client.
15. Report wording change without data mutation.
16. Factual correction requiring confirmation.
17. New work after EOD preview.
18. Gemini timeout/malformed output/offline behavior.
19. Missed reminders after Windows resumes.
20. Carry-forward preserving IDs and blockers.
21. Owner-only command and callback authorization.
22. Delete all tasks immediately with Undo.
23. Scoped task deletion.
24. Historical reports surviving task deletion.
25. Migration failure rollback and repeated initialization.
"""
import asyncio
import json
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch, MagicMock
from zoneinfo import ZoneInfo

import config
import handlers
import daily_assistant
from database import Database, SCHEMA_VERSION, TaskStatus
import reports
import scheduler
from report_validator import ReportValidator


class ConversationalPlanningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / 'work.sqlite3'
        self.db = Database(self.db_path)

        self.owner_patch = patch.object(config, 'OWNER_ID', 123)
        self.owner_patch.start()
        self.addCleanup(self.owner_patch.stop)

        self.mock_bot = SimpleNamespace(
            send_message=AsyncMock(),
            send_document=AsyncMock()
        )
        self.context = SimpleNamespace(
            application=SimpleNamespace(
                bot_data={'db': self.db},
                job_queue=None
            ),
            bot=self.mock_bot
        )

    def make_update(self, text='', user=123, chat=123, uid=1001, callback_data=None):
        msg = SimpleNamespace(
            text=text,
            caption=None,
            forward_origin=None,
            reply_text=AsyncMock(),
            reply_document=AsyncMock()
        )
        cq = None
        if callback_data:
            cq = SimpleNamespace(
                data=callback_data,
                answer=AsyncMock(),
                message=msg,
                from_user=SimpleNamespace(id=user)
            )
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=user),
            effective_chat=SimpleNamespace(id=chat, type='private'),
            update_id=uid,
            callback_query=cq,
            effective_message=msg
        )

    # 1. Startday with all details.
    async def test_01_startday_with_all_details(self):
        up = self.make_update("Start my day. Shift is 12 to 9, lunch at 4. Need to test attendance and follow up on the NVIZION request.")
        handled = await handlers.handle(up, self.context)
        up.effective_message.reply_text.assert_awaited()
        reply_call = up.effective_message.reply_text.call_args[0][0]
        self.assertIn("Proposed Daily Plan:", reply_call)
        self.assertIn("12:00 to 21:00", reply_call)
        self.assertIn("Lunch: 16:00", reply_call)
        self.assertIn("test attendance", reply_call)
        self.assertIn("follow up on the NVIZION request", reply_call)

        conv = self.db.get_active_planning_conversation(123)
        self.assertIsNotNone(conv)
        self.assertEqual(conv['step'], 'awaiting_confirmation')

        # Now confirm the plan via callback
        up_confirm = self.make_update(callback_data=f"plan:confirm:{conv['id']}", uid=1002)
        await handlers.handle(up_confirm, self.context)

        shift = self.db.active_shift()
        self.assertIsNotNone(shift)
        tasks = self.db.tasks_for_shift(shift['id'])
        self.assertEqual(len(tasks), 2)
        snap = self.db.get_baseline_plan_snapshot(shift['id'])
        self.assertIsNotNone(snap)
        self.assertEqual(len(snap['tasks']), 2)
        # Conversation completed
        self.assertIsNone(self.db.get_active_planning_conversation(123))

    # 2. Missing lunch time and a follow-up answer.
    async def test_02_missing_lunch_and_follow_up_answer(self):
        up1 = self.make_update("Start my day. Shift is 10 to 7. Need to review logs", uid=1001)
        await handlers.handle(up1, self.context)
        reply_call1 = up1.effective_message.reply_text.call_args[0][0]
        self.assertIn("When is your lunch break scheduled?", reply_call1)

        conv = self.db.get_active_planning_conversation(123)
        self.assertEqual(conv['step'], 'awaiting_lunch')

        up2 = self.make_update("lunch at 2pm", uid=1002)
        await handlers.handle(up2, self.context)
        reply_call2 = up2.effective_message.reply_text.call_args[0][0]
        self.assertIn("Proposed Daily Plan:", reply_call2)
        self.assertIn("10:00 to 19:00", reply_call2)
        self.assertIn("Lunch: 14:00", reply_call2)

    # 3. Restart during planning.
    async def test_03_restart_during_planning(self):
        up1 = self.make_update("Start my day. Shift is 12 to 9.", uid=1001)
        await handlers.handle(up1, self.context)

        # Simulate bot restart by creating fresh Database instance connected to same sqlite3 file
        new_db = Database(self.db_path)
        self.context.application.bot_data['db'] = new_db

        active_conv = new_db.get_active_planning_conversation(123)
        self.assertIsNotNone(active_conv)
        self.assertEqual(active_conv['step'], 'awaiting_lunch')

        up2 = self.make_update("lunch at 4", uid=1002)
        await handlers.handle(up2, self.context)
        reply_call2 = up2.effective_message.reply_text.call_args[0][0]
        self.assertIn("Proposed Daily Plan:", reply_call2)
        self.assertIn("12:00 to 21:00", reply_call2)
        self.assertIn("Lunch: 16:00", reply_call2)

    # 4. Duplicate Telegram update.
    async def test_04_duplicate_telegram_update(self):
        up = self.make_update("Start my day. Shift is 12 to 9, lunch at 4. Need to test build", uid=1001)
        await handlers.handle(up, self.context)
        conv = self.db.get_active_planning_conversation(123)

        up_confirm1 = self.make_update(callback_data=f"plan:confirm:{conv['id']}", uid=1002)
        await handlers.handle(up_confirm1, self.context)
        shift = self.db.active_shift()
        tasks_before = self.db.tasks_for_shift(shift['id'])

        # Duplicate delivery of same callback
        up_confirm2 = self.make_update(callback_data=f"plan:confirm:{conv['id']}", uid=1002)
        await handlers.handle(up_confirm2, self.context)
        reply_dup = up_confirm2.effective_message.reply_text.call_args[0][0]
        self.assertIn("already completed", reply_dup)
        tasks_after = self.db.tasks_for_shift(shift['id'])
        self.assertEqual(len(tasks_before), len(tasks_after))

    # 5. Stale confirmation button.
    async def test_05_stale_confirmation_button(self):
        up = self.make_update("Start my day. Shift is 12 to 9, lunch at 4.", uid=1001)
        await handlers.handle(up, self.context)
        conv = self.db.get_active_planning_conversation(123)

        # Cancel conversation
        up_cancel = self.make_update("/cancel", uid=1002)
        await handlers.handle(up_cancel, self.context)

        # Now click stale confirm button
        up_stale = self.make_update(callback_data=f"plan:confirm:{conv['id']}", uid=1003)
        await handlers.handle(up_stale, self.context)
        reply_stale = up_stale.effective_message.reply_text.call_args[0][0]
        self.assertIn("already cancelled", reply_stale)
        self.assertIsNone(self.db.active_shift())

    # 6. Exact and similar task titles.
    async def test_06_exact_and_similar_task_titles(self):
        t1 = self.db.add_task("Attendance Testing")
        t2 = self.db.add_task("Review Q3 security report")

        up = self.make_update("Start my day. Shift is 12 to 9, lunch at 4. Need to attendance testing and review q3 security report updates.")
        await handlers.handle(up, self.context)
        reply = up.effective_message.reply_text.call_args[0][0]

        self.assertIn(f"[Existing #{t1.id}]", reply)
        self.assertIn(f"[Review similar #{t2.id}: 'Review Q3 security report']", reply)

    # 7. Ambiguous task/client references.
    async def test_07_ambiguous_task_client_references(self):
        sid = self.db.start_shift('2026-09-11T12:00:00+05:30', '2026-09-11T21:00:00+05:30')
        t1 = self.db.add_task("Attendance bug for Client Alpha", shift_id=sid)
        t2 = self.db.add_task("Attendance bug for Client Beta", shift_id=sid)

        up = self.make_update("Started attendance bug")
        await handlers.handle(up, self.context)
        reply = up.effective_message.reply_text.call_args[0][0]
        self.assertIn("Multiple matching tasks found", reply)
        self.assertIn(f"Task #{t1.id}", reply)
        self.assertIn(f"Task #{t2.id}", reply)
        # Neither task mutated
        self.assertEqual(self.db.get_task(t1.id).status, TaskStatus.PENDING)
        self.assertEqual(self.db.get_task(t2.id).status, TaskStatus.PENDING)

    # 8. Linked task completed without closing its request.
    async def test_08_linked_task_completed_without_closing_request(self):
        sid = self.db.start_shift('2026-09-11T12:00:00+05:30', '2026-09-11T21:00:00+05:30')
        from work_messages import DraftStore
        store = DraftStore(self.db, 123)
        draft = store.create("NVIZION feature request", {'client': 'NVIZION', 'scenario': 'requirement'}, source_key="test_key_nvizion")
        task = self.db.add_task("Share requirement with dev", shift_id=sid)
        self.db.link_records('task', task.id, 'work_draft', draft['id'])

        # Mark task completed
        up = self.make_update("Finished share requirement with dev")
        await handlers.handle(up, self.context)
        self.assertEqual(self.db.get_task(task.id).status, TaskStatus.COMPLETED)

        # Draft remains active / open
        d = store.get(draft['id'])
        self.assertEqual(d['status'], 'draft')

    # 9. Failed retest after a developer reports a fix.
    async def test_09_failed_retest_after_developer_reports_fix(self):
        sid = self.db.start_shift('2026-09-11T12:00:00+05:30', '2026-09-11T21:00:00+05:30')
        task = self.db.add_task("Retest attendance bug", shift_id=sid)
        self.db.save_conversation_context('owner', active_task_id=task.id)

        up = self.make_update("Actually, it failed on Windows 11")
        await handlers.handle(up, self.context)
        reply = up.effective_message.reply_text.call_args[0][0]
        self.assertIn("Proposed correction for Task", reply)
        self.assertIn("Failed on Windows 11", reply)

        prop = self.db.get_active_nl_proposal(123)
        self.assertIsNotNone(prop)

        # Confirm correction
        up_confirm = self.make_update(callback_data=f"corr:confirm:{prop['id']}")
        await handlers.handle(up_confirm, self.context)
        t_after = self.db.get_task(task.id)
        self.assertEqual(t_after.status, TaskStatus.BLOCKED)
        self.assertEqual(t_after.blocked_reason, "Failed on Windows 11")

    # 10. Correction followed by Undo.
    async def test_10_correction_followed_by_undo(self):
        sid = self.db.start_shift('2026-09-11T12:00:00+05:30', '2026-09-11T21:00:00+05:30')
        task = self.db.add_task("Retest attendance bug", shift_id=sid)
        self.db.save_conversation_context('owner', active_task_id=task.id)

        up = self.make_update("Actually, it failed on Windows 11")
        await handlers.handle(up, self.context)
        prop = self.db.get_active_nl_proposal(123)

        up_confirm = self.make_update(callback_data=f"corr:confirm:{prop['id']}")
        await handlers.handle(up_confirm, self.context)
        self.assertEqual(self.db.get_task(task.id).status, TaskStatus.BLOCKED)

        # Undo the correction
        up_undo = self.make_update("/undo")
        await handlers.handle(up_undo, self.context)
        t_reverted = self.db.get_task(task.id)
        self.assertEqual(t_reverted.status, TaskStatus.PENDING)

    # 11. Late work during an overnight shift.
    async def test_11_late_work_during_overnight_shift(self):
        # Shift spans yesterday 20:00 to today 05:00
        sid = self.db.start_shift('2026-09-10T20:00:00+05:30', '2026-09-11T05:00:00+05:30')
        up = self.make_update("Yesterday at 11 pm I tested the new build.")
        await handlers.handle(up, self.context)

        acts = self.db.activities(sid)
        self.assertTrue(len(acts) > 0)
        late_act = [a for a in acts if 'tested the new build' in a['detail']][0]
        self.assertEqual(late_act['time_precision'], 'exact')
        self.assertEqual(late_act['category'], 'testing')
        self.assertIn("23:00", late_act['occurred_at'])

    # 12. Late work affecting a finalized EOD.
    async def test_12_late_work_affecting_finalized_eod(self):
        sid = self.db.start_shift('2026-09-10T10:00:00+05:30', '2026-09-10T19:00:00+05:30')
        rid = self.db.save_report(sid, 'eod', 'Finalized shift report content')
        self.db.finalize(rid)
        self.db.close_shift(sid)
        self.assertIsNone(self.db.active_shift())

        up = self.make_update("This happened during my previous shift: Client hotfix deployment")
        await handlers.handle(up, self.context)
        reply = up.effective_message.reply_text.call_args[0][0]
        self.assertIn("Existing finalized EOD is immutable", reply)
        self.assertIn("Generate a new revision with /eod revision", reply)

        # Verify finalized report text is unchanged
        rep = self.db.report(rid)
        self.assertEqual(rep['text'], 'Finalized shift report content')

    # 13. Planned versus unplanned checkpoint comparison.
    async def test_13_planned_versus_unplanned_checkpoint_comparison(self):
        sid = self.db.start_shift('2026-09-11T12:00:00+05:30', '2026-09-11T21:00:00+05:30', '2026-09-11T16:00:00+05:30')
        t1 = self.db.add_task("Planned Task 1", shift_id=sid)
        t2 = self.db.add_task("Planned Task 2", shift_id=sid)
        self.db.save_plan_snapshot(sid, [t1, t2])

        # Add unplanned task
        t3 = self.db.add_task("Unplanned Urgent Fix", shift_id=sid)
        self.db.mark_status(t1.id, TaskStatus.COMPLETED)
        self.db.mark_status(t2.id, TaskStatus.IN_PROGRESS)

        up = self.make_update("/checkpoint")
        with patch('handlers.make_report', AsyncMock()):
            await handlers.handle(up, self.context)
        
        # Check that replies contained comparisons
        calls = [c[0][0] for c in up.effective_message.reply_text.call_args_list]
        combined = " ".join(calls)
        self.assertIn("Completed: #1 Planned Task 1", combined)
        self.assertIn("In Progress: #2 Planned Task 2", combined)
        self.assertIn("Unplanned work added: #3 Unplanned Urgent Fix", combined)
        self.assertIn("is still in progress", combined)

    # 14. Multiple interactions for one client.
    async def test_14_multiple_interactions_for_one_client(self):
        sid = self.db.start_shift('2026-09-11T12:00:00+05:30', '2026-09-11T21:00:00+05:30')
        self.db.add_activity(sid, 'support', 'Query 1', client='Acme Corp')
        self.db.add_activity(sid, 'support', 'Query 2', client='Acme Corp')
        self.db.add_activity(sid, 'support', 'Query 3', client='Acme Corp')
        self.db.add_activity(sid, 'support', 'Query 4', client='Beta Inc')

        shift = self.db.active_shift()
        acts = self.db.activities(sid)
        tasks = self.db.tasks_for_shift(sid)
        val = ReportValidator.validate('eod', 'Clients handled: 2. Interactions: 4', shift, acts, tasks)
        self.assertEqual(val.verified_metrics['unique_clients_count'], 2)
        self.assertEqual(val.verified_metrics['support_interactions_count'], 4)

    # 15. Report wording change without data mutation.
    async def test_15_report_wording_change_without_data_mutation(self):
        sid = self.db.start_shift('2026-09-11T12:00:00+05:30', '2026-09-11T21:00:00+05:30')
        t = self.db.add_task("Module testing", shift_id=sid)
        self.db.add_activity(sid, 'support', 'Assisted user with setup', client='Acme')
        rid = self.db.save_report(sid, 'eod', 'Long detailed initial report')

        up = self.make_update("Make the EOD shorter")
        await handlers.handle(up, self.context)
        reply = up.effective_message.reply_text.call_args[0][0]
        self.assertIn("Updated EOD (Revision #", reply)

        revs = self.db.get_report_revisions(rid)
        self.assertEqual(len(revs), 1)
        self.assertEqual(revs[0]['style'], 'short')
        # Tasks and activities untouched
        self.assertEqual(self.db.get_task(t.id).title, "Module testing")
        self.assertEqual(len(self.db.activities(sid)), 2)

    # 16. Factual correction requiring confirmation.
    async def test_16_factual_correction_requiring_confirmation(self):
        sid = self.db.start_shift('2026-09-11T12:00:00+05:30', '2026-09-11T21:00:00+05:30')
        task = self.db.add_task("Verify API endpoint", client="ClientAlpha", shift_id=sid)
        self.db.save_conversation_context('owner', active_task_id=task.id)

        up = self.make_update("That was for client NVIZION")
        await handlers.handle(up, self.context)

        # Before confirmation, task client is still ClientAlpha
        t_before = self.db.get_task(task.id)
        self.assertEqual(t_before.client, "ClientAlpha")

        prop = self.db.get_active_nl_proposal(123)
        self.assertIsNotNone(prop)

        # Confirm
        up_confirm = self.make_update(callback_data=f"corr:confirm:{prop['id']}")
        await handlers.handle(up_confirm, self.context)
        t_after = self.db.get_task(task.id)
        self.assertEqual(t_after.client, "NVIZION")

    # 17. New work after EOD preview.
    async def test_17_new_work_after_eod_preview(self):
        sid = self.db.start_shift('2026-09-11T12:00:00+05:30', '2026-09-11T21:00:00+05:30')
        rid = self.db.save_report(sid, 'eod', 'EOD preview text')
        rep = self.db.report(rid)
        self.assertEqual(rep.get('is_stale', 0), 0)

        # User adds late work or new work after preview without manual mark_report_stale call
        self.db.add_activity(sid, 'support', 'Late customer support call')

        rep_stale = self.db.report(rid)
        self.assertEqual(rep_stale['is_stale'], 1)

    # 18. Gemini timeout/malformed output/offline behavior.
    async def test_18_gemini_timeout_malformed_output_offline_behavior(self):
        sid = self.db.start_shift('2026-09-11T12:00:00+05:30', '2026-09-11T21:00:00+05:30')
        t = self.db.add_task("Offline test task", shift_id=sid)
        rid = self.db.save_report(sid, 'eod', 'Original deterministic report text')

        up = self.make_update()

        with patch('ai.writer') as mock_w, patch.object(config, 'AI_KEY', 'dummy_key'), patch.object(config, 'AI_MODEL', 'dummy_model'):
            mock_engine = MagicMock()
            mock_engine.draft = AsyncMock(side_effect=TimeoutError("AI offline/timeout"))
            mock_w.return_value = mock_engine

            # Exercise AI report handler with timeout
            await handlers.ai_report(up, self.context, rid)

            # Assert graceful failure message and unchanged original report
            up.effective_message.reply_text.assert_any_call(
                f'AI unavailable. Report #{rid} is unchanged; use /report {rid}.', reply_markup=None
            )
            rep = self.db.report(rid)
            self.assertEqual(rep['text'], 'Original deterministic report text')

            # Verify that a failed AI event was recorded
            with self.db.connect() as conn:
                ev = conn.execute("SELECT * FROM ai_events WHERE operation='report' AND status='failed'").fetchone()
                self.assertIsNotNone(ev)
                self.assertEqual(ev['error_type'], 'TimeoutError')

    # 19. Missed reminders after Windows resumes.
    async def test_19_missed_reminders_after_windows_resumes(self):
        # Shift started at 10:00 and ended at 19:00
        sid = self.db.start_shift('2026-09-11T10:00:00+00:00', '2026-09-11T19:00:00+00:00', '2026-09-11T14:00:00+00:00')
        
        # Simulate machine was asleep and resumes at 21:00 (past shift end)
        scheduler._last_tick_time = datetime(2026, 9, 11, 11, 0, 0, tzinfo=timezone.utc)
        
        with patch('scheduler.datetime') as mock_dt,              patch.object(config, 'REMINDERS_ENABLED', True):
            mock_dt.now.return_value = datetime(2026, 9, 11, 21, 0, 0, tzinfo=timezone.utc)
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)
            await scheduler.tick(self.context)

        # Obsolete tod and pl reminders should be cancelled/delivered rather than spammed
        self.assertTrue(self.db.delivered(sid, 'tod'))
        self.assertTrue(self.db.delivered(sid, 'pl'))

    # 20. Carry-forward preserving IDs and blockers.
    async def test_20_carry_forward_preserving_ids_and_blockers(self):
        sid = self.db.start_shift('2026-09-11T12:00:00+05:30', '2026-09-11T21:00:00+05:30')
        task = self.db.add_task("Complex database migration", shift_id=sid)
        self.db.mark_status(task.id, TaskStatus.BLOCKED, blocked_reason="Waiting for Akhil's credentials")
        self.db.update_task(task.id, 'next_action', 'Ping Akhil tomorrow morning')

        up = self.make_update(f"/carrytask {task.id}")
        await handlers.handle(up, self.context)

        t_carried = self.db.get_task(task.id)
        self.assertEqual(t_carried.id, task.id)
        self.assertEqual(t_carried.status, TaskStatus.BLOCKED)
        self.assertEqual(t_carried.blocked_reason, "Waiting for Akhil's credentials")
        self.assertEqual(t_carried.next_action, "Ping Akhil tomorrow morning")
        self.assertIsNotNone(t_carried.due_date)

    # 21. Owner-only command and callback authorization.
    async def test_21_owner_only_command_and_callback_authorization(self):
        # User ID 99999 is unauthorized
        up = self.make_update("Start my day. Shift is 12 to 9", user=99999)
        await handlers.handle(up, self.context)
        up.effective_message.reply_text.assert_not_awaited()
        self.assertIsNone(self.db.get_active_planning_conversation(99999))

        up_cb = self.make_update(callback_data="plan:confirm:1", user=99999)
        await handlers.handle(up_cb, self.context)
        up_cb.effective_message.reply_text.assert_not_awaited()

    # 22. Delete all tasks immediately with Undo.
    async def test_22_delete_all_tasks_immediately_with_undo(self):
        sid = self.db.start_shift('2026-09-11T12:00:00+05:30', '2026-09-11T21:00:00+05:30')
        t1 = self.db.add_task("Task Alpha", shift_id=sid)
        t2 = self.db.add_task("Task Beta", shift_id=sid)

        up = self.make_update("delete all tasks")
        await handlers.handle(up, self.context)
        reply = up.effective_message.reply_text.call_args[0][0]
        self.assertIn("Deleted 2 tasks", reply)
        self.assertEqual(len(self.db.tasks_for_shift(sid)), 0)

        # Immediate undo
        up_undo = self.make_update("/undo")
        await handlers.handle(up_undo, self.context)
        self.assertEqual(len(self.db.tasks_for_shift(sid)), 2)

    # 23. Scoped task deletion.
    async def test_23_scoped_task_deletion(self):
        sid = self.db.start_shift('2026-09-11T12:00:00+05:30', '2026-09-11T21:00:00+05:30')
        t1 = self.db.add_task("Completed Task", shift_id=sid)
        t2 = self.db.add_task("Pending Task", shift_id=sid)
        self.db.mark_status(t1.id, TaskStatus.COMPLETED)

        up = self.make_update("delete completed tasks")
        await handlers.handle(up, self.context)

        remaining = self.db.tasks_for_shift(sid)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].id, t2.id)

    # 24. Historical reports surviving task deletion.
    async def test_24_historical_reports_surviving_task_deletion(self):
        sid = self.db.start_shift('2026-09-11T12:00:00+05:30', '2026-09-11T21:00:00+05:30')
        t1 = self.db.add_task("Task To Delete", shift_id=sid)
        self.db.save_plan_snapshot(sid, [t1])
        rid = self.db.save_report(sid, 'eod', 'Historical report with Task To Delete')

        up = self.make_update("delete all tasks")
        await handlers.handle(up, self.context)

        # Report survives
        rep = self.db.report(rid)
        self.assertIsNotNone(rep)
        self.assertEqual(rep['text'], 'Historical report with Task To Delete')
        # Snapshot survives with task title intact
        snap = self.db.get_baseline_plan_snapshot(sid)
        self.assertEqual(snap['tasks'][0]['title'], "Task To Delete")

    # 25. Migration failure rollback and repeated initialization.
    def test_25_migration_failure_rollback_and_repeated_initialization(self):
        tmp_db_path = self.root / 'migration_fail_test.sqlite3'
        # Create a schema version 8 database
        conn = sqlite3.connect(tmp_db_path)
        conn.execute('CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        conn.execute('CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)')
        conn.execute('INSERT INTO tasks (id, title, status, created_at) VALUES (1, "Existing Task", "pending", "2026-09-11T00:00:00Z")')
        conn.execute('PRAGMA user_version = 8')
        conn.commit()
        conn.close()

        class FailingDatabase(Database):
            def _seed_v9_defaults(self, connection):
                connection.execute("CREATE TABLE partial_v9_test (id INT)")
                raise sqlite3.OperationalError("Simulated v9 failure")

        with self.assertRaises(RuntimeError) as ctx:
            FailingDatabase(tmp_db_path)
        self.assertIn("Simulated v9 failure", str(ctx.exception))

        # Check rollback: version remains 8, partial table does NOT exist
        conn = sqlite3.connect(tmp_db_path)
        self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 8)
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        self.assertNotIn("partial_v9_test", tables)
        conn.close()

        # Backup created
        backups = list((self.root / 'backups').glob('pre-migration-v8-*.sqlite3'))
        self.assertEqual(len(backups), 1)

        # Repeated clean initialization succeeds
        clean_db = Database(tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 9)
        tables_v9 = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        self.assertIn("plan_snapshots", tables_v9)
        self.assertIn("record_links", tables_v9)
        self.assertIn("planning_conversations", tables_v9)
        conn.close()

    # 26. Retest disambiguation when multiple candidates exist.
    async def test_26_move_retest_multiple_candidates_disambiguation(self):
        t1 = self.db.add_task("Retest login timeout")
        t2 = self.db.add_task("Retest checkout flow")

        up = self.make_update("move the retest to tomorrow")
        await handlers.handle(up, self.context)

        reply = up.effective_message.reply_text.call_args[0][0]
        self.assertIn("Multiple active test tasks found", reply)
        self.assertIn(f"#{t1.id}", reply)
        self.assertIn(f"#{t2.id}", reply)

        # Neither task moved yet
        self.assertIsNone(self.db.get_task(t1.id).due_date)
        self.assertIsNone(self.db.get_task(t2.id).due_date)

        # User selects t1 via button callback
        up_cb = self.make_update(callback_data=f"task:reschedule:{t1.id}:tomorrow")
        await handlers.handle(up_cb, self.context)

        self.assertIsNotNone(self.db.get_task(t1.id).due_date)
        self.assertIsNone(self.db.get_task(t2.id).due_date)

    # 27. Factual correction state conflict rejected.
    async def test_27_factual_correction_state_conflict_rejected(self):
        t = self.db.add_task("Integration test", client="Alpha")
        prop_id = self.db.create_nl_proposal(
            owner_id=123, intent='factual_correction',
            proposal_dict={
                'action': 'factual_correction',
                'task_id': t.id,
                'field': 'client',
                'before': {'client': 'Alpha'},
                'after': {'client': 'Beta'}
            }
        )

        # Task client is changed to Gamma in the background before proposal is confirmed
        self.db.update_task(t.id, 'client', 'Gamma')

        up = self.make_update(callback_data=f"corr:confirm:{prop_id}")
        await handlers.handle(up, self.context)

        reply = up.effective_message.reply_text.call_args[0][0]
        self.assertIn("Stale correction rejected", reply)
        self.assertIn("client changed from 'Alpha' to 'Gamma'", reply)
        self.assertEqual(self.db.get_task(t.id).client, 'Gamma')

        prop = self.db.get_nl_proposal(prop_id)
        self.assertEqual(prop['status'], 'failed')

    # 28. Checkpoint stale button status not overwritten.
    async def test_28_checkpoint_stale_status_not_overwritten(self):
        t = self.db.add_task("Feature development")
        self.db.mark_status(t.id, TaskStatus.IN_PROGRESS)
        self.assertEqual(self.db.get_task(t.id).status, TaskStatus.IN_PROGRESS)

        # Task completes prior to button click
        self.db.mark_status(t.id, TaskStatus.COMPLETED)

        # Old button expecting 'in_progress' is clicked
        up = self.make_update(callback_data=f"checkpoint:update:{t.id}:in_progress:blocked")
        await handlers.handle(up, self.context)

        reply = up.effective_message.reply_text.call_args[0][0]
        self.assertIn("Checkpoint action ignored", reply)
        self.assertIn("status is now [completed], not [in_progress]", reply)
        self.assertEqual(self.db.get_task(t.id).status, TaskStatus.COMPLETED)

    # 29. Late work category and shift boundary matching.
    async def test_29_late_work_category_and_shift_matching(self):
        sid1 = self.db.start_shift('2026-09-10T10:00:00+05:30', '2026-09-10T19:00:00+05:30')
        self.db.close_shift(sid1)

        sid2 = self.db.start_shift('2026-09-11T09:00:00+05:30', '2026-09-11T18:00:00+05:30')

        up = self.make_update("Yesterday at 11 am I tested the auth token flow")
        await handlers.handle(up, self.context)

        reply = up.effective_message.reply_text.call_args[0][0]
        self.assertIn(f"Shift #{sid1}", reply)
        self.assertIn("category=testing", reply)

        acts1 = self.db.activities(sid1)
        self.assertTrue(any(a['category'] == 'testing' and 'auth token flow' in a['detail'] for a in acts1))

        acts2 = self.db.activities(sid2)
        self.assertFalse(any('auth token flow' in a['detail'] for a in acts2))

    # 30. Atomic planning confirmation associates existing tasks.
    async def test_30_atomic_planning_transaction_and_existing_task_shift_association(self):
        t_old = self.db.add_task("Legacy carried task")
        self.assertIsNone(t_old.planned_shift_id)

        up = self.make_update("start shift 12 to 9, lunch at 4 | Legacy carried task, Build new API")
        await handlers.handle(up, self.context)

        conv = self.db.get_active_planning_conversation(123)
        self.assertIsNotNone(conv)

        up_confirm = self.make_update(callback_data=f"plan:confirm:{conv['id']}")
        await handlers.handle(up_confirm, self.context)

        shift = self.db.active_shift()
        self.assertIsNotNone(shift)

        # Verify existing task has planned_shift_id updated to active shift
        t_refreshed = self.db.get_task(t_old.id)
        self.assertEqual(t_refreshed.planned_shift_id, shift['id'])

        shift_tasks = self.db.tasks_for_shift(shift['id'])
        shift_task_titles = [t.title for t in shift_tasks]
        self.assertIn("Legacy carried task", shift_task_titles)
        self.assertIn("Build new API", shift_task_titles)

        # Verify baseline snapshot contains both
        snap = self.db.get_baseline_plan_snapshot(shift['id'])
        snap_titles = [t['title'] for t in snap['tasks']]
        self.assertIn("Legacy carried task", snap_titles)
        self.assertIn("Build new API", snap_titles)


if __name__ == '__main__':
    unittest.main()
