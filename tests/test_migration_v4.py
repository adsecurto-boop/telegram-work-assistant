"""Tests for Schema Version 4 migration, preservation, idempotence, and integrity."""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from database import Database, SCHEMA_VERSION


class TestSchemaV4Migration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / 'work.sqlite3'

    def test_clean_schema_v4_creation(self):
        db = Database(self.db_path)
        self.assertEqual(db.integrity(), 'ok')
        with db.connect() as conn:
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            self.assertEqual(version, SCHEMA_VERSION)
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            expected_new_tables = {
                'nl_interactions', 'conversation_context', 'audit_log',
                'client_aliases', 'history_clusters', 'cluster_items',
                'bulk_operations', 'shift_templates', 'shift_calendar',
                'report_provenance'
            }
            for tbl in expected_new_tables:
                self.assertIn(tbl, tables, f"Table {tbl} should exist in v4")

            # Check default shift templates were seeded
            templates = conn.execute("SELECT name, start_time, end_time FROM shift_templates ORDER BY id").fetchall()
            names = [r['name'] for r in templates]
            self.assertIn('Morning', names)
            self.assertIn('General', names)
            self.assertIn('Evening', names)

    def test_v3_to_v4_migration_preserves_data(self):
        # 1. Create a schema v3 database manually and populate it with sample data
        conn = sqlite3.connect(self.db_path)
        conn.execute('PRAGMA foreign_keys=ON')
        conn.executescript('''
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
                status TEXT NOT NULL, blocked_reason TEXT, created_at TEXT NOT NULL,
                completed_at TEXT, priority INTEGER NOT NULL DEFAULT 0,
                planned_shift_id INTEGER, due_date TEXT, project TEXT, client TEXT,
                ticket TEXT, next_action TEXT, tags TEXT, completion_note TEXT);
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE shifts (
                id INTEGER PRIMARY KEY, start TEXT NOT NULL, end TEXT NOT NULL,
                lunch TEXT, closed_at TEXT);
            CREATE TABLE activities (
                id INTEGER PRIMARY KEY, shift_id INTEGER NOT NULL REFERENCES shifts(id),
                category TEXT NOT NULL, detail TEXT NOT NULL, client TEXT, channel TEXT,
                outcome TEXT, task_id INTEGER REFERENCES tasks(id), created_at TEXT NOT NULL,
                unplanned INTEGER NOT NULL DEFAULT 0, source_message_id INTEGER, case_id INTEGER);
            CREATE TABLE clients (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, normalized TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL);
            CREATE TABLE work_cases (
                id INTEGER PRIMARY KEY, case_key TEXT UNIQUE, client_id INTEGER REFERENCES clients(id),
                title TEXT NOT NULL, product TEXT, platform TEXT, channel TEXT, ticket TEXT,
                priority INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'new',
                participation TEXT NOT NULL DEFAULT 'owned', next_action TEXT, waiting_on TEXT,
                follow_up_at TEXT, resolution TEXT, client_updated INTEGER NOT NULL DEFAULT 0,
                review_state TEXT NOT NULL DEFAULT 'approved', source TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, closed_at TEXT);
            CREATE TABLE source_messages (
                id INTEGER PRIMARY KEY, import_id INTEGER,
                source_type TEXT NOT NULL, source_key TEXT NOT NULL UNIQUE, chat_name TEXT,
                external_message_id TEXT, occurred_at TEXT, author_name TEXT,
                author_is_owner INTEGER NOT NULL DEFAULT 0, text TEXT, redacted_text TEXT,
                reply_to_key TEXT, message_kind TEXT, media_json TEXT, classification TEXT,
                metadata_json TEXT, confidence REAL NOT NULL DEFAULT 0,
                review_status TEXT NOT NULL DEFAULT 'pending', case_id INTEGER, shift_id INTEGER,
                created_at TEXT NOT NULL);
            CREATE TABLE reports (
                id INTEGER PRIMARY KEY, shift_id INTEGER NOT NULL REFERENCES shifts(id),
                kind TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL,
                finalized INTEGER NOT NULL DEFAULT 0, style TEXT NOT NULL DEFAULT 'standard',
                provider TEXT, model TEXT, prompt_version TEXT, source_report_id INTEGER);
            PRAGMA user_version = 3;
        ''')

        # Insert 7 tasks (matching the 7 existing tasks in production)
        for i in range(1, 8):
            conn.execute(
                "INSERT INTO tasks (id, title, status, created_at, priority) VALUES (?, ?, 'pending', '2026-09-09T08:00:00Z', 1)",
                (i, f"Task {i}")
            )
        # Insert 1 shift
        conn.execute("INSERT INTO shifts (id, start, end) VALUES (1, '2026-09-09T10:00:00', '2026-09-09T19:00:00')")
        # Insert 10 source messages
        for i in range(1, 11):
            conn.execute(
                "INSERT INTO source_messages (id, source_type, source_key, author_name, author_is_owner, text, created_at) VALUES (?, 'telegram', ?, 'owner', 1, 'Msg', '2026-09-09T08:00:00Z')",
                (i, f"key-{i}")
            )
        conn.commit()
        conn.close()

        # 2. Open via Database class, triggering v3 -> v4 migration
        db = Database(self.db_path)

        # 3. Check that pre-migration-v3 backup was created
        backups = list((self.root / 'backups').glob('pre-migration-v3-*.sqlite3'))
        self.assertEqual(len(backups), 1, "A pre-migration-v3 backup must be created")

        # 4. Verify integrity
        self.assertEqual(db.integrity(), 'ok')

        # 5. Check row counts preserved
        with db.connect() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 7)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM shifts').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM source_messages').fetchone()[0], 10)

    def test_re_running_initialization_is_idempotent(self):
        db1 = Database(self.db_path)
        self.assertEqual(db1.integrity(), 'ok')
        # Re-open the database; should not create new backups or fail
        db2 = Database(self.db_path)
        self.assertEqual(db2.integrity(), 'ok')
        with db2.connect() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)

    def test_audit_and_undo_cycle(self):
        db = Database(self.db_path)
        sid = db.start_shift('2026-09-09T10:00:00+05:30', '2026-09-09T19:00:00+05:30')
        task = db.add_task('Initial title', shift_id=sid)

        # Record update with audit
        old_state = {'title': 'Initial title', 'status': 'pending'}
        new_state = {'title': 'Updated title', 'status': 'in_progress'}
        db.update_task(task.id, 'title', 'Updated title', shift_id=sid)
        db.mark_status(task.id, 'in_progress', shift_id=sid)

        audit_id = db.record_audit(
            correlation_id='test-corr-1',
            operation_type='update_task',
            actor='test',
            affected_table='tasks',
            record_id=task.id,
            before_state_json=json.dumps(old_state),
            after_state_json=json.dumps(new_state)
        )

        # Undo the audit entry
        undone = db.undo_audit_record(audit_id)
        self.assertTrue(undone)

        # Verify task was reverted
        reverted_task = db.get_task(task.id)
        self.assertEqual(reverted_task.title, 'Initial title')
        self.assertEqual(reverted_task.status, 'pending')

        # Trying to undo again should raise or return False (idempotent protection)
        with self.assertRaises(ValueError):
            db.undo_audit_record(audit_id)


if __name__ == '__main__':
    unittest.main()
