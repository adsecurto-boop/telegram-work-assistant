"""Unit tests for audit logging, safe undo, and bulk operations."""
import json
import tempfile
import unittest
from pathlib import Path

from database import Database
from models import TaskStatus


class TestAuditAndUndo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / 'work.sqlite3')
        self.sid = self.db.start_shift('2026-09-09T10:00:00+05:30', '2026-09-09T19:00:00+05:30')

    def test_bulk_accept_as_tasks_and_undo(self):
        m1, _ = self.db.add_source_message(
            source_type='telegram', source_key='m-1', text='Task 1 item',
            author_is_owner=True, review_status='pending', shift_id=self.sid
        )
        m2, _ = self.db.add_source_message(
            source_type='telegram', source_key='m-2', text='Task 2 item',
            author_is_owner=True, review_status='pending', shift_id=self.sid
        )

        res = self.db.bulk_accept_as_tasks([m1['id'], m2['id']], shift_id=self.sid, priority=2)
        self.assertEqual(res['affected_count'], 2)
        corr_id = res['correlation_id']

        # Check tasks exist and messages are accepted
        tasks = self.db.list_tasks()
        self.assertEqual(len(tasks), 2)
        self.assertEqual(self.db.source_message(m1['id'])['review_status'], 'accepted')
        self.assertEqual(self.db.source_message(m2['id'])['review_status'], 'accepted')

        # Undo the bulk operation
        undo_res = self.db.undo_bulk_operation(corr_id)
        self.assertEqual(undo_res['correlation_id'], corr_id)

        # Verify tasks were removed and messages reverted to pending
        self.assertEqual(len(self.db.list_tasks()), 0)
        self.assertEqual(self.db.source_message(m1['id'])['review_status'], 'pending')
        self.assertEqual(self.db.source_message(m2['id'])['review_status'], 'pending')

    def test_bulk_ignore_and_undo(self):
        m1, _ = self.db.add_source_message(
            source_type='telegram', source_key='ign-1', text='Ignore me',
            author_is_owner=True, review_status='pending', shift_id=self.sid
        )
        res = self.db.bulk_ignore([m1['id']])
        self.assertEqual(res['affected_count'], 1)
        self.assertEqual(self.db.source_message(m1['id'])['review_status'], 'ignored')

        self.db.undo_bulk_operation(res['correlation_id'])
        self.assertEqual(self.db.source_message(m1['id'])['review_status'], 'pending')

    def test_safe_undo_rejects_modified_record(self):
        task = self.db.add_task('Initial title', shift_id=self.sid)

        # Audit an update
        audit_id = self.db.record_audit(
            correlation_id='corr-mod',
            operation_type='update_task',
            actor='test',
            affected_table='tasks',
            record_id=task.id,
            before_state_json=json.dumps({'title': 'Initial title'}),
            after_state_json=json.dumps({'title': 'Intermediate title'})
        )
        self.db.update_task(task.id, 'title', 'Intermediate title')

        # Now someone modifies the task again externally!
        self.db.update_task(task.id, 'title', 'Subsequent title')

        # Trying to undo the earlier update must be rejected because record changed
        with self.assertRaises(ValueError) as ctx:
            self.db.undo_audit_record(audit_id)
        self.assertIn('has changed since this operation', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
