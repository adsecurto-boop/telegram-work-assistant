"""Read-only database health summary without displaying work content."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
from database import SCHEMA_VERSION

connection = sqlite3.connect(f'file:{Path(config.DB_PATH).resolve().as_posix()}?mode=ro', uri=True)
try:
    version = connection.execute('PRAGMA user_version').fetchone()[0]
    integrity = connection.execute('PRAGMA integrity_check').fetchone()[0]
    task_count = connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
    required = {'clients', 'support_interactions', 'testing_records', 'learning_records', 'ai_events',
                'source_imports', 'source_messages', 'work_cases', 'case_events', 'test_sessions',
                'evidence', 'followups', 'connector_state'}
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
finally:
    connection.close()

print(f'Live schema: {version} (expected {SCHEMA_VERSION})')
print(f'Live integrity: {integrity}')
print(f'Tasks preserved: {task_count}')
print(f'Structured tables present: {len(required & tables)}/{len(required)}')
raise SystemExit(0 if version == SCHEMA_VERSION and integrity == 'ok' and required <= tables else 1)
