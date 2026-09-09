"""Regressions for mid-shift corrections and report reminders."""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import config
import handlers
import reports
import scheduler
from database import Database, SCHEMA_VERSION
from nlp import DeterministicParser, NaturalLanguagePipeline, NLIntent, NLInterpretation, NLEntities
from report_validator import ReportValidator


class ShiftConfirmationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Database(Path(tmp.name) / 'work.sqlite3')
        self.day = datetime.now(ZoneInfo(config.TIMEZONE)).date().isoformat()
        self.sid = self.db.start_shift(f'{self.day}T17:00:00+05:30',
                                       f'{self.day}T23:00:00+05:30')
        self.original = self.db.active_shift()
        self.pipeline = NaturalLanguagePipeline(self.db)
        self.context = SimpleNamespace(application=SimpleNamespace(bot_data={'db': self.db}),
                                       bot=SimpleNamespace(send_message=AsyncMock()))
        for name, value in [('OWNER_ID', 123), ('AI_KEY', '')]:
            p = patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)

    def update(self, text='', callback=None):
        msg = SimpleNamespace(text=text, reply_text=AsyncMock())
        return SimpleNamespace(effective_user=SimpleNamespace(id=123),
            effective_chat=SimpleNamespace(id=123, type='private'), update_id=101,
            effective_message=msg, callback_query=SimpleNamespace(
                data=callback, answer=AsyncMock(), message=msg) if callback else None)

    async def propose(self, text):
        update = self.update(text)
        await handlers.save_plain_message(update, self.context, text)
        markup = update.effective_message.reply_text.call_args.kwargs['reply_markup']
        return markup.inline_keyboard[0]

    def test_shift_phrasings(self):
        for text in ['hello my todays shift is from 10 am to 7 pm',
                     "change the today's shift timing to 10 am to 7 pm",
                     'change the todays shift timing to 10 am to 7 pm',
                     'update my shift hours to 10 am to 7 pm',
                     "hello my today's shift is from 10 am to 7 pm",
                     'my shift today is from 10 am to 7 pm',
                     'today my shift is from 10 am to 7 pm',
                     'my shift started at 10 am and ends at 7 pm',
                     'my shift today is 10 to 7',
                     'my shift starts at 10 am and will end at 7 pm']:
            with self.subTest(text=text):
                parsed = DeterministicParser.parse(text)
                self.assertEqual(parsed.intent, NLIntent.SET_SHIFT)
                self.assertEqual((parsed.entities.shift_start, parsed.entities.shift_end), ('10:00', '19:00'))

    async def test_exact_greeting_shift_request_displays_confirmation(self):
        buttons = await self.propose('hello my todays shift is from 10 am to 7 pm')
        self.assertEqual([b.text for b in buttons], ['Confirm', 'Cancel'])
        self.assertEqual(self.db.active_shift(), self.original)

    async def test_timing_edit_wording_asks_before_changing_shift(self):
        buttons = await self.propose("change the today's shift timing to 10 am to 7 pm")
        self.assertEqual(self.db.active_shift(), self.original)
        await handlers.handle_callback(self.update(callback=buttons[0].callback_data), self.context)
        self.assertEqual(self.db.active_shift()['start'][11:16], '10:00')
        self.assertEqual(self.db.active_shift()['end'][11:16], '19:00')

    async def test_polling_conflict_is_visible_in_health(self):
        from telegram.error import Conflict
        self.context.error = Conflict('Another getUpdates request')
        self.context.application.job_queue = None
        await handlers.error_handler(None, self.context)
        self.assertIn('last_polling_conflict', self.context.application.bot_data)
        health = await handlers.health_text(self.context)
        from nlp import NL_PARSER_VERSION
        self.assertIn(NL_PARSER_VERSION, health)
        self.assertIn(str(self.db.path.resolve()), health)
        self.assertNotIn('none in this process', health)

    async def test_ai_am_pm_times_normalized_before_confirmation_and_execution(self):
        ai = SimpleNamespace(interpret=AsyncMock(return_value=NLInterpretation(
            intent=NLIntent.SET_SHIFT, confidence=0.96, provider='gemini',
            entities=NLEntities(shift_start='10:00 AM', shift_end='07:00 PM'))))
        pipeline = NaturalLanguagePipeline(self.db, ai)
        reply, interpretation = await pipeline.process('adjust the hours I worked', self.original)
        self.assertTrue(interpretation.needs_confirmation)
        self.assertIn('10:00 to 19:00', reply)
        self.assertEqual(self.db.active_shift(), self.original)
        interpretation.needs_confirmation = False
        interpretation.clarification_question = None
        await pipeline.executor.execute(interpretation, self.original)
        self.assertEqual(self.db.active_shift()['end'][11:16], '19:00')

    async def test_ai_invalid_time_does_not_mutate(self):
        interpretation = NLInterpretation(intent=NLIntent.SET_SHIFT, confidence=0.96,
            entities=NLEntities(shift_start='25:00 AM', shift_end='7 pm'))
        with self.assertRaisesRegex(ValueError, 'Please give valid shift times'):
            await self.pipeline.executor.execute(interpretation, self.original)
        self.assertEqual(self.db.active_shift(), self.original)

    async def test_shift_time_question_is_read_only(self):
        for phrase in ('whats my shift time', "what's my shift time", 'show my shift hours'):
            reply, parsed = await self.pipeline.process(phrase, self.original)
            self.assertEqual(parsed.intent, NLIntent.SHOW_SHIFT)
            self.assertIn('05:00 PM', reply)
            self.assertIn('11:00 PM', reply)
        self.assertEqual(self.db.active_shift(), self.original)
        self.assertFalse(self.db.get_audit_log())

    async def test_extension_confirms_and_preserves_start(self):
        self.db.schedule(self.sid, f'{self.day}T19:00:00+05:30', None)
        before = self.db.active_shift()
        buttons = await self.propose('my shift has been extended to 8 pm')
        self.assertEqual(self.db.active_shift(), before)
        await handlers.handle_callback(self.update(callback=buttons[0].callback_data), self.context)
        self.assertEqual(self.db.active_shift()['start'], before['start'])
        self.assertEqual(self.db.active_shift()['end'][11:16], '20:00')
        self.db.undo_audit_record(self.db.get_last_reversible_audit()['id'])
        self.assertEqual(self.db.active_shift(), before)

    async def test_extension_without_active_shift_does_not_invent_start(self):
        self.db.close_shift(self.sid)
        reply, parsed = await self.pipeline.process('my shift has been extended to 8 pm', None)
        self.assertIn('No shift is active', reply)
        self.assertIsNone(self.db.active_shift())

    async def test_resolved_support_is_structured_and_undoable(self):
        text = 'Resolved client query of agent not tracking for whatsapp client Vikram India Limited'
        reply, parsed = await self.pipeline.process(text, self.original)
        self.assertEqual(parsed.intent, NLIntent.LOG_SUPPORT)
        activity = self.db.activities(self.sid)[0]
        self.assertEqual(activity['client'], 'Vikram India Limited')
        self.assertEqual(activity['channel'], 'whatsapp')
        self.assertEqual(activity['outcome'], 'resolved')
        self.assertEqual(activity['detail'], 'agent not tracking')
        report = reports.generate_report('eod', self.original, self.db.activities(self.sid), [])
        self.assertIn('Vikram India Limited', report)
        self.db.undo_audit_record(self.db.get_last_reversible_audit()['id'])
        self.assertFalse(self.db.activities(self.sid))
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM support_interactions').fetchone()[0], 0)

    async def test_addressed_concern_logs_assistance_without_claiming_resolution(self):
        text = 'addressed client concern about exaggerated office hours for virtual street group client'
        reply, parsed = await self.pipeline.process(text, self.original)
        self.assertEqual(parsed.intent, NLIntent.LOG_SUPPORT)
        self.assertIn('[assisted]', reply)
        activity = self.db.activities(self.sid)[0]
        self.assertEqual(activity['client'], 'virtual street group')
        self.assertEqual(activity['detail'], 'exaggerated office hours')
        self.assertEqual(activity['outcome'], 'assisted')
        self.assertIsNone(activity['channel'])
        report = reports.generate_report('eod', self.original, [activity], [])
        self.assertIn('resolved interactions: 0', report)
        self.db.undo_audit_record(self.db.get_last_reversible_audit()['id'])
        self.assertFalse(self.db.activities(self.sid))

    def test_support_variants_preserve_explicit_outcomes(self):
        for text, outcome in [
            ('I handled a client question regarding office hours for client Virtual Street Group.', 'assisted'),
            ('investigated client issue about office hours for Virtual Street Group client', 'investigated'),
            ('escalated client concern about office hours for teams client Virtual Street Group', 'escalated'),
            ('resolved client concern about office hours for Virtual Street Group client', 'resolved')]:
            with self.subTest(text=text):
                parsed = DeterministicParser.parse(text)
                self.assertEqual(parsed.intent, NLIntent.LOG_SUPPORT)
                self.assertEqual(parsed.entities.client, 'Virtual Street Group')
                self.assertEqual(parsed.entities.status, outcome)

    async def test_client_first_resolution_preserves_issue_and_channel(self):
        text = 'resolved cloud destinations client in teams for absent user issue in macos devices'
        reply, parsed = await self.pipeline.process(text, self.original)
        self.assertEqual(parsed.intent, NLIntent.LOG_SUPPORT)
        self.assertIn('[resolved]', reply)
        activity = self.db.activities(self.sid)[0]
        self.assertEqual(activity['client'], 'cloud destinations')
        self.assertEqual(activity['channel'], 'teams')
        self.assertEqual(activity['detail'], 'absent user issue in macos devices')
        self.assertEqual(activity['outcome'], 'resolved')
        report = reports.generate_report('eod', self.original, [activity], [])
        self.assertIn('resolved interactions: 1', report)
        self.assertIn('absent user issue in macos devices', report)
        self.db.undo_audit_record(self.db.get_last_reversible_audit()['id'])
        self.assertFalse(self.db.activities(self.sid))

    def test_client_first_support_variations(self):
        for text, channel, outcome in [
            ('Resolved Cloud Destinations client via Teams for absent users on macOS.', 'teams', 'resolved'),
            ('I addressed client Cloud Destinations on Microsoft Teams regarding absent users', 'teams', 'assisted'),
            ('investigated Cloud Destinations client about absent users', None, 'investigated'),
            ('escalated Cloud Destinations client through email for absent users', 'email', 'escalated')]:
            with self.subTest(text=text):
                parsed = DeterministicParser.parse(text)
                self.assertEqual(parsed.intent, NLIntent.LOG_SUPPORT)
                self.assertEqual(parsed.entities.client, 'Cloud Destinations')
                self.assertEqual(parsed.entities.channel, channel)
                self.assertEqual(parsed.entities.status, outcome)

    def test_client_first_support_requires_completed_action_and_issue(self):
        for text in ('I have not resolved Cloud Destinations client in Teams for absent users',
                     'Will resolve Cloud Destinations client in Teams for absent users',
                     'resolved Cloud Destinations client in Teams'):
            parsed = DeterministicParser.parse(text)
            self.assertTrue(parsed is None or parsed.intent != NLIntent.LOG_SUPPORT)

    def test_negated_or_planned_resolution_is_not_logged_as_resolved(self):
        for prefix in ('have not resolved', 'will resolve', 'did not resolve'):
            parsed = DeterministicParser.parse(f'I {prefix} client query about hours for client Acme')
            self.assertTrue(parsed is None or parsed.intent != NLIntent.LOG_SUPPORT)

    async def test_testing_note_waits_for_explicit_result(self):
        text = 'tested and reported silah agent 3.0.2 for auto check out issue for request 2028'
        update = self.update(text)
        await handlers.save_plain_message(update, self.context, text)
        markup = update.effective_message.reply_text.call_args.kwargs['reply_markup']
        buttons = [row[0] for row in markup.inline_keyboard]
        self.assertFalse(self.db.test_sessions(self.sid))
        self.assertEqual([b.text for b in buttons[:4]], ['Passed', 'Failed', 'Partial', 'Blocked'])
        await handlers.handle_callback(self.update(callback=buttons[1].callback_data), self.context)
        sessions = self.db.test_sessions(self.sid)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]['scenario'], text)
        self.assertEqual(sessions[0]['result'], 'failed')

    async def test_end_it_prepares_eod_without_closing_shift(self):
        update = self.update('end it')
        await handlers.save_plain_message(update, self.context, 'end it')
        self.assertEqual(self.db.active_shift(), self.original)
        markup = update.effective_message.reply_text.call_args.kwargs['reply_markup']
        self.assertTrue(markup.inline_keyboard[0][0].callback_data.startswith('close:'))
        self.assertEqual(len(self.db.history()), 1)

    def test_utc_activity_is_compared_in_shift_timezone(self):
        shift = dict(self.original, start='2026-09-09T00:15:00+05:30', end='2026-09-09T09:15:00+05:30')
        activity = dict(id=1, category='note', detail='Work', created_at='2026-09-08T19:00:00+00:00')
        result = ReportValidator.validate('eod', 'Work', shift, [activity], [])
        self.assertNotIn('RECORD_OUTSIDE_SHIFT', [w.code for w in result.warnings])
        activity['created_at'] = '2026-09-08T10:00:00+00:00'
        result = ReportValidator.validate('eod', 'Work', shift, [activity], [])
        self.assertIn('RECORD_OUTSIDE_SHIFT', [w.code for w in result.warnings])

    async def test_confirm_preserves_work_and_undo_restores_times(self):
        task = self.db.add_task('Existing work', shift_id=self.sid)
        buttons = await self.propose('my shift started at 10 am and ends at 7 pm')
        self.assertEqual(self.db.active_shift(), self.original)
        self.assertEqual([b.text for b in buttons], ['Confirm', 'Cancel'])
        await handlers.handle_callback(self.update(callback=buttons[0].callback_data), self.context)
        changed = self.db.active_shift()
        self.assertEqual(changed['id'], self.sid)
        self.assertEqual(changed['start'][11:16], '10:00')
        self.assertEqual(changed['end'][11:16], '19:00')
        self.assertEqual(self.db.get_task(task.id).planned_shift_id, self.sid)
        await handlers.handle_callback(self.update(callback=buttons[0].callback_data), self.context)
        self.assertEqual(len(self.db.get_audit_log()), 1)
        self.db.undo_audit_record(self.db.get_last_reversible_audit()['id'])
        self.assertEqual(self.db.active_shift(), self.original)

    async def test_cancel_keeps_schedule(self):
        buttons = await self.propose('my shift is from 10 am to 7 pm')
        await handlers.handle_callback(self.update(callback=buttons[1].callback_data), self.context)
        self.assertEqual(self.db.active_shift(), self.original)
        self.assertFalse(self.db.get_audit_log())

    async def test_same_times_still_require_reconfirmation(self):
        reply, parsed = await self.pipeline.process('my shift is 5 pm to 11 pm', self.original)
        self.assertTrue(parsed.needs_confirmation)
        self.assertIn('reconfirm', reply)
        self.assertEqual(self.db.active_shift(), self.original)

    async def test_stale_confirmation_is_rejected(self):
        buttons = await self.propose('my shift is from 10 am to 7 pm')
        self.db.schedule(self.sid, f'{self.day}T22:00:00+05:30', None)
        with self.assertRaisesRegex(ValueError, 'changed since'):
            await handlers.handle_callback(self.update(callback=buttons[0].callback_data), self.context)
        self.assertEqual(self.db.active_shift()['end'][11:16], '22:00')

    async def test_closed_shift_proposal_cannot_change_replacement(self):
        buttons = await self.propose('my shift is from 10 am to 7 pm')
        self.db.close_shift(self.sid)
        self.db.start_shift(f'{self.day}T18:00:00+05:30', f'{self.day}T23:00:00+05:30')
        replacement = self.db.active_shift()
        with self.assertRaisesRegex(ValueError, 'active shift changed'):
            await handlers.handle_callback(self.update(callback=buttons[0].callback_data), self.context)
        self.assertEqual(self.db.active_shift(), replacement)

    async def test_combined_shift_reminder_is_atomic_and_undoable(self):
        buttons = await self.propose('today my shift is from 10 am to 8 pm, remind me for eod at 7 pm')
        self.assertEqual(self.db.active_shift(), self.original)
        await handlers.handle_callback(self.update(callback=buttons[0].callback_data), self.context)
        changed = self.db.active_shift()
        self.assertEqual(changed['end'][11:16], '20:00')
        self.assertEqual(changed['eod_reminder'][11:16], '19:00')
        self.assertFalse(self.db.list_followups())
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM work_cases').fetchone()[0], 0)
        self.db.undo_audit_record(self.db.get_last_reversible_audit()['id'])
        self.assertEqual(self.db.active_shift(), self.original)

    async def test_out_of_shift_reminder_does_not_partially_change_shift(self):
        buttons = await self.propose('my shift is 10 am to 7 pm, remind me for eod at 9 pm')
        with self.assertRaisesRegex(ValueError, 'within the shift'):
            await handlers.handle_callback(self.update(callback=buttons[0].callback_data), self.context)
        self.assertEqual(self.db.active_shift(), self.original)

    async def test_combined_request_starts_shift_without_case(self):
        self.db.close_shift(self.sid)
        reply, parsed = await self.pipeline.process(
            'today my shift is from 10 am to 8 pm, remind me for eod at 7 pm', None)
        self.assertEqual(parsed.intent, NLIntent.SET_SHIFT)
        self.assertIn('EOD reminder 19:00', reply)
        self.assertEqual(self.db.active_shift()['eod_reminder'][11:16], '19:00')
        self.assertFalse(self.db.list_followups())

    async def test_overnight_confirmation_keeps_shift_date(self):
        buttons = await self.propose('my shift is 10 pm to 7 am')
        await handlers.handle_callback(self.update(callback=buttons[0].callback_data), self.context)
        shift = self.db.active_shift()
        self.assertEqual(shift['start'][:10], self.day)
        self.assertEqual(datetime.fromisoformat(shift['end']) - datetime.fromisoformat(shift['start']),
                         timedelta(hours=9))

    async def test_reminder_uses_custom_time_and_delivers_once(self):
        buttons = await self.propose('my shift is 10 am to 8 pm, remind me for eod at 7 pm')
        await handlers.handle_callback(self.update(callback=buttons[0].callback_data), self.context)
        self.db.record_delivery(self.sid, 'tod')
        with patch.object(scheduler, 'datetime', wraps=datetime) as clock, patch.object(config, 'REMINDERS_ENABLED', True):
            clock.now.return_value = datetime.fromisoformat(f'{self.day}T18:59:00+05:30')
            await scheduler.tick(self.context)
            self.context.bot.send_message.assert_not_awaited()
            clock.now.return_value = datetime.fromisoformat(f'{self.day}T19:00:00+05:30')
            await scheduler.tick(self.context)
            self.assertIn('/eod', self.context.bot.send_message.call_args.kwargs['text'])
            await scheduler.tick(self.context)
            self.context.bot.send_message.assert_awaited_once()

    def test_empty_report_does_not_claim_testing(self):
        text = reports.generate_report('eod', self.original, [], [])
        result = ReportValidator.validate('eod', text, self.original, [], [])
        self.assertNotIn('UNVERIFIED_TEST_CLAIM', [w.code for w in result.warnings])
        result = ReportValidator.validate('eod', 'Testing done. Reproduced the bug.', self.original, [], [])
        self.assertIn('UNVERIFIED_TEST_CLAIM', [w.code for w in result.warnings])

    def test_v5_upgrade_preserves_shift_and_creates_backup(self):
        with self.db.connect() as conn:
            conn.execute('ALTER TABLE shifts DROP COLUMN eod_reminder')
            conn.execute('PRAGMA user_version=5')
        upgraded = Database(self.db.path)
        self.assertEqual(upgraded.active_shift(), self.original)
        self.assertTrue(list((self.db.path.parent / 'backups').glob('pre-migration-v5-*.sqlite3')))
        with upgraded.connect() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
