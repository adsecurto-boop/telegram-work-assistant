"""Comprehensive Regression Tests for Personal Work Assistant Correctness & Hardening.

Covers:
- Component 1: Report Finalization Staleness & Immutability Guards (DEFECT-1)
- Component 2: Scheduler State Isolation & Non-Suppressed Catchup (DEFECT-2 & DEFECT-7)
- Component 3: Conversation Context Data Preservation (DEFECT-3)
- Component 4: Evidence Pruning Transactional Safety (DEFECT-4)
- Component 6: Durable Conversation Memory & Schema v12
- Component 7: Bounded Assistant Context Builder & Secret Redaction
- Component 10: Report Validator Nullable Client, Masked Client, & Overnight Shift Scope
"""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import config
from database import Database, SCHEMA_VERSION
from maintenance import prune_evidence_files
from memory_service import build_assistant_context, format_context_for_prompt
from models import TaskStatus
from report_validator import ReportValidator
from reports import compute_shift_facts_hash, generate_report
import scheduler


class StaleReportFinalizationTests(unittest.TestCase):
    """Component 1: Verification that stale reports cannot be finalized without explicit override."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / 'work.sqlite3')
        self.sid = self.db.start_shift('2026-09-11T09:00:00+05:30', '2026-09-11T18:00:00+05:30')
        self.db.add_task('Write specs', client='Acme', shift_id=self.sid)
        self.db.add_activity(self.sid, 'support', 'Assisted Acme with API integration', client='Acme', outcome='resolved')

    def _create_report(self, style='standard'):
        shift = self.db.active_shift()
        activities = self.db.activities(self.sid)
        tasks = self.db.tasks_for_shift(self.sid)
        text = generate_report('eod', shift, activities, tasks, style=style)
        h = self.db.compute_live_facts_hash(self.sid)
        rep_id = self.db.save_report(self.sid, 'eod', text, style=style, facts_hash=h)
        return self.db.report(rep_id)

    def test_fresh_report_can_finalize(self):
        """A valid report with matching facts hash can be finalized."""
        rep = self._create_report()
        # Validate report
        self.db.save_report_validation(rep['id'], 1, '[]', '[]')
        self.db.finalize(rep['id'], require_validation=True)
        finalized_row = self.db.report(rep['id'])
        self.assertEqual(finalized_row['finalized'], 1)

    def test_stale_report_cannot_finalize(self):
        """If an activity is added after report generation, finalize() rejects it."""
        rep = self._create_report()
        self.db.save_report_validation(rep['id'], 1, '[]', '[]')

        # Add late activity — this invalidates underlying shift facts
        self.db.add_activity(self.sid, 'testing', 'Tested late build', outcome='verified')

        with self.assertRaises(ValueError) as cm:
            self.db.finalize(rep['id'], require_validation=True)
        self.assertIn('stale', str(cm.exception).lower())

        # Report must remain unfinalized
        row = self.db.report(rep['id'])
        self.assertEqual(row['finalized'], 0)
        self.assertEqual(row['is_stale'], 1)

    def test_report_live_hash_checked_at_finalization(self):
        """Direct database mutations to tasks/cases are detected via live facts hash at finalization."""
        rep = self._create_report()
        self.db.save_report_validation(rep['id'], 1, '[]', '[]')

        # Mutate task title directly in SQLite without calling mark_report_stale
        with self.db.connect() as conn:
            conn.execute("UPDATE tasks SET title='Changed title' WHERE planned_shift_id=?", (self.sid,))

        with self.assertRaises(ValueError) as cm:
            self.db.finalize(rep['id'], require_validation=True)
        self.assertIn('stale', str(cm.exception).lower())

    def test_acknowledge_stale_override(self):
        """acknowledge_stale=True permits emergency finalization of stale reports."""
        rep = self._create_report()
        self.db.save_report_validation(rep['id'], 1, '[]', '[]')
        self.db.add_activity(self.sid, 'testing', 'Late activity')

        # Attempting without flag fails
        with self.assertRaises(ValueError):
            self.db.finalize(rep['id'], require_validation=True)

        # Attempting with acknowledge_stale=True succeeds
        self.db.finalize(rep['id'], require_validation=True, acknowledge_stale=True)
        self.assertEqual(self.db.report(rep['id'])['finalized'], 1)

    def test_revision_report_with_frozen_snapshot_can_finalize(self):
        """A wording revision preserves its frozen snapshot and can finalize even when late activities exist."""
        rep = self._create_report()
        # Late activity
        self.db.add_activity(self.sid, 'testing', 'Late production test')

        # Create revision from original report
        rev_id = self.db.create_report_revision(
            rep['id'], 'Shorter text for Acme support', style='concise',
            facts_hash=rep['facts_hash'], facts_snapshot=rep.get('facts_snapshot_json')
        )
        self.db.save_report_validation(rev_id, 1, '[]', '[]')

        # Revision report is not stale because its frozen snapshot intentionally scopes to earlier facts
        self.db.finalize(rev_id, require_validation=True)
        self.assertEqual(self.db.report(rev_id)['finalized'], 1)


class SchedulerStateIsolationTests(unittest.IsolatedAsyncioTestCase):
    """Component 2: Scheduler state isolation and non-suppressed catchup."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / 'work.sqlite3')

    async def test_scheduler_state_isolated_between_contexts(self):
        """Two separate application contexts have independent last_tick_time in bot_data."""
        ctx1 = SimpleNamespace(
            application=SimpleNamespace(bot_data={'db': self.db}),
            bot=SimpleNamespace(send_message=AsyncMock())
        )
        ctx2 = SimpleNamespace(
            application=SimpleNamespace(bot_data={'db': self.db}),
            bot=SimpleNamespace(send_message=AsyncMock())
        )

        with patch.object(config, 'REMINDERS_ENABLED', True):
            await scheduler.tick(ctx1)
            self.assertIn('last_tick_time', ctx1.application.bot_data)
            self.assertNotIn('last_tick_time', ctx2.application.bot_data)

            await scheduler.tick(ctx2)
            self.assertIn('last_tick_time', ctx2.application.bot_data)

    async def test_catchup_sends_all_overdue_items(self):
        """When resuming after shift end, overdue items are not silently stripped."""
        # Shift that ended 1 hour ago
        start = (datetime.now(timezone.utc) - timedelta(hours=9)).isoformat()
        lunch = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        end = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        sid = self.db.start_shift(start, end, lunch=lunch)

        ctx = SimpleNamespace(
            application=SimpleNamespace(bot_data={
                'db': self.db,
                # Simulate last tick 2 hours ago (triggers is_resume)
                'last_tick_time': datetime.now(timezone.utc) - timedelta(hours=2)
            }),
            bot=SimpleNamespace(send_message=AsyncMock())
        )

        with patch.object(config, 'REMINDERS_ENABLED', True), \
             patch.object(config, 'OWNER_ID', 12345):
            await scheduler.tick(ctx)

            # Verification: combined message was sent
            self.assertTrue(ctx.bot.send_message.called)
            sent_text = ctx.bot.send_message.call_args.kwargs['text']
            # Combined message includes all overdue reminders
            self.assertIn('/tod', sent_text)
            self.assertIn('/pl', sent_text)
            self.assertIn('/eod', sent_text)

            # All three deliveries recorded
            self.assertTrue(self.db.delivered(sid, 'tod'))
            self.assertTrue(self.db.delivered(sid, 'pl'))
            self.assertTrue(self.db.delivered(sid, 'eod'))


class ConversationContextTests(unittest.TestCase):
    """Component 3: Conversation context shape and custom data persistence."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / 'work.sqlite3')

    def test_canonical_shape_includes_data_dict(self):
        """get_conversation_context returns canonical shape with nested 'data' key."""
        ctx = self.db.get_conversation_context('owner')
        self.assertIn('data', ctx)
        self.assertIsInstance(ctx['data'], dict)

    def test_conversation_context_custom_data_survives_updates(self):
        """Custom context fields in data survive subsequent update_conversation_context calls."""
        self.db.update_conversation_context(
            'owner',
            active_client='Acme',
            context_data={'pending_clarification': 'task_select', 'candidate_ids': [1, 2]}
        )

        ctx1 = self.db.get_conversation_context('owner')
        self.assertEqual(ctx1['active_client'], 'Acme')
        self.assertEqual(ctx1['data'].get('pending_clarification'), 'task_select')
        self.assertEqual(ctx1['data'].get('candidate_ids'), [1, 2])

        # Second update modifying only pointer should preserve existing custom data
        task = self.db.add_task('Context test task')
        self.db.update_conversation_context('owner', active_task_id=task.id)

        ctx2 = self.db.get_conversation_context('owner')
        self.assertEqual(ctx2['active_task_id'], task.id)
        self.assertEqual(ctx2['active_client'], 'Acme')
        self.assertEqual(ctx2['data'].get('pending_clarification'), 'task_select')
        self.assertEqual(ctx2['data'].get('candidate_ids'), [1, 2])

    def test_context_data_round_trip(self):
        """Complete round-trip of complex nested data."""
        complex_data = {
            'clarification': {'type': 'choice', 'step': 2},
            'filters': ['active', 'urgent'],
            'count': 5
        }
        self.db.update_conversation_context('owner', context_data=complex_data)
        ctx = self.db.get_conversation_context('owner')
        self.assertEqual(ctx['data']['clarification']['type'], 'choice')
        self.assertEqual(ctx['data']['filters'], ['active', 'urgent'])
        self.assertEqual(ctx['data']['count'], 5)


class EvidencePruningSafetyTests(unittest.TestCase):
    """Component 4: Evidence pruning transactional safety."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / 'work.sqlite3')
        self.evidence_dir = self.root / 'evidence'
        self.evidence_dir.mkdir()

    def test_prune_failure_consistency(self):
        """Even if unlink raises OSError, DB path is set to NULL and does not crash."""
        file_path = self.evidence_dir / 'test_evidence.png'
        file_path.write_bytes(b'dummy_content')

        # Insert evidence row with old timestamp
        old_time = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        with self.db.connect() as conn:
            ev_id = conn.execute(
                "INSERT INTO evidence (path, kind, created_at) VALUES (?, 'image', ?)",
                (str(file_path), old_time)
            ).lastrowid

        # Patch unlink to raise PermissionError
        with patch.object(Path, 'unlink', side_effect=PermissionError('Access denied')):
            pruned = prune_evidence_files(self.db, self.evidence_dir, retention_days=30)
            self.assertEqual(pruned, [])

        # DB path must still be nulled (consistency preserved)
        with self.db.connect() as conn:
            row = conn.execute("SELECT path FROM evidence WHERE id=?", (ev_id,)).fetchone()
            self.assertIsNone(row['path'])


class DurableConversationMemoryTests(unittest.TestCase):
    """Component 6: Durable Conversation Memory & Schema v12."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'work.sqlite3'
        self.db = Database(self.path)

    def test_schema_version_is_12(self):
        """PRAGMA user_version is 12."""
        self.assertEqual(SCHEMA_VERSION, 12)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 12)

    def test_conversation_turns_persist(self):
        """record_conversation_turn persists turns and returns unique IDs."""
        t1 = self.db.record_conversation_turn(
            owner_id=101, role='user', text='Start shift 9 to 6', intent='set_shift'
        )
        t2 = self.db.record_conversation_turn(
            owner_id=101, role='assistant', text='Shift proposed for 09:00–18:00'
        )
        self.assertGreater(t2, t1)
        self.assertEqual(self.db.count_conversation_turns(101), 2)

    def test_recent_conversation_context_retrieval(self):
        """get_recent_turns retrieves turns in chronological order with limit."""
        for i in range(10):
            self.db.record_conversation_turn(
                owner_id=101, role='user' if i % 2 == 0 else 'assistant',
                text=f'Turn message {i}'
            )

        recent = self.db.get_recent_turns(owner_id=101, limit=5)
        self.assertEqual(len(recent), 5)
        # Oldest of the 5 first, newest last
        self.assertEqual(recent[0]['text'], 'Turn message 5')
        self.assertEqual(recent[-1]['text'], 'Turn message 9')

    def test_dialogue_memory_survives_restart(self):
        """Re-instantiating Database preserves all conversation turns."""
        self.db.record_conversation_turn(101, 'user', 'Important instruction')
        db2 = Database(self.path)
        turns = db2.get_recent_turns(101)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]['text'], 'Important instruction')


class AssistantContextBuilderTests(unittest.TestCase):
    """Component 7: Assistant Context Builder and Secret Redaction."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / 'work.sqlite3')
        self.sid = self.db.start_shift('2026-09-11T09:00:00+05:30', '2026-09-11T18:00:00+05:30')
        self.db.add_task('Urgent security task', client='SecretClient')

    def test_ai_context_is_bounded(self):
        """Context limits turns and items strictly without dumping whole database."""
        for i in range(50):
            self.db.record_conversation_turn(101, 'user', f'Message {i}', shift_id=self.sid)

        ctx = build_assistant_context(self.db, owner_id=101, conversation_limit=6, retrieval_limit=4)
        self.assertEqual(len(ctx['recent_turns']), 6)
        self.assertLessEqual(len(ctx['pending_tasks']), 4)
        self.assertIsNotNone(ctx['shift'])

    def test_ai_context_redacts_secrets(self):
        """Sensitive tokens and keys in turns or tasks are redacted."""
        self.db.record_conversation_turn(
            101, 'user', 'Deploy with api_key: AIzaSyD12345SecretKey and pass: topsecret',
            shift_id=self.sid
        )
        ctx = build_assistant_context(self.db, owner_id=101, conversation_limit=5)
        turn_text = ctx['recent_turns'][0]['text']
        self.assertNotIn('AIzaSyD12345SecretKey', turn_text)
        self.assertIn('[REDACTED]', turn_text)

    def test_format_context_for_prompt(self):
        """Formatted prompt is concise and capped at max_chars."""
        self.db.record_conversation_turn(101, 'user', 'Hello assistant', shift_id=self.sid)
        ctx = build_assistant_context(self.db, owner_id=101)
        formatted = format_context_for_prompt(ctx, max_chars=500)
        self.assertIn('Active Shift:', formatted)
        self.assertIn('Recent Conversation:', formatted)
        self.assertLessEqual(len(formatted), 550)


class ReportValidatorRegressionsTests(unittest.TestCase):
    """Component 10: Report Validator Nullable Client, Masked Client, & Overnight Shift Scope."""

    def setUp(self):
        self.shift = {
            'id': 1,
            'start': '2026-09-11T20:00:00+05:30',
            'lunch': None,
            'end': '2026-09-12T05:00:00+05:30'
        }

    def test_report_validator_nullable_client(self):
        """Activities with client=None do not raise AttributeError during validation."""
        activities = [
            {'id': 1, 'category': 'support', 'client': None, 'detail': 'Internal query', 'outcome': 'resolved'},
            {'id': 2, 'category': 'support', 'client': '   ', 'detail': 'Empty client', 'outcome': 'resolved'},
        ]
        text = "Shift 20:00–05:00\nAccomplishments:\n• Internal query [resolved]\nRemaining:\n• None"
        result = ReportValidator.validate('eod', text, self.shift, activities, [])
        self.assertIsInstance(result.is_valid, bool)

    def test_masked_client_validation(self):
        """Masked client names like 'Client 10' or '[CLIENT_REDACTED]' are not flagged as hallucinated."""
        activities = [
            {'id': 1, 'category': 'support', 'client': 'SecretCorp', 'detail': 'Fixed bug', 'outcome': 'resolved'}
        ]
        text = (
            "Shift 20:00–05:00\nAccomplishments:\n"
            "• Handled query for Client 10: Fixed bug\n"
            "• Assisted [CLIENT_REDACTED]\n"
            "Remaining:\n• None"
        )
        result = ReportValidator.validate('eod', text, self.shift, activities, [])
        hallucinated = [w for w in result.warnings if w.code == 'HALLUCINATED_CLIENT']
        self.assertEqual(len(hallucinated), 0)

    def test_overnight_shift_scope(self):
        """Activity outside overnight shift datetime window is flagged RECORD_OUTSIDE_SHIFT."""
        activities = [
            # 12 hours before shift started
            {'id': 1, 'category': 'support', 'client': 'Acme', 'detail': 'Daytime test',
             'occurred_at': '2026-09-11T08:00:00+05:30', 'outcome': 'resolved'}
        ]
        text = "Shift 20:00–05:00\nAccomplishments:\n• Handled Acme query\nRemaining:\n• None"
        result = ReportValidator.validate('eod', text, self.shift, activities, [])
        outside_warnings = [w for w in result.warnings if w.code == 'RECORD_OUTSIDE_SHIFT']
        self.assertEqual(len(outside_warnings), 1)


if __name__ == '__main__':
    unittest.main()
