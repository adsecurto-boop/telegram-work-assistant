"""Transactional SQLite storage with versioned, backup-first migrations."""
import json
import shutil
import sqlite3
import uuid
from contextlib import contextmanager, closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from models import Task, TaskStatus
import config

SCHEMA_VERSION = 8


_last_iso_time = 0.0


def now_iso():
    global _last_iso_time
    t = datetime.now(timezone.utc).timestamp()
    if t <= _last_iso_time:
        t = _last_iso_time + 0.001
    _last_iso_time = t
    return datetime.fromtimestamp(t, timezone.utc).isoformat()



class Database:
    def __init__(self, db_path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA busy_timeout=15000')
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self):
        with closing(sqlite3.connect(self.path)) as raw:
            version = raw.execute('PRAGMA user_version').fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError('Database is newer than this application.')

        # Pre-flight integrity check
        with closing(sqlite3.connect(self.path)) as raw:
            chk = raw.execute('PRAGMA integrity_check').fetchall()
            if not chk or chk[0][0] != 'ok':
                raise RuntimeError(f'Pre-migration integrity check failed: {chk}')

        if 0 < version < SCHEMA_VERSION:
            backup_dir = self.path.parent / 'backups'
            backup_dir.mkdir(parents=True, exist_ok=True)
            destination = backup_dir / (
                f'pre-migration-v{version}-' + datetime.now().strftime('%Y%m%d-%H%M%S%f') + '.sqlite3'
            )
            with closing(sqlite3.connect(self.path)) as source, closing(sqlite3.connect(destination)) as target:
                source.backup(target)
            with closing(sqlite3.connect(destination)) as bck_chk:
                r = bck_chk.execute('PRAGMA integrity_check').fetchall()
                if not r or r[0][0] != 'ok':
                    raise RuntimeError(f'Pre-migration backup integrity check failed: {r}')

        conn = sqlite3.connect(self.path)
        conn.isolation_level = None
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            cursor.execute('BEGIN IMMEDIATE')
            self._create_schema(cursor)
            self._ensure_columns(cursor)
            if version == 1:
                self._seed_structured_records(cursor)
            if version < 4:
                self._seed_v4_defaults(cursor)
            if version < 5:
                self._seed_v5_defaults(cursor)
            self._create_indexes(cursor)
            self._validate_schema_integrity(cursor)
            cursor.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
            cursor.execute('COMMIT')
        except Exception as exc:
            try:
                conn.execute('ROLLBACK')
            except Exception:
                pass
            raise RuntimeError(f'Database migration failed and was rolled back: {exc}') from exc
        finally:
            conn.close()

        with closing(sqlite3.connect(self.path)) as raw:
            chk = raw.execute('PRAGMA integrity_check').fetchall()
            if not chk or chk[0][0] != 'ok':
                raise RuntimeError(f'Post-migration integrity check failed: {chk}')

    def _exec_sql_script(self, connection, script_text: str):
        for stmt in script_text.strip().split(';'):
            cleaned = stmt.strip()
            if cleaned:
                connection.execute(cleaned)

    def _create_schema(self, connection):
        self._exec_sql_script(connection, '''
            CREATE TABLE IF NOT EXISTS work_drafts (
                id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL, source_key TEXT UNIQUE,
                parent_id INTEGER REFERENCES work_drafts(id), raw_text TEXT NOT NULL,
                fields_json TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'draft', created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS work_draft_events (
                id INTEGER PRIMARY KEY, draft_id INTEGER NOT NULL REFERENCES work_drafts(id),
                revision INTEGER NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL,
                created_at TEXT NOT NULL, UNIQUE(draft_id,revision,kind));
            CREATE TABLE IF NOT EXISTS work_contacts (
                id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL, alias TEXT NOT NULL,
                mention TEXT NOT NULL, salutation TEXT NOT NULL,
                UNIQUE(owner_id,alias,mention));
            CREATE TABLE IF NOT EXISTS work_client_profiles (
                owner_id INTEGER NOT NULL, name TEXT NOT NULL, fields_json TEXT NOT NULL,
                PRIMARY KEY(owner_id,name));
        ''')
        self._exec_sql_script(connection, '''
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
                status TEXT NOT NULL, blocked_reason TEXT, created_at TEXT NOT NULL,
                completed_at TEXT, priority INTEGER NOT NULL DEFAULT 0,
                planned_shift_id INTEGER, due_date TEXT, project TEXT, client TEXT,
                ticket TEXT, next_action TEXT, tags TEXT, completion_note TEXT);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS shifts (
                id INTEGER PRIMARY KEY,start TEXT NOT NULL,end TEXT NOT NULL,
                lunch TEXT,closed_at TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_shift
                ON shifts((1)) WHERE closed_at IS NULL;
            CREATE TABLE IF NOT EXISTS activities (
                id INTEGER PRIMARY KEY,shift_id INTEGER NOT NULL REFERENCES shifts(id),
                category TEXT NOT NULL,detail TEXT NOT NULL,client TEXT,channel TEXT,
                outcome TEXT,task_id INTEGER REFERENCES tasks(id),created_at TEXT NOT NULL,
                unplanned INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS clients (
                id INTEGER PRIMARY KEY,name TEXT NOT NULL,normalized TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS support_interactions (
                id INTEGER PRIMARY KEY,activity_id INTEGER NOT NULL UNIQUE REFERENCES activities(id),
                client_id INTEGER REFERENCES clients(id),channel TEXT,product TEXT,
                query_category TEXT,outcome TEXT,follow_up TEXT,ticket TEXT,
                query_count INTEGER,issue_key TEXT);
            CREATE TABLE IF NOT EXISTS testing_records (
                id INTEGER PRIMARY KEY,activity_id INTEGER NOT NULL UNIQUE REFERENCES activities(id),
                product TEXT,environment TEXT,build TEXT,scenario TEXT NOT NULL,
                result TEXT,defects TEXT,ticket TEXT,retest TEXT);
            CREATE TABLE IF NOT EXISTS learning_records (
                id INTEGER PRIMARY KEY,activity_id INTEGER NOT NULL UNIQUE REFERENCES activities(id),
                topic TEXT NOT NULL,learning_type TEXT,product TEXT,takeaway TEXT,follow_up TEXT);
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY,shift_id INTEGER NOT NULL REFERENCES shifts(id),
                kind TEXT NOT NULL,text TEXT NOT NULL,created_at TEXT NOT NULL,
                finalized INTEGER NOT NULL DEFAULT 0,style TEXT NOT NULL DEFAULT 'standard',
                provider TEXT,model TEXT,prompt_version TEXT,source_report_id INTEGER);
            CREATE TABLE IF NOT EXISTS deliveries (
                shift_id INTEGER NOT NULL,kind TEXT NOT NULL,PRIMARY KEY(shift_id,kind));
            CREATE TABLE IF NOT EXISTS updates (id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS ai_usage (day TEXT PRIMARY KEY,requests INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS ai_events (
                id INTEGER PRIMARY KEY,created_at TEXT NOT NULL,operation TEXT NOT NULL,
                provider TEXT,model TEXT,prompt_version TEXT,status TEXT NOT NULL,error_type TEXT);
            CREATE TABLE IF NOT EXISTS activity_audit (
                id INTEGER PRIMARY KEY,activity_id INTEGER NOT NULL,previous TEXT NOT NULL,
                changed_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS proposals (
                id INTEGER PRIMARY KEY,activity_id INTEGER NOT NULL,original TEXT NOT NULL,
                payload TEXT NOT NULL,applied INTEGER NOT NULL DEFAULT 0,
                dismissed INTEGER NOT NULL DEFAULT 0,model TEXT,prompt_version TEXT);
            CREATE TABLE IF NOT EXISTS source_imports (
                id INTEGER PRIMARY KEY,source_type TEXT NOT NULL,source_name TEXT NOT NULL,
                source_path TEXT,fingerprint TEXT NOT NULL UNIQUE,started_at TEXT NOT NULL,
                completed_at TEXT,status TEXT NOT NULL DEFAULT 'running',stats_json TEXT);
            CREATE TABLE IF NOT EXISTS work_cases (
                id INTEGER PRIMARY KEY,case_key TEXT UNIQUE,client_id INTEGER REFERENCES clients(id),
                title TEXT NOT NULL,product TEXT,platform TEXT,channel TEXT,ticket TEXT,
                priority INTEGER NOT NULL DEFAULT 1,status TEXT NOT NULL DEFAULT 'new',
                participation TEXT NOT NULL DEFAULT 'owned',next_action TEXT,waiting_on TEXT,
                follow_up_at TEXT,resolution TEXT,client_updated INTEGER NOT NULL DEFAULT 0,
                review_state TEXT NOT NULL DEFAULT 'approved',source TEXT,
                created_at TEXT NOT NULL,updated_at TEXT NOT NULL,closed_at TEXT);
            CREATE TABLE IF NOT EXISTS source_messages (
                id INTEGER PRIMARY KEY,import_id INTEGER REFERENCES source_imports(id),
                source_type TEXT NOT NULL,source_key TEXT NOT NULL UNIQUE,chat_name TEXT,
                external_message_id TEXT,occurred_at TEXT,author_name TEXT,
                author_is_owner INTEGER NOT NULL DEFAULT 0,text TEXT,redacted_text TEXT,
                reply_to_key TEXT,message_kind TEXT,media_json TEXT,classification TEXT,
                metadata_json TEXT,
                confidence REAL NOT NULL DEFAULT 0,review_status TEXT NOT NULL DEFAULT 'pending',
                case_id INTEGER REFERENCES work_cases(id),shift_id INTEGER REFERENCES shifts(id),
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS case_events (
                id INTEGER PRIMARY KEY,case_id INTEGER NOT NULL REFERENCES work_cases(id),
                shift_id INTEGER REFERENCES shifts(id),event_type TEXT NOT NULL,detail TEXT NOT NULL,
                actor_role TEXT NOT NULL DEFAULT 'owner',outcome TEXT,
                source_message_id INTEGER UNIQUE REFERENCES source_messages(id),
                occurred_at TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS test_sessions (
                id INTEGER PRIMARY KEY,case_id INTEGER REFERENCES work_cases(id),
                shift_id INTEGER REFERENCES shifts(id),scenario TEXT NOT NULL,environment TEXT,
                build TEXT,preconditions TEXT,steps TEXT,expected TEXT,actual TEXT,
                result TEXT NOT NULL DEFAULT 'not_run',defects TEXT,developer_notified INTEGER NOT NULL DEFAULT 0,
                retest_required INTEGER NOT NULL DEFAULT 0,retest_result TEXT,verified_at TEXT,
                created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS evidence (
                id INTEGER PRIMARY KEY,case_id INTEGER REFERENCES work_cases(id),
                test_session_id INTEGER REFERENCES test_sessions(id),shift_id INTEGER REFERENCES shifts(id),
                kind TEXT NOT NULL,path TEXT,telegram_file_id TEXT,caption TEXT,sha256 TEXT,
                mime_type TEXT,created_at TEXT NOT NULL,
                UNIQUE(sha256,case_id,test_session_id));
            CREATE TABLE IF NOT EXISTS followups (
                id INTEGER PRIMARY KEY,case_id INTEGER NOT NULL REFERENCES work_cases(id),
                shift_id INTEGER REFERENCES shifts(id),due_at TEXT NOT NULL,kind TEXT NOT NULL DEFAULT 'case',
                waiting_on TEXT,note TEXT,status TEXT NOT NULL DEFAULT 'pending',
                reminded_at TEXT,created_at TEXT NOT NULL,completed_at TEXT);
            CREATE TABLE IF NOT EXISTS connector_state (
                connector TEXT PRIMARY KEY,cursor TEXT,last_sync_at TEXT,last_error TEXT,
                metadata_json TEXT);
            CREATE TABLE IF NOT EXISTS nl_interactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_update_id INTEGER,
                raw_text TEXT NOT NULL,
                intent TEXT NOT NULL,
                entities_json TEXT,
                confidence REAL NOT NULL DEFAULT 0.0,
                proposed_operations_json TEXT,
                applied_operations_json TEXT,
                provider TEXT,
                model TEXT,
                parser_version TEXT NOT NULL DEFAULT 'v1',
                status TEXT NOT NULL DEFAULT 'processed',
                clarification_json TEXT,
                error_details TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS conversation_context (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                context_key TEXT NOT NULL UNIQUE,
                active_case_id INTEGER REFERENCES work_cases(id) ON DELETE SET NULL,
                active_task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
                active_test_session_id INTEGER REFERENCES test_sessions(id) ON DELETE SET NULL,
                active_client TEXT,
                last_intent TEXT,
                context_data_json TEXT,
                expires_at TEXT NOT NULL,
                updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                correlation_id TEXT NOT NULL,
                operation_type TEXT NOT NULL,
                actor TEXT NOT NULL DEFAULT 'system',
                affected_table TEXT NOT NULL,
                record_id INTEGER NOT NULL,
                before_state_json TEXT,
                after_state_json TEXT,
                reversibility TEXT NOT NULL DEFAULT 'reversible',
                reverted_at TEXT,
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS client_aliases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
                alias TEXT NOT NULL UNIQUE,
                product TEXT,
                domain TEXT,
                confidence REAL NOT NULL DEFAULT 1.0,
                is_verified INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS history_clusters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                suggested_client TEXT,
                suggested_product TEXT,
                suggested_issue_type TEXT,
                confidence REAL NOT NULL DEFAULT 0.5,
                reason TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                merged_into_id INTEGER REFERENCES history_clusters(id),
                case_id INTEGER REFERENCES work_cases(id) ON DELETE SET NULL,
                fingerprint TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cluster_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cluster_id INTEGER NOT NULL REFERENCES history_clusters(id) ON DELETE CASCADE,
                source_message_id INTEGER NOT NULL UNIQUE REFERENCES source_messages(id) ON DELETE CASCADE,
                relevance_score REAL NOT NULL DEFAULT 1.0,
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS bulk_operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                correlation_id TEXT NOT NULL UNIQUE,
                operation_type TEXT NOT NULL,
                item_count INTEGER NOT NULL DEFAULT 0,
                parameters_json TEXT,
                status TEXT NOT NULL DEFAULT 'completed',
                created_at TEXT NOT NULL,
                reverted_at TEXT);
            CREATE TABLE IF NOT EXISTS shift_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                start_time TEXT NOT NULL,
                lunch_time TEXT,
                end_time TEXT NOT NULL,
                reminder_offsets TEXT DEFAULT '30,15',
                active_weekdays TEXT DEFAULT '0,1,2,3,4',
                timezone TEXT DEFAULT 'Asia/Kolkata',
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS shift_calendar (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL UNIQUE,
                template_id INTEGER REFERENCES shift_templates(id) ON DELETE SET NULL,
                start_time TEXT,
                lunch_time TEXT,
                end_time TEXT,
                is_day_off INTEGER NOT NULL DEFAULT 0,
                note TEXT,
                is_explicit_override INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS report_provenance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
                section_name TEXT NOT NULL,
                record_type TEXT NOT NULL,
                record_id INTEGER NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS nl_proposals (
                id TEXT PRIMARY KEY,
                owner_id INTEGER NOT NULL,
                source_update_id INTEGER,
                intent TEXT NOT NULL,
                proposal_json TEXT NOT NULL,
                record_version INTEGER DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'pending',
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS report_validations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
                is_valid INTEGER NOT NULL,
                warnings_json TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS nl_corrections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                interaction_id INTEGER REFERENCES nl_interactions(id) ON DELETE SET NULL,
                original_intent TEXT NOT NULL,
                corrected_intent TEXT NOT NULL,
                corrected_entities_json TEXT,
                owner_id INTEGER NOT NULL,
                parser_version TEXT NOT NULL DEFAULT 'v2',
                notes TEXT,
                created_at TEXT NOT NULL);
        ''')

    def _create_indexes(self, connection):
        self._exec_sql_script(connection, '''
            CREATE INDEX IF NOT EXISTS task_due_index ON tasks(due_date,status);
            CREATE INDEX IF NOT EXISTS activity_shift_index ON activities(shift_id,category);
            CREATE INDEX IF NOT EXISTS report_shift_index ON reports(shift_id,kind);
            CREATE INDEX IF NOT EXISTS support_client_index ON support_interactions(client_id,outcome);
            CREATE INDEX IF NOT EXISTS source_review_index ON source_messages(review_status,author_is_owner,occurred_at);
            CREATE INDEX IF NOT EXISTS source_case_index ON source_messages(case_id);
            CREATE INDEX IF NOT EXISTS case_status_index ON work_cases(review_state,status,follow_up_at);
            CREATE INDEX IF NOT EXISTS case_event_shift_index ON case_events(shift_id,case_id,event_type);
            CREATE INDEX IF NOT EXISTS test_session_shift_index ON test_sessions(shift_id,result);
            CREATE INDEX IF NOT EXISTS followup_due_index ON followups(status,due_at);
            CREATE INDEX IF NOT EXISTS nl_intent_status_index ON nl_interactions(intent, status);
            CREATE INDEX IF NOT EXISTS audit_correlation_index ON audit_log(correlation_id);
            CREATE INDEX IF NOT EXISTS audit_record_index ON audit_log(affected_table, record_id);
            CREATE INDEX IF NOT EXISTS cluster_status_index ON history_clusters(status);
            CREATE UNIQUE INDEX IF NOT EXISTS cluster_fingerprint_idx ON history_clusters(fingerprint) WHERE fingerprint IS NOT NULL;
            CREATE INDEX IF NOT EXISTS cluster_items_cluster_index ON cluster_items(cluster_id);
            CREATE INDEX IF NOT EXISTS client_alias_index ON client_aliases(alias);
            CREATE INDEX IF NOT EXISTS shift_calendar_date_index ON shift_calendar(date);
            CREATE INDEX IF NOT EXISTS report_provenance_report_index ON report_provenance(report_id, section_name);
            CREATE INDEX IF NOT EXISTS nl_proposals_status_idx ON nl_proposals(status, expires_at);
            CREATE INDEX IF NOT EXISTS report_validations_report_idx ON report_validations(report_id);
            CREATE INDEX IF NOT EXISTS nl_corrections_interaction_idx ON nl_corrections(interaction_id);
            CREATE INDEX IF NOT EXISTS nl_corrections_intent_idx ON nl_corrections(corrected_intent, created_at);
        ''')

    def _ensure_columns(self, connection):
        additions = {
            'shifts': {'eod_reminder': 'TEXT'},
            'tasks': {
                'planned_shift_id': 'INTEGER', 'due_date': 'TEXT', 'project': 'TEXT',
                'client': 'TEXT', 'ticket': 'TEXT', 'next_action': 'TEXT',
                'tags': 'TEXT', 'completion_note': 'TEXT'},
            'activities': {
                'unplanned': 'INTEGER NOT NULL DEFAULT 0',
                'source_message_id': 'INTEGER REFERENCES source_messages(id)',
                'case_id': 'INTEGER REFERENCES work_cases(id)'},
            'source_messages': {
                'metadata_json': 'TEXT',
                'cluster_id': 'INTEGER REFERENCES history_clusters(id)'},
            'reports': {
                'style': "TEXT NOT NULL DEFAULT 'standard'", 'provider': 'TEXT',
                'model': 'TEXT', 'prompt_version': 'TEXT', 'source_report_id': 'INTEGER',
                'provenance_json': 'TEXT'},
            'proposals': {
                'dismissed': 'INTEGER NOT NULL DEFAULT 0', 'model': 'TEXT',
                'prompt_version': 'TEXT'},
            'history_clusters': {
                'fingerprint': 'TEXT'},
            'shift_calendar': {
                'is_explicit_override': 'INTEGER NOT NULL DEFAULT 1'},
            'nl_interactions': {
                'reason_codes_json': 'TEXT',
                'normalized_text': 'TEXT'},
        }
        for table, columns in additions.items():
            current = {row['name'] for row in connection.execute(f'PRAGMA table_info({table})')}
            for name, declaration in columns.items():
                if name not in current:
                    connection.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')

    def _seed_v4_defaults(self, connection):
        now = now_iso()
        connection.execute('''INSERT OR IGNORE INTO shift_templates
            (name, start_time, lunch_time, end_time, reminder_offsets, active_weekdays, timezone, created_at)
            VALUES ('Morning', '08:00', '12:30', '17:00', '30,15', '0,1,2,3,4', 'Asia/Kolkata', ?)''', (now,))
        connection.execute('''INSERT OR IGNORE INTO shift_templates
            (name, start_time, lunch_time, end_time, reminder_offsets, active_weekdays, timezone, created_at)
            VALUES ('General', '10:00', '14:00', '19:00', '30,15', '0,1,2,3,4', 'Asia/Kolkata', ?)''', (now,))
        connection.execute('''INSERT OR IGNORE INTO shift_templates
            (name, start_time, lunch_time, end_time, reminder_offsets, active_weekdays, timezone, created_at)
            VALUES ('Evening', '12:00', '16:00', '21:00', '30,15', '0,1,2,3,4', 'Asia/Kolkata', ?)''', (now,))

    def _seed_v5_defaults(self, connection):
        import hashlib
        connection.execute("UPDATE shift_templates SET lunch_time='12:30' WHERE name='Morning' AND lunch_time='13:00'")
        # Backfill fingerprint for existing history_clusters if missing
        try:
            clusters = connection.execute("SELECT id FROM history_clusters WHERE fingerprint IS NULL").fetchall()
            for c in clusters:
                c_id = c['id'] if isinstance(c, sqlite3.Row) else c[0]
                items = connection.execute(
                    "SELECT source_message_id FROM cluster_items WHERE cluster_id=? ORDER BY source_message_id",
                    (c_id,)
                ).fetchall()
                ids = [r['source_message_id'] if isinstance(r, sqlite3.Row) else r[0] for r in items]
                raw = "cluster-v1:" + ','.join(str(i) for i in ids)
                fp = hashlib.sha256(raw.encode('utf-8')).hexdigest()
                connection.execute("UPDATE history_clusters SET fingerprint=? WHERE id=?", (fp, c_id))
        except Exception:
            pass

    def _validate_schema_integrity(self, cursor):
        required_tables = {
            'tasks', 'settings', 'shifts', 'activities', 'clients',
            'support_interactions', 'testing_records', 'learning_records',
            'reports', 'deliveries', 'updates', 'ai_usage', 'ai_events',
            'activity_audit', 'proposals', 'source_imports', 'work_cases',
            'source_messages', 'case_events', 'test_sessions', 'evidence',
            'followups', 'nl_interactions', 'conversation_context', 'audit_log',
            'client_aliases', 'history_clusters', 'cluster_items',
            'bulk_operations', 'shift_templates', 'shift_calendar',
            'report_provenance', 'nl_proposals', 'report_validations',
            'nl_corrections'
        }
        rows = cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        existing = {r['name'] if isinstance(r, sqlite3.Row) else r[0] for r in rows}
        missing = required_tables - existing
        if missing:
            raise RuntimeError(f"Missing required database tables after migration: {missing}")

    def _seed_structured_records(self, connection):
        for row in connection.execute("SELECT * FROM activities WHERE category='support'").fetchall():
            client_id = self._client_id(connection, row['client'])
            connection.execute('''INSERT OR IGNORE INTO support_interactions
                (activity_id,client_id,channel,outcome) VALUES (?,?,?,?)''',
                (row['id'], client_id, row['channel'], row['outcome']))
        for row in connection.execute("SELECT * FROM activities WHERE category='testing'").fetchall():
            connection.execute('INSERT OR IGNORE INTO testing_records(activity_id,scenario) VALUES (?,?)',
                               (row['id'], row['detail']))
        for row in connection.execute("SELECT * FROM activities WHERE category='learning'").fetchall():
            connection.execute('INSERT OR IGNORE INTO learning_records(activity_id,topic) VALUES (?,?)',
                               (row['id'], row['detail']))

    def migrate_json(self, path):
        source = Path(path)
        if self.get_setting('json_migrated') or not source.exists():
            return
        data = json.loads(source.read_text(encoding='utf-8-sig'))
        tasks = [Task.from_row(row) for row in data['tasks']]
        backup = source.with_name(source.name + '.migration-' + datetime.now().strftime('%Y%m%d%H%M%S%f') + '.bak')
        shutil.copy2(source, backup)
        with self.connect() as connection:
            if connection.execute('SELECT 1 FROM tasks LIMIT 1').fetchone():
                raise RuntimeError('Refusing to merge legacy JSON into a populated database.')
            for task in tasks:
                connection.execute('''INSERT INTO tasks
                    (id,title,status,blocked_reason,created_at,completed_at,priority)
                    VALUES (?,?,?,?,?,?,?)''',
                    (task.id, task.title, task.status.value, task.blocked_reason,
                     task.created_at, task.completed_at, task.priority))
            connection.execute('INSERT INTO settings VALUES (?,?)', ('json_migrated', str(backup)))

    def backup(self, destination):
        target = Path(destination)
        if target.resolve() == self.path.resolve():
            raise ValueError('Backup must differ from database.')
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            with closing(sqlite3.connect(target)) as output:
                connection.backup(output)
        return target

    def integrity(self):
        with self.connect() as connection:
            return connection.execute('PRAGMA integrity_check').fetchone()[0]

    def get_setting(self, key):
        with self.connect() as connection:
            row = connection.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            return row[0] if row else None

    def set_setting(self, key, value):
        with self.connect() as connection:
            connection.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, str(value)))

    def active_shift(self):
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM shifts WHERE closed_at IS NULL').fetchone()
            return dict(row) if row else None

    def shift(self, shift_id):
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM shifts WHERE id=?', (shift_id,)).fetchone()
            return dict(row) if row else None

    def shifts_since(self, start_iso):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(
                'SELECT * FROM shifts WHERE start>=? ORDER BY start', (start_iso,))]

    def start_shift(self, start, end, lunch=None, correlation_id=None, actor='system', eod_reminder=None):
        with self.connect() as connection:
            if connection.execute('SELECT 1 FROM shifts WHERE closed_at IS NULL').fetchone():
                raise ValueError('A shift is already active. End it before starting another.')
            shift_id = connection.execute('INSERT INTO shifts(start,end,lunch,eod_reminder) VALUES (?,?,?,?)',
                                          (start, end, lunch, eod_reminder)).lastrowid
            if correlation_id:
                self._record_audit_in_connection(
                    connection, correlation_id, 'start_shift', actor, 'shifts', shift_id,
                    None, {'start': start, 'end': end, 'lunch': lunch, 'eod_reminder': eod_reminder})
            return shift_id

    def revise_shift(self, expected, start, end, lunch, eod_reminder, correlation_id):
        """Apply confirmed timing changes only to the unchanged active shift."""
        from shifts import validate_schedule
        start_dt, end_dt = datetime.fromisoformat(start), datetime.fromisoformat(end)
        validate_schedule(start_dt, end_dt, datetime.fromisoformat(lunch) if lunch else None)
        if not start_dt < end_dt or (lunch and not start_dt < datetime.fromisoformat(lunch) < end_dt):
            raise ValueError('The proposed times conflict with the existing schedule. Include a lunch time within the new shift.')
        if eod_reminder and not start_dt <= datetime.fromisoformat(eod_reminder) <= end_dt:
            raise ValueError('EOD reminder must fall within the shift.')
        fields = ('id', 'start', 'end', 'lunch', 'eod_reminder', 'closed_at')
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT * FROM shifts WHERE id=?', (expected['id'],)).fetchone()
            if not row or row['closed_at'] or any(row[key] != expected.get(key) for key in fields):
                raise ValueError('The shift changed since this proposal. Please send the timing request again.')
            before = {key: row[key] for key in fields if key != 'id'}
            after = dict(start=start, end=end, lunch=lunch, eod_reminder=eod_reminder, closed_at=None)
            connection.execute('UPDATE shifts SET start=?,end=?,lunch=?,eod_reminder=? WHERE id=?',
                               (start, end, lunch, eod_reminder, expected['id']))
            self._record_audit_in_connection(connection, correlation_id, 'revise_shift', 'nl_engine',
                                             'shifts', expected['id'], before, after)

    def schedule(self, shift_id, end, lunch, correlation_id=None, actor='system'):
        with self.connect() as connection:
            before = connection.execute('SELECT end,lunch FROM shifts WHERE id=? AND closed_at IS NULL',
                                        (shift_id,)).fetchone()
            if not before:
                raise ValueError('Active shift not found.')
            connection.execute('UPDATE shifts SET end=?,lunch=? WHERE id=? AND closed_at IS NULL',
                               (end, lunch, shift_id))
            if correlation_id:
                self._record_audit_in_connection(
                    connection, correlation_id, 'schedule_shift', actor, 'shifts', shift_id,
                    dict(before), {'end': end, 'lunch': lunch})

    def close_shift(self, shift_id):
        with self.connect() as connection:
            connection.execute('UPDATE shifts SET closed_at=? WHERE id=? AND closed_at IS NULL',
                               (now_iso(), shift_id))

    def finalize_and_close(self, report_id, shift_id, acknowledge_errors=False, require_validation=False):
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            report = connection.execute(
                "SELECT * FROM reports WHERE id=? AND shift_id=? AND kind='eod'",
                (report_id, shift_id)).fetchone()
            if not report:
                raise ValueError('EOD report not found for this shift.')
            validation = connection.execute(
                'SELECT is_valid FROM report_validations WHERE report_id=? ORDER BY id DESC LIMIT 1',
                (report_id,)).fetchone()
            if require_validation and not validation:
                raise ValueError('EOD report has not been validated. Generate a fresh EOD before closing the shift.')
            if validation and not validation['is_valid'] and not acknowledge_errors:
                raise ValueError('EOD report contains error-level validation warnings. Review them before closing the shift.')
            if connection.execute('SELECT 1 FROM activities WHERE shift_id=? AND created_at>?',
                                  (shift_id, report['created_at'])).fetchone():
                raise ValueError('Work was logged after this draft. Use /endshift to review a fresh EOD.')
            if connection.execute('''SELECT 1 FROM activity_audit audit
                JOIN activities activity ON audit.activity_id=activity.id
                WHERE activity.shift_id=? AND audit.changed_at>?''',
                                  (shift_id, report['created_at'])).fetchone():
                raise ValueError('An activity was corrected after this draft. Use /endshift for a fresh EOD.')
            connection.execute('UPDATE reports SET finalized=1 WHERE id=?', (report_id,))
            connection.execute('UPDATE shifts SET closed_at=? WHERE id=? AND closed_at IS NULL',
                               (now_iso(), shift_id))

    def add_task(self, title, priority=0, shift_id=None, due_date=None, project=None,
                 client=None, ticket=None, next_action=None, tags=None):
        with self.connect() as connection:
            task_id = connection.execute('''INSERT INTO tasks
                (title,status,created_at,priority,planned_shift_id,due_date,project,client,ticket,next_action,tags)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                (title, 'pending', now_iso(), int(priority), shift_id, due_date, project,
                 client, ticket, next_action, tags)).lastrowid
            if shift_id:
                self._activity(connection, shift_id, 'plan', title, client=client, task_id=task_id)
            return Task.from_row(connection.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())

    def update_task(self, task_id, field, value, shift_id=None):
        allowed = {'title', 'priority', 'due_date', 'project', 'client', 'ticket',
                   'next_action', 'tags', 'completion_note'}
        if field not in allowed:
            raise ValueError('Editable fields: ' + ', '.join(sorted(allowed)) + '.')
        if field == 'priority':
            value = int(value)
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row:
                return None
            connection.execute(f'UPDATE tasks SET {field}=? WHERE id=?',
                               (value if value not in ('-', 'none') else None, task_id))
            if shift_id:
                self._activity(connection, shift_id, 'task_edit',
                               f"{row['title']}: {field} updated", task_id=task_id)
            return Task.from_row(connection.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())

    def get_task(self, task_id):
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            return Task.from_row(row) if row else None

    def list_tasks(self, status=None):
        with self.connect() as connection:
            rows = connection.execute('SELECT * FROM tasks ORDER BY priority DESC,id').fetchall()
            return [Task.from_row(row) for row in rows
                    if status is None or row['status'] == status.value]

    def tasks_for_shift(self, shift_id):
        shift = self.shift(shift_id)
        if not shift:
            return []
        shift_date = (shift.get('start') or '')[:10]
        return [
            task for task in self.list_tasks()
            if task.planned_shift_id == shift_id
            or (task.completed_at and task.completed_at[:10] == shift_date)
        ]

    def list_pending(self):
        return [task for task in self.list_tasks()
                if task.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS)]

    def mark_status(self, task_id, status, blocked_reason=None, shift_id=None, completion_note=None,
                    correlation_id=None, actor='system'):
        if isinstance(status, str):
            status = TaskStatus(status)
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row:
                return None
            completed_at = now_iso() if status == TaskStatus.COMPLETED else None
            note = completion_note if status == TaskStatus.COMPLETED else None
            connection.execute('''UPDATE tasks SET status=?,blocked_reason=?,completed_at=?,completion_note=?
                WHERE id=?''', (status.value, blocked_reason, completed_at, note, task_id))
            activity_id = None
            if shift_id and (row['status'] != status.value or row['blocked_reason'] != blocked_reason
                             or completion_note):
                detail = row['title'] + (f' — {completion_note}' if completion_note else '')
                activity_id = self._activity(connection, shift_id, 'task', detail,
                                             outcome=status.value, task_id=task_id,
                                             unplanned=1 if row['planned_shift_id'] != shift_id else 0)
            updated_row = connection.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if correlation_id:
                fields = ('status', 'blocked_reason', 'completed_at', 'completion_note')
                self._record_audit_in_connection(
                    connection, correlation_id, 'update_task_status', actor, 'tasks', task_id,
                    {k: row[k] for k in fields}, {k: updated_row[k] for k in fields})
                if activity_id:
                    activity = connection.execute('SELECT * FROM activities WHERE id=?', (activity_id,)).fetchone()
                    self._record_audit_in_connection(
                        connection, correlation_id, 'create_task_activity', actor, 'activities', activity_id,
                        None, dict(activity))
            return Task.from_row(updated_row)

    def delete_task(self, task_id, shift_id=None):
        return self.mark_status(task_id, TaskStatus.CANCELLED, shift_id=shift_id) is not None

    def delete_tasks(self, scope='all', shift_id=None, task_id=None):
        """Delete task rows atomically while retaining historical activity and reports."""
        clauses, params = [], []
        if scope in ('pending', 'completed', 'in_progress', 'blocked'):
            clauses.append('status=?'); params.append(scope)
        elif scope == 'today':
            if shift_id is None:
                raise ValueError('Start a shift before deleting this shift’s tasks.')
            clauses.append('planned_shift_id=?'); params.append(shift_id)
        elif scope != 'all':
            raise ValueError('Unsupported task deletion scope.')
        if task_id is not None:
            clauses.append('id=?'); params.append(task_id)
        correlation = 'delete-tasks-' + uuid.uuid4().hex
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            rows = conn.execute('SELECT * FROM tasks' + (' WHERE ' + ' AND '.join(clauses) if clauses else ''), params).fetchall()
            for row in rows:
                for activity in conn.execute('SELECT id,task_id FROM activities WHERE task_id=?', (row['id'],)).fetchall():
                    conn.execute('UPDATE activities SET task_id=NULL WHERE id=?', (activity['id'],))
                    self._record_audit_in_connection(conn, correlation, 'detach_task_history', 'owner',
                        'activities', activity['id'], {'task_id': row['id']}, {'task_id': None})
                conn.execute('UPDATE conversation_context SET active_task_id=NULL WHERE active_task_id=?', (row['id'],))
                conn.execute('DELETE FROM tasks WHERE id=?', (row['id'],))
                self._record_audit_in_connection(conn, correlation, 'delete_task', 'owner', 'tasks',
                                                 row['id'], dict(row), None)
        return len(rows), correlation

    def _activity(self, connection, shift_id, category, detail, client=None, channel=None,
                  outcome=None, task_id=None, unplanned=0, source_message_id=None, case_id=None):
        return connection.execute('''INSERT INTO activities
            (shift_id,category,detail,client,channel,outcome,task_id,created_at,unplanned,
             source_message_id,case_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
            (shift_id, category, detail, client, channel, outcome, task_id, now_iso(),
             int(bool(unplanned)), source_message_id, case_id)).lastrowid

    def _client_id(self, connection, name):
        if not name:
            return None
        clean = name.strip()
        normalized = clean.casefold()
        connection.execute('INSERT OR IGNORE INTO clients(name,normalized,created_at) VALUES (?,?,?)',
                           (clean, normalized, now_iso()))
        return connection.execute('SELECT id FROM clients WHERE normalized=?',
                                  (normalized,)).fetchone()[0]

    def add_activity(self, shift_id, category, detail, client=None, channel=None, outcome=None,
                     source_message_id=None, case_id=None):
        with self.connect() as connection:
            return self._activity(connection, shift_id, category, detail, client, channel, outcome,
                                  source_message_id=source_message_id, case_id=case_id)

    def add_support(self, shift_id, detail, client=None, channel=None, outcome=None,
                    product=None, query_category=None, follow_up=None, ticket=None,
                    query_count=1, issue_key=None, correlation_id=None):
        with self.connect() as connection:
            activity_id = self._activity(connection, shift_id, 'support', detail, client, channel, outcome)
            client_id = self._client_id(connection, client)
            connection.execute('''INSERT INTO support_interactions
                (activity_id,client_id,channel,product,query_category,outcome,follow_up,ticket,query_count,issue_key)
                VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (activity_id, client_id, channel, product, query_category, outcome,
                 follow_up, ticket, query_count, issue_key))
            if correlation_id:
                row = connection.execute('SELECT * FROM activities WHERE id=?', (activity_id,)).fetchone()
                self._record_audit_in_connection(connection, correlation_id, 'log_support', 'nl_engine',
                                                 'activities', activity_id, None, dict(row))
            return activity_id

    def add_testing(self, shift_id, scenario, result=None, product=None, environment=None,
                    build=None, defects=None, ticket=None, retest=None):
        with self.connect() as connection:
            activity_id = self._activity(connection, shift_id, 'testing', scenario)
            connection.execute('''INSERT INTO testing_records
                (activity_id,product,environment,build,scenario,result,defects,ticket,retest)
                VALUES (?,?,?,?,?,?,?,?,?)''',
                (activity_id, product, environment, build, scenario, result,
                 defects, ticket, retest))
            return activity_id

    def add_learning(self, shift_id, topic, learning_type=None, product=None,
                     takeaway=None, follow_up=None):
        with self.connect() as connection:
            activity_id = self._activity(connection, shift_id, 'learning', topic)
            connection.execute('''INSERT INTO learning_records
                (activity_id,topic,learning_type,product,takeaway,follow_up)
                VALUES (?,?,?,?,?,?)''',
                (activity_id, topic, learning_type, product, takeaway, follow_up))
            return activity_id

    def activities(self, shift_id):
        with self.connect() as connection:
            rows = connection.execute('''SELECT activity.*,
                COALESCE(client.name,activity.client) AS client_name,
                support.product AS support_product,support.query_category,
                support.follow_up AS support_follow_up,support.ticket AS support_ticket,
                support.query_count,support.issue_key,
                testing.product AS testing_product,testing.environment,testing.build,
                testing.scenario,testing.result,testing.defects,
                testing.ticket AS testing_ticket,testing.retest,
                learning.topic,learning.learning_type,learning.product AS learning_product,
                learning.takeaway,learning.follow_up AS learning_follow_up
                FROM activities activity
                LEFT JOIN support_interactions support ON support.activity_id=activity.id
                LEFT JOIN clients client ON client.id=support.client_id
                LEFT JOIN testing_records testing ON testing.activity_id=activity.id
                LEFT JOIN learning_records learning ON learning.activity_id=activity.id
                WHERE activity.shift_id=? AND activity.category NOT IN ('deleted','converted')
                ORDER BY activity.id''', (shift_id,)).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item['client'] = item.pop('client_name') or item.get('client')
                result.append(item)
            return result

    def all_activities_since(self, start_iso):
        with self.connect() as connection:
            shift_ids = [row[0] for row in connection.execute(
                'SELECT id FROM shifts WHERE start>=?', (start_iso,))]
        result = []
        for shift_id in shift_ids:
            result.extend(self.activities(shift_id))
        return result

    def correct_activity(self, shift_id, activity_id, replacement):
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM activities WHERE id=? AND shift_id=?',
                                     (activity_id, shift_id)).fetchone()
            if not row or row['category'] not in ('note', 'testing', 'learning', 'support'):
                raise ValueError('Only support, testing, learning, and note entries in this shift can be corrected.')
            connection.execute('INSERT INTO activity_audit(activity_id,previous,changed_at) VALUES (?,?,?)',
                               (activity_id, json.dumps(dict(row)), now_iso()))
            if replacement is None:
                connection.execute("UPDATE activities SET category='deleted' WHERE id=?", (activity_id,))
            else:
                connection.execute('UPDATE activities SET detail=? WHERE id=?', (replacement, activity_id))
                if row['category'] == 'testing':
                    connection.execute('UPDATE testing_records SET scenario=? WHERE activity_id=?',
                                       (replacement, activity_id))
                elif row['category'] == 'learning':
                    connection.execute('UPDATE learning_records SET topic=? WHERE activity_id=?',
                                       (replacement, activity_id))

    def save_report(self, shift_id, kind, text, style='standard', provider=None, model=None,
                    prompt_version=None, source_report_id=None):
        with self.connect() as connection:
            return connection.execute('''INSERT INTO reports
                (shift_id,kind,text,created_at,style,provider,model,prompt_version,source_report_id)
                VALUES (?,?,?,?,?,?,?,?,?)''',
                (shift_id, kind, text, now_iso(), style, provider, model,
                 prompt_version, source_report_id)).lastrowid

    def report(self, report_id):
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM reports WHERE id=?', (report_id,)).fetchone()
            return dict(row) if row else None

    def finalize(self, report_id, acknowledge_errors=False, require_validation=False):
        with self.connect() as connection:
            report = connection.execute('SELECT 1 FROM reports WHERE id=?', (report_id,)).fetchone()
            if not report:
                raise ValueError('Report not found.')
            validation = connection.execute(
                'SELECT is_valid FROM report_validations WHERE report_id=? ORDER BY id DESC LIMIT 1',
                (report_id,)).fetchone()
            if require_validation and not validation:
                raise ValueError('Report has not been validated. Regenerate or validate it before finalizing.')
            if validation and not validation['is_valid'] and not acknowledge_errors:
                raise ValueError('Report contains error-level validation warnings. Review or acknowledge them before finalizing.')
            connection.execute('UPDATE reports SET finalized=1 WHERE id=?', (report_id,))

    def history(self):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute('''SELECT id,shift_id,kind,created_at,
                finalized,style,provider,model,source_report_id FROM reports ORDER BY id DESC LIMIT 30''')]

    def delivered(self, shift_id, kind):
        with self.connect() as connection:
            return connection.execute('SELECT 1 FROM deliveries WHERE shift_id=? AND kind=?',
                                      (shift_id, kind)).fetchone() is not None

    def record_delivery(self, shift_id, kind):
        with self.connect() as connection:
            connection.execute('INSERT OR IGNORE INTO deliveries VALUES (?,?)', (shift_id, kind))

    def claim_update(self, update_id):
        with self.connect() as connection:
            return connection.execute('INSERT OR IGNORE INTO updates VALUES (?)',
                                      (update_id,)).rowcount == 1

    def reserve_ai(self, day, limit):
        with self.connect() as connection:
            connection.execute('INSERT OR IGNORE INTO ai_usage VALUES (?,0)', (day,))
            return connection.execute('''UPDATE ai_usage SET requests=requests+1
                WHERE day=? AND requests<?''', (day, limit)).rowcount == 1

    def record_ai_event(self, operation=None, provider='gemini', model='gemini-2.5-flash',
                        prompt_version='v1', status='success', error_type=None, feature=None):
        op = operation or feature or 'unknown'
        with self.connect() as connection:
            connection.execute('''INSERT INTO ai_events
                (created_at,operation,provider,model,prompt_version,status,error_type)
                VALUES (?,?,?,?,?,?,?)''',
                (now_iso(), op, provider, model, prompt_version, status, error_type))

    def ai_stats(self):
        class AIStatsResult(list):
            def get(self, key, default=None):
                if key == 'total_events':
                    return sum(item.get('count', 0) for item in self)
                for item in self:
                    if item.get('status') == key:
                        return item.get('count', default)
                return default

        with self.connect() as connection:
            rows = [dict(row) for row in connection.execute('''SELECT status,COUNT(*) AS count
                FROM ai_events GROUP BY status ORDER BY status''')]
            return AIStatsResult(rows)

    def propose(self, activity_id, original, payload, model=None, prompt_version=None):
        with self.connect() as connection:
            return connection.execute('''INSERT INTO proposals
                (activity_id,original,payload,model,prompt_version) VALUES (?,?,?,?,?)''',
                (activity_id, original, json.dumps(payload), model, prompt_version)).lastrowid

    def proposal(self, proposal_id):
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM proposals WHERE id=?', (proposal_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result['entries'] = json.loads(result['payload'])
            return result

    def update_proposal_entry(self, proposal_id, index, detail=None, remove=False, field='detail'):
        with self.connect() as connection:
            row = connection.execute('''SELECT * FROM proposals
                WHERE id=? AND applied=0 AND dismissed=0''', (proposal_id,)).fetchone()
            if not row:
                raise ValueError('Suggestion is unavailable or already closed.')
            entries = json.loads(row['payload'])
            if not 1 <= index <= len(entries):
                raise ValueError('Suggestion item number is out of range.')
            if remove:
                entries.pop(index - 1)
                if not entries:
                    raise ValueError('Keep at least one item or dismiss the suggestion.')
            else:
                allowed = {
                    'detail','client','channel','outcome','priority','due_date','project','product',
                    'ticket','next_action','tags','query_category','follow_up','query_count','issue_key',
                    'environment','build','result','defects','retest','learning_type','takeaway',
                    'needs_confirmation','question','status','participation','platform','waiting_on',
                    'client_updated'}
                if field not in allowed:
                    raise ValueError('That suggestion field is not editable.')
                value = None if detail in ('-', 'none') else detail
                if field in ('priority','query_count') and value is not None:
                    value = int(value)
                if field == 'needs_confirmation':
                    value = str(value).casefold() in ('true','yes','1','on')
                candidate = dict(entries[index - 1])
                candidate[field] = value
                from ai import Entry
                entries[index - 1] = Entry.model_validate(candidate).model_dump()
            connection.execute('UPDATE proposals SET payload=? WHERE id=?',
                               (json.dumps(entries), proposal_id))
            return entries

    def dismiss_proposal(self, proposal_id):
        with self.connect() as connection:
            if connection.execute('''UPDATE proposals SET dismissed=1
                WHERE id=? AND applied=0 AND dismissed=0''', (proposal_id,)).rowcount != 1:
                raise ValueError('Suggestion is unavailable or already closed.')

    def apply_proposal(self, proposal_id, shift_id):
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            proposal = connection.execute('''SELECT * FROM proposals
                WHERE id=? AND applied=0 AND dismissed=0''', (proposal_id,)).fetchone()
            if not proposal:
                raise ValueError('Suggestion already applied, dismissed, or unavailable.')
            row = connection.execute("""SELECT * FROM activities
                WHERE id=? AND shift_id=? AND category='note'""",
                                     (proposal['activity_id'], shift_id)).fetchone()
            if not row or row['detail'] != proposal['original']:
                raise ValueError('Original note changed. Generate a fresh suggestion.')
            connection.execute('INSERT INTO activity_audit(activity_id,previous,changed_at) VALUES (?,?,?)',
                               (row['id'], json.dumps(dict(row)), now_iso()))
            connection.execute("UPDATE activities SET category='converted' WHERE id=?", (row['id'],))
            for entry in json.loads(proposal['payload']):
                self._apply_entry(connection, shift_id, entry)
            connection.execute('UPDATE proposals SET applied=1 WHERE id=?', (proposal_id,))

    def _apply_entry(self, connection, shift_id, entry):
        category = entry['category']
        if category == 'plan':
            task_id = connection.execute('''INSERT INTO tasks
                (title,status,created_at,priority,planned_shift_id,due_date,project,client,ticket,next_action,tags)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                (entry['detail'], 'pending', now_iso(), int(entry.get('priority') or 0), shift_id,
                 entry.get('due_date'), entry.get('project'), entry.get('client'), entry.get('ticket'),
                 entry.get('next_action'), entry.get('tags'))).lastrowid
            self._activity(connection, shift_id, 'plan', entry['detail'],
                           client=entry.get('client'), task_id=task_id)
        elif category == 'case':
            stamp = now_iso()
            client_id = self._client_id(connection, entry.get('client'))
            case_id = connection.execute('''INSERT INTO work_cases
                (client_id,title,product,platform,channel,ticket,priority,status,participation,
                 next_action,waiting_on,client_updated,review_state,source,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (client_id, entry['detail'], entry.get('product'), entry.get('platform'),
                 entry.get('channel'), entry.get('ticket'), int(entry.get('priority') or 1),
                 entry.get('status') or 'new', entry.get('participation') or 'handled',
                 entry.get('next_action'), entry.get('waiting_on'),
                 int(bool(entry.get('client_updated'))), 'approved', 'ai_proposal', stamp, stamp)).lastrowid
            connection.execute('''INSERT INTO case_events
                (case_id,shift_id,event_type,detail,actor_role,outcome,occurred_at,created_at)
                VALUES (?,?,?,?,?,?,?,?)''',
                (case_id, shift_id, 'created', entry['detail'], 'owner',
                 entry.get('status') or 'new', stamp, stamp))
        elif category == 'support':
            activity_id = self._activity(connection, shift_id, 'support', entry['detail'],
                                         entry.get('client'), entry.get('channel'), entry.get('outcome'))
            client_id = self._client_id(connection, entry.get('client'))
            connection.execute('''INSERT INTO support_interactions
                (activity_id,client_id,channel,product,query_category,outcome,follow_up,ticket,query_count,issue_key)
                VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (activity_id, client_id, entry.get('channel'), entry.get('product'),
                 entry.get('query_category'), entry.get('outcome'), entry.get('follow_up'),
                 entry.get('ticket'), entry.get('query_count'), entry.get('issue_key')))
        elif category == 'testing':
            activity_id = self._activity(connection, shift_id, 'testing', entry['detail'])
            connection.execute('''INSERT INTO testing_records
                (activity_id,product,environment,build,scenario,result,defects,ticket,retest)
                VALUES (?,?,?,?,?,?,?,?,?)''',
                (activity_id, entry.get('product'), entry.get('environment'), entry.get('build'),
                 entry['detail'], entry.get('result'), entry.get('defects'), entry.get('ticket'),
                 entry.get('retest')))
        elif category == 'learning':
            activity_id = self._activity(connection, shift_id, 'learning', entry['detail'])
            connection.execute('''INSERT INTO learning_records
                (activity_id,topic,learning_type,product,takeaway,follow_up)
                VALUES (?,?,?,?,?,?)''',
                (activity_id, entry['detail'], entry.get('learning_type'), entry.get('product'),
                 entry.get('takeaway'), entry.get('follow_up')))
        else:
            self._activity(connection, shift_id, 'note', entry['detail'])

    # Phase 3: evidence-backed cases, imports, test sessions and follow-ups.
    def create_import(self, source_type, source_name, source_path, fingerprint):
        with self.connect() as connection:
            existing = connection.execute(
                'SELECT * FROM source_imports WHERE fingerprint=?', (fingerprint,)).fetchone()
            if existing:
                if existing['status'] == 'rolled_back':
                    connection.execute('''UPDATE source_imports SET status='running',started_at=?,
                        completed_at=NULL,stats_json=NULL WHERE id=?''', (now_iso(), existing['id']))
                    reopened = connection.execute('SELECT * FROM source_imports WHERE id=?',
                                                  (existing['id'],)).fetchone()
                    return dict(reopened), True
                return dict(existing), False
            import_id = connection.execute('''INSERT INTO source_imports
                (source_type,source_name,source_path,fingerprint,started_at)
                VALUES (?,?,?,?,?)''',
                (source_type, source_name, source_path, fingerprint, now_iso())).lastrowid
            row = connection.execute('SELECT * FROM source_imports WHERE id=?', (import_id,)).fetchone()
            return dict(row), True

    def finish_import(self, import_id, status, stats=None):
        with self.connect() as connection:
            connection.execute('''UPDATE source_imports
                SET completed_at=?,status=?,stats_json=? WHERE id=?''',
                (now_iso(), status, json.dumps(stats or {}), import_id))

    def imports(self, limit=20):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(
                'SELECT * FROM source_imports ORDER BY id DESC LIMIT ?', (limit,))]

    def rollback_import(self, import_id):
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            batch = connection.execute('SELECT * FROM source_imports WHERE id=?', (import_id,)).fetchone()
            if not batch:
                raise ValueError('Import batch not found.')
            if connection.execute("SELECT 1 FROM source_messages WHERE import_id=? AND review_status='accepted'",
                                  (import_id,)).fetchone():
                raise ValueError('Accepted items exist. Move or ignore those cases before rolling back this batch.')
            count = connection.execute('DELETE FROM source_messages WHERE import_id=?', (import_id,)).rowcount
            connection.execute("UPDATE source_imports SET status='rolled_back',completed_at=? WHERE id=?",
                               (now_iso(), import_id))
            return count

    def add_source_message(self, *, import_id=None, source_type, source_key, chat_name=None,
                           external_message_id=None, occurred_at=None, author_name=None,
                           author_is_owner=False, text=None, redacted_text=None, reply_to_key=None,
                           message_kind='text', media=None, classification=None, confidence=0,
                           review_status='pending', shift_id=None, metadata=None):
        with self.connect() as connection:
            inserted = connection.execute('''INSERT OR IGNORE INTO source_messages
                (import_id,source_type,source_key,chat_name,external_message_id,occurred_at,
                 author_name,author_is_owner,text,redacted_text,reply_to_key,message_kind,
                 media_json,classification,confidence,review_status,shift_id,created_at,metadata_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (import_id, source_type, source_key, chat_name, external_message_id, occurred_at,
                 author_name, int(bool(author_is_owner)), text, redacted_text, reply_to_key,
                 message_kind, json.dumps(media or []), classification, float(confidence),
                 review_status, shift_id, now_iso(), json.dumps(metadata or {}))).rowcount == 1
            row = connection.execute('SELECT * FROM source_messages WHERE source_key=?',
                                     (source_key,)).fetchone()
            return dict(row), inserted

    def source_message(self, message_id):
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM source_messages WHERE id=?', (message_id,)).fetchone()
            return dict(row) if row else None

    def inbox(self, limit=20, include_observed=False):
        with self.connect() as connection:
            owner_filter = '' if include_observed else ' AND author_is_owner=1'
            return [dict(row) for row in connection.execute('''SELECT * FROM source_messages
                WHERE review_status='pending' ''' + owner_filter + '''
                ORDER BY COALESCE(occurred_at,created_at) DESC,id DESC LIMIT ?''', (limit,))]

    def ignore_source_message(self, message_id):
        with self.connect() as connection:
            if connection.execute('''UPDATE source_messages SET review_status='ignored'
                WHERE id=? AND review_status='pending' ''', (message_id,)).rowcount != 1:
                raise ValueError('Inbox item is unavailable or already reviewed.')

    def filter_inbox(self, review_status='pending', author_is_owner=True, import_id=None,
                     start_date=None, end_date=None, client=None, product=None,
                     classification=None, min_confidence=None, cluster_id=None,
                     has_media=None, has_ticket=None, search_text=None,
                     offset=0, limit=50):
        with self.connect() as connection:
            where_clauses = ['1=1']
            params = []
            if review_status:
                where_clauses.append('review_status=?')
                params.append(review_status)
            if author_is_owner:
                where_clauses.append('author_is_owner=1')
            if import_id is not None:
                where_clauses.append('import_id=?')
                params.append(import_id)
            if start_date:
                where_clauses.append('occurred_at >= ?')
                params.append(start_date)
            if end_date:
                where_clauses.append('occurred_at <= ?')
                params.append(end_date)
            if classification:
                where_clauses.append('classification=?')
                params.append(classification)
            if min_confidence is not None:
                where_clauses.append('confidence >= ?')
                params.append(float(min_confidence))
            if cluster_id is not None:
                where_clauses.append('cluster_id=?')
                params.append(cluster_id)
            if has_media is True:
                where_clauses.append("media_json IS NOT NULL AND media_json != '[]'")
            elif has_media is False:
                where_clauses.append("(media_json IS NULL OR media_json == '[]')")
            if has_ticket is True:
                where_clauses.append("metadata_json LIKE '%\"ticket\":%'")
            elif has_ticket is False:
                where_clauses.append("metadata_json NOT LIKE '%\"ticket\":%'")
            if client:
                where_clauses.append("(metadata_json LIKE ? OR text LIKE ?)")
                params.extend([f'%"client": "%{client}%"', f'%{client}%'])
            if product:
                where_clauses.append("metadata_json LIKE ?")
                params.append(f'%"product": "%{product}%"')
            if search_text:
                where_clauses.append("(text LIKE ? OR redacted_text LIKE ?)")
                params.extend([f'%{search_text}%', f'%{search_text}%'])

            where_sql = ' AND '.join(where_clauses)
            count_row = connection.execute(f'SELECT COUNT(*) FROM source_messages WHERE {where_sql}', params).fetchone()
            total = count_row[0] if count_row else 0

            query_params = list(params) + [limit, offset]
            sql = f'''SELECT * FROM source_messages WHERE {where_sql}
                      ORDER BY COALESCE(occurred_at, created_at) DESC, id DESC
                      LIMIT ? OFFSET ?'''
            items = [dict(r) for r in connection.execute(sql, query_params).fetchall()]
            return items, total

    def bulk_accept_as_tasks(self, message_ids: list[int], shift_id=None, priority=1):
        if not message_ids:
            return {'correlation_id': None, 'affected_count': 0, 'task_ids': []}
        correlation_id = f'bulk-task-{uuid.uuid4().hex[:10]}'
        created_task_ids = []
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            for m_id in message_ids:
                row = connection.execute('SELECT * FROM source_messages WHERE id=? AND review_status="pending"', (m_id,)).fetchone()
                if not row:
                    continue
                meta = json.loads(row['metadata_json']) if row['metadata_json'] else {}
                title = (row['redacted_text'] or row['text'] or 'Imported task')[:200]
                stamp = now_iso()
                t_id = connection.execute('''INSERT INTO tasks
                    (title, status, created_at, priority, planned_shift_id, client, project, ticket)
                    VALUES (?, 'pending', ?, ?, ?, ?, ?, ?)''',
                    (title, stamp, int(priority), shift_id, meta.get('client'), meta.get('product'), meta.get('ticket'))).lastrowid
                created_task_ids.append(t_id)

                connection.execute('''INSERT INTO audit_log
                    (correlation_id, operation_type, actor, affected_table, record_id, before_state_json, after_state_json, created_at)
                    VALUES (?, 'create_task', 'bulk_action', 'tasks', ?, NULL, ?, ?)''',
                    (correlation_id, t_id, json.dumps({'title': title, 'status': 'pending'}), stamp))

                before_msg = {'review_status': 'pending'}
                connection.execute('UPDATE source_messages SET review_status="accepted" WHERE id=?', (m_id,))
                connection.execute('''INSERT INTO audit_log
                    (correlation_id, operation_type, actor, affected_table, record_id, before_state_json, after_state_json, created_at)
                    VALUES (?, 'update_source_message', 'bulk_action', 'source_messages', ?, ?, ?, ?)''',
                    (correlation_id, m_id, json.dumps(before_msg), json.dumps({'review_status': 'accepted'}), stamp))

            connection.execute('''INSERT INTO bulk_operations
                (correlation_id, operation_type, item_count, parameters_json, status, created_at)
                VALUES (?, 'bulk_accept_tasks', ?, ?, 'completed', ?)''',
                (correlation_id, len(created_task_ids), json.dumps({'shift_id': shift_id, 'priority': priority}), now_iso()))

        return {'correlation_id': correlation_id, 'affected_count': len(created_task_ids), 'task_ids': created_task_ids}

    def bulk_accept_as_case_events(self, message_ids: list[int], case_id: int, shift_id=None):
        if not message_ids:
            return {'correlation_id': None, 'affected_count': 0}
        correlation_id = f'bulk-event-{uuid.uuid4().hex[:10]}'
        affected = 0
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            case_obj = connection.execute('SELECT * FROM work_cases WHERE id=?', (case_id,)).fetchone()
            if not case_obj:
                raise ValueError(f'Case #{case_id} not found.')
            stamp = now_iso()
            for m_id in message_ids:
                row = connection.execute('SELECT * FROM source_messages WHERE id=? AND review_status="pending"', (m_id,)).fetchone()
                if not row:
                    continue
                detail = (row['redacted_text'] or row['text'] or 'Imported message')[:300]
                event_id = connection.execute('''INSERT INTO case_events
                    (case_id, shift_id, event_type, detail, actor_role, outcome, source_message_id, occurred_at, created_at)
                    VALUES (?, ?, ?, ?, 'owner', NULL, ?, ?, ?)''',
                    (case_id, shift_id, row['classification'] or 'note', detail, m_id, row['occurred_at'] or stamp, stamp)).lastrowid

                connection.execute('''INSERT INTO audit_log
                    (correlation_id, operation_type, actor, affected_table, record_id, before_state_json, after_state_json, created_at)
                    VALUES (?, 'create_case_event', 'bulk_action', 'case_events', ?, NULL, ?, ?)''',
                    (correlation_id, event_id, json.dumps({'case_id': case_id, 'detail': detail}), stamp))

                before_msg = {'review_status': 'pending', 'case_id': row['case_id']}
                connection.execute('UPDATE source_messages SET review_status="accepted", case_id=? WHERE id=?', (case_id, m_id))
                connection.execute('''INSERT INTO audit_log
                    (correlation_id, operation_type, actor, affected_table, record_id, before_state_json, after_state_json, created_at)
                    VALUES (?, 'update_source_message', 'bulk_action', 'source_messages', ?, ?, ?, ?)''',
                    (correlation_id, m_id, json.dumps(before_msg), json.dumps({'review_status': 'accepted', 'case_id': case_id}), stamp))
                affected += 1

            connection.execute('UPDATE work_cases SET updated_at=? WHERE id=?', (stamp, case_id))
            connection.execute('''INSERT INTO bulk_operations
                (correlation_id, operation_type, item_count, parameters_json, status, created_at)
                VALUES (?, 'bulk_accept_case_events', ?, ?, 'completed', ?)''',
                (correlation_id, affected, json.dumps({'case_id': case_id, 'shift_id': shift_id}), stamp))

        return {'correlation_id': correlation_id, 'affected_count': affected}

    def bulk_accept_cluster_as_case(self, cluster_id: int, shift_id=None, case_title=None):
        correlation_id = f'bulk-cluster-{uuid.uuid4().hex[:10]}'
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            cluster = connection.execute('SELECT * FROM history_clusters WHERE id=?', (cluster_id,)).fetchone()
            if not cluster:
                raise ValueError('Cluster not found.')
            if cluster['status'] != 'pending':
                raise ValueError(f'Cluster #{cluster_id} is already {cluster["status"]}; only pending clusters can be accepted.')

            items = connection.execute('''SELECT sm.* FROM cluster_items ci
                JOIN source_messages sm ON sm.id=ci.source_message_id
                WHERE ci.cluster_id=?''', (cluster_id,)).fetchall()
            if not items:
                raise ValueError('Cluster contains no messages.')

            title = case_title or cluster['title']
            client_id = self._client_id(connection, cluster['suggested_client'])
            stamp = now_iso()
            case_id = connection.execute('''INSERT INTO work_cases
                (client_id, title, product, status, participation, review_state, source, created_at, updated_at)
                VALUES (?, ?, ?, 'new', 'handled', 'approved', 'cluster', ?, ?)''',
                (client_id, title, cluster['suggested_product'], stamp, stamp)).lastrowid

            connection.execute('''INSERT INTO audit_log
                (correlation_id, operation_type, actor, affected_table, record_id, before_state_json, after_state_json, created_at)
                VALUES (?, 'create_case', 'bulk_action', 'work_cases', ?, NULL, ?, ?)''',
                (correlation_id, case_id, json.dumps({'title': title, 'client_id': client_id}), stamp))

            seen_event_details = set()
            for row in items:
                event_detail = (row['redacted_text'] or row['text'] or 'Cluster message')[:300]
                if event_detail not in seen_event_details:
                    seen_event_details.add(event_detail)
                    ev_id = connection.execute('''INSERT INTO case_events
                        (case_id, shift_id, event_type, detail, actor_role, source_message_id, occurred_at, created_at)
                        VALUES (?, ?, ?, ?, 'owner', ?, ?, ?)''',
                        (case_id, shift_id, row['classification'] or 'note', event_detail, row['id'], row['occurred_at'] or stamp, stamp)).lastrowid
                    connection.execute('''INSERT INTO audit_log
                        (correlation_id, operation_type, actor, affected_table, record_id, before_state_json, after_state_json, created_at)
                        VALUES (?, 'create_case_event', 'bulk_action', 'case_events', ?, NULL, ?, ?)''',
                        (correlation_id, ev_id, json.dumps({'case_id': case_id, 'detail': event_detail}), stamp))

                before_msg = {'review_status': row['review_status'], 'case_id': row['case_id']}
                connection.execute('UPDATE source_messages SET review_status="accepted", case_id=? WHERE id=?', (case_id, row['id']))
                connection.execute('''INSERT INTO audit_log
                    (correlation_id, operation_type, actor, affected_table, record_id, before_state_json, after_state_json, created_at)
                    VALUES (?, 'update_source_message', 'bulk_action', 'source_messages', ?, ?, ?, ?)''',
                    (correlation_id, row['id'], json.dumps(before_msg), json.dumps({'review_status': 'accepted', 'case_id': case_id}), stamp))

            before_cluster = {'status': cluster['status'], 'case_id': cluster['case_id']}
            connection.execute('UPDATE history_clusters SET status="accepted", case_id=?, updated_at=? WHERE id=?', (case_id, stamp, cluster_id))
            connection.execute('''INSERT INTO audit_log
                (correlation_id, operation_type, actor, affected_table, record_id, before_state_json, after_state_json, created_at)
                VALUES (?, 'update_history_cluster', 'bulk_action', 'history_clusters', ?, ?, ?, ?)''',
                (correlation_id, cluster_id, json.dumps(before_cluster), json.dumps({'status': 'accepted', 'case_id': case_id}), stamp))

            connection.execute('''INSERT INTO bulk_operations
                (correlation_id, operation_type, item_count, parameters_json, status, created_at)
                VALUES (?, 'bulk_accept_cluster', ?, ?, 'completed', ?)''',
                (correlation_id, len(items), json.dumps({'cluster_id': cluster_id, 'case_id': case_id}), stamp))

        return {'correlation_id': correlation_id, 'case_id': case_id, 'affected_count': len(items), 'title': title}

    def reject_cluster(self, cluster_id: int) -> dict:
        correlation_id = f'cluster-reject-{uuid.uuid4().hex[:10]}'
        stamp = now_iso()
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            cluster = connection.execute('SELECT * FROM history_clusters WHERE id=?', (cluster_id,)).fetchone()
            if not cluster:
                raise ValueError('Cluster not found.')
            if cluster['status'] != 'pending':
                raise ValueError(f'Cluster #{cluster_id} is already {cluster["status"]}.')
            before = {'status': cluster['status']}
            connection.execute('UPDATE history_clusters SET status="rejected", updated_at=? WHERE id=?', (stamp, cluster_id))
            connection.execute('''INSERT INTO audit_log
                (correlation_id, operation_type, actor, affected_table, record_id, before_state_json, after_state_json, created_at)
                VALUES (?, 'reject_cluster', 'system', 'history_clusters', ?, ?, ?, ?)''',
                (correlation_id, cluster_id, json.dumps(before), json.dumps({'status': 'rejected'}), stamp))
            return {'correlation_id': correlation_id, 'cluster_id': cluster_id, 'status': 'rejected'}

    def split_cluster(self, cluster_id: int, message_ids_to_split: list[int], new_title: str = None) -> dict:
        if not message_ids_to_split:
            raise ValueError('Select message IDs to split.')
        correlation_id = f'cluster-split-{uuid.uuid4().hex[:10]}'
        stamp = now_iso()
        import hashlib
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            cluster = connection.execute('SELECT * FROM history_clusters WHERE id=?', (cluster_id,)).fetchone()
            if not cluster:
                raise ValueError('Cluster not found.')
            if cluster['status'] != 'pending':
                raise ValueError(f'Cluster #{cluster_id} is already {cluster["status"]}; only pending clusters can be split.')

            remaining_items = connection.execute(
                'SELECT source_message_id FROM cluster_items WHERE cluster_id=? AND source_message_id NOT IN (' +
                ','.join('?' * len(message_ids_to_split)) + ')',
                [cluster_id] + message_ids_to_split
            ).fetchall()
            if not remaining_items:
                raise ValueError('Cannot split all messages out of cluster; at least one must remain.')

            title = new_title or f"Split: {cluster['title'][:40]}"
            new_cluster_id = connection.execute('''INSERT INTO history_clusters
                (title, suggested_client, suggested_product, suggested_issue_type, confidence, reason, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)''',
                (title, cluster['suggested_client'], cluster['suggested_product'], cluster['suggested_issue_type'],
                 cluster['confidence'], f"Split from #{cluster_id}", stamp, stamp)).lastrowid

            connection.execute('''INSERT INTO audit_log
                (correlation_id, operation_type, actor, affected_table, record_id, before_state_json, after_state_json, created_at)
                VALUES (?, 'create_cluster', 'system', 'history_clusters', ?, NULL, ?, ?)''',
                (correlation_id, new_cluster_id, json.dumps({'title': title}), stamp))

            for m_id in message_ids_to_split:
                old_item = connection.execute(
                    'SELECT * FROM cluster_items WHERE cluster_id=? AND source_message_id=?',
                    (cluster_id, m_id)).fetchone()
                if not old_item:
                    raise ValueError(f'Message #{m_id} does not belong to cluster #{cluster_id}.')
                source_before = connection.execute(
                    'SELECT cluster_id FROM source_messages WHERE id=?', (m_id,)).fetchone()
                connection.execute('DELETE FROM cluster_items WHERE id=?', (old_item['id'],))
                self._record_audit_in_connection(
                    connection, correlation_id, 'remove_cluster_item', 'system',
                    'cluster_items', old_item['id'], dict(old_item), None)
                new_item_id = connection.execute('''INSERT INTO cluster_items
                    (cluster_id, source_message_id, relevance_score, created_at)
                    VALUES (?, ?, 1.0, ?)''', (new_cluster_id, m_id, stamp)).lastrowid
                new_item = connection.execute('SELECT * FROM cluster_items WHERE id=?', (new_item_id,)).fetchone()
                self._record_audit_in_connection(
                    connection, correlation_id, 'create_cluster_item', 'system',
                    'cluster_items', new_item_id, None, dict(new_item))
                connection.execute('UPDATE source_messages SET cluster_id=? WHERE id=?', (new_cluster_id, m_id))
                self._record_audit_in_connection(
                    connection, correlation_id, 'move_source_cluster', 'system',
                    'source_messages', m_id, dict(source_before), {'cluster_id': new_cluster_id})

            rem_ids = sorted([r[0] for r in remaining_items])
            new_ids = sorted(message_ids_to_split)
            fp_rem = hashlib.sha256(('v1:' + ','.join(str(i) for i in rem_ids)).encode()).hexdigest()
            fp_new = hashlib.sha256(('v1:' + ','.join(str(i) for i in new_ids)).encode()).hexdigest()
            original_before = {'fingerprint': cluster['fingerprint'], 'updated_at': cluster['updated_at']}
            connection.execute('UPDATE history_clusters SET fingerprint=?, updated_at=? WHERE id=?', (fp_rem, stamp, cluster_id))
            self._record_audit_in_connection(
                connection, correlation_id, 'update_cluster_fingerprint', 'system',
                'history_clusters', cluster_id, original_before,
                {'fingerprint': fp_rem, 'updated_at': stamp})
            connection.execute('UPDATE history_clusters SET fingerprint=?, updated_at=? WHERE id=?', (fp_new, stamp, new_cluster_id))

            return {
                'correlation_id': correlation_id,
                'original_cluster_id': cluster_id,
                'new_cluster_id': new_cluster_id,
                'split_count': len(message_ids_to_split)
            }

    def merge_clusters(self, source_cluster_id: int, target_cluster_id: int) -> dict:
        if source_cluster_id == target_cluster_id:
            raise ValueError('Source and target clusters must be distinct.')
        correlation_id = f'cluster-merge-{uuid.uuid4().hex[:10]}'
        stamp = now_iso()
        import hashlib
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            src = connection.execute('SELECT * FROM history_clusters WHERE id=?', (source_cluster_id,)).fetchone()
            dst = connection.execute('SELECT * FROM history_clusters WHERE id=?', (target_cluster_id,)).fetchone()
            if not src or not dst:
                raise ValueError('Both source and target clusters must exist.')
            if src['status'] != 'pending' or dst['status'] != 'pending':
                raise ValueError('Only pending clusters can be merged.')

            src_items = connection.execute('SELECT source_message_id FROM cluster_items WHERE cluster_id=?', (source_cluster_id,)).fetchall()
            for r in src_items:
                m_id = r[0]
                old_item = connection.execute(
                    'SELECT * FROM cluster_items WHERE cluster_id=? AND source_message_id=?',
                    (source_cluster_id, m_id)).fetchone()
                source_before = connection.execute(
                    'SELECT cluster_id FROM source_messages WHERE id=?', (m_id,)).fetchone()
                connection.execute('DELETE FROM cluster_items WHERE id=?', (old_item['id'],))
                self._record_audit_in_connection(
                    connection, correlation_id, 'remove_cluster_item', 'system',
                    'cluster_items', old_item['id'], dict(old_item), None)
                new_item_id = connection.execute('''INSERT INTO cluster_items
                    (cluster_id, source_message_id, relevance_score, created_at)
                    VALUES (?, ?, 1.0, ?)''', (target_cluster_id, m_id, stamp)).lastrowid
                new_item = connection.execute('SELECT * FROM cluster_items WHERE id=?', (new_item_id,)).fetchone()
                self._record_audit_in_connection(
                    connection, correlation_id, 'create_cluster_item', 'system',
                    'cluster_items', new_item_id, None, dict(new_item))
                connection.execute('UPDATE source_messages SET cluster_id=? WHERE id=?', (target_cluster_id, m_id))
                self._record_audit_in_connection(
                    connection, correlation_id, 'move_source_cluster', 'system',
                    'source_messages', m_id, dict(source_before), {'cluster_id': target_cluster_id})

            before_src = {'status': src['status'], 'merged_into_id': src['merged_into_id'], 'updated_at': src['updated_at']}
            connection.execute('UPDATE history_clusters SET status="merged", merged_into_id=?, updated_at=? WHERE id=?',
                               (target_cluster_id, stamp, source_cluster_id))
            connection.execute('''INSERT INTO audit_log
                (correlation_id, operation_type, actor, affected_table, record_id, before_state_json, after_state_json, created_at)
                VALUES (?, 'merge_cluster', 'system', 'history_clusters', ?, ?, ?, ?)''',
                (correlation_id, source_cluster_id, json.dumps(before_src), json.dumps({'status': 'merged', 'merged_into_id': target_cluster_id, 'updated_at': stamp}), stamp))

            dst_items = connection.execute('SELECT source_message_id FROM cluster_items WHERE cluster_id=?', (target_cluster_id,)).fetchall()
            dst_ids = sorted([r[0] for r in dst_items])
            fp_dst = hashlib.sha256(('v1:' + ','.join(str(i) for i in dst_ids)).encode()).hexdigest()
            before_dst = {'fingerprint': dst['fingerprint'], 'updated_at': dst['updated_at']}
            connection.execute('UPDATE history_clusters SET fingerprint=?, updated_at=? WHERE id=?', (fp_dst, stamp, target_cluster_id))
            self._record_audit_in_connection(
                connection, correlation_id, 'update_cluster_fingerprint', 'system',
                'history_clusters', target_cluster_id, before_dst,
                {'fingerprint': fp_dst, 'updated_at': stamp})

            return {
                'correlation_id': correlation_id,
                'source_cluster_id': source_cluster_id,
                'target_cluster_id': target_cluster_id,
                'merged_count': len(src_items)
            }

    def attach_cluster_to_case(self, cluster_id: int, case_id: int, shift_id=None) -> dict:
        correlation_id = f'cluster-attach-{uuid.uuid4().hex[:10]}'
        stamp = now_iso()
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            cluster = connection.execute('SELECT * FROM history_clusters WHERE id=?', (cluster_id,)).fetchone()
            case_obj = connection.execute('SELECT * FROM work_cases WHERE id=?', (case_id,)).fetchone()
            if not cluster:
                raise ValueError('Cluster not found.')
            if not case_obj:
                raise ValueError(f'Case #{case_id} not found.')
            if cluster['status'] != 'pending':
                raise ValueError(f'Cluster #{cluster_id} is already {cluster["status"]}.')

            items = connection.execute('''SELECT sm.* FROM cluster_items ci
                JOIN source_messages sm ON sm.id=ci.source_message_id
                WHERE ci.cluster_id=?''', (cluster_id,)).fetchall()
            if not items:
                raise ValueError('Cluster contains no messages.')

            seen_event_details = set()
            for row in items:
                event_detail = (row['redacted_text'] or row['text'] or 'Cluster message')[:300]
                if event_detail not in seen_event_details:
                    seen_event_details.add(event_detail)
                    ev_id = connection.execute('''INSERT INTO case_events
                        (case_id, shift_id, event_type, detail, actor_role, source_message_id, occurred_at, created_at)
                        VALUES (?, ?, ?, ?, 'owner', ?, ?, ?)''',
                        (case_id, shift_id, row['classification'] or 'note', event_detail, row['id'], row['occurred_at'] or stamp, stamp)).lastrowid
                    connection.execute('''INSERT INTO audit_log
                        (correlation_id, operation_type, actor, affected_table, record_id, before_state_json, after_state_json, created_at)
                        VALUES (?, 'create_case_event', 'system', 'case_events', ?, NULL, ?, ?)''',
                        (correlation_id, ev_id, json.dumps({'case_id': case_id, 'detail': event_detail}), stamp))

                before_msg = {'review_status': row['review_status'], 'case_id': row['case_id']}
                connection.execute('UPDATE source_messages SET review_status="accepted", case_id=? WHERE id=?', (case_id, row['id']))
                connection.execute('''INSERT INTO audit_log
                    (correlation_id, operation_type, actor, affected_table, record_id, before_state_json, after_state_json, created_at)
                    VALUES (?, 'update_source_message', 'system', 'source_messages', ?, ?, ?, ?)''',
                    (correlation_id, row['id'], json.dumps(before_msg), json.dumps({'review_status': 'accepted', 'case_id': case_id}), stamp))

            before_cluster = {'status': cluster['status'], 'case_id': cluster['case_id']}
            connection.execute('UPDATE history_clusters SET status="accepted", case_id=?, updated_at=? WHERE id=?', (case_id, stamp, cluster_id))
            connection.execute('''INSERT INTO audit_log
                (correlation_id, operation_type, actor, affected_table, record_id, before_state_json, after_state_json, created_at)
                VALUES (?, 'update_history_cluster', 'system', 'history_clusters', ?, ?, ?, ?)''',
                (correlation_id, cluster_id, json.dumps(before_cluster), json.dumps({'status': 'accepted', 'case_id': case_id}), stamp))

            return {'correlation_id': correlation_id, 'cluster_id': cluster_id, 'case_id': case_id, 'affected_count': len(items)}

    def bulk_ignore(self, message_ids: list[int]):
        if not message_ids:
            return {'correlation_id': None, 'affected_count': 0}
        correlation_id = f'bulk-ignore-{uuid.uuid4().hex[:10]}'
        affected = 0
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            stamp = now_iso()
            for m_id in message_ids:
                row = connection.execute('SELECT * FROM source_messages WHERE id=? AND review_status="pending"', (m_id,)).fetchone()
                if not row:
                    continue
                before_msg = {'review_status': 'pending'}
                connection.execute('UPDATE source_messages SET review_status="ignored" WHERE id=?', (m_id,))
                connection.execute('''INSERT INTO audit_log
                    (correlation_id, operation_type, actor, affected_table, record_id, before_state_json, after_state_json, created_at)
                    VALUES (?, 'update_source_message', 'bulk_action', 'source_messages', ?, ?, ?, ?)''',
                    (correlation_id, m_id, json.dumps(before_msg), json.dumps({'review_status': 'ignored'}), stamp))
                affected += 1

            connection.execute('''INSERT INTO bulk_operations
                (correlation_id, operation_type, item_count, parameters_json, status, created_at)
                VALUES (?, 'bulk_ignore', ?, '{}', 'completed', ?)''',
                (correlation_id, affected, stamp))

        return {'correlation_id': correlation_id, 'affected_count': affected}

    def bulk_assign_metadata(self, message_ids: list[int], client=None, product=None, classification=None):
        if not message_ids:
            return {'correlation_id': None, 'affected_count': 0}
        correlation_id = f'bulk-assign-{uuid.uuid4().hex[:10]}'
        affected = 0
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            stamp = now_iso()
            for m_id in message_ids:
                row = connection.execute('SELECT * FROM source_messages WHERE id=?', (m_id,)).fetchone()
                if not row:
                    continue
                meta = json.loads(row['metadata_json']) if row['metadata_json'] else {}
                before_meta = dict(meta)
                before_class = row['classification']

                if client:
                    meta['client'] = client
                if product:
                    meta['product'] = product
                new_class = classification or before_class

                connection.execute('UPDATE source_messages SET metadata_json=?, classification=? WHERE id=?',
                                   (json.dumps(meta), new_class, m_id))

                connection.execute('''INSERT INTO audit_log
                    (correlation_id, operation_type, actor, affected_table, record_id, before_state_json, after_state_json, created_at)
                    VALUES (?, 'update_source_message', 'bulk_action', 'source_messages', ?, ?, ?, ?)''',
                    (correlation_id, m_id,
                     json.dumps({'metadata_json': json.dumps(before_meta), 'classification': before_class}),
                     json.dumps({'metadata_json': json.dumps(meta), 'classification': new_class}),
                     stamp))
                affected += 1

            connection.execute('''INSERT INTO bulk_operations
                (correlation_id, operation_type, item_count, parameters_json, status, created_at)
                VALUES (?, 'bulk_assign', ?, ?, 'completed', ?)''',
                (correlation_id, affected, json.dumps({'client': client, 'product': product, 'classification': classification}), stamp))

        return {'correlation_id': correlation_id, 'affected_count': affected}

    def create_case(self, title, client=None, product=None, platform=None, channel=None,
                    ticket=None, priority=1, status='new', participation='owned',
                    next_action=None, waiting_on=None, follow_up_at=None, resolution=None,
                    client_updated=False, review_state='approved', source='manual', case_key=None,
                    shift_id=None, detail=None, event_type='created'):
        valid_statuses = {'new','triaged','investigating','waiting_client','waiting_internal',
                          'fix_ready','testing','retest_required','resolved','client_updated','closed'}
        valid_participation = {'owned','handled','assisted','assigned','observed'}
        if status not in valid_statuses:
            raise ValueError('Invalid case status.')
        if participation not in valid_participation:
            raise ValueError('Invalid participation type.')
        with self.connect() as connection:
            client_id = self._client_id(connection, client)
            stamp = now_iso()
            case_id = connection.execute('''INSERT INTO work_cases
                (case_key,client_id,title,product,platform,channel,ticket,priority,status,
                 participation,next_action,waiting_on,follow_up_at,resolution,client_updated,
                 review_state,source,created_at,updated_at,closed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (case_key, client_id, title, product, platform, channel, ticket, int(priority),
                 status, participation, next_action, waiting_on, follow_up_at, resolution,
                 int(bool(client_updated)), review_state, source, stamp, stamp,
                 stamp if status == 'closed' else None)).lastrowid
            if shift_id or detail:
                connection.execute('''INSERT INTO case_events
                    (case_id,shift_id,event_type,detail,actor_role,outcome,occurred_at,created_at)
                    VALUES (?,?,?,?,?,?,?,?)''',
                    (case_id, shift_id, event_type, detail or title, 'owner', status, stamp, stamp))
            return case_id

    def case(self, case_id):
        with self.connect() as connection:
            row = connection.execute('''SELECT work_case.*,client.name AS client
                FROM work_cases work_case LEFT JOIN clients client ON client.id=work_case.client_id
                WHERE work_case.id=?''', (case_id,)).fetchone()
            return dict(row) if row else None

    def list_cases(self, status=None, limit=50, review_state='approved'):
        with self.connect() as connection:
            sql = '''SELECT work_case.*,client.name AS client
                FROM work_cases work_case LEFT JOIN clients client ON client.id=work_case.client_id
                WHERE work_case.review_state=?'''
            params = [review_state]
            if status:
                sql += ' AND work_case.status=?'
                params.append(status)
            sql += ' ORDER BY work_case.updated_at DESC,work_case.id DESC LIMIT ?'
            params.append(limit)
            return [dict(row) for row in connection.execute(sql, params)]

    def add_case_event(self, case_id, event_type, detail, shift_id=None, actor_role='owner',
                       outcome=None, occurred_at=None, source_message_id=None,
                       correlation_id=None, audit_actor='system'):
        with self.connect() as connection:
            case = connection.execute('SELECT * FROM work_cases WHERE id=?', (case_id,)).fetchone()
            if not case:
                raise ValueError('Case not found.')
            stamp = now_iso()
            event_id = connection.execute('''INSERT INTO case_events
                (case_id,shift_id,event_type,detail,actor_role,outcome,source_message_id,
                 occurred_at,created_at) VALUES (?,?,?,?,?,?,?,?,?)''',
                (case_id, shift_id, event_type, detail, actor_role, outcome,
                 source_message_id, occurred_at or stamp, stamp)).lastrowid
            connection.execute('UPDATE work_cases SET updated_at=? WHERE id=?', (stamp, case_id))
            if correlation_id:
                event = connection.execute('SELECT * FROM case_events WHERE id=?', (event_id,)).fetchone()
                self._record_audit_in_connection(
                    connection, correlation_id, 'create_case_event', audit_actor,
                    'case_events', event_id, None, dict(event))
            return event_id

    def update_case(self, case_id, field, value, shift_id=None, detail=None,
                    correlation_id=None, actor='system'):
        allowed = {'title','product','platform','channel','ticket','priority','status','participation',
                   'next_action','waiting_on','follow_up_at','resolution','client_updated','review_state'}
        if field not in allowed:
            raise ValueError('Editable case fields: ' + ', '.join(sorted(allowed)) + '.')
        if field == 'status' and value not in {
                'new','triaged','investigating','waiting_client','waiting_internal','fix_ready',
                'testing','retest_required','resolved','client_updated','closed'}:
            raise ValueError('Invalid case status.')
        if field == 'participation' and value not in {'owned','handled','assisted','assigned','observed'}:
            raise ValueError('Invalid participation type.')
        if field in ('priority','client_updated'):
            value = int(value) if field == 'priority' else int(str(value).casefold() in ('1','true','yes','on'))
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM work_cases WHERE id=?', (case_id,)).fetchone()
            if not row:
                return None
            stamp = now_iso()
            closed_at = stamp if field == 'status' and value == 'closed' else row['closed_at']
            if field == 'status' and value != 'closed':
                closed_at = None
            connection.execute(f'''UPDATE work_cases SET {field}=?,updated_at=?,closed_at=?
                WHERE id=?''', (None if value in ('-', 'none', '') else value, stamp, closed_at, case_id))
            event_detail = detail or f'{field} changed from {row[field]} to {value}'
            event_id = connection.execute('''INSERT INTO case_events
                (case_id,shift_id,event_type,detail,actor_role,outcome,occurred_at,created_at)
                VALUES (?,?,?,?,?,?,?,?)''',
                (case_id, shift_id, 'status' if field == 'status' else 'updated', event_detail,
                 'owner', value if field == 'status' else None, stamp, stamp)).lastrowid
            updated = connection.execute('''SELECT work_case.*,client.name AS client
                FROM work_cases work_case LEFT JOIN clients client ON client.id=work_case.client_id
                WHERE work_case.id=?''', (case_id,)).fetchone()
            if correlation_id:
                before_state = {field: row[field], 'closed_at': row['closed_at']}
                after_state = {field: updated[field], 'closed_at': updated['closed_at']}
                self._record_audit_in_connection(
                    connection, correlation_id, 'update_case', actor, 'work_cases', case_id,
                    before_state, after_state)
                event = connection.execute('SELECT * FROM case_events WHERE id=?', (event_id,)).fetchone()
                self._record_audit_in_connection(
                    connection, correlation_id, 'create_case_event', actor, 'case_events', event_id,
                    None, dict(event))
            return dict(updated)

    def case_events(self, case_id):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute('''SELECT * FROM case_events
                WHERE case_id=? ORDER BY occurred_at,id''', (case_id,))]

    def accept_source_message(self, message_id, shift_id=None, case_id=None):
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            message = connection.execute('''SELECT * FROM source_messages
                WHERE id=? AND review_status='pending' ''', (message_id,)).fetchone()
            if not message:
                raise ValueError('Inbox item is unavailable or already reviewed.')
            if case_id:
                case = connection.execute('SELECT * FROM work_cases WHERE id=?', (case_id,)).fetchone()
                if not case:
                    raise ValueError('Case not found.')
            else:
                classification = message['classification'] or 'note'
                try:
                    metadata = json.loads(message['metadata_json'] or '{}')
                except (json.JSONDecodeError, TypeError):
                    metadata = {}
                status_map = {
                    'client_query': 'new', 'assignment': 'triaged', 'investigation': 'investigating',
                    'testing': 'testing', 'resolution': 'resolved', 'pending': 'waiting_internal',
                    'call': 'investigating', 'note': 'new'}
                stamp = now_iso()
                client_id = self._client_id(connection, metadata.get('client'))
                case_id = connection.execute('''INSERT INTO work_cases
                    (client_id,title,product,platform,channel,ticket,status,participation,
                     review_state,source,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (client_id, (message['redacted_text'] or message['text'] or 'Imported work')[:180],
                     metadata.get('product'), metadata.get('platform'), metadata.get('channel'),
                     metadata.get('ticket'), status_map.get(classification, 'new'), 'handled', 'approved',
                     message['source_type'], stamp, stamp)).lastrowid
            stamp = now_iso()
            connection.execute('''INSERT INTO case_events
                (case_id,shift_id,event_type,detail,actor_role,outcome,source_message_id,
                 occurred_at,created_at) VALUES (?,?,?,?,?,?,?,?,?)''',
                (case_id, shift_id or message['shift_id'], message['classification'] or 'note',
                 message['redacted_text'] or message['text'] or 'Imported work', 'owner', None,
                 message_id, message['occurred_at'] or stamp, stamp))
            connection.execute('''UPDATE source_messages SET review_status='accepted',case_id=?
                WHERE id=?''', (case_id, message_id))
            connection.execute('UPDATE work_cases SET updated_at=? WHERE id=?', (stamp, case_id))
            return case_id

    def merge_cases(self, source_id, target_id):
        if source_id == target_id:
            raise ValueError('Choose two different cases.')
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            for case_id in (source_id, target_id):
                if not connection.execute('SELECT 1 FROM work_cases WHERE id=?', (case_id,)).fetchone():
                    raise ValueError('Case not found.')
            connection.execute('UPDATE case_events SET case_id=? WHERE case_id=?', (target_id, source_id))
            connection.execute('UPDATE source_messages SET case_id=? WHERE case_id=?', (target_id, source_id))
            connection.execute('UPDATE test_sessions SET case_id=? WHERE case_id=?', (target_id, source_id))
            connection.execute('UPDATE evidence SET case_id=? WHERE case_id=?', (target_id, source_id))
            connection.execute('UPDATE followups SET case_id=? WHERE case_id=?', (target_id, source_id))
            connection.execute("UPDATE work_cases SET review_state='merged',updated_at=? WHERE id=?",
                               (now_iso(), source_id))
            connection.execute('UPDATE work_cases SET updated_at=? WHERE id=?', (now_iso(), target_id))

    def add_test_session(self, scenario, shift_id=None, case_id=None, environment=None, build=None,
                         preconditions=None, steps=None, expected=None, actual=None, result='not_run',
                         defects=None, developer_notified=False, retest_required=False,
                         retest_result=None, correlation_id=None, actor='system'):
        if result not in {'passed','failed','partial','blocked','not_run','inconclusive'}:
            raise ValueError('Invalid test result.')
        stamp = now_iso()
        with self.connect() as connection:
            session_id = connection.execute('''INSERT INTO test_sessions
                (case_id,shift_id,scenario,environment,build,preconditions,steps,expected,actual,
                 result,defects,developer_notified,retest_required,retest_result,verified_at,
                 created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (case_id, shift_id, scenario, environment, build, preconditions, steps, expected,
                 actual, result, defects, int(bool(developer_notified)), int(bool(retest_required)),
                 retest_result, stamp if result == 'passed' else None, stamp, stamp)).lastrowid
            event_id = None
            before_case = None
            if case_id:
                before_case = connection.execute('SELECT status,updated_at FROM work_cases WHERE id=?',
                                                 (case_id,)).fetchone()
                event_id = connection.execute('''INSERT INTO case_events
                    (case_id,shift_id,event_type,detail,actor_role,outcome,occurred_at,created_at)
                    VALUES (?,?,?,?,?,?,?,?)''',
                    (case_id, shift_id, 'testing', scenario, 'owner', result, stamp, stamp)).lastrowid
                case_status = 'retest_required' if retest_required else ('fix_ready' if result == 'passed' else 'testing')
                current = connection.execute('SELECT status FROM work_cases WHERE id=?', (case_id,)).fetchone()
                if current and current['status'] not in ('resolved','client_updated','closed'):
                    connection.execute('UPDATE work_cases SET status=?,updated_at=? WHERE id=?',
                                       (case_status, stamp, case_id))
            if correlation_id:
                session = connection.execute('SELECT * FROM test_sessions WHERE id=?', (session_id,)).fetchone()
                self._record_audit_in_connection(
                    connection, correlation_id, 'create_test_session', actor,
                    'test_sessions', session_id, None, dict(session))
                if event_id:
                    event = connection.execute('SELECT * FROM case_events WHERE id=?', (event_id,)).fetchone()
                    self._record_audit_in_connection(
                        connection, correlation_id, 'create_test_event', actor,
                        'case_events', event_id, None, dict(event))
                if before_case:
                    after_case = connection.execute('SELECT status,updated_at FROM work_cases WHERE id=?',
                                                    (case_id,)).fetchone()
                    if dict(after_case) != dict(before_case):
                        self._record_audit_in_connection(
                            connection, correlation_id, 'update_case_for_test', actor,
                            'work_cases', case_id, dict(before_case), dict(after_case))
            return session_id

    def test_sessions(self, shift_id=None, case_id=None, limit=50):
        with self.connect() as connection:
            sql = 'SELECT * FROM test_sessions WHERE 1=1'
            params = []
            if shift_id is not None:
                sql += ' AND shift_id=?'; params.append(shift_id)
            if case_id is not None:
                sql += ' AND case_id=?'; params.append(case_id)
            sql += ' ORDER BY updated_at DESC,id DESC LIMIT ?'; params.append(limit)
            return [dict(row) for row in connection.execute(sql, params)]

    def update_test_session(self, session_id, field, value, shift_id=None,
                            correlation_id=None, actor='system'):
        allowed = {'scenario','environment','build','preconditions','steps','expected','actual',
                   'result','defects','developer_notified','retest_required','retest_result'}
        if field not in allowed:
            raise ValueError('Invalid test-session field.')
        if field == 'result' and value not in {'passed','failed','partial','blocked','not_run','inconclusive'}:
            raise ValueError('Invalid test result.')
        if field in ('developer_notified','retest_required'):
            value = int(str(value).casefold() in ('1','true','yes','on'))
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM test_sessions WHERE id=?', (session_id,)).fetchone()
            if not row:
                raise ValueError('Test session not found.')
            stamp = now_iso()
            verified = stamp if field == 'result' and value == 'passed' else row['verified_at']
            connection.execute(f'''UPDATE test_sessions SET {field}=?,updated_at=?,verified_at=?
                WHERE id=?''', (None if value in ('','-','none') else value, stamp, verified, session_id))
            event_id = None
            if row['case_id'] and field in ('result','actual','defects','retest_result'):
                event_id = connection.execute('''INSERT INTO case_events
                    (case_id,shift_id,event_type,detail,actor_role,outcome,occurred_at,created_at)
                    VALUES (?,?,?,?,?,?,?,?)''',
                    (row['case_id'], shift_id or row['shift_id'], 'testing_update',
                     f'TEST-{session_id} {field}: {value}', 'owner',
                     value if field == 'result' else None, stamp, stamp)).lastrowid
            if correlation_id:
                updated = connection.execute('SELECT * FROM test_sessions WHERE id=?', (session_id,)).fetchone()
                fields = (field, 'updated_at', 'verified_at')
                self._record_audit_in_connection(
                    connection, correlation_id, 'update_test_session', actor,
                    'test_sessions', session_id,
                    {k: row[k] for k in fields}, {k: updated[k] for k in fields})
                if event_id:
                    event = connection.execute('SELECT * FROM case_events WHERE id=?', (event_id,)).fetchone()
                    self._record_audit_in_connection(
                        connection, correlation_id, 'create_test_update_event', actor,
                        'case_events', event_id, None, dict(event))

    def evidence_item(self, evidence_id):
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM evidence WHERE id=?', (evidence_id,)).fetchone()
            return dict(row) if row else None

    def add_evidence(self, kind, shift_id=None, case_id=None, test_session_id=None, path=None,
                     telegram_file_id=None, caption=None, sha256=None, mime_type=None):
        with self.connect() as connection:
            evidence_id = connection.execute('''INSERT OR IGNORE INTO evidence
                (case_id,test_session_id,shift_id,kind,path,telegram_file_id,caption,sha256,mime_type,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (case_id, test_session_id, shift_id, kind, path, telegram_file_id, caption,
                 sha256, mime_type, now_iso())).lastrowid
            if not evidence_id and sha256:
                row = connection.execute('''SELECT id FROM evidence WHERE sha256=?
                    AND case_id IS ? AND test_session_id IS ?''',
                    (sha256, case_id, test_session_id)).fetchone()
                return row[0]
            return evidence_id

    def evidence(self, case_id=None, test_session_id=None):
        with self.connect() as connection:
            sql = 'SELECT * FROM evidence WHERE 1=1'; params = []
            if case_id is not None:
                sql += ' AND case_id=?'; params.append(case_id)
            if test_session_id is not None:
                sql += ' AND test_session_id=?'; params.append(test_session_id)
            sql += ' ORDER BY id DESC'
            return [dict(row) for row in connection.execute(sql, params)]

    def add_followup(self, case_id, due_at, note=None, waiting_on=None, shift_id=None, kind='case',
                     correlation_id=None, actor='system'):
        with self.connect() as connection:
            if not connection.execute('SELECT 1 FROM work_cases WHERE id=?', (case_id,)).fetchone():
                raise ValueError('Case not found.')
            before_case = connection.execute(
                'SELECT follow_up_at,waiting_on,updated_at FROM work_cases WHERE id=?', (case_id,)).fetchone()
            followup_id = connection.execute('''INSERT INTO followups
                (case_id,shift_id,due_at,kind,waiting_on,note,created_at)
                VALUES (?,?,?,?,?,?,?)''',
                (case_id, shift_id, due_at, kind, waiting_on, note, now_iso())).lastrowid
            connection.execute('''UPDATE work_cases SET follow_up_at=?,waiting_on=COALESCE(?,waiting_on),
                updated_at=? WHERE id=?''', (due_at, waiting_on, now_iso(), case_id))
            if correlation_id:
                followup = connection.execute('SELECT * FROM followups WHERE id=?', (followup_id,)).fetchone()
                self._record_audit_in_connection(
                    connection, correlation_id, 'create_followup', actor,
                    'followups', followup_id, None, dict(followup))
                after_case = connection.execute(
                    'SELECT follow_up_at,waiting_on,updated_at FROM work_cases WHERE id=?', (case_id,)).fetchone()
                self._record_audit_in_connection(
                    connection, correlation_id, 'update_case_for_followup', actor,
                    'work_cases', case_id, dict(before_case), dict(after_case))
            return followup_id

    def list_followups(self, status='pending', limit=50):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute('''SELECT followup.*,work_case.title
                FROM followups followup JOIN work_cases work_case ON work_case.id=followup.case_id
                WHERE followup.status=? ORDER BY followup.due_at LIMIT ?''', (status, limit))]

    def due_followups(self, due_at, limit=20):
        target = datetime.fromisoformat(due_at)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        with self.connect() as connection:
            rows = [dict(row) for row in connection.execute('''SELECT followup.*,work_case.title
                FROM followups followup JOIN work_cases work_case ON work_case.id=followup.case_id
                WHERE followup.status='pending' AND followup.reminded_at IS NULL
                ORDER BY followup.due_at''')]
            due = []
            for row in rows:
                moment = datetime.fromisoformat(row['due_at'])
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
                if moment <= target:
                    due.append(row)
                    if len(due) >= limit:
                        break
            return due

    def remind_followups(self, followup_ids):
        if not followup_ids:
            return
        placeholders = ','.join('?' for _ in followup_ids)
        with self.connect() as connection:
            connection.execute(f'UPDATE followups SET reminded_at=? WHERE id IN ({placeholders})',
                               [now_iso(), *followup_ids])

    def complete_followup(self, followup_id, correlation_id=None, actor='system'):
        with self.connect() as connection:
            before = connection.execute('SELECT * FROM followups WHERE id=?', (followup_id,)).fetchone()
            if connection.execute('''UPDATE followups SET status='completed',completed_at=?
                WHERE id=? AND status='pending' ''', (now_iso(), followup_id)).rowcount != 1:
                raise ValueError('Follow-up is unavailable or already completed.')
            if correlation_id:
                after = connection.execute('SELECT * FROM followups WHERE id=?', (followup_id,)).fetchone()
                fields = ('status', 'completed_at')
                self._record_audit_in_connection(
                    connection, correlation_id, 'complete_followup', actor, 'followups', followup_id,
                    {k: before[k] for k in fields}, {k: after[k] for k in fields})

    def snooze_followup(self, followup_id, due_at, correlation_id=None, actor='system'):
        with self.connect() as connection:
            before = connection.execute('SELECT * FROM followups WHERE id=?', (followup_id,)).fetchone()
            if connection.execute('''UPDATE followups SET due_at=?,reminded_at=NULL
                WHERE id=? AND status='pending' ''', (due_at, followup_id)).rowcount != 1:
                raise ValueError('Follow-up is unavailable or already completed.')
            if correlation_id:
                after = connection.execute('SELECT * FROM followups WHERE id=?', (followup_id,)).fetchone()
                fields = ('due_at', 'reminded_at')
                self._record_audit_in_connection(
                    connection, correlation_id, 'snooze_followup', actor, 'followups', followup_id,
                    {k: before[k] for k in fields}, {k: after[k] for k in fields})

    def connector_state(self, connector):
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM connector_state WHERE connector=?',
                                     (connector,)).fetchone()
            return dict(row) if row else None

    def save_connector_state(self, connector, cursor=None, error=None, metadata=None):
        with self.connect() as connection:
            connection.execute('''INSERT INTO connector_state
                (connector,cursor,last_sync_at,last_error,metadata_json) VALUES (?,?,?,?,?)
                ON CONFLICT(connector) DO UPDATE SET cursor=excluded.cursor,
                last_sync_at=excluded.last_sync_at,last_error=excluded.last_error,
                metadata_json=excluded.metadata_json''',
                (connector, cursor, now_iso(), error, json.dumps(metadata or {})))

    def cases_for_shift(self, shift_id):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute('''SELECT DISTINCT work_case.*,
                client.name AS client FROM work_cases work_case
                LEFT JOIN clients client ON client.id=work_case.client_id
                JOIN case_events event ON event.case_id=work_case.id
                WHERE event.shift_id=? AND work_case.review_state='approved'
                ORDER BY work_case.updated_at''', (shift_id,))]

    def case_metrics(self, shift_id):
        cases = self.cases_for_shift(shift_id)
        clients = {item['client'].strip().casefold() for item in cases if item.get('client')}
        worked = [item for item in cases if item['participation'] in ('owned','handled','assisted')]
        return {
            'clients_handled': len({item['client'].strip().casefold() for item in worked if item.get('client')}),
            'queries_worked': len(worked),
            'queries_resolved': sum(item['status'] in ('resolved','client_updated','closed') for item in worked),
            'clients_updated': sum(bool(item['client_updated']) or item['status'] in ('client_updated','closed') for item in worked),
            'assisted': sum(item['participation'] == 'assisted' for item in worked),
            'named_clients': len(clients),
        }

    def search(self, query, category=None, limit=30):
        pattern = f'%{query.casefold()}%'
        results = []
        with self.connect() as connection:
            for row in connection.execute('''SELECT id,title,status,due_date,project,client,ticket
                FROM tasks WHERE lower(title||' '||coalesce(project,'')||' '||coalesce(client,'')||' '
                    ||coalesce(ticket,'')||' '||coalesce(tags,'')) LIKE ? ORDER BY id DESC LIMIT ?''',
                                          (pattern, limit)):
                results.append({'type': 'task', **dict(row)})
            sql = '''SELECT activity.id,activity.category,activity.detail,activity.created_at,
                COALESCE(client.name,activity.client) AS client,activity.channel FROM activities activity
                LEFT JOIN support_interactions support ON support.activity_id=activity.id
                LEFT JOIN clients client ON client.id=support.client_id
                LEFT JOIN testing_records testing ON testing.activity_id=activity.id
                LEFT JOIN learning_records learning ON learning.activity_id=activity.id
                WHERE activity.category NOT IN ('deleted','converted')
                AND lower(activity.detail||' '||coalesce(client.name,activity.client,'')||' '
                    ||coalesce(activity.channel,'')||' '||coalesce(support.product,'')||' '
                    ||coalesce(support.query_category,'')||' '||coalesce(support.ticket,'')||' '
                    ||coalesce(testing.product,'')||' '||coalesce(testing.environment,'')||' '
                    ||coalesce(testing.build,'')||' '||coalesce(testing.ticket,'')||' '
                    ||coalesce(learning.product,'')||' '||coalesce(learning.takeaway,'')) LIKE ?'''
            params = [pattern]
            if category:
                sql += ' AND activity.category=?'
                params.append(category)
            sql += ' ORDER BY activity.id DESC LIMIT ?'
            params.append(limit)
            for row in connection.execute(sql, params):
                results.append({'type': 'activity', **dict(row)})
            if not category:
                for row in connection.execute('''SELECT work_case.id,work_case.title,work_case.status,
                    client.name AS client,work_case.channel,work_case.ticket,work_case.updated_at
                    FROM work_cases work_case LEFT JOIN clients client ON client.id=work_case.client_id
                    WHERE work_case.review_state='approved' AND lower(work_case.title||' '
                        ||coalesce(client.name,'')||' '||coalesce(work_case.channel,'')||' '
                        ||coalesce(work_case.ticket,'')||' '||coalesce(work_case.product,'')||' '
                        ||coalesce(work_case.platform,'')) LIKE ? ORDER BY work_case.id DESC LIMIT ?''',
                                              (pattern, limit)):
                    results.append({'type': 'case', **dict(row)})
        return results[:limit]

    # --- Phase 4: Audit and Safe Undo Service ---

    def _record_audit_in_connection(self, connection, correlation_id, operation_type, actor,
                                    affected_table, record_id, before_state, after_state,
                                    reversibility='reversible'):
        return connection.execute('''INSERT INTO audit_log
            (correlation_id, operation_type, actor, affected_table, record_id,
             before_state_json, after_state_json, reversibility, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (correlation_id, operation_type, actor, affected_table, record_id,
             json.dumps(before_state, default=str) if before_state is not None else None,
             json.dumps(after_state, default=str) if after_state is not None else None,
             reversibility, now_iso())).lastrowid

    def record_audit(self, correlation_id, operation_type, actor, affected_table, record_id,
                     before_state_json=None, after_state_json=None, reversibility='reversible'):
        with self.connect() as connection:
            before = json.loads(before_state_json) if before_state_json else None
            after = json.loads(after_state_json) if after_state_json else None
            return self._record_audit_in_connection(
                connection, correlation_id, operation_type, actor, affected_table,
                record_id, before, after, reversibility)

    def get_audit_log(self, limit=50, correlation_id=None, table_name=None, record_id=None):
        with self.connect() as connection:
            sql = 'SELECT * FROM audit_log WHERE 1=1'
            params = []
            if correlation_id:
                sql += ' AND correlation_id=?'
                params.append(correlation_id)
            if table_name:
                sql += ' AND affected_table=?'
                params.append(table_name)
            if record_id is not None:
                sql += ' AND record_id=?'
                params.append(record_id)
            sql += ' ORDER BY id DESC LIMIT ?'
            params.append(limit)
            return [dict(r) for r in connection.execute(sql, params).fetchall()]

    def get_last_reversible_audit(self):
        with self.connect() as connection:
            row = connection.execute('''SELECT * FROM audit_log
                WHERE reversibility='reversible' AND reverted_at IS NULL
                ORDER BY id DESC LIMIT 1''').fetchone()
            return dict(row) if row else None

    def undo_audit_record(self, audit_id: int) -> dict:
        """
        Undoes the entire correlation batch associated with audit_id.
        Returns a consistent typed UndoResult dict.
        """
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM audit_log WHERE id=?', (audit_id,)).fetchone()
            if not row:
                raise ValueError(f'Audit record #{audit_id} not found.')
            correlation_id = row['correlation_id']
        return self.undo_audit_batch(correlation_id)

    def _apply_single_undo(self, connection, row, stamp: str) -> dict:
        table = row['affected_table']
        record_id = row['record_id']
        audit_id = row['id']
        allowed_tables = {
            'tasks', 'work_cases', 'case_events', 'activities',
            'followups', 'test_sessions', 'evidence', 'client_aliases',
            'history_clusters', 'cluster_items', 'shift_calendar',
            'source_messages', 'shift_templates', 'shifts'
        }
        if table not in allowed_tables:
            raise ValueError(f'Table {table} does not support automatic undo.')

        before_state = json.loads(row['before_state_json']) if row['before_state_json'] else None
        after_state = json.loads(row['after_state_json']) if row['after_state_json'] else None

        current = connection.execute(f'SELECT * FROM {table} WHERE id=?', (record_id,)).fetchone()

        # Case 1: Was an INSERT (before is None, after is not None)
        if before_state is None and after_state is not None:
            if not current:
                raise ValueError(f'Record #{record_id} in {table} already deleted; cannot undo insertion.')
            current_dict = dict(current)
            for k, v in after_state.items():
                if k in current_dict and v is not None:
                    curr_v = current_dict[k]
                    if str(curr_v) != str(v) and curr_v != v:
                        raise ValueError(f'Tamper detected on {table} #{record_id}: field {k} has changed since this operation ({curr_v!r} != {v!r}); cannot safely undo.')

            if table == 'tasks':
                connection.execute('DELETE FROM activities WHERE task_id=?', (record_id,))
                connection.execute('UPDATE conversation_context SET active_task_id=NULL WHERE active_task_id=?', (record_id,))
            elif table == 'activities':
                connection.execute('DELETE FROM support_interactions WHERE activity_id=?', (record_id,))
                connection.execute('DELETE FROM testing_records WHERE activity_id=?', (record_id,))
                connection.execute('DELETE FROM learning_records WHERE activity_id=?', (record_id,))
                connection.execute('DELETE FROM activity_audit WHERE activity_id=?', (record_id,))
                connection.execute('DELETE FROM proposals WHERE activity_id=?', (record_id,))
            elif table == 'work_cases':
                connection.execute('DELETE FROM case_events WHERE case_id=?', (record_id,))
                connection.execute('DELETE FROM activities WHERE case_id=?', (record_id,))
                connection.execute('UPDATE conversation_context SET active_case_id=NULL WHERE active_case_id=?', (record_id,))
            elif table == 'test_sessions':
                connection.execute('DELETE FROM evidence WHERE test_session_id=?', (record_id,))
                if current and current['case_id'] and current['scenario']:
                    connection.execute('DELETE FROM case_events WHERE case_id=? AND event_type="testing" AND detail=?',
                                       (current['case_id'], current['scenario']))
                connection.execute('UPDATE conversation_context SET active_test_session_id=NULL WHERE active_test_session_id=?', (record_id,))
            elif table == 'shifts':
                connection.execute('DELETE FROM activities WHERE shift_id=?', (record_id,))

            connection.execute(f'DELETE FROM {table} WHERE id=?', (record_id,))

        # Case 2: Was a DELETE (before is not None, after is None)
        elif before_state is not None and after_state is None:
            if current:
                raise ValueError(f'Record #{record_id} in {table} already exists; cannot undo deletion.')
            cols = list(before_state.keys())
            placeholders = ','.join(['?'] * len(cols))
            col_names = ','.join(cols)
            connection.execute(f'INSERT INTO {table} ({col_names}) VALUES ({placeholders})',
                               [before_state[c] for c in cols])

        # Case 3: Was an UPDATE (both before and after exist)
        elif before_state is not None and after_state is not None:
            if not current:
                raise ValueError(f'Record #{record_id} in {table} no longer exists; cannot undo update.')
            current_dict = dict(current)
            for k, v in after_state.items():
                if k in current_dict and v is not None:
                    curr_v = current_dict[k]
                    if str(curr_v) != str(v) and curr_v != v:
                        raise ValueError(f'Tamper detected on {table} #{record_id}: field {k} has changed since this operation ({curr_v!r} != {v!r}); cannot safely undo.')

            set_clauses = ', '.join(f'{k}=?' for k in before_state.keys())
            connection.execute(f'UPDATE {table} SET {set_clauses} WHERE id=?',
                               list(before_state.values()) + [record_id])
        else:
            raise ValueError(f'Invalid audit record state for audit #{audit_id}.')

        connection.execute('UPDATE audit_log SET reverted_at=? WHERE id=?', (stamp, audit_id))
        return {'audit_id': audit_id, 'table': table, 'record_id': record_id}

    def undo_audit_batch(self, correlation_id: str) -> dict:
        """
        Atomically undoes all operations for correlation_id inside a single transaction.
        If any tamper check or validation fails, rolls back completely.
        Returns a typed UndoResult dict.
        """
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            rows = connection.execute('''SELECT * FROM audit_log
                WHERE correlation_id=? AND reverted_at IS NULL AND reversibility='reversible'
                ORDER BY id DESC''', (correlation_id,)).fetchall()
            if not rows:
                raise ValueError(f'No reversible audit entries found for correlation ID {correlation_id}.')

            primary_row = dict(rows[0])
            stamp = now_iso()
            undone_details = []

            for row in rows:
                detail = self._apply_single_undo(connection, row, stamp)
                undone_details.append(detail)

            connection.execute('UPDATE bulk_operations SET reverted_at=? WHERE correlation_id=?', (stamp, correlation_id))
            return {
                'id': primary_row['id'],
                'correlation_id': correlation_id,
                'operation_type': primary_row['operation_type'],
                'affected_table': primary_row['affected_table'],
                'record_id': primary_row['record_id'],
                'undone_count': len(undone_details),
                'details': undone_details
            }

    # --- Phase 4: Natural Language Interactions ---

    def record_nl_interaction(self, raw_text, intent, entities=None, confidence=0.0, proposed_ops=None,
                              applied_ops=None, provider=None, model=None, parser_version='v2',
                              status='processed', clarification=None, error_details=None, source_update_id=None,
                              reason_codes=None, normalized_text=None):
        now = now_iso()
        with self.connect() as connection:
            cur = connection.execute('''INSERT INTO nl_interactions
                (source_update_id, raw_text, intent, entities_json, confidence, proposed_operations_json,
                 applied_operations_json, provider, model, parser_version, status, clarification_json,
                 error_details, reason_codes_json, normalized_text, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (source_update_id, raw_text, intent,
                 json.dumps(entities) if entities else None,
                 confidence,
                 json.dumps(proposed_ops) if proposed_ops else None,
                 json.dumps(applied_ops) if applied_ops else None,
                 provider, model, parser_version, status,
                 json.dumps(clarification) if clarification else None,
                 error_details,
                 json.dumps(reason_codes) if reason_codes else None,
                 normalized_text,
                 now, now))
            return cur.lastrowid

    def get_nl_interactions(self, limit=50):
        with self.connect() as connection:
            rows = connection.execute('SELECT * FROM nl_interactions ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
            return [dict(r) for r in rows]

    def get_nl_interaction(self, interaction_id: int) -> dict | None:
        with self.connect() as connection:
            row = connection.execute(
                'SELECT * FROM nl_interactions WHERE id=?', (interaction_id,)).fetchone()
            return dict(row) if row else None

    def add_nl_correction(self, original_intent: str, corrected_intent: str,
                          interaction_id: int = None, corrected_entities: dict = None,
                          owner_id: int = None, notes: str = None,
                          parser_version: str = 'v2') -> int:
        now = now_iso()
        oid = int(owner_id) if (owner_id is not None and str(owner_id).isdigit()) else (config.OWNER_ID or 1)
        entities_json = json.dumps(corrected_entities) if corrected_entities else None
        with self.connect() as connection:
            cur = connection.execute('''INSERT INTO nl_corrections
                (interaction_id, original_intent, corrected_intent, corrected_entities_json,
                 owner_id, parser_version, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (interaction_id, original_intent, corrected_intent, entities_json,
                 oid, parser_version, notes, now))
            corr_id = cur.lastrowid

        self.record_audit(
            correlation_id=f"corr_{corr_id}",
            operation_type="create_nl_correction",
            actor=str(oid),
            affected_table="nl_corrections",
            record_id=corr_id,
            before_state_json=None,
            after_state_json=json.dumps({
                'id': corr_id,
                'interaction_id': interaction_id,
                'original_intent': original_intent,
                'corrected_intent': corrected_intent,
                'corrected_entities': corrected_entities,
                'notes': notes,
            }),
            reversibility='non_reversible'
        )
        return corr_id

    def get_recent_unknown_interactions(self, limit: int = 10, max_confidence: float = 0.80) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute('''
                SELECT * FROM nl_interactions
                WHERE intent = 'unknown' OR confidence < ?
                ORDER BY id DESC LIMIT ?
            ''', (max_confidence, limit)).fetchall()
            results = []
            for r in rows:
                d = dict(r)
                if d.get('entities_json'):
                    try:
                        d['entities'] = json.loads(d['entities_json'])
                    except Exception:
                        d['entities'] = {}
                else:
                    d['entities'] = {}
                if d.get('reason_codes_json'):
                    try:
                        d['reason_codes'] = json.loads(d['reason_codes_json'])
                    except Exception:
                        d['reason_codes'] = []
                else:
                    d['reason_codes'] = []
                results.append(d)
            return results

    def get_nl_stats(self, days: int = 7) -> dict:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self.connect() as connection:
            total_rows = connection.execute(
                'SELECT COUNT(*), AVG(confidence) FROM nl_interactions WHERE created_at >= ?',
                (cutoff,)
            ).fetchone()
            total_count = total_rows[0] or 0
            avg_confidence = round(total_rows[1] or 0.0, 3)

            by_intent_rows = connection.execute('''
                SELECT intent, COUNT(*) as cnt, AVG(confidence) as avg_c
                FROM nl_interactions
                WHERE created_at >= ?
                GROUP BY intent
                ORDER BY cnt DESC
            ''', (cutoff,)).fetchall()
            by_intent = {r['intent']: {'count': r['cnt'], 'avg_confidence': round(r['avg_c'] or 0.0, 3)} for r in by_intent_rows}

            unknown_count = connection.execute(
                "SELECT COUNT(*) FROM nl_interactions WHERE created_at >= ? AND intent = 'unknown'",
                (cutoff,)
            ).fetchone()[0] or 0

            low_conf_count = connection.execute(
                "SELECT COUNT(*) FROM nl_interactions WHERE created_at >= ? AND confidence < 0.80",
                (cutoff,)
            ).fetchone()[0] or 0

            corrections_count = connection.execute(
                "SELECT COUNT(*) FROM nl_corrections WHERE created_at >= ?",
                (cutoff,)
            ).fetchone()[0] or 0

            return {
                'days': days,
                'total_interactions': total_count,
                'avg_confidence': avg_confidence,
                'unknown_count': unknown_count,
                'low_confidence_count': low_conf_count,
                'corrections_count': corrections_count,
                'by_intent': by_intent,
            }

    def get_approved_corrections(self, limit: int = 5, intent: str = None) -> list[dict]:
        with self.connect() as connection:
            if intent:
                rows = connection.execute('''
                    SELECT c.*, i.raw_text
                    FROM nl_corrections c
                    LEFT JOIN nl_interactions i ON c.interaction_id = i.id
                    WHERE c.corrected_intent = ?
                    ORDER BY c.id DESC LIMIT ?
                ''', (intent, limit)).fetchall()
            else:
                rows = connection.execute('''
                    SELECT c.*, i.raw_text
                    FROM nl_corrections c
                    LEFT JOIN nl_interactions i ON c.interaction_id = i.id
                    ORDER BY c.id DESC LIMIT ?
                ''', (limit,)).fetchall()
            results = []
            for r in rows:
                d = dict(r)
                if d.get('corrected_entities_json'):
                    try:
                        d['corrected_entities'] = json.loads(d['corrected_entities_json'])
                    except Exception:
                        d['corrected_entities'] = {}
                else:
                    d['corrected_entities'] = {}
                results.append(d)
            return results

    def update_nl_interaction(self, interaction_id, status=None, applied_ops=None, error_details=None):
        now = now_iso()
        with self.connect() as connection:
            clauses = ['updated_at=?']
            params = [now]
            if status is not None:
                clauses.append('status=?')
                params.append(status)
            if applied_ops is not None:
                clauses.append('applied_operations_json=?')
                params.append(json.dumps(applied_ops))
            if error_details is not None:
                clauses.append('error_details=?')
                params.append(error_details)
            params.append(interaction_id)
            connection.execute(f'UPDATE nl_interactions SET {", ".join(clauses)} WHERE id=?', params)

    # --- Phase 4: Conversation Context ---

    def get_conversation_context(self, context_key='owner') -> dict:
        now = now_iso()
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM conversation_context WHERE context_key=?', (context_key,)).fetchone()
            if not row:
                return {}
            if row['expires_at'] < now:
                return {}
            data = json.loads(row['context_data_json']) if row['context_data_json'] else {}
            data.update({
                'active_case_id': row['active_case_id'],
                'active_task_id': row['active_task_id'],
                'active_test_session_id': row['active_test_session_id'],
                'active_client': row['active_client'],
                'last_intent': row['last_intent'],
                'expires_at': row['expires_at']
            })
            return data

    def update_conversation_context(self, context_key='owner', active_case_id=None, active_task_id=None,
                                    active_test_session_id=None, active_client=None, last_intent=None,
                                    context_data=None, ttl_minutes=60):
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(minutes=ttl_minutes)).isoformat()
        current = self.get_conversation_context(context_key)
        merged_data = current.get('data', {})
        if context_data:
            merged_data.update(context_data)

        c_id = active_case_id if active_case_id is not None else current.get('active_case_id')
        t_id = active_task_id if active_task_id is not None else current.get('active_task_id')
        ts_id = active_test_session_id if active_test_session_id is not None else current.get('active_test_session_id')
        client = active_client if active_client is not None else current.get('active_client')
        intent = last_intent if last_intent is not None else current.get('last_intent')

        with self.connect() as connection:
            connection.execute('''INSERT INTO conversation_context
                (context_key, active_case_id, active_task_id, active_test_session_id, active_client,
                 last_intent, context_data_json, expires_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(context_key) DO UPDATE SET
                    active_case_id=excluded.active_case_id, active_task_id=excluded.active_task_id,
                    active_test_session_id=excluded.active_test_session_id, active_client=excluded.active_client,
                    last_intent=excluded.last_intent, context_data_json=excluded.context_data_json,
                    expires_at=excluded.expires_at, updated_at=excluded.updated_at''',
                (context_key, c_id, t_id, ts_id, client, intent, json.dumps(merged_data), expires, now.isoformat()))

    def clear_expired_context(self):
        now = now_iso()
        with self.connect() as connection:
            connection.execute('DELETE FROM conversation_context WHERE expires_at < ?', (now,))

    # --- Phase 4: Client Normalization ---

    def add_client_alias(self, client_id, alias, product=None, domain=None, confidence=1.0, is_verified=0):
        with self.connect() as connection:
            cur = connection.execute('''INSERT INTO client_aliases
                (client_id, alias, product, domain, confidence, is_verified, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(alias) DO UPDATE SET
                    client_id=excluded.client_id, product=excluded.product,
                    domain=excluded.domain, confidence=excluded.confidence,
                    is_verified=excluded.is_verified''',
                (client_id, alias.strip().casefold(), product, domain, confidence, is_verified, now_iso()))
            return cur.lastrowid

    def normalize_client(self, raw_name: str) -> tuple[int | None, str, float]:
        if not raw_name or not raw_name.strip():
            return None, '', 0.0
        cleaned = raw_name.strip()
        folded = cleaned.casefold()
        with self.connect() as connection:
            alias_row = connection.execute('''SELECT ca.client_id, c.name, ca.confidence
                FROM client_aliases ca JOIN clients c ON c.id=ca.client_id
                WHERE ca.alias=?''', (folded,)).fetchone()
            if alias_row:
                return alias_row['client_id'], alias_row['name'], float(alias_row['confidence'])

            client_row = connection.execute('SELECT id, name FROM clients WHERE normalized=?', (folded,)).fetchone()
            if client_row:
                return client_row['id'], client_row['name'], 1.0

            match = connection.execute('SELECT id, name FROM clients WHERE normalized LIKE ? LIMIT 2', (folded + '%',)).fetchall()
            if len(match) == 1:
                return match[0]['id'], match[0]['name'], 0.8

            return None, cleaned, 0.0

    def list_clients_with_aliases(self):
        with self.connect() as connection:
            clients = [dict(r) for r in connection.execute('SELECT * FROM clients ORDER BY name').fetchall()]
            for c in clients:
                aliases = connection.execute('SELECT * FROM client_aliases WHERE client_id=?', (c['id'],)).fetchall()
                c['aliases'] = [dict(a) for a in aliases]
            return clients

    # --- Phase 4: Historical Clustering ---

    def create_cluster(self, title, suggested_client=None, suggested_product=None,
                       suggested_issue_type=None, confidence=0.5, reason=None, status='pending', fingerprint=None):
        now = now_iso()
        with self.connect() as connection:
            cur = connection.execute('''INSERT INTO history_clusters
                (title, suggested_client, suggested_product, suggested_issue_type, confidence,
                 reason, status, fingerprint, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (title, suggested_client, suggested_product, suggested_issue_type,
                 confidence, reason, status, fingerprint, now, now))
            return cur.lastrowid

    def get_cluster_by_fingerprint(self, fingerprint: str) -> dict | None:
        if not fingerprint:
            return None
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM history_clusters WHERE fingerprint=?', (fingerprint,)).fetchone()
            return dict(row) if row else None

    def add_cluster_item(self, cluster_id, source_message_id, relevance_score=1.0):
        with self.connect() as connection:
            cur = connection.execute('''INSERT OR IGNORE INTO cluster_items
                (cluster_id, source_message_id, relevance_score, created_at)
                VALUES (?, ?, ?, ?)''',
                (cluster_id, source_message_id, relevance_score, now_iso()))
            connection.execute('UPDATE source_messages SET cluster_id=? WHERE id=?', (cluster_id, source_message_id))
            return cur.lastrowid

    def get_clusters(self, status=None):
        with self.connect() as connection:
            sql = '''SELECT hc.*, COUNT(ci.id) as message_count
                FROM history_clusters hc
                LEFT JOIN cluster_items ci ON ci.cluster_id=hc.id'''
            params = []
            if status:
                sql += ' WHERE hc.status=?'
                params.append(status)
            sql += ' GROUP BY hc.id ORDER BY hc.id DESC'
            return [dict(r) for r in connection.execute(sql, params).fetchall()]

    def get_cluster(self, cluster_id):
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM history_clusters WHERE id=?', (cluster_id,)).fetchone()
            if not row:
                return None
            data = dict(row)
            items = connection.execute('''SELECT sm.*, ci.relevance_score
                FROM cluster_items ci JOIN source_messages sm ON sm.id=ci.source_message_id
                WHERE ci.cluster_id=? ORDER BY sm.occurred_at''', (cluster_id,)).fetchall()
            data['messages'] = [dict(r) for r in items]
            return data

    def get_cluster_items(self, cluster_id):
        with self.connect() as connection:
            items = connection.execute('''SELECT sm.*, ci.relevance_score
                FROM cluster_items ci JOIN source_messages sm ON sm.id=ci.source_message_id
                WHERE ci.cluster_id=? ORDER BY sm.occurred_at''', (cluster_id,)).fetchall()
            return [dict(r) for r in items]

    def update_cluster_status(self, cluster_id, status, case_id=None, merged_into_id=None):
        with self.connect() as connection:
            connection.execute('''UPDATE history_clusters
                SET status=?, case_id=coalesce(?, case_id), merged_into_id=coalesce(?, merged_into_id), updated_at=?
                WHERE id=?''', (status, case_id, merged_into_id, now_iso(), cluster_id))

    # --- Phase 4: Bulk Operations Registry ---

    def record_bulk_operation(self, correlation_id, operation_type, item_count, parameters=None):
        now = now_iso()
        with self.connect() as connection:
            cur = connection.execute('''INSERT INTO bulk_operations
                (correlation_id, operation_type, item_count, parameters_json, status, created_at)
                VALUES (?, ?, ?, ?, 'completed', ?)''',
                (correlation_id, operation_type, item_count, json.dumps(parameters or {}), now))
            return cur.lastrowid

    def get_bulk_operations(self, limit=20):
        with self.connect() as connection:
            rows = connection.execute('SELECT * FROM bulk_operations ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
            return [dict(r) for r in rows]

    def undo_bulk_operation(self, correlation_id: str) -> dict:
        undone_res = self.undo_audit_batch(correlation_id)
        now = now_iso()
        with self.connect() as connection:
            connection.execute('UPDATE bulk_operations SET status="reverted", reverted_at=? WHERE correlation_id=?',
                               (now, correlation_id))
        return {'correlation_id': correlation_id, 'reverted_records': undone_res.get('undone_count', 0)}

    # --- Phase 4: Shift Templates and Calendar ---

    def create_shift_template(self, name, start_time, lunch_time, end_time, reminder_offsets='30,15',
                              active_weekdays='0,1,2,3,4', timezone='Asia/Kolkata'):
        with self.connect() as connection:
            cur = connection.execute('''INSERT INTO shift_templates
                (name, start_time, lunch_time, end_time, reminder_offsets, active_weekdays, timezone, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    start_time=excluded.start_time, lunch_time=excluded.lunch_time,
                    end_time=excluded.end_time, reminder_offsets=excluded.reminder_offsets,
                    active_weekdays=excluded.active_weekdays, timezone=excluded.timezone''',
                (name, start_time, lunch_time, end_time, reminder_offsets, active_weekdays, timezone, now_iso()))
            return cur.lastrowid

    def list_shift_templates(self):
        with self.connect() as connection:
            return [dict(r) for r in connection.execute('SELECT * FROM shift_templates ORDER BY id').fetchall()]

    def get_shift_template(self, identifier):
        with self.connect() as connection:
            if isinstance(identifier, int) or (isinstance(identifier, str) and identifier.isdigit()):
                row = connection.execute('SELECT * FROM shift_templates WHERE id=?', (int(identifier),)).fetchone()
            else:
                row = connection.execute('SELECT * FROM shift_templates WHERE lower(name)=?', (str(identifier).casefold(),)).fetchone()
            return dict(row) if row else None

    def set_shift_calendar_override(self, date_str, template_id=None, start_time=None, lunch_time=None,
                                    end_time=None, is_day_off=0, note=None, is_explicit_override=1, correlation_id=None):
        now = now_iso()
        corr = correlation_id or f'shift-cal-{uuid.uuid4().hex[:10]}'
        with self.connect() as connection:
            existing = connection.execute('SELECT * FROM shift_calendar WHERE date=?', (date_str,)).fetchone()
            before = dict(existing) if existing else None
            cur = connection.execute('''INSERT INTO shift_calendar
                (date, template_id, start_time, lunch_time, end_time, is_day_off, note, is_explicit_override, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    template_id=excluded.template_id, start_time=excluded.start_time,
                    lunch_time=excluded.lunch_time, end_time=excluded.end_time,
                    is_day_off=excluded.is_day_off, note=excluded.note,
                    is_explicit_override=excluded.is_explicit_override, updated_at=excluded.updated_at''',
                (date_str, template_id, start_time, lunch_time, end_time, 1 if is_day_off else 0, note,
                 1 if is_explicit_override else 0, now, now))
            cal_id = cur.lastrowid or (existing['id'] if existing else None)
            after = {'date': date_str, 'start_time': start_time, 'end_time': end_time, 'is_day_off': 1 if is_day_off else 0}
            if cal_id:
                connection.execute('''INSERT INTO audit_log
                    (correlation_id, operation_type, actor, affected_table, record_id, before_state_json, after_state_json, created_at)
                    VALUES (?, 'set_shift_override', 'system', 'shift_calendar', ?, ?, ?, ?)''',
                    (corr, cal_id, json.dumps(before) if before else None, json.dumps(after), now))
            return cal_id

    def get_shift_calendar_override(self, date_str):
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM shift_calendar WHERE date=?', (date_str,)).fetchone()
            return dict(row) if row else None

    get_shift_calendar = get_shift_calendar_override

    def get_calendar_range(self, start_date, end_date):
        with self.connect() as connection:
            rows = connection.execute('''SELECT sc.*, st.name as template_name
                FROM shift_calendar sc LEFT JOIN shift_templates st ON st.id=sc.template_id
                WHERE sc.date BETWEEN ? AND ? ORDER BY sc.date''', (start_date, end_date)).fetchall()
            return [dict(r) for r in rows]

    def get_shift_for_date(self, date_str, default_timezone='Asia/Kolkata'):
        dt = datetime.strptime(date_str, '%Y-%m-%d')
        weekday = str(dt.weekday())
        with self.connect() as connection:
            override = connection.execute('''SELECT sc.*, st.name as template_name, st.start_time as t_start,
                st.lunch_time as t_lunch, st.end_time as t_end, st.timezone as t_tz
                FROM shift_calendar sc LEFT JOIN shift_templates st ON st.id=sc.template_id
                WHERE sc.date=?''', (date_str,)).fetchone()
            if override:
                if override['is_day_off']:
                    return {'date': date_str, 'is_day_off': True, 'note': override['note']}
                start = override['start_time'] or override['t_start']
                lunch = override['lunch_time'] or override['t_lunch']
                end = override['end_time'] or override['t_end']
                tz = override['t_tz'] or default_timezone
                return {
                    'date': date_str, 'is_day_off': False, 'start_time': start,
                    'lunch_time': lunch, 'end_time': end, 'timezone': tz,
                    'source': 'calendar_override', 'note': override['note']
                }
            templates = connection.execute('SELECT * FROM shift_templates ORDER BY id').fetchall()
            for t in templates:
                weekdays = [w.strip() for w in (t['active_weekdays'] or '').split(',')]
                if weekday in weekdays:
                    return {
                        'date': date_str, 'is_day_off': False, 'start_time': t['start_time'],
                        'lunch_time': t['lunch_time'], 'end_time': t['end_time'],
                        'timezone': t['timezone'] or default_timezone, 'source': f"template:{t['name']}"
                    }
            return {
                'date': date_str, 'is_day_off': dt.weekday() >= 5,
                'start_time': '10:00', 'lunch_time': '14:00', 'end_time': '19:00',
                'timezone': default_timezone, 'source': 'default'
            }

    # --- Phase 4: Report Provenance and Validation ---

    def record_report_provenance(self, report_id, section_name, record_type, record_id, detail=None):
        with self.connect() as connection:
            cur = connection.execute('''INSERT INTO report_provenance
                (report_id, section_name, record_type, record_id, detail, created_at)
                VALUES (?, ?, ?, ?, ?, ?)''',
                (report_id, section_name, record_type, record_id, detail, now_iso()))
            return cur.lastrowid

    def get_report_provenance(self, report_id):
        with self.connect() as connection:
            rows = connection.execute('SELECT * FROM report_provenance WHERE report_id=? ORDER BY id',
                                      (report_id,)).fetchall()
            return [dict(r) for r in rows]

    def save_report_validation(self, report_id: int, is_valid: bool, warnings: list[dict], metrics: dict) -> int:
        stamp = now_iso()
        with self.connect() as connection:
            cur = connection.execute('''INSERT INTO report_validations
                (report_id, is_valid, warnings_json, metrics_json, created_at)
                VALUES (?, ?, ?, ?, ?)''',
                (report_id, 1 if is_valid else 0, json.dumps(warnings), json.dumps(metrics), stamp))
            return cur.lastrowid

    def get_report_validation(self, report_id: int) -> dict | None:
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM report_validations WHERE report_id=? ORDER BY id DESC LIMIT 1',
                                      (report_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d['warnings'] = json.loads(d['warnings_json'])
            d['metrics'] = json.loads(d['metrics_json'])
            return d

    # --- Phase 4: NL Proposals ---

    def create_nl_proposal(self, owner_id=None, intent: str = 'unknown', proposal_dict: dict = None,
                           source_update_id: int = None, ttl_minutes: int = 15, proposal_data: dict = None, **kwargs) -> str:
        proposal_id = f'prop_{uuid.uuid4().hex[:10]}'
        now = datetime.now(timezone.utc)
        expires_at = kwargs.get('expires_at') or (now + timedelta(minutes=ttl_minutes)).isoformat()
        payload = proposal_dict or proposal_data or {}
        oid = int(owner_id) if (owner_id is not None and str(owner_id).isdigit()) else (config.OWNER_ID or 1)
        with self.connect() as connection:
            connection.execute('''INSERT INTO nl_proposals
                (id, owner_id, source_update_id, intent, proposal_json, status, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)''',
                (proposal_id, oid, source_update_id, intent, json.dumps(payload), expires_at, now.isoformat()))
        return proposal_id

    def get_nl_proposal(self, proposal_id: str) -> dict | None:
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM nl_proposals WHERE id=?', (proposal_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d['proposal'] = json.loads(d['proposal_json'])
            return d

    def accept_nl_proposal(self, proposal_id: str, owner_id=None) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        oid = int(owner_id) if (owner_id is not None and str(owner_id).isdigit()) else None
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT * FROM nl_proposals WHERE id=?', (proposal_id,)).fetchone()
            if not row:
                raise ValueError('Proposal not found.')
            if oid is not None and row['owner_id'] != oid:
                raise ValueError('Unauthorized.')
            if row['status'] != 'pending':
                raise ValueError(f'Proposal is already {row["status"]}.')
            if row['expires_at'] < now:
                connection.execute('UPDATE nl_proposals SET status="expired" WHERE id=?', (proposal_id,))
                raise ValueError('Proposal has expired.')

            connection.execute('UPDATE nl_proposals SET status="accepted" WHERE id=?', (proposal_id,))
            d = dict(row)
            d['status'] = 'accepted'
            d['proposal'] = json.loads(d['proposal_json'])
            return d

    def claim_nl_proposal(self, proposal_id: str, owner_id=None) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        oid = int(owner_id) if owner_id is not None else None
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT * FROM nl_proposals WHERE id=?', (proposal_id,)).fetchone()
            if not row:
                raise ValueError('Proposal not found.')
            if oid is not None and row['owner_id'] != oid:
                raise ValueError('Unauthorized proposal.')
            if row['status'] != 'pending':
                raise ValueError(f'Proposal is already {row["status"]}.')
            if row['expires_at'] < now:
                connection.execute('UPDATE nl_proposals SET status="expired" WHERE id=?', (proposal_id,))
                raise ValueError('Proposal has expired.')
            connection.execute('UPDATE nl_proposals SET status="executing" WHERE id=?', (proposal_id,))
            result = dict(row)
            result['status'] = 'executing'
            result['proposal'] = json.loads(result['proposal_json'])
            return result

    def finish_nl_proposal(self, proposal_id: str, status: str):
        if status not in {'accepted', 'failed', 'cancelled'}:
            raise ValueError('Invalid proposal completion status.')
        with self.connect() as connection:
            if connection.execute(
                    'UPDATE nl_proposals SET status=? WHERE id=? AND status="executing"',
                    (status, proposal_id)).rowcount != 1:
                raise ValueError('Proposal is no longer executing.')

    def cancel_nl_proposal(self, proposal_id: str, owner_id=None):
        oid = int(owner_id) if owner_id is not None else None
        with self.connect() as connection:
            row = connection.execute('SELECT owner_id,status FROM nl_proposals WHERE id=?',
                                     (proposal_id,)).fetchone()
            if not row:
                raise ValueError('Proposal not found.')
            if oid is not None and row['owner_id'] != oid:
                raise ValueError('Unauthorized proposal.')
            if row['status'] != 'pending':
                raise ValueError(f'Proposal is already {row["status"]}.')
            connection.execute('UPDATE nl_proposals SET status="cancelled" WHERE id=?', (proposal_id,))
