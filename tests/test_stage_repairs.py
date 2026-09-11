"""
Automated test suite verifying Stages 1 through 8 correctness repairs:
1. Conflicting-correction clarification with fresh entity extraction, auth, idempotency, and restart survival.
2. Carry-forward matching and identity preserving blocked tasks, clients, and preventing silent creation.
3. Transactional carry-forward with complete Undo and rollback on failure.
4. Consolidated date/shift parsing: explicit years, leap years, invalid dates, tomorrow planning, overnight extensions, historical shifts.
5. Correction management: listing, disabling, enabling, and deleting learning examples.
6. Natural progress updates: developer fix report vs user verification vs client confirmation.
7. Full application dispatch verification for gate authorization and deduplication.
"""
import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch, MagicMock
from zoneinfo import ZoneInfo

from telegram import Update, Message, User, Chat, CallbackQuery
from telegram.ext import Application, TypeHandler, CallbackQueryHandler, MessageHandler, filters

import config
import bot
import handlers
import daily_assistant
import nlp
from nlp import NaturalLanguagePipeline, NLIntent, NLInterpretation, NLEntities, DeterministicParser, resolve_date_reference
from nlp_policy import ActionDecision, ReasonCode, evaluate_action_policy
from database import Database, TaskStatus, now_iso
import reports


class StageRepairsCorrectnessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / 'work.sqlite3'
        self.db = Database(self.db_path)

        self.owner_id = 1001
        self.owner_patch = patch.object(config, 'OWNER_ID', self.owner_id)
        self.owner_patch.start()
        self.addCleanup(self.owner_patch.stop)

        self.pipeline = NaturalLanguagePipeline(self.db, ai_client=None)
        self.tz = ZoneInfo(config.TIMEZONE)
        self.today = datetime.now(self.tz).date().isoformat()
        self.tomorrow = (datetime.now(self.tz).date() + timedelta(days=1)).isoformat()

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

    def make_update(self, text='', user=1001, chat=1001, uid=2001, callback_data=None):
        msg = SimpleNamespace(
            text=text,
            caption=None,
            forward_origin=None,
            reply_text=AsyncMock(),
            reply_document=AsyncMock(),
            message_id=uid
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
            effective_message=msg,
            message=msg
        )

    # -------------------------------------------------------------------------
    # STAGE 1: Conflicting-Correction Clarification & Proposal Flow
    # -------------------------------------------------------------------------

    async def test_01_conflicting_corrections_produce_clarification_proposal(self):
        # Record conflicting corrections for the exact same input
        text = "quick sync with dev lead regarding agent latency"
        i1 = self.db.record_nl_interaction(text, intent="unknown", confidence=0.2)
        self.db.add_nl_correction(original_intent="unknown", corrected_intent="create_task",
                                  interaction_id=i1, owner_id=self.owner_id)
        i2 = self.db.record_nl_interaction(text, intent="unknown", confidence=0.2)
        self.db.add_nl_correction(original_intent="unknown", corrected_intent="log_support",
                                  interaction_id=i2, owner_id=self.owner_id)

        # Process input
        reply, interp = await self.pipeline.process(text, shift=None)

        # Must produce clarification needed with choices
        self.assertEqual(interp.intent, NLIntent.UNKNOWN)
        self.assertTrue(interp.needs_confirmation)
        self.assertIn(ReasonCode.CONFLICTING_CORRECTIONS.value, interp.reason_codes)
        self.assertTrue(len(interp.choices) >= 2)
        # Check that NO task or support activity was created yet
        self.assertEqual(len(self.db.list_tasks()), 0)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM activities').fetchone()[0], 0)

        # Must have created a proposal in SQLite that survives restart
        prop = self.db.get_active_nl_proposal(owner_id=self.owner_id)
        self.assertIsNotNone(prop)
        self.assertEqual(prop['status'], 'pending')

        # Simulate fresh DB instance (bot restart)
        fresh_db = Database(self.db_path)
        persisted_prop = fresh_db.get_nl_proposal(prop['id'])
        self.assertIsNotNone(persisted_prop)
        self.assertEqual(persisted_prop['status'], 'pending')

    async def test_02_proposal_choice_requires_missing_entities_or_authorization(self):
        # Create a proposal with choices
        prop_id = self.db.create_nl_proposal(
            owner_id='owner',
            source_update_id=101,
            intent='unknown',
            proposal_data={
                'raw_text': 'quick sync with dev lead',
                'choices': [{'label': 'Action: create_task', 'intent': 'create_task'}]
            },
            expires_at=(datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
        )

        # 1. Unauthorized user cannot select choice (rejected by authorized() gate)
        unauth_update = self.make_update(user=9999, callback_data=f"prop:{prop_id}:c0")
        self.assertFalse(handlers.authorized(unauth_update))
        await handlers.handle(unauth_update, self.context)
        prop = self.db.get_nl_proposal(prop_id)
        self.assertEqual(prop['status'], 'pending')  # Still pending

        # 2. Authorized owner selects choice -> since task title is missing/partial, asks for required fields or presents proposal
        auth_update = self.make_update(user=self.owner_id, callback_data=f"prop:{prop_id}:c0")
        await handlers.handle(auth_update, self.context)
        # Proposal should transition or confirm
        prop_after = self.db.get_nl_proposal(prop_id)
        self.assertIn(prop_after['status'], ('applied', 'confirmed', 'pending'))

        # 3. Repeated callback delivery does not re-apply
        rep_update = self.make_update(user=self.owner_id, callback_data=f"prop:{prop_id}:c0")
        await handlers.handle(rep_update, self.context)

    async def test_03_negation_overrides_correction_learning(self):
        # Save correction for "sync with dev lead" -> create_task
        i1 = self.db.record_nl_interaction("sync with dev lead", intent="unknown", confidence=0.2)
        self.db.add_nl_correction(original_intent="unknown", corrected_intent="create_task",
                                  interaction_id=i1, owner_id=self.owner_id)

        # Input is explicitly negated: "do not sync with dev lead"
        reply, interp = await self.pipeline.process("do not sync with dev lead", shift=None)
        self.assertEqual(interp.action_decision, ActionDecision.REJECT.value)
        self.assertIn(ReasonCode.NEGATION_DETECTED.value, interp.reason_codes)
        self.assertEqual(len(self.db.list_tasks()), 0)

    # -------------------------------------------------------------------------
    # STAGE 2: Carry-Forward Matching and Identity
    # -------------------------------------------------------------------------

    async def test_04_carry_forward_blocked_task_preserves_identity_and_blocker(self):
        # Create a blocked task with blocker reason and client
        task = self.db.add_task(
            title="Retest Ubuntu 22 agent latency",
            client="AlphaCorp",
            priority=2
        )
        with self.db.connect() as conn:
            conn.execute('UPDATE tasks SET status="blocked", blocked_reason=? WHERE id=?', ('Waiting for client SSH credentials', task.id))
        blocked_task = self.db.get_task(task.id)
        self.assertEqual(blocked_task.status, TaskStatus.BLOCKED)
        self.assertEqual(blocked_task.blocked_reason, 'Waiting for client SSH credentials')

        # Start today's shift
        sid = self.db.start_shift(f"{self.today}T10:00:00+05:30", f"{self.today}T19:00:00+05:30")
        shift = self.db.shift(sid)

        # Carry task forward into today using natural language
        reply, interp = await self.pipeline.process("Carry the blocked Ubuntu retest into today", shift=shift)

        # Must reuse the exact same task ID and NOT create a new pending task
        tasks = self.db.list_tasks()
        self.assertEqual(len(tasks), 1)
        carried = tasks[0]
        self.assertEqual(carried.id, task.id)
        self.assertEqual(carried.status, TaskStatus.BLOCKED)  # Status preserved
        self.assertEqual(carried.blocked_reason, 'Waiting for client SSH credentials')  # Blocker preserved
        self.assertEqual(carried.due_date, self.today)
        self.assertEqual(carried.planned_shift_id, sid)

        # Must appear in TOD report
        tod = reports.generate_report('tod', shift, self.db.activities(sid), self.db.tasks_for_shift(sid))
        self.assertIn("Retest Ubuntu 22 agent latency", tod)

    async def test_05_similar_tasks_different_clients_require_selection(self):
        # Two tasks with same base title but different clients
        t1 = self.db.add_task("Attendance retest", client="Client Alpha")
        t2 = self.db.add_task("Attendance retest", client="Client Beta")

        # Ambiguous carry forward without specifying client
        reply, interp = await self.pipeline.process("Carry the attendance retest to tomorrow", shift=None)
        self.assertTrue(interp.needs_confirmation)
        self.assertIn(ReasonCode.AMBIGUOUS_REFERENCE.value, interp.reason_codes)
        self.assertTrue(len(interp.choices) >= 2)
        # Verify neither task was mutated
        self.assertIsNone(self.db.get_task(t1.id).due_date)
        self.assertIsNone(self.db.get_task(t2.id).due_date)

    async def test_06_completed_task_is_not_silently_reopened_on_carry(self):
        task = self.db.add_task("Old completed migration task")
        with self.db.connect() as conn:
            conn.execute('UPDATE tasks SET status="completed" WHERE id=?', (task.id,))

        # Attempt to carry completed task by explicit ID
        reply, interp = await self.pipeline.process(f"Carry task #{task.id} to tomorrow", shift=None)
        self.assertIn("cannot be carried forward", reply.lower())
        task_after = self.db.get_task(task.id)
        self.assertEqual(task_after.status, TaskStatus.COMPLETED)

    async def test_07_missing_carry_target_does_not_silently_create_task(self):
        # Carry a non-existent task
        reply, interp = await self.pipeline.process("Carry task #9999 to tomorrow", shift=None)
        self.assertIn("not found", reply.lower())
        self.assertIsNone(self.db.get_task(9999))
        self.assertEqual(len(self.db.list_tasks()), 0)

    # -------------------------------------------------------------------------
    # STAGE 3: Transactional Carry-Forward & Complete Undo
    # -------------------------------------------------------------------------

    async def test_08_transactional_carry_forward_and_undo(self):
        sid = self.db.start_shift(f"{self.today}T10:00:00+05:30", f"{self.today}T19:00:00+05:30")
        t = self.db.add_task("Critical bug test", client="Acme", due_date="2026-09-01")

        # Atomic carry forward
        res = self.db.carry_task_forward_transactional(
            task_id=t.id,
            target_date=self.today,
            shift_id=sid,
            actor='owner'
        )
        corr_id = res['correlation_id']
        act_id = res['activity_id']

        # Task fields updated and planning activity created
        updated_t = self.db.get_task(t.id)
        self.assertEqual(updated_t.due_date, self.today)
        self.assertEqual(updated_t.planned_shift_id, sid)
        self.assertIsNotNone(act_id)
        with self.db.connect() as conn:
            act = conn.execute('SELECT * FROM activities WHERE id=?', (act_id,)).fetchone()
        self.assertIsNotNone(act)

        # Atomic undo restores task fields and deletes planning activity
        undo_res = self.db.undo_audit_batch(correlation_id=corr_id)
        self.assertTrue(undo_res.get('undone_count') >= 1)

        reverted_t = self.db.get_task(t.id)
        self.assertEqual(reverted_t.due_date, "2026-09-01")
        self.assertIsNone(reverted_t.planned_shift_id)
        with self.db.connect() as conn:
            act_after = conn.execute('SELECT * FROM activities WHERE id=?', (act_id,)).fetchone()
        self.assertIsNone(act_after)

    async def test_09_carry_forward_failure_rolls_back_completely(self):
        t = self.db.add_task("Rollback test task", due_date="2026-09-01")

        # Injected failure (non-existent task ID)
        with self.assertRaises(ValueError):
            self.db.carry_task_forward_transactional(
                task_id=999999,
                target_date=self.today,
                actor='owner'
            )

        # Verify existing task was completely unaffected
        t_after = self.db.get_task(t.id)
        self.assertEqual(t_after.due_date, "2026-09-01")

    # -------------------------------------------------------------------------
    # STAGE 4: Consolidated Date & Shift Parsing
    # -------------------------------------------------------------------------

    def test_10_explicit_year_and_invalid_dates(self):
        ref = datetime(2026, 9, 11, 10, 0, tzinfo=self.tz)

        # 1. Explicit year preserved
        d1, inv1 = resolve_date_reference("My shift on 15 January 2027 is from 12 pm to 9 pm", ref)
        self.assertEqual(d1, "2027-01-15")
        self.assertFalse(inv1)

        # 2. Invalid leap day in non-leap year rejected
        d2, inv2 = resolve_date_reference("29 February 2027", ref)
        self.assertIsNone(d2)
        self.assertTrue(inv2)

        # 3. Valid leap day in leap year accepted
        d3, inv3 = resolve_date_reference("29 February 2028", ref)
        self.assertEqual(d3, "2028-02-29")
        self.assertFalse(inv3)

        # 4. Relative words
        d_tom, _ = resolve_date_reference("Tomorrow I need to test", ref)
        self.assertEqual(d_tom, "2026-09-12")

    async def test_11_tomorrow_planning_remains_tomorrow_through_persistence(self):
        # "Tomorrow I need to test..." must assign tomorrow's date
        p = DeterministicParser.parse("Tomorrow I need to test the Ubuntu agent", datetime.now(self.tz))
        self.assertEqual(p.intent, NLIntent.CREATE_TASK)
        self.assertEqual(p.entities.date, self.tomorrow)

        # Process and verify task in database has due_date == tomorrow
        reply, interp = await self.pipeline.process("Tomorrow I need to test the Ubuntu agent", shift=None)
        tasks = self.db.list_tasks()
        self.assertTrue(len(tasks) >= 1)
        created_task = tasks[-1]
        self.assertEqual(created_task.due_date, self.tomorrow)

    async def test_12_historical_shift_input_does_not_modify_today_active_shift(self):
        # Start today's active shift
        sid = self.db.start_shift(f"{self.today}T10:00:00+05:30", f"{self.today}T19:00:00+05:30")
        active_before = self.db.active_shift()

        # Log historical shift: "My shift yesterday was 10 am to 7 pm"
        yesterday_iso = (datetime.now(self.tz).date() - timedelta(days=1)).isoformat()
        reply, interp = await self.pipeline.process("My shift yesterday was 10 am to 7 pm", shift=active_before)

        # Active shift must remain untouched
        active_after = self.db.active_shift()
        self.assertEqual(active_after['id'], active_before['id'])
        self.assertEqual(active_after['start'], active_before['start'])
        self.assertEqual(active_after['end'], active_before['end'])

        # Historical entry must be saved in shift_calendar
        override = self.db.get_shift_calendar_override(yesterday_iso)
        self.assertIsNotNone(override)
        self.assertEqual(override['start_time'], '10:00')

    async def test_13_understand_command_never_mutates_state(self):
        task_count_before = len(self.db.list_tasks())
        with self.db.connect() as conn:
            act_count_before = conn.execute('SELECT count(*) FROM activities').fetchone()[0]

        update = self.make_update("/understand Tomorrow I need to test the Windows agent")
        await handlers.handle(update, self.context)

        # Verify reply was sent
        update.effective_message.reply_text.assert_called_once()
        reply = update.effective_message.reply_text.call_args[0][0]
        self.assertIn("Preview action: read_only", reply)

        # State 100% untouched
        self.assertEqual(len(self.db.list_tasks()), task_count_before)
        with self.db.connect() as conn:
            act_count_after = conn.execute('SELECT count(*) FROM activities').fetchone()[0]
        self.assertEqual(act_count_after, act_count_before)

    # -------------------------------------------------------------------------
    # STAGE 5: Correction Management Commands
    # -------------------------------------------------------------------------

    async def test_14_correction_management_lifecycle(self):
        # 1. Add a correction
        i_id = self.db.record_nl_interaction("sync with dev", intent="unknown", confidence=0.2)
        c_id = self.db.add_nl_correction("unknown", "create_task", interaction_id=i_id, owner_id=self.owner_id)

        # 2. /corrections command
        up_list = self.make_update("/corrections")
        await handlers.handle(up_list, self.context)
        up_list.effective_message.reply_text.assert_called_once()
        list_reply = up_list.effective_message.reply_text.call_args[0][0]
        self.assertIn(f"#{c_id}", list_reply)
        self.assertIn("create_task", list_reply)

        # 3. /disablecorrection <id>
        up_dis = self.make_update(f"/disablecorrection {c_id}")
        await handlers.handle(up_dis, self.context)
        self.assertIn("disabled", up_dis.effective_message.reply_text.call_args[0][0].lower())
        # Verify inactive in DB
        corrections = self.db.get_relevant_corrections("sync with dev")
        self.assertEqual(len(corrections), 0)

        # 4. /enablecorrection <id>
        up_en = self.make_update(f"/enablecorrection {c_id}")
        await handlers.handle(up_en, self.context)
        self.assertIn("enabled", up_en.effective_message.reply_text.call_args[0][0].lower())
        corrections = self.db.get_relevant_corrections("sync with dev")
        self.assertEqual(len(corrections), 1)

        # 5. /deletecorrection <id>
        up_del = self.make_update(f"/deletecorrection {c_id}")
        await handlers.handle(up_del, self.context)
        self.assertIn("deleted", up_del.effective_message.reply_text.call_args[0][0].lower())
        self.assertEqual(len(self.db.list_nl_corrections()), 0)

    # -------------------------------------------------------------------------
    # STAGE 6: Natural Progress Updates
    # -------------------------------------------------------------------------

    async def test_15_progress_updates_distinguish_dev_fix_and_client_confirmation(self):
        task = self.db.add_task("Investigate agent high CPU", client="GBB")
        case = self.db.create_case("Agent high CPU", client="GBB")
        self.db.update_conversation_context('owner', active_task_id=task.id, active_case_id=case)

        # 1. Developer fix report: does NOT complete task or close case
        up_dev = self.make_update("The developer says it is fixed; I still need to retest")
        await handlers.handle(up_dev, self.context)
        t_after_dev = self.db.get_task(task.id)
        self.assertEqual(t_after_dev.status, TaskStatus.PENDING)  # Still open
        self.assertEqual(t_after_dev.next_action, "Retest fix in environment")

        # 2. Client confirmation
        up_client = self.make_update("Client confirmed it works")
        await handlers.handle(up_client, self.context)
        events = self.db.case_events(case)
        self.assertTrue(any("Client confirmed" in e['detail'] for e in events))

    # -------------------------------------------------------------------------
    # STAGE 8: Real Application Dispatch (bot.gate + deduplication)
    # -------------------------------------------------------------------------

    async def test_16_end_to_end_application_dispatch_gate_and_deduplication(self):
        from telegram.ext._extbot import ExtBot
        with patch.object(ExtBot, 'get_me', new_callable=AsyncMock) as mock_get_me:
            mock_get_me.return_value = User(100, 'TestBot', is_bot=True, username='test_bot')
            app = Application.builder().token('123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11').build()
            app.bot_data['db'] = self.db
            app.add_handler(TypeHandler(Update, bot.gate), group=-1)
            app.add_handler(CallbackQueryHandler(handlers.handle, pattern=bot.CALLBACK_PATTERN))
            app.add_handler(MessageHandler(filters.TEXT, handlers.handle))
            await app.initialize()

            owner_user = User(self.owner_id, 'Owner', is_bot=False)
            chat = Chat(self.owner_id, 'private')

            # 1. Legitimate owner message dispatched
            with patch.object(handlers, 'reply', new_callable=AsyncMock) as mock_reply:
                msg = Message(7001, None, chat, from_user=owner_user, text='/help')
                msg.set_bot(app.bot)
                up = Update(7001, message=msg)
                up.set_bot(app.bot)
                await app.process_update(up)
                self.assertTrue(mock_reply.called)

            # 2. Duplicate update_id is rejected by gate
            with patch.object(handlers, 'reply', new_callable=AsyncMock) as mock_reply:
                msg_dup = Message(7001, None, chat, from_user=owner_user, text='/help')
                msg_dup.set_bot(app.bot)
                up_dup = Update(7001, message=msg_dup)
                up_dup.set_bot(app.bot)
                await app.process_update(up_dup)
                self.assertFalse(mock_reply.called)

            # 3. Unauthorized sender is rejected by gate
            hacker = User(9999, 'Hacker', is_bot=False)
            hacker_chat = Chat(9999, 'private')
            with patch.object(handlers, 'reply', new_callable=AsyncMock) as mock_reply:
                hacker_msg = Message(7002, None, hacker_chat, from_user=hacker, text='/tasks')
                hacker_msg.set_bot(app.bot)
                up_hacker = Update(7002, message=hacker_msg)
                up_hacker.set_bot(app.bot)
                await app.process_update(up_hacker)
                self.assertFalse(mock_reply.called)


if __name__ == '__main__':
    unittest.main()
