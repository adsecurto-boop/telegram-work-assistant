"""Transactional SQLite storage with versioned, backup-first migrations."""
import json
import shutil
import sqlite3
from contextlib import contextmanager, closing
from datetime import datetime, timezone
from pathlib import Path

from models import Task, TaskStatus

SCHEMA_VERSION = 3


def now_iso():
    return datetime.now(timezone.utc).isoformat()


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
        if 0 < version < SCHEMA_VERSION:
            backup_dir = self.path.parent / 'backups'
            backup_dir.mkdir(parents=True, exist_ok=True)
            destination = backup_dir / (
                f'pre-migration-v{version}-' + datetime.now().strftime('%Y%m%d-%H%M%S%f') + '.sqlite3'
            )
            with closing(sqlite3.connect(self.path)) as source, closing(sqlite3.connect(destination)) as target:
                source.backup(target)
        with self.connect() as connection:
            self._create_schema(connection)
            self._ensure_columns(connection)
            if version == 1:
                self._seed_structured_records(connection)
            self._create_indexes(connection)
            connection.execute(f'PRAGMA user_version={SCHEMA_VERSION}')

    def _create_schema(self, connection):
        connection.executescript('''
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
        ''')

    def _create_indexes(self, connection):
        connection.executescript('''
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
        ''')

    def _ensure_columns(self, connection):
        additions = {
            'tasks': {
                'planned_shift_id': 'INTEGER', 'due_date': 'TEXT', 'project': 'TEXT',
                'client': 'TEXT', 'ticket': 'TEXT', 'next_action': 'TEXT',
                'tags': 'TEXT', 'completion_note': 'TEXT'},
            'activities': {
                'unplanned': 'INTEGER NOT NULL DEFAULT 0',
                'source_message_id': 'INTEGER REFERENCES source_messages(id)',
                'case_id': 'INTEGER REFERENCES work_cases(id)'},
            'source_messages': {'metadata_json': 'TEXT'},
            'reports': {
                'style': "TEXT NOT NULL DEFAULT 'standard'", 'provider': 'TEXT',
                'model': 'TEXT', 'prompt_version': 'TEXT', 'source_report_id': 'INTEGER'},
            'proposals': {
                'dismissed': 'INTEGER NOT NULL DEFAULT 0', 'model': 'TEXT',
                'prompt_version': 'TEXT'},
        }
        for table, columns in additions.items():
            current = {row['name'] for row in connection.execute(f'PRAGMA table_info({table})')}
            for name, declaration in columns.items():
                if name not in current:
                    connection.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')

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

    def shifts_since(self, start_iso):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(
                'SELECT * FROM shifts WHERE start>=? ORDER BY start', (start_iso,))]

    def start_shift(self, start, end, lunch=None):
        with self.connect() as connection:
            if connection.execute('SELECT 1 FROM shifts WHERE closed_at IS NULL').fetchone():
                raise ValueError('A shift is already active. End it before starting another.')
            return connection.execute('INSERT INTO shifts(start,end,lunch) VALUES (?,?,?)',
                                      (start, end, lunch)).lastrowid

    def schedule(self, shift_id, end, lunch):
        with self.connect() as connection:
            connection.execute('UPDATE shifts SET end=?,lunch=? WHERE id=? AND closed_at IS NULL',
                               (end, lunch, shift_id))

    def close_shift(self, shift_id):
        with self.connect() as connection:
            connection.execute('UPDATE shifts SET closed_at=? WHERE id=? AND closed_at IS NULL',
                               (now_iso(), shift_id))

    def finalize_and_close(self, report_id, shift_id):
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            report = connection.execute(
                "SELECT * FROM reports WHERE id=? AND shift_id=? AND kind='eod'",
                (report_id, shift_id)).fetchone()
            if not report:
                raise ValueError('EOD report not found for this shift.')
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

    def list_pending(self):
        return [task for task in self.list_tasks()
                if task.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS)]

    def mark_status(self, task_id, status, blocked_reason=None, shift_id=None, completion_note=None):
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row:
                return None
            completed_at = now_iso() if status == TaskStatus.COMPLETED else None
            note = completion_note if status == TaskStatus.COMPLETED else None
            connection.execute('''UPDATE tasks SET status=?,blocked_reason=?,completed_at=?,completion_note=?
                WHERE id=?''', (status.value, blocked_reason, completed_at, note, task_id))
            if shift_id and (row['status'] != status.value or row['blocked_reason'] != blocked_reason
                             or completion_note):
                detail = row['title'] + (f' — {completion_note}' if completion_note else '')
                self._activity(connection, shift_id, 'task', detail,
                               outcome=status.value, task_id=task_id,
                               unplanned=1 if row['planned_shift_id'] != shift_id else 0)
            return Task.from_row(connection.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())

    def delete_task(self, task_id, shift_id=None):
        return self.mark_status(task_id, TaskStatus.CANCELLED, shift_id=shift_id) is not None

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
                    query_count=1, issue_key=None):
        with self.connect() as connection:
            activity_id = self._activity(connection, shift_id, 'support', detail, client, channel, outcome)
            client_id = self._client_id(connection, client)
            connection.execute('''INSERT INTO support_interactions
                (activity_id,client_id,channel,product,query_category,outcome,follow_up,ticket,query_count,issue_key)
                VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (activity_id, client_id, channel, product, query_category, outcome,
                 follow_up, ticket, query_count, issue_key))
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

    def finalize(self, report_id):
        with self.connect() as connection:
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

    def record_ai_event(self, operation, provider, model, prompt_version, status, error_type=None):
        with self.connect() as connection:
            connection.execute('''INSERT INTO ai_events
                (created_at,operation,provider,model,prompt_version,status,error_type)
                VALUES (?,?,?,?,?,?,?)''',
                (now_iso(), operation, provider, model, prompt_version, status, error_type))

    def ai_stats(self):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute('''SELECT status,COUNT(*) AS count
                FROM ai_events GROUP BY status ORDER BY status''')]

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
                       outcome=None, occurred_at=None, source_message_id=None):
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
            return event_id

    def update_case(self, case_id, field, value, shift_id=None, detail=None):
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
            connection.execute('''INSERT INTO case_events
                (case_id,shift_id,event_type,detail,actor_role,outcome,occurred_at,created_at)
                VALUES (?,?,?,?,?,?,?,?)''',
                (case_id, shift_id, 'status' if field == 'status' else 'updated', event_detail,
                 'owner', value if field == 'status' else None, stamp, stamp))
            updated = connection.execute('''SELECT work_case.*,client.name AS client
                FROM work_cases work_case LEFT JOIN clients client ON client.id=work_case.client_id
                WHERE work_case.id=?''', (case_id,)).fetchone()
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
                         retest_result=None):
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
            if case_id:
                connection.execute('''INSERT INTO case_events
                    (case_id,shift_id,event_type,detail,actor_role,outcome,occurred_at,created_at)
                    VALUES (?,?,?,?,?,?,?,?)''',
                    (case_id, shift_id, 'testing', scenario, 'owner', result, stamp, stamp))
                case_status = 'retest_required' if retest_required else ('fix_ready' if result == 'passed' else 'testing')
                current = connection.execute('SELECT status FROM work_cases WHERE id=?', (case_id,)).fetchone()
                if current and current['status'] not in ('resolved','client_updated','closed'):
                    connection.execute('UPDATE work_cases SET status=?,updated_at=? WHERE id=?',
                                       (case_status, stamp, case_id))
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

    def update_test_session(self, session_id, field, value, shift_id=None):
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
            if row['case_id'] and field in ('result','actual','defects','retest_result'):
                connection.execute('''INSERT INTO case_events
                    (case_id,shift_id,event_type,detail,actor_role,outcome,occurred_at,created_at)
                    VALUES (?,?,?,?,?,?,?,?)''',
                    (row['case_id'], shift_id or row['shift_id'], 'testing_update',
                     f'TEST-{session_id} {field}: {value}', 'owner',
                     value if field == 'result' else None, stamp, stamp))

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

    def add_followup(self, case_id, due_at, note=None, waiting_on=None, shift_id=None, kind='case'):
        with self.connect() as connection:
            if not connection.execute('SELECT 1 FROM work_cases WHERE id=?', (case_id,)).fetchone():
                raise ValueError('Case not found.')
            followup_id = connection.execute('''INSERT INTO followups
                (case_id,shift_id,due_at,kind,waiting_on,note,created_at)
                VALUES (?,?,?,?,?,?,?)''',
                (case_id, shift_id, due_at, kind, waiting_on, note, now_iso())).lastrowid
            connection.execute('''UPDATE work_cases SET follow_up_at=?,waiting_on=COALESCE(?,waiting_on),
                updated_at=? WHERE id=?''', (due_at, waiting_on, now_iso(), case_id))
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

    def complete_followup(self, followup_id):
        with self.connect() as connection:
            if connection.execute('''UPDATE followups SET status='completed',completed_at=?
                WHERE id=? AND status='pending' ''', (now_iso(), followup_id)).rowcount != 1:
                raise ValueError('Follow-up is unavailable or already completed.')

    def snooze_followup(self, followup_id, due_at):
        with self.connect() as connection:
            if connection.execute('''UPDATE followups SET due_at=?,reminded_at=NULL
                WHERE id=? AND status='pending' ''', (due_at, followup_id)).rowcount != 1:
                raise ValueError('Follow-up is unavailable or already completed.')

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
