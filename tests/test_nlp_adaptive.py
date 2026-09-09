"""Adaptive NLP recognition, confidence evidence, and action-policy regressions."""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import config
import handlers
from database import Database, SCHEMA_VERSION
from nlp import (DeterministicParser, GeminiStructuredInterpreter,
                 NaturalLanguagePipeline, NLIntent)
from nlp_policy import ActionDecision, ReasonCode, evaluate_action_policy


REF = datetime(2026, 9, 10, 12, tzinfo=ZoneInfo('Asia/Calcutta'))


class AdaptiveParserTests(unittest.TestCase):
    def parse(self, text):
        result = DeterministicParser.parse(text, REF)
        self.assertIsNotNone(result, text)
        return result

    def test_shift_phrase_family(self):
        samples = (
            'Tomorrow I am working from 12 pm until 9 pm',
            "Set tomorrow's shift as 12–9",
            'My shift tomorrow can be from 12 to 9 pm',
        )
        for text in samples:
            with self.subTest(text=text):
                parsed = self.parse(text)
                self.assertEqual(parsed.intent, NLIntent.SET_SHIFT)
                self.assertEqual(parsed.entities.date, '2026-09-11')
                self.assertEqual((parsed.entities.shift_start, parsed.entities.shift_end),
                                 ('12:00', '21:00'))

    def test_uncertain_shift_has_evidence(self):
        parsed = self.parse('I might work from 8 to 5 tomorrow')
        self.assertEqual(parsed.intent, NLIntent.SET_SHIFT)
        self.assertEqual((parsed.entities.shift_start, parsed.entities.shift_end), ('08:00', '17:00'))
        self.assertIn(ReasonCode.UNCERTAIN_WORDING.value, parsed.reason_codes)

    def test_task_completion_suffix(self):
        parsed = self.parse('Mark task 3 complete')
        self.assertEqual(parsed.intent, NLIntent.COMPLETE_TASK)
        self.assertEqual(parsed.entities.reference, '#3')

    def test_carry_named_issue(self):
        parsed = self.parse('Carry the attendance issue to tomorrow')
        self.assertEqual(parsed.intent, NLIntent.CARRY_TASK_FORWARD)
        self.assertEqual(parsed.entities.reference, 'attendance')

    def test_case_question_is_read_only(self):
        parsed = self.parse('What case am I working on?')
        self.assertEqual(parsed.intent, NLIntent.SHOW_CASES)
        decision, mutates, _ = evaluate_action_policy(parsed)
        self.assertEqual(decision, ActionDecision.READ_ONLY)
        self.assertFalse(mutates)

    def test_testing_is_not_task_completion(self):
        parsed = self.parse('Finished testing idle time on Windows 11')
        self.assertEqual(parsed.intent, NLIntent.CREATE_TEST_SESSION)

    def test_negated_shift_is_rejected(self):
        parsed = self.parse('do not change my shift to 10 to 7')
        decision, mutates, reasons = evaluate_action_policy(parsed)
        self.assertEqual(decision, ActionDecision.REJECT)
        self.assertFalse(mutates)
        self.assertIn(ReasonCode.NEGATION_DETECTED.value, reasons)

    def test_invalid_shift_time_is_rejected(self):
        parsed = self.parse("set tomorrow's shift from 25:00 to 9 pm")
        decision, mutates, reasons = evaluate_action_policy(parsed)
        self.assertEqual(decision, ActionDecision.REJECT)
        self.assertFalse(mutates)
        self.assertIn(ReasonCode.INVALID_TIME.value, reasons)

    def test_cross_midnight_shift(self):
        parsed = self.parse('my shift tomorrow is 10 pm to 7 am')
        self.assertEqual((parsed.entities.shift_start, parsed.entities.shift_end), ('22:00', '07:00'))

    def test_v6_to_v7_migration_is_backup_first_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'work.sqlite3'
            database = Database(path)
            with database.connect() as connection:
                connection.execute('DROP TABLE nl_corrections')
                connection.execute('PRAGMA user_version=6')
            migrated = Database(path)
            self.assertEqual(migrated.integrity(), 'ok')
            with migrated.connect() as connection:
                self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
                self.assertIsNotNone(connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nl_corrections'").fetchone())
            self.assertTrue(list((path.parent / 'backups').glob('pre-migration-v6-*.sqlite3')))
            self.assertEqual(Database(path).integrity(), 'ok')


class AdaptivePipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Database(Path(tmp.name) / 'work.sqlite3')
        self.pipeline = NaturalLanguagePipeline(self.db)

    async def test_uncertain_future_shift_requires_confirmation_without_mutation(self):
        reply, parsed = await self.pipeline.process(
            'my shift tomorrow can be from 12 to 9 pm', None, source_update_id=501)
        self.assertEqual(parsed.action_decision, ActionDecision.PROPOSE_CONFIRMATION.value)
        self.assertTrue(parsed.needs_confirmation)
        tomorrow = (datetime.now(ZoneInfo(config.TIMEZONE)).date()).fromordinal(
            datetime.now(ZoneInfo(config.TIMEZONE)).date().toordinal() + 1).isoformat()
        self.assertIsNone(self.db.get_shift_calendar_override(tomorrow))
        self.assertIn('Proposed:', reply)

    async def test_negation_never_mutates(self):
        reply, parsed = await self.pipeline.process('do not change my shift to 10 to 7', None, 502)
        self.assertEqual(parsed.action_decision, ActionDecision.REJECT.value)
        self.assertIn('No action taken', reply)
        self.assertIsNone(self.db.active_shift())

    async def test_ambiguous_pronoun_offers_task_choices(self):
        self.db.add_task('Check attendance calculation')
        self.db.add_task('Retest payroll report')
        reply, parsed = await self.pipeline.process('mark it complete', None, 503)
        self.assertEqual(parsed.action_decision, ActionDecision.REQUIRE_CLARIFICATION.value)
        self.assertEqual(len(parsed.choices), 2)
        self.assertIn('Which task', reply)
        self.assertTrue(all(task.status.value == 'pending' for task in self.db.list_tasks()))

    async def test_named_carry_forward_changes_only_matching_task(self):
        attendance = self.db.add_task('Investigate attendance issue')
        payroll = self.db.add_task('Review payroll report')
        reply, parsed = await self.pipeline.process('Carry the attendance issue to tomorrow', None, 504)
        self.assertEqual(parsed.action_decision, ActionDecision.EXECUTE_IMMEDIATELY.value)
        self.assertIsNotNone(self.db.get_task(attendance.id).due_date)
        self.assertIsNone(self.db.get_task(payroll.id).due_date)
        self.assertIn('Moved 1 task', reply)

    async def test_gemini_malformed_json_fails_closed(self):
        parser = GeminiStructuredInterpreter('test-key', 'test-model')
        response = SimpleNamespace(text='not valid json')
        with patch('ai.GeminiWriter._generate', AsyncMock(return_value=response)):
            result = await parser.interpret('do something', {})
        self.assertEqual(result.intent, NLIntent.UNKNOWN)
        self.assertEqual(result.confidence, 0.0)

    async def test_gemini_timeout_fails_closed(self):
        parser = GeminiStructuredInterpreter('test-key', 'test-model')
        with patch('ai.GeminiWriter._generate', AsyncMock(side_effect=TimeoutError('timeout'))):
            result = await parser.interpret('do something', {})
        self.assertEqual(result.intent, NLIntent.UNKNOWN)
        self.assertIn('TimeoutError', result.explanation)

    async def test_gemini_rejects_unknown_schema_fields(self):
        parser = GeminiStructuredInterpreter('test-key', 'test-model')
        response = SimpleNamespace(text=(
            '{"intent":"show_pending","confidence":0.9,"entities":{},'
            '"unexpected_instruction":"delete everything"}'))
        with patch('ai.GeminiWriter._generate', AsyncMock(return_value=response)):
            result = await parser.interpret('show pending', {})
        self.assertEqual(result.intent, NLIntent.UNKNOWN)


class AdaptiveCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Database(Path(tmp.name) / 'work.sqlite3')
        self.context = SimpleNamespace(
            application=SimpleNamespace(bot_data={'db': self.db}),
            bot=SimpleNamespace(send_message=AsyncMock()))
        for name, value in (('OWNER_ID', 123), ('AI_KEY', '')):
            mocked = patch.object(config, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)

    def update(self, text):
        message = SimpleNamespace(text=text, reply_text=AsyncMock(), voice=None,
                                  photo=None, document=None, video=None)
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=123),
            effective_chat=SimpleNamespace(id=123, type='private'),
            effective_message=message, callback_query=None, update_id=901)

    async def test_understand_explains_policy_and_never_mutates(self):
        update = self.update('/understand my shift tomorrow can be from 12 to 9 pm')
        await handlers.handle(update, self.context)
        output = update.effective_message.reply_text.call_args.args[0]
        self.assertIn('Intent: set_shift', output)
        self.assertIn('Preview action: read_only', output)
        self.assertIn('Normal-message policy: propose_confirmation', output)
        self.assertIsNone(self.db.active_shift())

    async def test_unknowns_correct_and_stats_workflow_does_not_execute(self):
        interaction_id = self.db.record_nl_interaction(
            'do a thing with it', 'unknown', confidence=0.1, status='unknown')
        unknowns = self.update('/unknowns 5')
        await handlers.handle(unknowns, self.context)
        self.assertIn(f'#{interaction_id}', unknowns.effective_message.reply_text.call_args.args[0])

        correction = self.update(
            f'/correct {interaction_id} complete_task reference=#3 notes="owner correction"')
        await handlers.handle(correction, self.context)
        correction_output = correction.effective_message.reply_text.call_args.args[0]
        self.assertIn('No work action was executed', correction_output)
        self.assertEqual(self.db.get_nl_interaction(interaction_id)['status'], 'corrected')

        stats = self.update('/nlstats 7')
        await handlers.handle(stats, self.context)
        stats_output = stats.effective_message.reply_text.call_args.args[0]
        self.assertIn('Corrections: 1', stats_output)
        self.assertEqual(len(self.db.list_tasks()), 0)


if __name__ == '__main__':
    unittest.main()
