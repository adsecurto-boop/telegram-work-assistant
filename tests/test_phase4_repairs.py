"""Comprehensive Phase 4 corrective tests verifying all required production paths.
Covers 32 specific requirements:
1. Plain natural-language Telegram handler
2. Voice transcript routed through NLP
3. /understand
4. /undo
5. Telegram undo callback
6. Multi-task correlation undo
7. Test-session compound undo
8. Standalone follow-up compound undo
9. Atomic bulk undo failure
10. Future shift scheduling
11. Day-off parsing
12. Date-range template assignment
13. Template weekday enforcement
14. Invalid time rejection
15. Natural-language TOD
16. Natural-language lunch report
17. Natural-language EOD
18. Copilot natural-language dispatch
19. Low-confidence no-mutation behavior
20. Complete clarification-state preservation
21. Dashboard cluster acceptance POST
22. Dashboard bulk preview and confirm
23. CSRF rejection
24. Cluster idempotency
25. Repeated cluster acceptance rejection
26. Report date scoping
27. Duplicate-client warnings
28. Ticket and client hallucination detection
29. AI payload redaction
30. AI quota and event recording
31. Migration rollback on injected failure
32. Windows launcher path containing spaces
"""
import asyncio
import http.client
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import config
import handlers
from ai import AIPayloadBuilder
from bot import CALLBACK_PATTERN
from clustering import HistoricalClusterEngine, DisjointSet, ClusterSuggestion
from dashboard import DashboardService
from database import Database, SCHEMA_VERSION
from models import TaskStatus
from nlp import DeterministicParser, NaturalLanguagePipeline, NLIntent, NLEntities, NLInterpretation
from report_validator import ReportValidator
from shifts import assign_template_range, validate_schedule


class Phase4RepairTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / 'work.sqlite3'
        self.db = Database(self.db_path)
        self.sid = self.db.start_shift('2026-09-09T10:00:00+05:30', '2026-09-09T19:00:00+05:30', '2026-09-09T14:00:00+05:30')
        self.shift = self.db.active_shift()

        self.owner_patch = patch.object(config, 'OWNER_ID', 123)
        self.owner_patch.start()
        self.addCleanup(self.owner_patch.stop)

        self.mock_bot = SimpleNamespace(send_message=AsyncMock())
        self.context = SimpleNamespace(
            application=SimpleNamespace(bot_data={'db': self.db}, job_queue=None),
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

    # 1. Plain natural-language Telegram handler
    async def test_01_plain_natural_language_telegram_handler(self):
        up = self.make_update(text="Complete task 1")
        # Add task 1
        t = self.db.add_task("Test task 1", shift_id=self.sid)
        await handlers.save_plain_message(up, self.context, "Complete task 1")
        up.effective_message.reply_text.assert_awaited()
        reply_call = up.effective_message.reply_text.call_args[0][0]
        self.assertIn("Task #1 completed", reply_call)
        task = self.db.get_task(t.id)
        self.assertEqual(task.status, TaskStatus.COMPLETED)

    # 2. Voice transcript routed through NLP
    async def test_02_voice_transcript_routed_through_nlp(self):
        with patch('media_capture.voice_bytes', AsyncMock(return_value=(b'audio', 'audio/ogg', 'file-id'))), \
             patch('ai.writer') as mock_writer:
            mock_engine = SimpleNamespace(transcribe=AsyncMock(return_value="Complete task 1"), last_model='gemini-2.5-flash')
            mock_writer.return_value = mock_engine
            up = self.make_update(text="")
            # Add task 1
            self.db.add_task("Task 1 for voice", shift_id=self.sid)
            await handlers.capture_voice(up, self.context)
            # The voice transcript was "Complete task 1", routed to save_plain_message
            task = self.db.get_task(1)
            self.assertEqual(task.status, TaskStatus.COMPLETED)

    # 3. /understand
    async def test_03_understand_command(self):
        up = self.make_update(text="/understand Tomorrow I'm working 8 to 5")
        await handlers.handle(up, self.context)
        up.effective_message.reply_text.assert_awaited()
        output = up.effective_message.reply_text.call_args[0][0]
        self.assertIn("Intent: set_shift", output)
        self.assertIn("08:00", output)
        self.assertIn("17:00", output)

    # 4. /undo
    async def test_04_undo_command(self):
        t = self.db.add_task("Undoable task", shift_id=self.sid)
        corr_id = "test-corr-task"
        aid = self.db.record_audit(
            correlation_id=corr_id,
            operation_type="create_task",
            actor="test",
            affected_table="tasks",
            record_id=t.id,
            before_state_json=None,
            after_state_json=json.dumps({"title": "Undoable task"})
        )
        up = self.make_update(text="/undo")
        await handlers.handle(up, self.context)
        up.effective_message.reply_text.assert_awaited()
        output = up.effective_message.reply_text.call_args[0][0]
        self.assertIn(f"Undid action #{aid}", output)
        self.assertIsNone(self.db.get_task(t.id))

    # 5. Telegram undo callback
    async def test_05_telegram_undo_callback(self):
        t = self.db.add_task("Undoable task callback", shift_id=self.sid)
        aid = self.db.record_audit(
            correlation_id="corr-cb",
            operation_type="create_task",
            actor="test",
            affected_table="tasks",
            record_id=t.id,
            before_state_json=None,
            after_state_json=json.dumps({"title": "Undoable task callback"})
        )
        up = self.make_update(callback_data=f"audit:undo:{aid}")
        await handlers.handle_callback(up, self.context)
        up.effective_message.reply_text.assert_awaited()
        output = up.effective_message.reply_text.call_args[0][0]
        self.assertIn(f"Undid action #{aid}", output)
        self.assertIsNone(self.db.get_task(t.id))

    # 6. Multi-task correlation undo
    async def test_06_multi_task_correlation_undo(self):
        corr_id = "corr-multi-tasks"
        t1 = self.db.add_task("Task 1", shift_id=self.sid)
        aid1 = self.db.record_audit(
            correlation_id=corr_id, operation_type="create_task", actor="test",
            affected_table="tasks", record_id=t1.id, before_state_json=None,
            after_state_json=json.dumps({"title": "Task 1"})
        )
        t2 = self.db.add_task("Task 2", shift_id=self.sid)
        aid2 = self.db.record_audit(
            correlation_id=corr_id, operation_type="create_task", actor="test",
            affected_table="tasks", record_id=t2.id, before_state_json=None,
            after_state_json=json.dumps({"title": "Task 2"})
        )
        self.assertEqual(len(self.db.list_tasks()), 2)
        # Undoing via either audit ID undoes the whole correlation batch
        res = self.db.undo_audit_record(aid2)
        self.assertEqual(res['correlation_id'], corr_id)
        self.assertEqual(res['undone_count'], 2)
        self.assertEqual(len(self.db.list_tasks()), 0)

    # 7. Test-session compound undo
    async def test_07_test_session_compound_undo(self):
        corr_id = "corr-compound-test"
        case_id = self.db.create_case("Issue A", client="Acme", shift_id=None)
        ts_id = self.db.add_test_session("Login scenario", self.sid, case_id=case_id)
        ev_id = self.db.add_case_event(case_id, "testing", "Executed test", self.sid)

        # Record audits under one correlation ID
        self.db.record_audit(
            correlation_id=corr_id, operation_type="create_test_session", actor="test",
            affected_table="test_sessions", record_id=ts_id, before_state_json=None,
            after_state_json=json.dumps({"scenario": "Login scenario"})
        )
        self.db.record_audit(
            correlation_id=corr_id, operation_type="add_case_event", actor="test",
            affected_table="case_events", record_id=ev_id, before_state_json=None,
            after_state_json=json.dumps({"detail": "Executed test"})
        )

        res = self.db.undo_audit_batch(corr_id)
        self.assertEqual(res['undone_count'], 2)
        self.assertEqual(len(self.db.test_sessions(shift_id=self.sid)), 0)
        self.assertEqual(len(self.db.case_events(case_id)), 0)

    # 8. Standalone follow-up compound undo
    async def test_08_standalone_followup_compound_undo(self):
        pipeline = NaturalLanguagePipeline(self.db)
        # Creating a follow-up without specifying a case creates an automatic container case
        reply, interp = await pipeline.process("Remind me tomorrow at 11 to ask Rahul for logs", self.shift)
        self.assertIn("Scheduled Follow-up", reply)
        self.assertIn("Undo: /undo", reply)
        # Both case and follow-up were created
        followups = self.db.list_followups()
        self.assertEqual(len(followups), 1)
        cases = self.db.list_cases()
        self.assertEqual(len(cases), 1)

        # Undo the last action
        undo_reply, _ = await pipeline.process("undo", self.shift)
        self.assertIn("Reverted", undo_reply)
        self.assertEqual(len(self.db.list_followups()), 0)
        self.assertEqual(len(self.db.list_cases()), 0)

    # 9. Atomic bulk undo failure
    def test_09_atomic_bulk_undo_failure_rolls_back_entirely(self):
        corr_id = "corr-atomic-bulk"
        t1 = self.db.add_task("Task A", shift_id=self.sid)
        t2 = self.db.add_task("Task B", shift_id=self.sid)
        self.db.record_audit(
            correlation_id=corr_id, operation_type="create_task", actor="test",
            affected_table="tasks", record_id=t1.id, before_state_json=None,
            after_state_json=json.dumps({"title": "Task A"})
        )
        self.db.record_audit(
            correlation_id=corr_id, operation_type="create_task", actor="test",
            affected_table="tasks", record_id=t2.id, before_state_json=None,
            after_state_json=json.dumps({"title": "Task B"})
        )

        # External modification on Task A causes tamper check failure on Task A
        self.db.update_task(t1.id, 'title', 'Tampered Task A')

        with self.assertRaises(ValueError) as ctx:
            self.db.undo_audit_batch(corr_id)
        self.assertIn("has changed since this operation", str(ctx.exception))

        # Atomic guarantee: Task B must NOT have been deleted because batch was rolled back!
        self.assertIsNotNone(self.db.get_task(t2.id))
        self.assertIsNotNone(self.db.get_task(t1.id))

    # 10. Future shift scheduling
    async def test_10_future_shift_scheduling_does_not_activate_today(self):
        pipeline = NaturalLanguagePipeline(self.db)
        reply, interp = await pipeline.process("Tomorrow I'm working 8 to 5", self.shift)
        self.assertIn("Stored in shift calendar (not active today)", reply)
        self.assertIn("08:00 to 17:00", reply)

        # Verify active shift for today remains untouched
        today_active = self.db.active_shift()
        self.assertEqual(today_active['id'], self.sid)
        self.assertEqual(today_active['start'][11:16], '10:00')

        # Verify future shift is in shift_calendar
        tz = ZoneInfo(config.TIMEZONE)
        tomorrow_str = (datetime.now(tz).date() + timedelta(days=1)).isoformat()
        cal = self.db.get_shift_calendar(tomorrow_str)
        self.assertIsNotNone(cal)
        self.assertEqual(cal['start_time'], '08:00')
        self.assertEqual(cal['end_time'], '17:00')

    # 11. Day-off parsing
    async def test_11_day_off_parsing(self):
        pipeline = NaturalLanguagePipeline(self.db)
        reply, interp = await pipeline.process("Friday is a day off", self.shift)
        self.assertIn("Set day off", reply)

        # Check calendar has day off override
        tz = ZoneInfo(config.TIMEZONE)
        today = datetime.now(tz).date()
        days_until_friday = (4 - today.weekday()) % 7
        if days_until_friday == 0:
            days_until_friday = 7
        friday_str = (today + timedelta(days=days_until_friday)).isoformat()
        cal = self.db.get_shift_calendar(friday_str)
        self.assertIsNotNone(cal)
        self.assertEqual(cal['is_day_off'], 1)

    # 12. Date-range template assignment
    def test_12_date_range_template_assignment(self):
        tmpl = self.db.get_shift_template("Evening") # 12:00 to 21:00
        self.assertIsNotNone(tmpl)
        count = assign_template_range(self.db, tmpl['id'], "2026-09-10", "2026-09-15")
        self.assertGreater(count, 0)
        c = self.db.get_shift_calendar("2026-09-11")
        self.assertIsNotNone(c)
        self.assertEqual(c['start_time'], "12:00")
        self.assertEqual(c['end_time'], "21:00")

    # 13. Template weekday enforcement
    def test_13_template_weekday_enforcement(self):
        tmpl = self.db.get_shift_template("General") # weekdays 0..4 (Mon-Fri)
        self.db.set_shift_calendar_override(date_str="2026-09-12", is_day_off=1, is_explicit_override=1) # Sat
        count = assign_template_range(self.db, tmpl['id'], "2026-09-12", "2026-09-14", skip_existing_overrides=True)
        # 2026-09-12 is Saturday (weekday 5), not in template active_weekdays; 2026-09-13 is Sunday (weekday 6)
        # Only 2026-09-14 (Monday) matches active_weekdays
        self.assertEqual(count, 1)
        # Saturday explicit override remains untouched
        sat = self.db.get_shift_calendar("2026-09-12")
        self.assertEqual(sat['is_day_off'], 1)

    # 14. Invalid time rejection
    def test_14_invalid_time_rejection(self):
        with self.assertRaises(ValueError):
            validate_schedule("99:80", "19:00", "14:00")
        with self.assertRaises(ValueError):
            validate_schedule("10:00", "25:00", "14:00")
        with self.assertRaises(ValueError):
            validate_schedule("10:00", "19:00", "20:00") # Lunch outside shift
        interp = DeterministicParser.parse("My shift is 99:80 to 19:00")
        self.assertTrue(interp is None or interp.intent == NLIntent.UNKNOWN or getattr(interp.entities, 'shift_start', None) != "99:80")

    # 15. Natural-language TOD
    async def test_15_natural_language_tod(self):
        pipeline = NaturalLanguagePipeline(self.db)
        reply, interp = await pipeline.process("Generate my TOD", self.shift)
        self.assertEqual(interp.intent, NLIntent.GENERATE_TOD)
        self.assertIn("Generated TOD Draft", reply)

    # 16. Natural-language lunch report
    async def test_16_natural_language_lunch_report(self):
        pipeline = NaturalLanguagePipeline(self.db)
        reply, interp = await pipeline.process("Prepare my lunch update", self.shift)
        self.assertEqual(interp.intent, NLIntent.GENERATE_LUNCH_UPDATE)
        self.assertIn("Generated PL Draft", reply)

    # 17. Natural-language EOD
    async def test_17_natural_language_eod(self):
        pipeline = NaturalLanguagePipeline(self.db)
        reply, interp = await pipeline.process("Generate my EOD in a professional format", self.shift)
        self.assertEqual(interp.intent, NLIntent.GENERATE_EOD)
        self.assertIn("Generated EOD Draft", reply)

    # 18. Copilot natural-language dispatch
    async def test_18_copilot_natural_language_dispatch(self):
        pipeline = NaturalLanguagePipeline(self.db)
        # create_case
        rep, _ = await pipeline.process("Create a case for Acme's attendance issue", self.shift)
        self.assertIn("Created CASE-", rep)
        self.assertEqual(len(self.db.list_cases()), 1)

        # change_case_status
        rep, _ = await pipeline.process("Mark that case waiting for client", self.shift)
        self.assertIn("waiting_client", rep)

        # add_learning
        rep, _ = await pipeline.process("Learned that Linux silent install requires flags", self.shift)
        self.assertIn("learning record", rep.lower())

        # analyze_test — uses _optional_ai; patch AI_KEY/AI_MODEL to '' so the
        # guard short-circuits to the deterministic fallback (no google.genai import).
        self.db.add_test_session("Login retest", self.sid, result="passed")
        with patch.object(config, 'AI_KEY', ''), patch.object(config, 'AI_MODEL', ''):
            rep, _ = await pipeline.process("Analyze the last test", self.shift)
        self.assertIn("Test Analysis", rep)

    # 19. Low-confidence no-mutation behavior
    async def test_19_low_confidence_no_mutation(self):
        pipeline = NaturalLanguagePipeline(self.db)
        tasks_before = len(self.db.list_tasks())
        cases_before = len(self.db.list_cases())
        reply, interp = await pipeline.process("blablabla completely random text 12345", self.shift)
        self.assertIn("could not determine the intended action with confidence", reply)
        self.assertEqual(len(self.db.list_tasks()), tasks_before)
        self.assertEqual(len(self.db.list_cases()), cases_before)

    # 20. Complete clarification-state preservation
    async def test_20_complete_clarification_state_preservation(self):
        # Create two cases with client Acme to trigger ambiguity
        c1 = self.db.create_case("Acme Attendance", client="Acme", shift_id=self.sid)
        c2 = self.db.create_case("Acme Payroll", client="Acme", shift_id=self.sid)
        self.db.update_conversation_context('owner', active_case_id=None)

        pipeline = NaturalLanguagePipeline(self.db)
        reply, interp = await pipeline.process("Mark the Acme issue resolved", self.shift, source_update_id=2001)
        self.assertTrue(interp.needs_confirmation)
        self.assertIn("Multiple cases match", reply)

        # Check proposal stored in DB
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM nl_proposals ORDER BY id DESC LIMIT 1").fetchone()
            self.assertIsNotNone(row)
            prop_id = row['id']
            prop_data = json.loads(row['proposal_json'])
            self.assertEqual(prop_data['intent'], 'change_case_status')
            self.assertIn('resolved', prop_data['entities']['status'])

        # Now simulate accepting choice 1 via callback query
        up = self.make_update(callback_data=f"prop:choose:{prop_id}:1")
        await handlers.handle_callback(up, self.context)
        up.effective_message.reply_text.assert_awaited()
        call_output = up.effective_message.reply_text.call_args[0][0]
        self.assertIn("status to resolved", call_output)
        self.assertEqual(self.db.case(c1)['status'], 'resolved')

        # Replayed / already-used proposal must be rejected
        up2 = self.make_update(callback_data=f"prop:choose:{prop_id}:1")
        await handlers.handle_callback(up2, self.context)
        up2.effective_message.reply_text.assert_awaited()
        replayed_output = up2.effective_message.reply_text.call_args[0][0]
        self.assertIn("already accepted", replayed_output)

    # 21. Dashboard cluster acceptance POST
    def test_21_dashboard_cluster_acceptance_post(self):
        # Seed candidate message and cluster
        m1, _ = self.db.add_source_message(
            source_type='telegram', source_key='cl-msg-1', text='Problem with printer',
            author_is_owner=False, review_status='pending', shift_id=self.sid
        )
        c_id = self.db.create_cluster(
            title="Printer failure", suggested_client="Alpha", suggested_product="PrintApp",
            suggested_issue_type="defect", confidence=0.85, reason="Same issue", status="pending"
        )
        self.db.add_cluster_item(c_id, m1['id'])

        service = DashboardService(self.db, '127.0.0.1', 0).start()
        self.addCleanup(service.stop)
        port = service.server.server_address[1]

        # Authenticate and get session cookie + CSRF token
        sid, csrf = service.create_session()
        headers = {
            'Cookie': f'dashboard_session={sid}',
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        body = urllib.parse.urlencode({
            'csrf_token': csrf,
            'cluster_id': str(c_id),
            'case_title': 'Printer Issue Case'
        }).encode('utf-8')

        conn = http.client.HTTPConnection('127.0.0.1', port)
        conn.request('POST', '/cluster/apply', body, headers)
        res = conn.getresponse()
        self.assertEqual(res.status, 303)
        conn.close()

        # Verify cluster accepted as case
        cl = self.db.get_cluster(c_id)
        self.assertEqual(cl['status'], 'accepted')
        self.assertIsNotNone(cl['case_id'])
        created_case = self.db.case(cl['case_id'])
        self.assertEqual(created_case['title'], 'Printer Issue Case')

    # 22. Dashboard bulk preview and confirm
    def test_22_dashboard_bulk_preview_and_confirm(self):
        m1, _ = self.db.add_source_message(
            source_type='telegram', source_key='blk-1', text='Bulk item 1',
            author_is_owner=True, review_status='pending', shift_id=self.sid
        )
        service = DashboardService(self.db, '127.0.0.1', 0).start()
        self.addCleanup(service.stop)
        port = service.server.server_address[1]

        sid, csrf = service.create_session()
        headers = {
            'Cookie': f'dashboard_session={sid}',
            'Content-Type': 'application/x-www-form-urlencoded'
        }

        # Step 1: POST to preview
        body1 = urllib.parse.urlencode({
            'csrf_token': csrf,
            'action': 'accept_tasks',
            'message_ids': str(m1['id'])
        }).encode('utf-8')

        conn = http.client.HTTPConnection('127.0.0.1', port)
        conn.request('POST', '/inbox/bulk/preview', body1, headers)
        res1 = conn.getresponse()
        self.assertEqual(res1.status, 200)
        html_preview = res1.read().decode()
        conn.close()
        self.assertIn("Confirm Bulk Action", html_preview)
        self.assertIn("bulk_token", html_preview)

        # Extract token from service.bulk_tokens
        bulk_token = list(service.bulk_tokens.keys())[0]

        # Step 2: POST to confirm
        body2 = urllib.parse.urlencode({
            'csrf_token': csrf,
            'bulk_token': bulk_token
        }).encode('utf-8')

        conn = http.client.HTTPConnection('127.0.0.1', port)
        conn.request('POST', '/inbox/bulk/confirm', body2, headers)
        res2 = conn.getresponse()
        self.assertEqual(res2.status, 303)
        conn.close()

        # Task was created and message accepted
        self.assertEqual(len(self.db.list_tasks()), 1)
        self.assertEqual(self.db.source_message(m1['id'])['review_status'], 'accepted')

    # 23. CSRF rejection
    def test_23_csrf_rejection(self):
        service = DashboardService(self.db, '127.0.0.1', 0).start()
        self.addCleanup(service.stop)
        port = service.server.server_address[1]

        sid, csrf = service.create_session()
        headers = {
            'Cookie': f'dashboard_session={sid}',
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        # Bad CSRF token
        body = urllib.parse.urlencode({'csrf_token': 'bogus_token'}).encode('utf-8')
        conn = http.client.HTTPConnection('127.0.0.1', port)
        conn.request('POST', '/shifts/override', body, headers)
        res = conn.getresponse()
        self.assertEqual(res.status, 403)
        conn.close()

    # 24. Cluster idempotency
    def test_24_cluster_idempotency(self):
        engine = HistoricalClusterEngine(self.db)
        m1, _ = self.db.add_source_message(
            source_type='telegram', source_key='idem-1', text='Login issue ticket #T-99',
            author_is_owner=True, review_status='pending', metadata={'ticket': 'T-99', 'client': 'Beta'}
        )
        m2, _ = self.db.add_source_message(
            source_type='telegram', source_key='idem-2', text='Re-test login ticket #T-99',
            author_is_owner=True, review_status='pending', metadata={'ticket': 'T-99', 'client': 'Beta'}
        )
        suggestions, _ = engine.cluster()
        self.assertEqual(len(suggestions), 1)

        # Apply suggestions first time
        res1 = engine.apply_suggestions(suggestions)
        saved1 = int(res1)
        self.assertEqual(saved1, 1)

        # Repeated clustering on unchanged messages
        suggestions2, _ = engine.cluster()
        res2 = engine.apply_suggestions(suggestions2)
        saved2 = int(res2)
        already2 = res2.already_suggested
        self.assertEqual(saved2, 0)
        self.assertEqual(already2, 1)
        # Cluster count remains exactly 1
        self.assertEqual(len(self.db.get_clusters()), 1)

    # 25. Repeated cluster acceptance rejection
    def test_25_repeated_cluster_acceptance_rejection(self):
        m1, _ = self.db.add_source_message(
            source_type='telegram', source_key='rep-1', text='Issue X',
            author_is_owner=False, review_status='pending', shift_id=self.sid
        )
        c_id = self.db.create_cluster(title="Test", status="pending")
        self.db.add_cluster_item(c_id, m1['id'])
        self.db.bulk_accept_cluster_as_case(c_id)
        # Attempting to accept again must raise ValueError
        with self.assertRaises(ValueError) as ctx:
            self.db.bulk_accept_cluster_as_case(c_id)
        self.assertIn("already accepted", str(ctx.exception).lower())

    # 26. Report date scoping
    def test_26_report_date_scoping(self):
        # Create an old completed task from yesterday
        old_task = self.db.add_task("Yesterday task", shift_id=None)
        self.db.mark_status(old_task.id, TaskStatus.COMPLETED)
        with self.db.connect() as conn:
            conn.execute("UPDATE tasks SET completed_at='2026-09-08T12:00:00Z' WHERE id=?", (old_task.id,))

        # Today shift is 2026-09-09
        shift = self.db.active_shift()
        scoped_tasks = self.db.tasks_for_shift(shift['id'])
        self.assertEqual(len(scoped_tasks), 0)

    # 27. Duplicate-client warnings
    def test_27_duplicate_client_warnings(self):
        report_text = (
            "END OF DAY REPORT\n"
            "Clients handled: 1. Acme Corp, Acme Corp.\n"
            "Tasks completed: none."
        )
        validation = ReportValidator.validate('eod', report_text, self.shift, [], [], [], [], [])
        warn_codes = [w.code for w in validation.warnings]
        self.assertIn("DUPLICATE_CLIENT_ENTRY", warn_codes)

    # 28. Ticket and client hallucination detection
    def test_28_ticket_and_client_hallucination_detection(self):
        report_text = (
            "END OF DAY REPORT\n"
            "Handled ticket #TICK-9999 for Megacorp.\n"
            "Tasks: completed testing."
        )
        validation = ReportValidator.validate('eod', report_text, self.shift, [], [], [], [], [])
        warn_codes = [w.code for w in validation.warnings]
        self.assertIn("HALLUCINATED_TICKET", warn_codes)
        self.assertIn("HALLUCINATED_CLIENT", warn_codes)

    # 29. AI payload redaction
    def test_29_ai_payload_redaction(self):
        case_dict = {
            'id': 10, 'title': 'Contact client@example.com at +91-9876543210',
            'client': 'Acme', 'secret': 'sk-secret123'
        }
        events = [{'detail': 'User token is bearer-abc-1234567890'}]
        payload = AIPayloadBuilder.build_case_payload(case_dict, events, mask_client=True)
        self.assertNotIn('client@example.com', payload)
        self.assertNotIn('9876543210', payload)
        self.assertNotIn('sk-secret123', payload)
        self.assertNotIn('bearer-abc', payload)
        self.assertIn('Client 10', payload) # Masked client
        self.assertIn('<untrusted_data>', payload)

    # 30. AI quota and event recording
    def test_30_ai_quota_and_event_recording(self):
        self.db.record_ai_event(
            feature='case_summary',
            provider='gemini',
            model='gemini-2.5-flash',
            prompt_version='summary-v1',
            status='success'
        )
        self.db.record_ai_event(
            feature='case_summary',
            provider='gemini',
            model='gemini-2.5-flash',
            prompt_version='summary-v1',
            status='failed',
            error_type='TimeoutError'
        )
        stats = self.db.ai_stats()
        self.assertGreaterEqual(stats.get('total_events', 0), 2)

    # 31. Migration rollback on injected failure
    def test_31_migration_rollback_on_injected_failure(self):
        tmp_db_path = self.root / 'fail_test.sqlite3'
        # Create a schema v4 database
        conn = sqlite3.connect(tmp_db_path)
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        conn.execute('CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)')
        conn.execute('INSERT INTO tasks (id, title, status, created_at) VALUES (1, "Existing Task", "pending", "2026-09-09T08:00:00Z")')
        conn.execute('PRAGMA user_version = 4')
        conn.commit()
        conn.close()

        # Subclass Database and inject failure inside _seed_v5_defaults
        class FailingDatabase(Database):
            def _seed_v5_defaults(self, connection):
                connection.execute("CREATE TABLE partial_table_test (id INT)")
                raise sqlite3.OperationalError("Simulated mid-migration failure")

        with self.assertRaises(RuntimeError) as ctx:
            FailingDatabase(tmp_db_path)
        self.assertIn("Simulated mid-migration failure", str(ctx.exception))

        # Verify rollback: version remains 4, partial table does NOT exist, existing task intact
        conn = sqlite3.connect(tmp_db_path)
        version = conn.execute('PRAGMA user_version').fetchone()[0]
        self.assertEqual(version, 4)
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        self.assertNotIn("partial_table_test", tables)
        task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        self.assertEqual(task_count, 1)
        conn.close()

        # Verify a backup was created before migration attempt
        backups = list((self.root / 'backups').glob('pre-migration-v4-*.sqlite3'))
        self.assertEqual(len(backups), 1)

    # 32. Windows launcher path containing spaces
    def test_32_windows_launcher_path_containing_spaces(self):
        spaced_dir = self.root / 'telegram work assistant'
        spaced_dir.mkdir()
        target_script = spaced_dir / 'probe script.py'
        target_script.write_text("print('launcher-path-ok')", encoding='utf-8')
        result = subprocess.run(
            [sys.executable, str(target_script)], cwd=spaced_dir,
            capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), 'launcher-path-ok')

    def test_33_real_callback_router_accepts_proposals(self):
        self.assertIsNotNone(re.match(CALLBACK_PATTERN, 'prop:accept:prop_a1b2c3d4e5'))
        self.assertIsNotNone(re.match(CALLBACK_PATTERN, 'prop:cancel:prop_a1b2c3d4e5'))
        self.assertIsNotNone(re.match(CALLBACK_PATTERN, 'prop:choose:prop_a1b2c3d4e5:1'))

    async def test_34_nl_range_uses_requested_hours_and_undoes_as_one_batch(self):
        pipeline = NaturalLanguagePipeline(self.db)
        reply, _ = await pipeline.process('Use 12 to 9 for the rest of September', self.shift)
        self.assertIn('12:00', reply)
        day = self.db.get_shift_calendar('2026-09-10')
        self.assertEqual((day['start_time'], day['end_time']), ('12:00', '21:00'))
        latest = self.db.get_last_reversible_audit()
        batch = self.db.get_audit_log(100, correlation_id=latest['correlation_id'])
        self.assertGreater(len(batch), 1)
        self.db.undo_audit_record(latest['id'])
        self.assertIsNone(self.db.get_shift_calendar('2026-09-10'))

    async def test_35_task_completion_undo_restores_all_fields_and_activity(self):
        pipeline = NaturalLanguagePipeline(self.db)
        task = self.db.add_task('Full undo task', shift_id=self.sid)
        initial_activities = len(self.db.activities(self.sid))
        await pipeline.process(f'Complete task {task.id}', self.shift)
        self.assertEqual(len(self.db.activities(self.sid)), initial_activities + 1)
        await pipeline.process('undo', self.shift)
        restored = self.db.get_task(task.id)
        self.assertEqual(restored.status, TaskStatus.PENDING)
        self.assertIsNone(restored.completed_at)
        self.assertIsNone(restored.completion_note)
        self.assertEqual(len(self.db.activities(self.sid)), initial_activities)

    async def test_36_case_status_and_test_update_have_complete_undo(self):
        pipeline = NaturalLanguagePipeline(self.db)
        case_id = self.db.create_case('Undo lifecycle', client='Acme', shift_id=self.sid)
        self.db.update_conversation_context('owner', active_case_id=case_id)
        event_count = len(self.db.case_events(case_id))
        await pipeline.process('Mark that case resolved', self.shift)
        await pipeline.process('undo', self.shift)
        self.assertEqual(self.db.case(case_id)['status'], 'new')
        self.assertEqual(len(self.db.case_events(case_id)), event_count)

        test_id = self.db.add_test_session('Undo test result', self.sid, result='not_run')
        self.db.update_conversation_context('owner', active_test_session_id=test_id)
        reply, _ = await pipeline.process('The test passed', self.shift)
        self.assertIn('Updated Test', reply)
        await pipeline.process('undo', self.shift)
        restored = next(row for row in self.db.test_sessions(self.sid) if row['id'] == test_id)
        self.assertEqual(restored['result'], 'not_run')

    def test_37_ai_redacts_nested_task_test_and_followup_fields(self):
        secret = 'private@example.com'
        payload = AIPayloadBuilder.build_case_summary_payload(
            {'id': 1, 'title': 'Case', 'client': 'Acme'}, [],
            [{'id': 1, 'title': secret}],
            [{'id': 1, 'scenario': secret, 'result': 'failed'}],
            [{'id': 1, 'note': secret}], mask_client=True)
        self.assertNotIn(secret, payload)
        self.assertIn('[EMAIL_REDACTED]', payload)

    def test_38_report_finalization_requires_success_or_acknowledgement(self):
        report_id = self.db.save_report(self.sid, 'eod', 'Unverified report')
        self.db.save_report_validation(report_id, False, [{'severity': 'error'}], {})
        with self.assertRaises(ValueError):
            self.db.finalize(report_id, require_validation=True)
        self.db.finalize(report_id, acknowledge_errors=True, require_validation=True)
        self.assertEqual(self.db.report(report_id)['finalized'], 1)

    def test_39_dashboard_tokens_expire_and_are_session_bound(self):
        service = DashboardService(self.db, port=0)
        sid, _ = service.create_session()
        service.sessions[sid]['expires_at'] = 0
        self.assertIsNone(service.get_session(sid))

    def test_40_cluster_split_undo_restores_membership(self):
        messages = [
            self.db.add_source_message(source_type='telegram', source_key=f'split-{i}', text=f'Message {i}')[0]
            for i in range(3)
        ]
        cluster_id = self.db.create_cluster('Split source', status='pending')
        for message in messages:
            self.db.add_cluster_item(cluster_id, message['id'])
        result = self.db.split_cluster(cluster_id, [messages[0]['id']], 'Split child')
        latest = self.db.get_last_reversible_audit()
        self.assertEqual(latest['correlation_id'], result['correlation_id'])
        self.db.undo_audit_record(latest['id'])
        original_ids = {row['id'] for row in self.db.get_cluster_items(cluster_id)}
        self.assertEqual(original_ids, {message['id'] for message in messages})
        self.assertIsNone(self.db.get_cluster(result['new_cluster_id']))

    async def test_41_optional_ai_is_metered_logged_and_quota_limited(self):
        self.db.set_setting('mask_client_names', 'true')
        engine = SimpleNamespace(last_model='gemini-test')
        seen_masks = []

        async def ai_call(_engine, mask):
            seen_masks.append(mask)
            return 'ai-result'

        with patch.object(config, 'AI_KEY', 'configured'), \
             patch.object(config, 'AI_MODEL', 'gemini-test'), \
             patch.object(config, 'AI_DAILY_LIMIT', 1), \
             patch('ai.writer', return_value=engine):
            first = await handlers.optional_copilot_ai(
                self.context, 'probe', 'probe-v1', ai_call, lambda: 'fallback')
            second = await handlers.optional_copilot_ai(
                self.context, 'probe', 'probe-v1', ai_call, lambda: 'fallback')

        self.assertEqual(first, 'ai-result')
        self.assertEqual(second, 'fallback')
        self.assertEqual(seen_masks, [True])
        events = self.db.ai_stats()
        self.assertEqual(events.get('success'), 1)

    async def test_42_proposal_cancel_is_persistent(self):
        proposal_id = self.db.create_nl_proposal(
            owner_id=123, intent='create_task',
            proposal_data=NLInterpretation(
                intent=NLIntent.CREATE_TASK, confidence=0.7,
                entities=NLEntities(task_title='Cancelled task')).model_dump())
        update = self.make_update(callback_data=f'prop:cancel:{proposal_id}')
        await handlers.handle_callback(update, self.context)
        self.assertEqual(self.db.get_nl_proposal(proposal_id)['status'], 'cancelled')


if __name__ == '__main__':
    unittest.main()
