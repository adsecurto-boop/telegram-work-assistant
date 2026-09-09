"""Read-only database health summary without displaying work content."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
from database import SCHEMA_VERSION

path = Path(config.DB_PATH).resolve()
if not path.is_file():
    raise SystemExit(
        f'Database file not found: {path}\n'
        'Start the bot once to initialize the database and migrate legacy tasks.json data, '
        'then rerun this check. If you already have a database, check SQLITE_PATH in .env.'
    )
try:
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
except sqlite3.OperationalError as exc:
    raise SystemExit(f'Cannot open database: {path}\n{exc}') from None
try:
    version = connection.execute('PRAGMA user_version').fetchone()[0]
    integrity = connection.execute('PRAGMA integrity_check').fetchone()[0]
    task_count = connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
    required = {'clients', 'support_interactions', 'testing_records', 'learning_records', 'ai_events',
                'source_imports', 'source_messages', 'work_cases', 'case_events', 'test_sessions',
                'evidence', 'followups', 'connector_state', 'nl_interactions', 'conversation_context',
                'audit_log', 'client_aliases', 'history_clusters', 'cluster_items', 'bulk_operations',
                'shift_templates', 'shift_calendar', 'report_provenance'}
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}

finally:
    connection.close()

print(f'Live schema: {version} (expected {SCHEMA_VERSION})')
print(f'Live integrity: {integrity}')
print(f'Tasks preserved: {task_count}')
print(f'Structured tables present: {len(required & tables)}/{len(required)}')
raise SystemExit(0 if version == SCHEMA_VERSION and integrity == 'ok' and required <= tables else 1)
