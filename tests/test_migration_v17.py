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
                conn.execute('PRAGMA user_version=16')
            upgraded = Database(path)
            self.assertEqual(SCHEMA_VERSION, 17)
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
