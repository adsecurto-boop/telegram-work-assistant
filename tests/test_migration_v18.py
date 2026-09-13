import sqlite3
import tempfile
import unittest
from pathlib import Path

from database import Database, SCHEMA_VERSION


class MigrationV18Tests(unittest.TestCase):
    def test_v17_upgrade_creates_backup_and_conversation_message_requests(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            path = Path(temp) / 'work.sqlite3'
            # Initialize a modern database first to establish full tables
            db = Database(path)
            db.record_conversation_turn(1, 'user', 'v17-preserved-message',
                                        source_channel='web', client_message_id='web:v17-id')

            # Revert to a realistic v17 state:
            # 1. Drop conversation_message_requests
            # 2. Set PRAGMA user_version = 17
            with sqlite3.connect(path) as conn:
                conn.execute('DROP TABLE IF EXISTS conversation_message_requests')
                conn.execute('PRAGMA user_version = 17')
                chk = conn.execute('PRAGMA integrity_check').fetchone()[0]
                self.assertEqual(chk, 'ok')

            # Now trigger the v17 -> v18 migration by instantiating Database
            upgraded = Database(path)
            self.assertEqual(SCHEMA_VERSION, 18)

            # Verify old conversation turns preserved
            turns = upgraded.get_recent_turns(1)
            self.assertEqual(turns[0]['text'], 'v17-preserved-message')
            self.assertEqual(turns[0]['client_message_id'], 'web:v17-id')

            # Verify conversation_message_requests table exists and functions
            with sqlite3.connect(path) as conn:
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertIn('conversation_message_requests', tables)
                columns = {row[1] for row in conn.execute('PRAGMA table_info(conversation_message_requests)')}
                self.assertTrue({'owner_id', 'client_message_id', 'source_channel', 'status', 'response_json', 'created_at', 'updated_at'} <= columns)
                version = conn.execute('PRAGMA user_version').fetchone()[0]
                self.assertEqual(version, 18)
                integrity = conn.execute('PRAGMA integrity_check').fetchone()[0]
                self.assertEqual(integrity, 'ok')

            # Verify pre-migration backup for v17 was generated
            backup_files = list((Path(temp) / 'backups').glob('pre-migration-v17-*.sqlite3'))
            self.assertTrue(len(backup_files) >= 1)

            # Verify the backup integrity and version
            with sqlite3.connect(backup_files[0]) as bck_conn:
                self.assertEqual(bck_conn.execute('PRAGMA user_version').fetchone()[0], 17)
                self.assertEqual(bck_conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

            del upgraded, db


if __name__ == '__main__':
    unittest.main()
