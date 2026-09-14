import sqlite3
import tempfile
import unittest
from pathlib import Path

from database import Database, SCHEMA_VERSION


class MigrationV17Tests(unittest.TestCase):
    def test_v16_upgrade_preserves_turn_and_creates_backup(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            path = Path(temp) / 'work.sqlite3'
            db = Database(path)
            db.record_conversation_turn(1, 'user', 'preserved')
            with sqlite3.connect(path) as conn:
                # Rebuild this one table exactly as it existed in v16.  The
                # rest of the database is deliberately left intact so the
                # fixture exercises the real upgrade path rather than merely
                # changing PRAGMA user_version on an already-v17 table.
                conn.execute('ALTER TABLE conversation_turns RENAME TO conversation_turns_v17')
                conn.execute('''CREATE TABLE conversation_turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER NOT NULL,
                    shift_id INTEGER, role TEXT NOT NULL, text TEXT NOT NULL,
                    intent TEXT, entities_json TEXT, case_id INTEGER, task_id INTEGER,
                    test_session_id INTEGER, source_update_id INTEGER,
                    correlation_id TEXT, created_at TEXT NOT NULL)''')
                conn.execute('''INSERT INTO conversation_turns
                    (id, owner_id, shift_id, role, text, intent, entities_json, case_id,
                     task_id, test_session_id, source_update_id, correlation_id, created_at)
                    SELECT id, owner_id, shift_id, role, text, intent, entities_json, case_id,
                           task_id, test_session_id, source_update_id, correlation_id, created_at
                    FROM conversation_turns_v17''')
                conn.execute('DROP TABLE conversation_turns_v17')
                conn.execute('PRAGMA user_version=16')
            upgraded = Database(path)
            self.assertEqual(SCHEMA_VERSION, 19)
            self.assertEqual(upgraded.get_recent_turns(1)[0]['text'], 'preserved')
            with sqlite3.connect(path) as conn:
                columns = {row[1] for row in conn.execute('PRAGMA table_info(conversation_turns)')}
                self.assertTrue({'thread_id', 'source_channel', 'source_message_id', 'client_message_id'} <= columns)
                self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertTrue(list((Path(temp) / 'backups').glob('pre-migration-v16-*.sqlite3')))
            del upgraded, db

    def test_client_message_replay_is_unique(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Database(Path(temp) / 'work.sqlite3')
            first = db.record_conversation_turn(1, 'user', 'hello', source_channel='web', client_message_id='web:x')
            self.assertEqual(first, db.record_conversation_turn(1, 'user', 'hello', source_channel='web', client_message_id='web:x'))
