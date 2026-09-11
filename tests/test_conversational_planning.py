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
from contextlib import closing
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
        now = datetime.now(ZoneInfo(config.TIMEZONE))
        yest = now - timedelta(days=1)
        sid = self.db.start_shift(yest.strftime('%Y-%m-%dT20:00:00+05:30'), now.strftime('%Y-%m-%dT05:00:00+05:30'))
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
        with closing(sqlite3.connect(tmp_db_path)) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 8)
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            self.assertNotIn("partial_v9_test", tables)

        # Backup created
        backups = list((self.root / 'backups').glob('pre-migration-v8-*.sqlite3'))
        self.assertEqual(len(backups), 1)

        # Repeated clean initialization succeeds
        clean_db = Database(tmp_db_path)
        with closing(sqlite3.connect(tmp_db_path)) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
            tables_v10 = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            self.assertIn("plan_snapshots", tables_v10)
            self.assertIn("record_links", tables_v10)
            self.assertIn("planning_conversations", tables_v10)

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
        now = datetime.now(ZoneInfo(config.TIMEZONE))
        yest = now - timedelta(days=1)
        sid1 = self.db.start_shift(yest.strftime('%Y-%m-%dT10:00:00+05:30'), yest.strftime('%Y-%m-%dT19:00:00+05:30'))
        self.db.close_shift(sid1)

        sid2 = self.db.start_shift(now.strftime('%Y-%m-%dT09:00:00+05:30'), now.strftime('%Y-%m-%dT18:00:00+05:30'))

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

    # 31. Dispatcher and CALLBACK_PATTERN regex coverage for all button formats.
    async def test_31_callback_pattern_matches_all_new_button_formats(self):
        from bot import CALLBACK_PATTERN
        test_callbacks = [
            "checkpoint:update:12:in_progress:completed",
            "checkpoint:update:12:pending:blocked",
            "task:reschedule:15:tomorrow",
            "task:reschedule:15:today",
            "corr:pick_task:prop_abcd1234:42",
            "corr:pick_client:prop_abcd1234:NVIZION",
            "corr:confirm:prop_abcd1234",
            "corr:cancel:prop_abcd1234",
            "late:shift:prop_abcd1234:5",
            "late:cancel:prop_abcd1234",
        ]
        for cb in test_callbacks:
            with self.subTest(callback=cb):
                self.assertIsNotNone(re.match(CALLBACK_PATTERN, cb), f"CALLBACK_PATTERN failed to match {cb}")

    # 32. Correction selection updates proposal_json without database error and clicks through.
    async def test_32_correction_selection_writes_proposal_json_and_executes(self):
        sid = self.db.start_shift('2026-09-11T09:00:00+05:30', '2026-09-11T18:00:00+05:30')
        t1 = self.db.add_task("Payment gateway integration", shift_id=sid)
        t2 = self.db.add_task("Payment gateway stress testing", shift_id=sid)

        # Ambiguous correction referencing failure without active context task
        up1 = self.make_update("Actually, it failed on staging", uid=1001)
        await handlers.handle(up1, self.context)

        reply1 = up1.effective_message.reply_text.call_args[0][0]
        self.assertIn("Which task", reply1)
        markup1 = up1.effective_message.reply_text.call_args[1].get('reply_markup')
        self.assertIsNotNone(markup1)

        # Find the button for task t1
        t1_buttons = [btn for row in markup1.inline_keyboard for btn in row if f":{t1.id}" in btn.callback_data]
        self.assertTrue(len(t1_buttons) > 0)
        pick_cb = t1_buttons[0].callback_data
        from bot import CALLBACK_PATTERN
        self.assertIsNotNone(re.match(CALLBACK_PATTERN, pick_cb))

        # Click the task pick button through real handler dispatch
        up_pick = self.make_update(callback_data=pick_cb, uid=1002)
        await handlers.handle(up_pick, self.context)

        # Verify proposal updated successfully in database (no schema column error)
        prop_id = pick_cb.split(':')[2]
        prop = self.db.get_nl_proposal(prop_id)
        self.assertIsNotNone(prop)
        self.assertEqual(prop['proposal']['task_id'], t1.id)

        # Verify confirmation prompt was sent with Confirm Correction button
        reply2 = up_pick.effective_message.reply_text.call_args[0][0]
        self.assertIn("Confirm to apply?", reply2)
        markup2 = up_pick.effective_message.reply_text.call_args[1].get('reply_markup')
        confirm_btn = [btn for row in markup2.inline_keyboard for btn in row if btn.callback_data.startswith('corr:confirm:')][0]
        self.assertIsNotNone(re.match(CALLBACK_PATTERN, confirm_btn.callback_data))

        # Click confirm button through dispatcher
        up_confirm = self.make_update(callback_data=confirm_btn.callback_data, uid=1003)
        await handlers.handle(up_confirm, self.context)

        # Verify task is now blocked
        t1_updated = self.db.get_task(t1.id)
        self.assertEqual(t1_updated.status, TaskStatus.BLOCKED)
        self.assertIn("staging", t1_updated.blocked_reason)

    # 33. Wording-only revision preserves immutable factual snapshot and excludes subsequent work.
    async def test_33_wording_revision_uses_immutable_factual_snapshot(self):
        sid = self.db.start_shift('2026-09-11T09:00:00+05:30', '2026-09-11T18:00:00+05:30')
        t = self.db.add_task("Initial Feature A", shift_id=sid)
        self.db.add_activity(sid, 'task', 'Initial Feature A completed', outcome='completed', task_id=t.id)

        up_eod = self.make_update('/eod', uid=1001)
        await handlers.handle(up_eod, self.context)

        reps = self.db.list_reports(sid)
        self.assertEqual(len(reps), 1)
        orig_rep = reps[0]
        orig_hash = orig_rep['facts_hash']
        orig_snap_json = orig_rep['facts_snapshot_json']
        self.assertIsNotNone(orig_snap_json)
        self.assertIn("Initial Feature A", orig_rep['text'])

        # Now add new work after EOD generation
        self.db.add_activity(sid, 'support', 'Post-EOD critical hotfix for ACME', client='ACME', outcome='resolved')

        # Request wording revision: "make the eod shorter"
        up_shorter = self.make_update("make the eod shorter", uid=1002)
        await handlers.handle(up_shorter, self.context)

        reps_after = self.db.list_reports(sid)
        self.assertEqual(len(reps_after), 2)
        rev_rep = reps_after[1]
        self.assertEqual(rev_rep['revision'], 2)
        self.assertEqual(rev_rep['style'], 'short')

        # Crucial check: the wording revision must NOT leak the post-EOD work!
        self.assertNotIn("Post-EOD critical hotfix", rev_rep['text'])
        # Fingerprint must match the immutable snapshot
        self.assertEqual(rev_rep['facts_hash'], orig_hash)
        self.assertEqual(rev_rep['facts_snapshot_json'], orig_snap_json)

    # 34. Staleness check consistency across cases, test sessions, and client changes.
    async def test_34_staleness_consistency_across_cases_sessions_and_client_changes(self):
        sid = self.db.start_shift('2026-09-11T09:00:00+05:30', '2026-09-11T18:00:00+05:30')
        t = self.db.add_task("Core task", client="InitialClient", shift_id=sid)
        self.db.create_case("Case 101", client="InitialClient", status="new", shift_id=sid)
        self.db.add_testing(sid, "Login scenario", result="pass", environment="staging")

        # Generate report
        up = self.make_update('/eod', uid=1001)
        await handlers.handle(up, self.context)

        reps = self.db.list_reports(sid)
        self.assertEqual(len(reps), 1)
        rid = reps[0]['id']

        # Retrieval check: must NOT be stale immediately after creation
        rep_fetched = self.db.report(rid)
        self.assertEqual(rep_fetched['is_stale'], 0)

        # Mutate client field on task
        self.db.update_task(t.id, 'client', 'UpdatedClient')

        # Retrieval check: must detect mutation as stale
        rep_stale = self.db.report(rid)
        self.assertEqual(rep_stale['is_stale'], 1)

    # 35. Unmatched and overlapping late work requests shift selection, clicking button assigns to selected shift.
    async def test_35_unmatched_and_overlapping_late_work_prompts_and_assigns(self):
        tz = ZoneInfo(config.TIMEZONE)
        now_dt = datetime.now(tz)
        yest_date = (now_dt - timedelta(days=1)).date()
        today_date = now_dt.date()

        # Shift 1: 09:00 to 17:00 yesterday
        sid1 = self.db.start_shift(f"{yest_date.isoformat()}T09:00:00+05:30", f"{yest_date.isoformat()}T17:00:00+05:30")
        self.db.close_shift(sid1)
        # Shift 2: 16:30 to 23:00 yesterday (overlaps with Shift 1 from 16:30 to 17:00)
        sid2 = self.db.start_shift(f"{yest_date.isoformat()}T16:30:00+05:30", f"{yest_date.isoformat()}T23:00:00+05:30")
        self.db.close_shift(sid2)

        # Active shift: today
        sid3 = self.db.start_shift(f"{today_date.isoformat()}T09:00:00+05:30", f"{today_date.isoformat()}T18:00:00+05:30")

        # 1. Overlapping match: 16:45 falls into both Shift 1 and Shift 2
        up_overlap = self.make_update("Yesterday at 4:45 pm I resolved the memory leak", uid=1001)
        await handlers.handle(up_overlap, self.context)

        reply1 = up_overlap.effective_message.reply_text.call_args[0][0]
        self.assertIn("Multiple shifts match", reply1)
        markup1 = up_overlap.effective_message.reply_text.call_args[1].get('reply_markup')
        self.assertIsNotNone(markup1)

        # Find button for sid2
        from bot import CALLBACK_PATTERN
        sid2_btn = [btn for row in markup1.inline_keyboard for btn in row if f":{sid2}" in btn.callback_data][0]
        self.assertIsNotNone(re.match(CALLBACK_PATTERN, sid2_btn.callback_data))

        # Click sid2 button
        up_click = self.make_update(callback_data=sid2_btn.callback_data, uid=1002)
        await handlers.handle(up_click, self.context)

        acts2 = self.db.activities(sid2)
        self.assertTrue(any('memory leak' in a['detail'] for a in acts2))
        self.assertFalse(any('memory leak' in a['detail'] for a in self.db.activities(sid1)))
        self.assertFalse(any('memory leak' in a['detail'] for a in self.db.activities(sid3)))

        # 2. Unmatched timestamp: 3:00 am yesterday (no shift ran at 3:00 am yesterday)
        up_unmatched = self.make_update("Yesterday at 3:00 am I investigated a server reboot", uid=1003)
        await handlers.handle(up_unmatched, self.context)

        reply2 = up_unmatched.effective_message.reply_text.call_args[0][0]
        self.assertIn("No shift directly matched", reply2)
        markup2 = up_unmatched.effective_message.reply_text.call_args[1].get('reply_markup')
        self.assertIsNotNone(markup2)

        # Must NOT have defaulted to active shift sid3
        self.assertFalse(any('server reboot' in a['detail'] for a in self.db.activities(sid3)))

    # 36. Historical /eod revision works on closed shift without active shift.
    async def test_36_historical_eod_revision_without_active_shift(self):
        sid = self.db.start_shift('2026-09-10T09:00:00+05:30', '2026-09-10T18:00:00+05:30')
        t = self.db.add_task("Original Feature", shift_id=sid)
        self.db.add_activity(sid, 'task', 'Original Feature done', outcome='completed', task_id=t.id)
        # Generate initial EOD
        up_eod = self.make_update('/eod', uid=1001)
        await handlers.handle(up_eod, self.context)
        reps = self.db.list_reports(sid)
        self.assertEqual(len(reps), 1)
        orig_rid = reps[0]['id']
        self.db.finalize(orig_rid)
        self.db.close_shift(sid)

        # Confirm no active shift
        self.assertIsNone(self.db.active_shift())

        # Add late work to closed shift
        self.db.add_activity(sid, 'testing', 'Late sanity test on prod', outcome='verified')

        # Run /eod revision without an active shift
        up_rev = self.make_update(f'/eod revision {sid}', uid=1002)
        await handlers.handle(up_rev, self.context)

        reply = up_rev.effective_message.reply_text.call_args[0][0]
        self.assertIn("Revised EOD", reply)
        self.assertIn(f"Shift #{sid}", reply)
        self.assertIn("Late sanity test on prod", reply)

        reps_after = self.db.list_reports(sid)
        self.assertEqual(len(reps_after), 2)
        rev_report = reps_after[1]
        self.assertEqual(rev_report['source_report_id'], orig_rid)
        self.assertEqual(rev_report['revision'], 2)

    # 37. Atomic planning confirmations increment snapshot versions.
    async def test_37_atomic_planning_increments_snapshot_versions(self):
        sid = self.db.start_shift('2026-09-11T09:00:00+05:30', '2026-09-11T18:00:00+05:30')
        # Version 1 confirmation
        conv_id1 = self.db.save_planning_conversation(
            123, 'daily_planning', 'awaiting_confirmation',
            {'shift_range': {'start': '09:00', 'end': '18:00'}, 'tasks': [{'title': 'Task A', 'is_new': True}]},
            shift_id=sid
        )
        self.db.confirm_daily_plan_atomic(123, conv_id1, '2026-09-11T09:00:00+05:30', '2026-09-11T18:00:00+05:30', None, [{'title': 'Task A', 'is_new': True}])

        with self.db.connect() as conn:
            snaps = conn.execute('SELECT version FROM plan_snapshots WHERE shift_id=? ORDER BY version ASC', (sid,)).fetchall()
            versions = [r['version'] for r in snaps]
            self.assertEqual(versions, [1])

        # Version 2 confirmation
        conv_id2 = self.db.save_planning_conversation(
            123, 'daily_planning', 'awaiting_confirmation',
            {'shift_range': {'start': '09:00', 'end': '18:00'}, 'tasks': [{'title': 'Task B', 'is_new': True}]},
            shift_id=sid
        )
        self.db.confirm_daily_plan_atomic(123, conv_id2, '2026-09-11T09:00:00+05:30', '2026-09-11T18:00:00+05:30', None, [{'title': 'Task B', 'is_new': True}])

        with self.db.connect() as conn:
            snaps = conn.execute('SELECT version FROM plan_snapshots WHERE shift_id=? ORDER BY version ASC', (sid,)).fetchall()
            versions = [r['version'] for r in snaps]
            self.assertEqual(versions, [1, 2])

        # Version 3 confirmation
        conv_id3 = self.db.save_planning_conversation(
            123, 'daily_planning', 'awaiting_confirmation',
            {'shift_range': {'start': '09:00', 'end': '18:00'}, 'tasks': [{'title': 'Task C', 'is_new': True}]},
            shift_id=sid
        )
        self.db.confirm_daily_plan_atomic(123, conv_id3, '2026-09-11T09:00:00+05:30', '2026-09-11T18:00:00+05:30', None, [{'title': 'Task C', 'is_new': True}])

        with self.db.connect() as conn:
            snaps = conn.execute('SELECT version FROM plan_snapshots WHERE shift_id=? ORDER BY version ASC', (sid,)).fetchall()
            versions = [r['version'] for r in snaps]
            self.assertEqual(versions, [1, 2, 3])

    # 38. Client selection with colon in name via candidate index avoids callback unpack errors.
    async def test_38_client_selection_with_colon_in_name_candidate_index(self):
        sid = self.db.start_shift('2026-09-11T09:00:00+05:30', '2026-09-11T18:00:00+05:30')
        t1 = self.db.add_task('Attend morning standup', client='Acme:Corp:Alpha', shift_id=sid)
        self.db.update_conversation_context('owner', active_task_id=t1.id)
        # Create at least two other clients in system so len(known_clients) > 1 and prompts picker
        self.db.add_task('Other work 1', client='Omega:Beta:Services', shift_id=sid)
        self.db.add_task('Other work 2', client='Gamma:Delta:Tech', shift_id=sid)

        # Trigger ambiguous correction: "That was for the other client"
        up = self.make_update("That was for the other client", uid=1001)
        await handlers.handle(up, self.context)
        call_args = up.effective_message.reply_text.call_args
        reply_text = call_args[0][0]
        keyboard = call_args[1].get('reply_markup') if 'reply_markup' in call_args[1] else (call_args[0][1] if len(call_args[0]) > 1 else None)
        self.assertIn("Which client was Task", reply_text)
        self.assertIsNotNone(keyboard)

        # Verify buttons use index instead of raw name
        first_btn = keyboard.inline_keyboard[0][0]
        self.assertTrue(re.match(r'^corr:pick_client:prop_[a-f0-9]+:\d+$', first_btn.callback_data))

        # Click the candidate button
        up_click = self.make_update(uid=1002, callback_data=first_btn.callback_data)
        await handlers.handle(up_click, self.context)

        # Verify proposal was updated with client name that has colons without error
        prop_id = first_btn.callback_data.split(':')[2]
        prop = self.db.get_nl_proposal(prop_id)
        self.assertIn('client', prop['proposal']['after'])
        self.assertIn(':', prop['proposal']['after']['client'])

    # 39. Staleness hashing includes all report-visible facts (case participation, test session build/defects/retest, activities).
    async def test_39_complete_staleness_hashing_covers_all_renderer_facts(self):
        sid = self.db.start_shift('2026-09-11T09:00:00+05:30', '2026-09-11T18:00:00+05:30')
        with self.db.connect() as conn:
            client_id = self.db._client_id(conn, 'Acme')
            stamp = '2026-09-11T09:30:00'
            cid = conn.execute('''INSERT INTO work_cases
                (client_id, title, product, platform, channel, ticket, priority, status, participation,
                 next_action, waiting_on, client_updated, review_state, source, created_at, updated_at)
                VALUES (?, ?, 'AuthService', 'web', 'slack', 'PAY-101', 1, 'new', 'owned',
                        'Check logs', NULL, 0, 'approved', 'test', ?, ?)''',
                (client_id, 'Payment Gateway Timeout', stamp, stamp)).lastrowid
            conn.execute('''INSERT INTO case_events
                (case_id, shift_id, event_type, detail, actor_role, outcome, occurred_at, created_at)
                VALUES (?, ?, 'created', 'Payment Gateway Timeout', 'owner', 'new', ?, ?)''',
                (cid, sid, stamp, stamp))
        sess_id = self.db.add_test_session(case_id=cid, shift_id=sid, scenario='Checkout flow', build='v1.0.0', defects=None, retest_required=False)
        aid = self.db.add_support(sid, client='Acme', detail='Investigated auth error', product='AuthService', ticket='SUP-1')
        t_obj = self.db.add_task('Fix retry logic', client='Acme', ticket='DEV-55', shift_id=sid)
        tid = t_obj.id

        base_hash = self.db.compute_live_facts_hash(sid)

        # 1. Changing case participation must alter hash
        self.db.update_case(cid, 'participation', 'assisted', sid)
        hash_case_part = self.db.compute_live_facts_hash(sid)
        self.assertNotEqual(base_hash, hash_case_part)
        self.db.update_case(cid, 'participation', 'owned', sid)

        # 2. Changing test session build must alter hash
        with self.db.connect() as conn:
            conn.execute("UPDATE test_sessions SET build='v1.0.1' WHERE id=?", (sess_id,))
        hash_sess_build = self.db.compute_live_facts_hash(sid)
        self.assertNotEqual(base_hash, hash_sess_build)
        with self.db.connect() as conn:
            conn.execute("UPDATE test_sessions SET build='v1.0.0' WHERE id=?", (sess_id,))

        # 3. Changing test session defects must alter hash
        with self.db.connect() as conn:
            conn.execute("UPDATE test_sessions SET defects='BUG-404' WHERE id=?", (sess_id,))
        hash_sess_def = self.db.compute_live_facts_hash(sid)
        self.assertNotEqual(base_hash, hash_sess_def)
        with self.db.connect() as conn:
            conn.execute("UPDATE test_sessions SET defects=NULL WHERE id=?", (sess_id,))

        # 4. Changing test session retest_required must alter hash
        with self.db.connect() as conn:
            conn.execute("UPDATE test_sessions SET retest_required=1 WHERE id=?", (sess_id,))
        hash_sess_retest = self.db.compute_live_facts_hash(sid)
        self.assertNotEqual(base_hash, hash_sess_retest)
        with self.db.connect() as conn:
            conn.execute("UPDATE test_sessions SET retest_required=0 WHERE id=?", (sess_id,))

        # 5. Changing support product must alter hash
        with self.db.connect() as conn:
            conn.execute("UPDATE support_interactions SET product='BillingService' WHERE activity_id=?", (aid,))
        hash_supp_prod = self.db.compute_live_facts_hash(sid)
        self.assertNotEqual(base_hash, hash_supp_prod)
        with self.db.connect() as conn:
            conn.execute("UPDATE support_interactions SET product='AuthService' WHERE activity_id=?", (aid,))

        # 6. Changing task ticket must alter hash
        with self.db.connect() as conn:
            conn.execute("UPDATE tasks SET ticket='DEV-99' WHERE id=?", (tid,))
        hash_task_ticket = self.db.compute_live_facts_hash(sid)
        self.assertNotEqual(base_hash, hash_task_ticket)

    # 40. Upgrading an actual version-9 database triggers pre-migration backup and updates to v10.
    async def test_40_schema_v9_to_v10_upgrade_creates_verified_backup(self):
        migration_dir = self.root / 'migration_test'
        migration_dir.mkdir()
        v9_db_path = migration_dir / 'work.sqlite3'

        # Initialize a complete valid database, then revert to v9 schema
        base_db = Database(v9_db_path)
        with closing(sqlite3.connect(str(v9_db_path))) as conn:
            conn.execute('PRAGMA user_version = 9')
            conn.execute('ALTER TABLE reports DROP COLUMN facts_snapshot_json')
            conn.execute("INSERT INTO tasks (title, status, created_at) VALUES ('Legacy task', 'completed', '2026-09-10T09:00:00')")
            conn.commit()

        # Initialize Database on this v9 file
        upgraded_db = Database(v9_db_path)

        # 1. Verify PRAGMA user_version is now 10
        with upgraded_db.connect() as connection:
            ver = connection.execute('PRAGMA user_version').fetchone()[0]
            self.assertEqual(ver, 10)
            # Verify facts_snapshot_json column exists
            cols = {row['name'] for row in connection.execute('PRAGMA table_info(reports)')}
            self.assertIn('facts_snapshot_json', cols)
            # Verify data preserved
            t = connection.execute("SELECT title FROM tasks WHERE title='Legacy task'").fetchone()
            self.assertIsNotNone(t)
            self.assertEqual(t['title'], 'Legacy task')

        # 2. Verify pre-migration backup file was created
        backup_dir = migration_dir / 'backups'
        self.assertTrue(backup_dir.is_dir())
        backups = list(backup_dir.glob('pre-migration-v9-*.sqlite3'))
        self.assertGreaterEqual(len(backups), 1)

        # 3. Verify backup integrity
        backup_file = backups[0]
        with closing(sqlite3.connect(str(backup_file))) as bconn:
            b_ver = bconn.execute('PRAGMA user_version').fetchone()[0]
            self.assertEqual(b_ver, 9)
            b_integrity = bconn.execute('PRAGMA integrity_check').fetchone()[0]
            self.assertEqual(b_integrity, 'ok')
            b_task = bconn.execute("SELECT title FROM tasks WHERE title='Legacy task'").fetchone()
            self.assertIsNotNone(b_task)
            self.assertEqual(b_task[0], 'Legacy task')
            # Verify backup did NOT have facts_snapshot_json
            b_cols = {row[1] for row in bconn.execute('PRAGMA table_info(reports)')}
            self.assertNotIn('facts_snapshot_json', b_cols)

    # 41. Complete shared report lifecycle through real Application dispatch with gate auth & deduplication.
    async def test_41_end_to_end_report_lifecycle_application_dispatch(self):
        from telegram import Update, User, Chat, Message, CallbackQuery
        from telegram.ext import Application, TypeHandler, CallbackQueryHandler, MessageHandler, filters
        from telegram.ext._extbot import ExtBot
        import bot

        with patch.object(ExtBot, 'get_me', new_callable=AsyncMock) as mock_get_me, \
             patch.object(ExtBot, 'answer_callback_query', new_callable=AsyncMock):
            mock_get_me.return_value = User(100, 'TestBot', is_bot=True, username='test_bot')
            app = Application.builder().token('123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11').build()
            app.bot_data['db'] = self.db
            app.add_handler(TypeHandler(Update, bot.gate), group=-1)
            app.add_handler(CallbackQueryHandler(handlers.handle, pattern=bot.CALLBACK_PATTERN))
            app.add_handler(MessageHandler(filters.TEXT, handlers.handle))
            await app.initialize()

            # Helper to create real telegram Updates with bot attached
            user = User(123, 'Owner', is_bot=False)
            chat = Chat(123, 'private')

            async def dispatch_text(text: str, update_id: int):
                with patch.object(handlers, 'reply', new_callable=AsyncMock) as mock_reply:
                    msg = Message(update_id, None, chat, from_user=user, text=text)
                    msg.set_bot(app.bot)
                    up = Update(update_id, message=msg)
                    up.set_bot(app.bot)
                    await app.process_update(up)
                    replies = [call[0][1] for call in mock_reply.call_args_list if len(call[0]) > 1]
                    keyboards = [call[0][2] if len(call[0]) > 2 else call[1].get('reply_markup') or call[1].get('keyboard') for call in mock_reply.call_args_list]
                    return replies, keyboards

            async def dispatch_cb(callback_data: str, update_id: int):
                with patch.object(handlers, 'reply', new_callable=AsyncMock) as mock_reply:
                    msg = Message(update_id, None, chat, from_user=user, text='...')
                    msg.set_bot(app.bot)
                    cq = CallbackQuery(f'cq_{update_id}', user, chat_instance='1', message=msg, data=callback_data)
                    cq.set_bot(app.bot)
                    up = Update(update_id, callback_query=cq)
                    up.set_bot(app.bot)
                    await app.process_update(up)
                    replies = [call[0][1] for call in mock_reply.call_args_list if len(call[0]) > 1]
                    keyboards = [call[0][2] if len(call[0]) > 2 else call[1].get('reply_markup') or call[1].get('keyboard') for call in mock_reply.call_args_list]
                    return replies, keyboards

            # --- Gate Verification ---
            # 1. Unauthorized user is blocked by bot.gate
            hacker = User(999, 'Attacker', is_bot=False)
            hacker_chat = Chat(999, 'private')
            with patch.object(handlers, 'reply', new_callable=AsyncMock) as mock_reply:
                hacker_msg = Message(5001, None, hacker_chat, from_user=hacker, text='/health')
                hacker_msg.set_bot(app.bot)
                up_bad = Update(5001, message=hacker_msg)
                up_bad.set_bot(app.bot)
                await app.process_update(up_bad)
                self.assertFalse(mock_reply.called)
                self.assertTrue(self.db.claim_update(5001))  # Never claimed because gate stopped before claim

            # 2. Duplicate update is blocked by bot.gate
            replies, _ = await dispatch_text('/health', 5002)
            self.assertTrue(len(replies) > 0)
            with patch.object(handlers, 'reply', new_callable=AsyncMock) as mock_reply:
                # Same update_id 5002
                msg_dup = Message(5002, None, chat, from_user=user, text='/health')
                msg_dup.set_bot(app.bot)
                up_dup = Update(5002, message=msg_dup)
                up_dup.set_bot(app.bot)
                await app.process_update(up_dup)
                self.assertFalse(mock_reply.called)

            # --- Report Lifecycle ---
            # Step 1: Start shift and plan work
            sid = self.db.start_shift('2026-09-11T09:00:00+05:30', '2026-09-11T18:00:00+05:30')
            tid1 = self.db.add_task('Complete client report', client='Acme', shift_id=sid)
            self.db.add_activity(sid, 'support', 'Helped Acme with API integration', client='Acme', outcome='resolved')

            # Step 2: Generate initial EOD report via /eod
            replies, keyboards = await dispatch_text('/eod', 6001)
            self.assertTrue(any("Draft #" in r for r in replies))
            reps = self.db.list_reports(sid)
            self.assertEqual(len(reps), 1)
            rep1 = reps[0]
            self.assertFalse(rep1['finalized'])
            self.assertIsNotNone(rep1['facts_snapshot_json'])
            self.assertIsNotNone(rep1['facts_hash'])
            orig_hash = rep1['facts_hash']

            # Step 3: Dispatch AI rewrite via callback ai:{rep1['id']}
            with patch('ai.writer') as mock_writer_cls:
                mock_engine = MagicMock()
                mock_engine.draft = AsyncMock(return_value="AI Rewritten: Handled Acme support resolved.")
                mock_engine.last_model = 'gemini-1.5-flash'
                mock_writer_cls.return_value = mock_engine
                with patch.object(config, 'AI_KEY', 'mock_key'), patch.object(config, 'AI_MODEL', 'gemini-1.5-flash'):
                    replies, keyboards = await dispatch_cb(f"ai:{rep1['id']}", 6002)

            reps = self.db.list_reports(sid)
            self.assertEqual(len(reps), 2)
            ai_rep = reps[1]
            self.assertEqual(ai_rep['source_report_id'], rep1['id'])
            # CRITICAL FINDING 1 VERIFICATION: AI draft preserves facts_snapshot_json and facts_hash
            self.assertEqual(ai_rep['facts_hash'], orig_hash)
            self.assertIsNotNone(ai_rep['facts_snapshot_json'])

            # Step 4: User logs NEW late work (activity #2)
            self.db.add_activity(sid, 'testing', 'Tested late production deployment', outcome='verified')

            # Step 5: User says "make the EOD shorter"
            replies, keyboards = await dispatch_text("make the EOD shorter", 6003)
            self.assertTrue(any("Updated EOD (Revision #" in r for r in replies))
            reps = self.db.list_reports(sid)
            self.assertEqual(len(reps), 3)
            rev_rep = reps[2]

            # CRITICAL FINDINGS 1 & 3 VERIFICATION:
            # 1. Wording revision only has activity #1 and does NOT include late activity #2
            self.assertNotIn("Tested late production deployment", rev_rep['text'])
            self.assertIn("Helped Acme with API integration", rev_rep['text'])
            self.assertEqual(rev_rep['facts_hash'], orig_hash)

            # 3. Wording revision has validation saved and Finalize button provided
            with self.db.connect() as conn:
                v_row = conn.execute('SELECT is_valid FROM report_validations WHERE report_id=?', (rev_rep['id'],)).fetchone()
                self.assertIsNotNone(v_row)
                self.assertEqual(v_row['is_valid'], 1)

            # Check that Finalize button was returned in reply
            has_finalize_btn = False
            for kb in keyboards:
                if kb and hasattr(kb, 'inline_keyboard'):
                    for row in kb.inline_keyboard:
                        for btn in row:
                            if btn.callback_data == f"final:{rev_rep['id']}":
                                has_finalize_btn = True
            self.assertTrue(has_finalize_btn, "Wording revision must provide a Finalize button")

            # Step 6: User clicks Finalize button
            replies, _ = await dispatch_cb(f"final:{rev_rep['id']}", 6004)
            self.assertTrue(any(f"Report #{rev_rep['id']} finalized" in r for r in replies))
            self.assertTrue(self.db.report(rev_rep['id'])['finalized'])

            # Step 7: Close the shift
            self.db.close_shift(sid)
            self.assertIsNone(self.db.active_shift())

            # Step 8: Historical /eod revision for the closed shift reflects new late work
            replies, _ = await dispatch_text(f"/eod revision {sid}", 6005)
            self.assertTrue(any("Revised EOD" in r for r in replies))
            reps = self.db.list_reports(sid)
            self.assertEqual(len(reps), 4)
            hist_rep = reps[3]
            self.assertFalse(hist_rep['finalized'])
            # Historical revision includes the late activity #2
            self.assertIn("Tested late production deployment", hist_rep['text'])
            # Hash reflects the updated facts
            self.assertNotEqual(hist_rep['facts_hash'], orig_hash)


if __name__ == '__main__':
    unittest.main()
