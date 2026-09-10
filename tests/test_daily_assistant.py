import tempfile
import unittest
from pathlib import Path
from database import Database


class DeleteTaskTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Database(Path(tmp.name) / 'test.db')

    def test_delete_all_and_restore_history_links(self):
        sid = self.db.start_shift('2026-09-10T12:00:00+05:30','2026-09-10T21:00:00+05:30')
        task = self.db.add_task('Test', shift_id=sid)
        count, correlation = self.db.delete_tasks()
        self.assertEqual(count, 1)
        self.assertFalse(self.db.list_tasks())
        self.assertEqual(len(self.db.activities(sid)), 1)
        self.db.undo_audit_batch(correlation)
        self.assertEqual(self.db.get_task(task.id).title, 'Test')
        self.assertEqual(self.db.activities(sid)[0]['task_id'], task.id)

    def test_scope_and_empty_delete(self):
        pending = self.db.add_task('Pending')
        completed = self.db.add_task('Completed')
        self.db.mark_status(completed.id, 'completed')
        self.assertEqual(self.db.delete_tasks('pending')[0], 1)
        self.assertIsNotNone(self.db.get_task(completed.id))
        self.assertEqual(self.db.delete_tasks('pending')[0], 0)
        self.assertIsNone(self.db.get_task(pending.id))
