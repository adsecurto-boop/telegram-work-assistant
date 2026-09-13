"""Transactional SQLite storage with versioned, backup-first migrations."""
import difflib
import json
import re
import shutil
import sqlite3
import threading
import uuid
from contextlib import contextmanager, closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from models import Task, TaskStatus
import config

SCHEMA_VERSION = 17


_last_iso_time = 0.0
_iso_time_lock = threading.Lock()


def now_iso():
    global _last_iso_time
    with _iso_time_lock:
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
        try:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('PRAGMA synchronous=NORMAL')
        except sqlite3.Error:
            pass
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
            if version < 9:
                self._seed_v9_defaults(cursor)
            if version < 10:
                self._seed_v10_defaults(cursor)
            if version < 11:
                self._seed_v11_defaults(cursor)
            if version < 12:
                self._seed_v12_defaults(cursor)
            if version < 13:
                self._seed_v13_defaults(cursor)
            if version < 14:
                self._seed_v14_defaults(cursor)
            if version < 15:
                self._seed_v15_defaults(cursor)
            if version < 16:
                self._seed_v16_defaults(cursor)
            if version < 17:
                self._seed_v17_defaults(cursor)
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
            CREATE TABLE IF NOT EXISTS plan_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shift_id INTEGER NOT NULL REFERENCES shifts(id),
                version INTEGER NOT NULL DEFAULT 1,
                snapshot_json TEXT NOT NULL,
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS record_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type TEXT NOT NULL,
                source_id INTEGER NOT NULL,
                target_type TEXT NOT NULL,
                target_id INTEGER NOT NULL,
                link_type TEXT NOT NULL DEFAULT 'related',
                created_at TEXT NOT NULL,
                UNIQUE(source_type, source_id, target_type, target_id));
            CREATE TABLE IF NOT EXISTS planning_conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                purpose TEXT NOT NULL DEFAULT 'daily_planning',
                step TEXT NOT NULL,
                proposed_values_json TEXT NOT NULL,
                shift_id INTEGER REFERENCES shifts(id),
                selected_record_type TEXT,
                selected_record_id INTEGER,
                source_update_id INTEGER,
                source_message_id INTEGER,
                status TEXT NOT NULL DEFAULT 'active',
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL);
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
            CREATE TABLE IF NOT EXISTS updates (
                id INTEGER PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'completed',
                claimed_at TEXT,
                completed_at TEXT,
                attempts INTEGER NOT NULL DEFAULT 1,
                last_error TEXT);
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
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS conversation_turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                shift_id INTEGER REFERENCES shifts(id),
                role TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
                text TEXT NOT NULL,
                intent TEXT,
                entities_json TEXT,
                case_id INTEGER REFERENCES work_cases(id),
                task_id INTEGER REFERENCES tasks(id),
                test_session_id INTEGER REFERENCES test_sessions(id),
                source_update_id INTEGER,
                thread_id TEXT NOT NULL DEFAULT 'primary',
                source_channel TEXT NOT NULL DEFAULT 'system',
                source_message_id TEXT,
                client_message_id TEXT,
                correlation_id TEXT,
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS conversation_threads (
                id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, title TEXT,
                status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, archived_at TEXT);
            CREATE TABLE IF NOT EXISTS assistant_memory_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                shift_id INTEGER REFERENCES shifts(id),
                memory_type TEXT NOT NULL,
                summary_text TEXT NOT NULL,
                source_turn_start_id INTEGER,
                source_turn_end_id INTEGER,
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL);

            -- Phase A: Work Items, Configurable Workflows, Members, Roles & Testing Workspace
            CREATE TABLE IF NOT EXISTS workflow_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT,
                work_type TEXT NOT NULL DEFAULT 'requirement',
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL);

            CREATE TABLE IF NOT EXISTS workflow_stages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_id INTEGER NOT NULL REFERENCES workflow_templates(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                stage_order INTEGER NOT NULL,
                description TEXT,
                expected_role TEXT,
                expected_duration_hours REAL,
                is_waiting INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                required_artifacts_json TEXT,
                checklist_items_json TEXT,
                UNIQUE(template_id, name),
                UNIQUE(template_id, stage_order));

            CREATE TABLE IF NOT EXISTS members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                email TEXT,
                telegram_handle TEXT,
                notes TEXT,
                created_at TEXT NOT NULL);

            CREATE TABLE IF NOT EXISTS roles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT,
                created_at TEXT NOT NULL);

            CREATE TABLE IF NOT EXISTS member_roles (
                member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
                role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
                PRIMARY KEY(member_id, role_id));

            CREATE TABLE IF NOT EXISTS work_item_meta (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL,
                entity_id INTEGER NOT NULL,
                workflow_template_id INTEGER REFERENCES workflow_templates(id),
                current_stage_id INTEGER REFERENCES workflow_stages(id),
                operational_status TEXT NOT NULL DEFAULT 'active',
                owner_member_id INTEGER REFERENCES members(id),
                target_role TEXT,
                estimated_hours REAL,
                actual_hours REAL,
                due_date TEXT,
                next_action TEXT,
                completion_note TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(entity_type, entity_id));

            CREATE TABLE IF NOT EXISTS stage_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL,
                entity_id INTEGER NOT NULL,
                stage_id INTEGER NOT NULL REFERENCES workflow_stages(id),
                entered_at TEXT NOT NULL,
                exited_at TEXT,
                actor_member_id INTEGER REFERENCES members(id),
                note TEXT);

            CREATE TABLE IF NOT EXISTS blockers_dependencies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL,
                entity_id INTEGER NOT NULL,
                dependency_type TEXT NOT NULL,
                description TEXT NOT NULL,
                waiting_on_member_id INTEGER REFERENCES members(id),
                waiting_on_role TEXT,
                target_entity_type TEXT,
                target_entity_id INTEGER,
                status TEXT NOT NULL DEFAULT 'active',
                started_at TEXT NOT NULL,
                expected_resolution_date TEXT,
                resolved_at TEXT,
                resolution_notes TEXT);

            CREATE TABLE IF NOT EXISTS requirements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                requirement_text TEXT,
                user_story TEXT,
                acceptance_criteria TEXT,
                client TEXT,
                product TEXT,
                ticket TEXT,
                priority INTEGER NOT NULL DEFAULT 1,
                due_date TEXT,
                owner_member_id INTEGER REFERENCES members(id),
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL);

            CREATE TABLE IF NOT EXISTS test_conditions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                requirement_id INTEGER NOT NULL REFERENCES requirements(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                description TEXT,
                category TEXT NOT NULL DEFAULT 'functional',
                risk_level TEXT NOT NULL DEFAULT 'medium',
                status TEXT NOT NULL DEFAULT 'draft',
                notes TEXT,
                created_at TEXT NOT NULL);

            CREATE TABLE IF NOT EXISTS test_cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                requirement_id INTEGER REFERENCES requirements(id) ON DELETE SET NULL,
                test_condition_id INTEGER REFERENCES test_conditions(id) ON DELETE SET NULL,
                title TEXT NOT NULL,
                objective TEXT,
                preconditions TEXT,
                steps TEXT,
                test_data TEXT,
                expected_result TEXT,
                priority INTEGER NOT NULL DEFAULT 2,
                automation_status TEXT NOT NULL DEFAULT 'manual',
                created_at TEXT NOT NULL);

            CREATE TABLE IF NOT EXISTS test_executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                test_case_id INTEGER NOT NULL REFERENCES test_cases(id) ON DELETE CASCADE,
                session_id INTEGER REFERENCES test_sessions(id) ON DELETE SET NULL,
                build TEXT,
                environment TEXT,
                result TEXT NOT NULL DEFAULT 'not_run',
                actual_result TEXT,
                defect_id INTEGER,
                executed_by_member_id INTEGER REFERENCES members(id),
                notes TEXT,
                executed_at TEXT NOT NULL);

            CREATE TABLE IF NOT EXISTS artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL,
                entity_id INTEGER NOT NULL,
                artifact_type TEXT NOT NULL,
                name TEXT NOT NULL,
                path TEXT,
                url TEXT,
                details_json TEXT,
                created_at TEXT NOT NULL);

            CREATE TABLE IF NOT EXISTS defects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                severity TEXT NOT NULL DEFAULT 'major',
                priority INTEGER NOT NULL DEFAULT 2,
                status TEXT NOT NULL DEFAULT 'new',
                requirement_id INTEGER REFERENCES requirements(id) ON DELETE SET NULL,
                test_condition_id INTEGER REFERENCES test_conditions(id) ON DELETE SET NULL,
                test_case_id INTEGER REFERENCES test_cases(id) ON DELETE SET NULL,
                execution_id INTEGER REFERENCES test_executions(id) ON DELETE SET NULL,
                assigned_member_id INTEGER REFERENCES members(id) ON DELETE SET NULL,
                client TEXT,
                product TEXT,
                ticket TEXT,
                steps_to_reproduce TEXT,
                expected_result TEXT,
                actual_result TEXT,
                build_found TEXT,
                build_fixed TEXT,
                environment TEXT,
                retest_notes TEXT,
                resolution TEXT,
                closed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL);
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
            CREATE INDEX IF NOT EXISTS nl_corrections_active_idx ON nl_corrections(is_active, corrected_intent);
            CREATE INDEX IF NOT EXISTS plan_snapshot_shift_idx ON plan_snapshots(shift_id, version);
            CREATE INDEX IF NOT EXISTS record_links_source_idx ON record_links(source_type, source_id);
            CREATE INDEX IF NOT EXISTS record_links_target_idx ON record_links(target_type, target_id);
            CREATE INDEX IF NOT EXISTS planning_active_idx ON planning_conversations(owner_id, status);
            CREATE INDEX IF NOT EXISTS conversation_turns_owner_idx ON conversation_turns(owner_id, created_at);
            CREATE INDEX IF NOT EXISTS conversation_turns_shift_idx ON conversation_turns(shift_id, created_at);
            CREATE INDEX IF NOT EXISTS workflow_stages_template_idx ON workflow_stages(template_id, stage_order);
            CREATE INDEX IF NOT EXISTS work_item_meta_type_idx ON work_item_meta(entity_type, entity_id);
            CREATE INDEX IF NOT EXISTS work_item_meta_stage_idx ON work_item_meta(current_stage_id);
            CREATE INDEX IF NOT EXISTS work_item_meta_status_idx ON work_item_meta(operational_status);
            CREATE INDEX IF NOT EXISTS work_item_meta_owner_idx ON work_item_meta(owner_member_id);
            CREATE INDEX IF NOT EXISTS stage_history_entity_idx ON stage_history(entity_type, entity_id);
            CREATE INDEX IF NOT EXISTS blockers_entity_idx ON blockers_dependencies(entity_type, entity_id, status);
            CREATE INDEX IF NOT EXISTS blockers_waiting_member_idx ON blockers_dependencies(waiting_on_member_id, status);
            CREATE INDEX IF NOT EXISTS requirements_status_idx ON requirements(status, priority);
            CREATE INDEX IF NOT EXISTS test_conditions_req_idx ON test_conditions(requirement_id, status);
            CREATE INDEX IF NOT EXISTS test_cases_req_idx ON test_cases(requirement_id);
            CREATE INDEX IF NOT EXISTS test_cases_cond_idx ON test_cases(test_condition_id);
            CREATE INDEX IF NOT EXISTS test_executions_case_idx ON test_executions(test_case_id, result);
            CREATE INDEX IF NOT EXISTS artifacts_entity_idx ON artifacts(entity_type, entity_id);
            CREATE INDEX IF NOT EXISTS defects_status_idx ON defects(status, priority);
            CREATE INDEX IF NOT EXISTS defects_req_idx ON defects(requirement_id);
            CREATE INDEX IF NOT EXISTS defects_case_idx ON defects(test_case_id);
            CREATE INDEX IF NOT EXISTS defects_assigned_idx ON defects(assigned_member_id);
        ''')

    def _ensure_columns(self, connection):
        additions = {
            'shifts': {'eod_reminder': 'TEXT'},
            'tasks': {
                'planned_shift_id': 'INTEGER', 'due_date': 'TEXT', 'project': 'TEXT',
                'client': 'TEXT', 'ticket': 'TEXT', 'next_action': 'TEXT',
                'tags': 'TEXT', 'completion_note': 'TEXT', 'blocked_reason': 'TEXT',
                'completed_at': 'TEXT'},
            'activities': {
                'unplanned': 'INTEGER NOT NULL DEFAULT 0',
                'source_message_id': 'INTEGER REFERENCES source_messages(id)',
                'case_id': 'INTEGER REFERENCES work_cases(id)',
                'occurred_at': 'TEXT',
                'time_precision': "TEXT NOT NULL DEFAULT 'exact'"},
            'source_messages': {
                'metadata_json': 'TEXT',
                'cluster_id': 'INTEGER REFERENCES history_clusters(id)'},
            'reports': {
                'style': "TEXT NOT NULL DEFAULT 'standard'", 'provider': 'TEXT',
                'model': 'TEXT', 'prompt_version': 'TEXT', 'source_report_id': 'INTEGER',
                'provenance_json': 'TEXT',
                'revision': 'INTEGER NOT NULL DEFAULT 1',
                'is_stale': 'INTEGER NOT NULL DEFAULT 0',
                'facts_hash': 'TEXT',
                'facts_snapshot_json': 'TEXT'},
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
            'nl_corrections': {
                'is_active': 'INTEGER NOT NULL DEFAULT 1'},
            'updates': {
                'status': "TEXT NOT NULL DEFAULT 'completed'",
                'claimed_at': 'TEXT',
                'completed_at': 'TEXT',
                'attempts': 'INTEGER NOT NULL DEFAULT 1',
                'last_error': 'TEXT'},
            'conversation_turns': {
                'thread_id': "TEXT NOT NULL DEFAULT 'primary'", 'source_channel': "TEXT NOT NULL DEFAULT 'system'",
                'source_message_id': 'TEXT', 'client_message_id': 'TEXT'},
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

    def _seed_v9_defaults(self, connection):
        connection.execute('''
            UPDATE activities
            SET occurred_at = created_at
            WHERE occurred_at IS NULL
        ''')
        connection.execute('''
            UPDATE activities
            SET time_precision = 'exact'
            WHERE time_precision IS NULL
        ''')

    def _seed_v10_defaults(self, connection):
        current = {row['name'] for row in connection.execute('PRAGMA table_info(reports)')}
        if 'facts_snapshot_json' not in current:
            connection.execute('ALTER TABLE reports ADD COLUMN facts_snapshot_json TEXT')

    def _seed_v11_defaults(self, connection):
        current = {row['name'] for row in connection.execute('PRAGMA table_info(nl_corrections)')}
        if 'is_active' not in current:
            connection.execute('ALTER TABLE nl_corrections ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1')
        connection.execute('UPDATE nl_corrections SET is_active=1 WHERE is_active IS NULL')

    def _seed_v12_defaults(self, connection):
        # v12 introduces conversation_turns table (created by _create_schema)
        pass

    def _seed_v17_defaults(self, connection):
        connection.execute('''CREATE TABLE IF NOT EXISTS conversation_threads (
            id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, title TEXT,
            status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, archived_at TEXT)''')
        connection.execute('CREATE INDEX IF NOT EXISTS conversation_turns_thread_idx ON conversation_turns(owner_id, thread_id, created_at)')
        connection.execute('CREATE UNIQUE INDEX IF NOT EXISTS conversation_turns_web_idempotency_idx ON conversation_turns(owner_id, client_message_id, role) WHERE client_message_id IS NOT NULL')

    def _seed_v13_defaults(self, connection):
        connection.execute('''CREATE TABLE IF NOT EXISTS assistant_memory_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            shift_id INTEGER REFERENCES shifts(id),
            memory_type TEXT NOT NULL,
            summary_text TEXT NOT NULL,
            source_turn_start_id INTEGER,
            source_turn_end_id INTEGER,
            version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL)''')
        try:
            connection.execute('''CREATE VIRTUAL TABLE IF NOT EXISTS fts_work_memory USING fts5(
                source_type, source_id, title, content, client, product, created_at
            )''')
        except sqlite3.OperationalError:
            connection.execute('''CREATE TABLE IF NOT EXISTS fts_work_memory (
                source_type TEXT, source_id TEXT, title TEXT, content TEXT, client TEXT, product TEXT, created_at TEXT
            )''')

    def _seed_v14_defaults(self, connection):
        """Schema v14: Backfill historical work records into fts_work_memory."""
        try:
            connection.execute('''CREATE VIRTUAL TABLE IF NOT EXISTS fts_work_memory USING fts5(
                source_type, source_id, title, content, client, product, created_at
            )''')
        except sqlite3.OperationalError:
            connection.execute('''CREATE TABLE IF NOT EXISTS fts_work_memory (
                source_type TEXT, source_id TEXT, title TEXT, content TEXT, client TEXT, product TEXT, created_at TEXT
            )''')

        # Backfill tasks
        try:
            cursor = connection.execute("SELECT id, title, client, project, created_at FROM tasks")
            for row in cursor.fetchall():
                connection.execute('''INSERT OR IGNORE INTO fts_work_memory (source_type, source_id, title, content, client, product, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)''',
                    ('task', str(row['id']), row['title'] or '', f"Task: {row['title'] or ''}", row['client'] or '', row['project'] or '', row['created_at'] or now_iso()))
        except Exception:
            pass

    def _fts_replace(self, connection, source_type, source_id, title, content,
                     client='', product='', created_at=None):
        """Replace one logical search document inside the caller's transaction."""
        key = str(source_id)
        connection.execute(
            'DELETE FROM fts_work_memory WHERE source_type=? AND source_id=?',
            (source_type, key))
        connection.execute('''INSERT INTO fts_work_memory
            (source_type, source_id, title, content, client, product, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (source_type, key, title or '', content or '', client or '', product or '',
             created_at or now_iso()))

    def _index_source_in_connection(self, connection, source_type, source_id):
        if source_type == 'task':
            row = connection.execute('SELECT * FROM tasks WHERE id=?', (source_id,)).fetchone()
            if row:
                content = ' '.join(str(row[k] or '') for k in (
                    'title', 'status', 'due_date', 'project', 'client', 'ticket',
                    'next_action', 'tags', 'blocked_reason', 'completion_note'))
                self._fts_replace(connection, 'task', row['id'], row['title'], content,
                                  row['client'], row['project'], row['created_at'])
        elif source_type == 'case':
            row = connection.execute('''SELECT w.*, c.name AS client FROM work_cases w
                LEFT JOIN clients c ON c.id=w.client_id WHERE w.id=?''', (source_id,)).fetchone()
            if row:
                content = ' '.join(str(row[k] or '') for k in (
                    'title', 'product', 'platform', 'ticket', 'priority', 'status',
                    'participation', 'next_action', 'waiting_on', 'resolution'))
                self._fts_replace(connection, 'case', row['id'], f"Case #{row['id']}: {row['title']}",
                                  content, row['client'], row['product'], row['created_at'])
        elif source_type == 'case_event':
            row = connection.execute('''SELECT e.*, w.product, c.name AS client
                FROM case_events e JOIN work_cases w ON w.id=e.case_id
                LEFT JOIN clients c ON c.id=w.client_id WHERE e.id=?''', (source_id,)).fetchone()
            if row:
                content = f"Case #{row['case_id']} [{row['event_type']}]: {row['detail'] or ''} {row['outcome'] or ''}"
                self._fts_replace(connection, 'case_event', row['id'], f"CaseEvent #{row['id']}",
                                  content, row['client'], row['product'], row['occurred_at'])
        elif source_type == 'test_session':
            row = connection.execute('SELECT * FROM test_sessions WHERE id=?', (source_id,)).fetchone()
            if row:
                content = ' '.join(str(row[k] or '') for k in (
                    'scenario', 'environment', 'build', 'preconditions', 'steps', 'expected',
                    'actual', 'result', 'defects', 'retest_result'))
                self._fts_replace(connection, 'test_session', row['id'],
                                  f"Test Session #{row['id']}: {row['scenario']}", content,
                                  created_at=row['created_at'])
        elif source_type == 'conversation_summary':
            row = connection.execute('SELECT * FROM assistant_memory_summaries WHERE id=?',
                                     (source_id,)).fetchone()
            if row:
                self._fts_replace(connection, 'conversation_summary', row['id'],
                                  'Conversation Summary', row['summary_text'],
                                  created_at=row['created_at'])

    def _seed_v15_defaults(self, connection):
        """Repair v14's incomplete FTS backfill from the real relational schema."""
        connection.execute('DELETE FROM fts_work_memory')
        for source_type, table in (
            ('task', 'tasks'), ('case', 'work_cases'), ('case_event', 'case_events'),
            ('test_session', 'test_sessions'),
            ('conversation_summary', 'assistant_memory_summaries')):
            for row in connection.execute(f'SELECT id FROM {table} ORDER BY id').fetchall():
                self._index_source_in_connection(connection, source_type, row['id'])

    def _seed_v16_defaults(self, connection):
        """Schema v16: Seed default workflow templates, standard stages, and roles."""
        now = now_iso()
        # Ensure default roles exist
        roles = [
            ('Lead Tester / QA', 'Quality Assurance engineer executing test suites and exploratory sessions'),
            ('Developer', 'Software engineer building features and resolving defects'),
            ('Support Engineer', 'Customer support specialist handling client interactions and tickets'),
            ('Product Owner', 'Product manager defining requirements and acceptance criteria'),
            ('DevOps / Infrastructure', 'Platform engineer managing environments and builds')
        ]
        for name, desc in roles:
            connection.execute('INSERT OR IGNORE INTO roles (name, description, created_at) VALUES (?, ?, ?)',
                               (name, desc, now))

        # Ensure default requirement workflow template exists
        connection.execute('''INSERT OR IGNORE INTO workflow_templates
            (name, description, work_type, is_default, created_at)
            VALUES (?, ?, ?, 1, ?)''',
            ('Standard Feature Delivery', 'Complete lifecycle from analysis to live deployment', 'requirement', now))
        row = connection.execute('SELECT id FROM workflow_templates WHERE name=?',
                                 ('Standard Feature Delivery',)).fetchone()
        if row:
            template_id = row['id']
            stages = [
                (1, 'Requirement Received', 'Initial requirement intake and preliminary scope assessment', 'Product Owner', 2.0, 0),
                (2, 'Requirement Analysis', 'Review user story, acceptance criteria, and refine test conditions', 'Product Owner', 4.0, 0),
                (3, 'Test Conditions', 'Identify test conditions, quality criteria, and risk boundaries', 'Lead Tester / QA', 4.0, 0),
                (4, 'Waiting for Development', 'Waiting for engineering build, implementation, or API delivery', 'Developer', 16.0, 1),
                (5, 'Build Ready', 'New software build deployed to target QA / staging test environment', 'DevOps / Infrastructure', 1.0, 0),
                (6, 'Implementation Analysis', 'Analyze implementation changes, inspect code/PRs, verify impact scope', 'Lead Tester / QA', 3.0, 0),
                (7, 'Test Case Design', 'Author test cases, prepare preconditions, steps, test data, and expected results', 'Lead Tester / QA', 6.0, 0),
                (8, 'Testware / Test Data Preparation', 'Prepare test accounts, mock payloads, SQL fixtures, and automation scripts', 'Lead Tester / QA', 4.0, 0),
                (9, 'Test Execution', 'Execute manual and automated test suites, capture evidence, record logs', 'Lead Tester / QA', 8.0, 0),
                (10, 'Defects', 'Investigate failures, triage bugs, log severity/priority, notify engineers', 'Lead Tester / QA', 4.0, 0),
                (11, 'Waiting for Fix', 'Awaiting developer resolution, hotfix delivery, or revised build', 'Developer', 12.0, 1),
                (12, 'Retest', 'Retest resolved defects on target build against original steps', 'Lead Tester / QA', 4.0, 0),
                (13, 'Regression / Impact Validation', 'Execute regression test suite, verify side-effects on adjacent modules', 'Lead Tester / QA', 4.0, 0),
                (14, 'Completion', 'All criteria met, release sign-off, completion report generated and verified', 'Product Owner', 1.0, 0),
            ]
            for order, name, desc, role, dur, waiting in stages:
                connection.execute('''INSERT OR IGNORE INTO workflow_stages
                    (template_id, name, stage_order, description, expected_role, expected_duration_hours, is_waiting, is_active)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1)''',
                    (template_id, name, order, desc, role, dur, waiting))

        # Defect Workflow Template
        connection.execute('''INSERT OR IGNORE INTO workflow_templates
            (name, description, work_type, is_default, created_at)
            VALUES (?, ?, ?, 0, ?)''',
            ('Bug / Defect Triage & Verification', 'End-to-end defect tracking and retesting workflow', 'defect', now))
        bug_row = connection.execute('SELECT id FROM workflow_templates WHERE name=?',
                                     ('Bug / Defect Triage & Verification',)).fetchone()
        if bug_row:
            b_id = bug_row['id']
            bug_stages = [
                (1, 'New', 'Defect reported and pending triage', 'Lead Tester / QA', 1.0, 0),
                (2, 'Triaged', 'Defect confirmed reproducible, severity and priority assigned', 'Product Owner', 2.0, 0),
                (3, 'Assigned', 'Assigned to developer for investigation and fix', 'Developer', 2.0, 0),
                (4, 'In Development', 'Fix under active development', 'Developer', 12.0, 1),
                (5, 'Fix Ready', 'Fix committed and packaged into candidate build', 'Developer', 1.0, 0),
                (6, 'Retest Required', 'Deployed to test environment, awaiting tester verification', 'Lead Tester / QA', 1.0, 0),
                (7, 'Retesting', 'Active retest execution in progress', 'Lead Tester / QA', 3.0, 0),
                (8, 'Verified', 'Fix successfully verified against original issue', 'Lead Tester / QA', 1.0, 0),
                (9, 'Closed', 'Defect closed with verified resolution notes', None, 0.0, 0),
            ]
            for order, name, desc, role, dur, waiting in bug_stages:
                connection.execute('''INSERT OR IGNORE INTO workflow_stages
                    (template_id, name, stage_order, description, expected_role, expected_duration_hours, is_waiting, is_active)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1)''',
                    (b_id, name, order, desc, role, dur, waiting))

    def rebuild_work_memory_index(self):
        with self.connect() as conn:
            self._seed_v15_defaults(conn)

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
            'nl_corrections', 'plan_snapshots', 'record_links',
            'planning_conversations', 'conversation_turns',
            'assistant_memory_summaries', 'fts_work_memory',
            'workflow_templates', 'workflow_stages', 'members', 'roles',
            'member_roles', 'work_item_meta', 'stage_history',
            'blockers_dependencies', 'requirements', 'test_conditions',
            'test_cases', 'test_executions', 'artifacts', 'defects'
        }
        rows = cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        existing = {r['name'] if isinstance(r, sqlite3.Row) else r[0] for r in rows}
        missing = required_tables - existing
        if missing:
            raise RuntimeError(f"Missing required database tables after migration: {missing}")
        report_cols = {row['name'] for row in cursor.execute('PRAGMA table_info(reports)')}
        if 'facts_snapshot_json' not in report_cols:
            raise RuntimeError("Missing required column 'facts_snapshot_json' on reports table after migration")
        corr_cols = {row['name'] for row in cursor.execute('PRAGMA table_info(nl_corrections)')}
        if 'is_active' not in corr_cols:
            raise RuntimeError("Missing required column 'is_active' on nl_corrections table after migration")

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
            try:
                row = connection.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
                return row[0] if row else None
            except sqlite3.OperationalError:
                connection.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
                row = connection.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
                return row[0] if row else None

    def set_setting(self, key, value):
        with self.connect() as connection:
            try:
                connection.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, str(value)))
            except sqlite3.OperationalError:
                connection.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
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
            self._index_source_in_connection(connection, 'task', task_id)
            return Task.from_row(connection.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())

    def update_task(self, task_id, field, value, shift_id=None):
        allowed = {'title', 'priority', 'due_date', 'project', 'client', 'ticket',
                   'next_action', 'tags', 'completion_note', 'planned_shift_id'}
        if field not in allowed:
            raise ValueError('Editable fields: ' + ', '.join(sorted(allowed)) + '.')
        if field in ('priority', 'planned_shift_id'):
            value = int(value) if value is not None and value not in ('-', 'none') else None
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row:
                return None
            connection.execute(f'UPDATE tasks SET {field}=? WHERE id=?',
                               (value if value not in ('-', 'none') else None, task_id))
            if shift_id:
                self._activity(connection, shift_id, 'task_edit',
                               f"{row['title']}: {field} updated", task_id=task_id)
            self._index_source_in_connection(connection, 'task', task_id)
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

    def list_carry_eligible_tasks(self):
        """Eligible for carry-forward: pending, in_progress, and blocked tasks.
        Completed and cancelled tasks are excluded and must not silently reopen."""
        eligible = (TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED)
        return [task for task in self.list_tasks() if task.status in eligible]

    def carry_task_forward_transactional(self, task_id: int, target_date: str,
                                         target_shift_id: int | None = None,
                                         create_plan_activity: bool = True,
                                         actor: str = 'owner',
                                         correlation_id: str | None = None,
                                         shift_id: int | None = None) -> dict:
        """
        Atomically carries forward an eligible task in a single transaction:
        - Validates existence and eligibility (pending, in_progress, blocked).
        - Rejects completed and cancelled tasks.
        - Updates task due_date and optional planned_shift_id.
        - Creates a planning activity if target_shift_id is provided and create_plan_activity is True.
        - Writes audit entries under a single correlation_id.
        - Rolls back completely if any step fails.
        """
        actual_shift_id = target_shift_id if target_shift_id is not None else shift_id
        corr_id = correlation_id or f'carry-{uuid.uuid4().hex}'
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row:
                raise ValueError(f'Task #{task_id} not found.')
            if row['status'] in (TaskStatus.COMPLETED.value, 'cancelled'):
                raise ValueError(
                    f"Task #{task_id} is {row['status']} and cannot be carried forward. Completed tasks must not silently reopen."
                )

            task_before = {
                'due_date': row['due_date'],
                'planned_shift_id': row['planned_shift_id']
            }

            conn.execute(
                'UPDATE tasks SET due_date=?, planned_shift_id=COALESCE(?, planned_shift_id) WHERE id=?',
                (target_date, actual_shift_id, task_id)
            )
            updated_row = conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            task_after = {
                'due_date': updated_row['due_date'],
                'planned_shift_id': updated_row['planned_shift_id']
            }

            self._record_audit_in_connection(
                conn, corr_id, 'carry_task', actor, 'tasks', task_id, task_before, task_after
            )

            created_activity_id = None
            if actual_shift_id and create_plan_activity:
                ex = conn.execute(
                    "SELECT id FROM activities WHERE shift_id=? AND task_id=? AND category='plan'",
                    (actual_shift_id, task_id)
                ).fetchone()
                if not ex:
                    stamp = now_iso()
                    created_activity_id = conn.execute('''INSERT INTO activities
                        (shift_id, category, detail, client, task_id, created_at, unplanned, occurred_at, time_precision)
                        VALUES (?, 'plan', ?, ?, ?, ?, 0, ?, 'exact')''',
                        (actual_shift_id, row['title'], row['client'], task_id, stamp, stamp)).lastrowid
                    conn.execute('UPDATE reports SET is_stale=1 WHERE shift_id=? AND finalized=0', (actual_shift_id,))
                    self._record_audit_in_connection(
                        conn, corr_id, 'create_plan_activity', actor, 'activities', created_activity_id,
                        None, {'shift_id': actual_shift_id, 'category': 'plan', 'task_id': task_id, 'detail': row['title']}
                    )

            # Update conversation context
            conn.execute(
                'UPDATE conversation_context SET active_task_id=? WHERE context_key=?',
                (task_id, 'owner')
            )

            audit_row = conn.execute(
                'SELECT id FROM audit_log WHERE correlation_id=? ORDER BY id DESC LIMIT 1',
                (corr_id,)
            ).fetchone()

            return {
                'task': Task.from_row(updated_row),
                'correlation_id': corr_id,
                'activity_id': created_activity_id,
                'audit_id': audit_row['id'] if audit_row else None
            }

    def carry_all_eligible_tasks_transactional(self, target_date: str,
                                               target_shift_id: int | None = None,
                                               create_plan_activities: bool = True,
                                               actor: str = 'owner',
                                               shift_id: int | None = None) -> dict:
        """
        Atomically carries all eligible tasks (pending, in_progress, blocked) in one single transaction.
        All task updates, activity creations, and audit records share the same correlation_id.
        One /undo reverts the entire batch atomically.
        """
        actual_shift_id = target_shift_id if target_shift_id is not None else shift_id
        corr_id = f'bulk-carry-{uuid.uuid4().hex}'
        eligible = (TaskStatus.PENDING.value, TaskStatus.IN_PROGRESS.value, TaskStatus.BLOCKED.value)
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            rows = conn.execute(
                f"SELECT * FROM tasks WHERE status IN ({','.join(['?']*len(eligible))}) ORDER BY id ASC",
                eligible
            ).fetchall()
            carried_tasks = []
            activity_ids = []
            for row in rows:
                tid = row['id']
                task_before = {
                    'due_date': row['due_date'],
                    'planned_shift_id': row['planned_shift_id']
                }
                conn.execute(
                    'UPDATE tasks SET due_date=?, planned_shift_id=COALESCE(?, planned_shift_id) WHERE id=?',
                    (target_date, actual_shift_id, tid)
                )
                updated_row = conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
                task_after = {
                    'due_date': updated_row['due_date'],
                    'planned_shift_id': updated_row['planned_shift_id']
                }
                self._record_audit_in_connection(
                    conn, corr_id, 'carry_task', actor, 'tasks', tid, task_before, task_after
                )
                carried_tasks.append(Task.from_row(updated_row))

                if actual_shift_id and create_plan_activities:
                    ex = conn.execute(
                        "SELECT id FROM activities WHERE shift_id=? AND task_id=? AND category='plan'",
                        (actual_shift_id, tid)
                    ).fetchone()
                    if not ex:
                        stamp = now_iso()
                        aid = conn.execute('''INSERT INTO activities
                            (shift_id, category, detail, client, task_id, created_at, unplanned, occurred_at, time_precision)
                            VALUES (?, 'plan', ?, ?, ?, ?, 0, ?, 'exact')''',
                            (actual_shift_id, row['title'], row['client'], tid, stamp, stamp)).lastrowid
                        conn.execute('UPDATE reports SET is_stale=1 WHERE shift_id=? AND finalized=0', (actual_shift_id,))
                        self._record_audit_in_connection(
                            conn, corr_id, 'create_plan_activity', actor, 'activities', aid,
                            None, {'shift_id': actual_shift_id, 'category': 'plan', 'task_id': tid, 'detail': row['title']}
                        )
                        activity_ids.append(aid)

            stamp = now_iso()
            conn.execute('''INSERT INTO bulk_operations
                (correlation_id, operation_type, scope, affected_count, created_at)
                VALUES (?, 'bulk_carry', 'eligible_tasks', ?, ?)''',
                (corr_id, len(carried_tasks), stamp))

            audit_row = conn.execute(
                'SELECT id FROM audit_log WHERE correlation_id=? ORDER BY id DESC LIMIT 1',
                (corr_id,)
            ).fetchone()

            return {
                'tasks': carried_tasks,
                'count': len(carried_tasks),
                'correlation_id': corr_id,
                'activity_ids': activity_ids,
                'audit_id': audit_row['id'] if audit_row else None
            }

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
            self._index_source_in_connection(connection, 'task', task_id)
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
                for rlink in conn.execute('SELECT * FROM record_links WHERE (source_type="task" AND source_id=?) OR (target_type="task" AND target_id=?)', (row['id'], row['id'])).fetchall():
                    conn.execute('DELETE FROM record_links WHERE id=?', (rlink['id'],))
                    self._record_audit_in_connection(conn, correlation, 'detach_record_link', 'owner',
                        'record_links', rlink['id'], dict(rlink), None)
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
                  outcome=None, task_id=None, unplanned=0, source_message_id=None, case_id=None,
                  occurred_at=None, time_precision='exact'):
        stamp = now_iso()
        occ = occurred_at or stamp
        aid = connection.execute('''INSERT INTO activities
            (shift_id,category,detail,client,channel,outcome,task_id,created_at,unplanned,
             source_message_id,case_id,occurred_at,time_precision)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (shift_id, category, detail, client, channel, outcome, task_id, stamp,
             int(bool(unplanned)), source_message_id, case_id, occ, time_precision)).lastrowid
        connection.execute('UPDATE reports SET is_stale=1 WHERE shift_id=? AND finalized=0', (shift_id,))
        return aid

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
                     source_message_id=None, case_id=None, occurred_at=None, time_precision='exact',
                     task_id=None):
        with self.connect() as connection:
            return self._activity(connection, shift_id, category, detail, client, channel, outcome,
                                  task_id=task_id, source_message_id=source_message_id, case_id=case_id,
                                  occurred_at=occurred_at, time_precision=time_precision)

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
                    prompt_version=None, source_report_id=None, facts_hash=None, revision=1,
                    facts_snapshot=None):
        snapshot_json = json.dumps(facts_snapshot) if isinstance(facts_snapshot, dict) else facts_snapshot
        with self.connect() as connection:
            return connection.execute('''INSERT INTO reports
                (shift_id,kind,text,created_at,style,provider,model,prompt_version,source_report_id,facts_hash,revision,is_stale,facts_snapshot_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?)''',
                (shift_id, kind, text, now_iso(), style, provider, model,
                 prompt_version, source_report_id, facts_hash, revision, snapshot_json)).lastrowid

    def compute_live_facts_hash(self, shift_id: int) -> str:
        from reports import compute_shift_facts_hash
        acts = self.activities(shift_id)
        tasks = self.tasks_for_shift(shift_id)
        cases = self.cases_for_shift(shift_id)
        sessions = self.test_sessions(shift_id)
        return compute_shift_facts_hash(acts, tasks, cases, sessions)

    def report(self, report_id):
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM reports WHERE id=?', (report_id,)).fetchone()
            if not row:
                return None
            res = dict(row)
            if not res.get('finalized') and res.get('facts_hash') and not res.get('is_stale') and not res.get('source_report_id'):
                live_hash = self.compute_live_facts_hash(res['shift_id'])
                if live_hash != res['facts_hash']:
                    connection.execute('UPDATE reports SET is_stale=1 WHERE id=?', (report_id,))
                    res['is_stale'] = 1
            return res

    def finalize(self, report_id, acknowledge_errors=False, require_validation=False,
                 acknowledge_stale=False):
        """Finalize a report.

        Staleness check: if the report's stored facts_hash no longer matches the
        live state of the shift, the report is marked stale and finalization is
        rejected.  Pass ``acknowledge_stale=True`` only as an emergency override
        (e.g. the shift is already closed and re-generation is impossible).

        Args:
            report_id: Primary key of the report to finalize.
            acknowledge_errors: If True, bypass error-level validation warnings.
            require_validation: If True, reject if no validation record exists.
            acknowledge_stale: If True, allow finalizing a stale report (use
                sparingly; the report text may not reflect current facts).

        Raises:
            ValueError: For missing report, staleness, or validation failures.
        """
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM reports WHERE id=?', (report_id,)).fetchone()
            if not row:
                raise ValueError('Report not found.')
            report = dict(row)

            # --- Staleness check (Product Invariant 4.1 Truthfulness) ---
            # Re-check is_stale flag first (may already be set by report() or
            # mark_report_stale()).  For PRIMARY reports (no source_report_id),
            # also recompute the live hash inside this transaction to catch any
            # changes since the flag was last evaluated.
            #
            # REVISION reports (source_report_id set) carry a FROZEN snapshot and
            # intentionally represent a subset of facts (e.g. wording edits that
            # don't pull in late-logged activities).  Their staleness is governed
            # exclusively by the explicit is_stale flag.
            already_stale = bool(report.get('is_stale'))
            is_revision = bool(report.get('source_report_id'))
            if not already_stale and not is_revision and report.get('facts_hash') and not report.get('finalized'):
                live_hash = self.compute_live_facts_hash(report['shift_id'])
                if live_hash != report['facts_hash']:
                    connection.execute('UPDATE reports SET is_stale=1 WHERE id=?', (report_id,))
                    already_stale = True

            if already_stale and not acknowledge_stale:
                raise ValueError(
                    'Report is stale: facts changed since it was generated '
                    '(tasks, cases, or test sessions were modified). '
                    'Regenerate the report or use a revision before finalizing.'
                )

            # --- Validation check ---
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

    def claim_update(self, update_id: int, timeout_seconds: float = 60.0) -> bool:
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM updates WHERE id=?', (update_id,)).fetchone()
            now = now_iso()
            if not row:
                connection.execute('''INSERT INTO updates
                    (id, status, claimed_at, attempts) VALUES (?, 'processing', ?, 1)''',
                    (update_id, now))
                return True
            status = row['status']
            if status == 'completed':
                return False
            if status == 'failed':
                attempts = (row['attempts'] or 1) + 1
                connection.execute('''UPDATE updates SET status='processing',
                    claimed_at=?, attempts=? WHERE id=?''', (now, attempts, update_id))
                return True
            if status == 'processing':
                claimed_at = row['claimed_at']
                if claimed_at:
                    try:
                        diff = (datetime.now(timezone.utc) - datetime.fromisoformat(claimed_at)).total_seconds()
                        if diff > timeout_seconds:
                            attempts = (row['attempts'] or 1) + 1
                            connection.execute('''UPDATE updates SET claimed_at=?, attempts=?
                                WHERE id=?''', (now, attempts, update_id))
                            return True
                    except Exception:
                        pass
                return False
            return False

    def complete_update(self, update_id: int):
        with self.connect() as connection:
            connection.execute('''UPDATE updates SET status='completed', completed_at=?
                WHERE id=?''', (now_iso(), update_id))

    def fail_update(self, update_id: int, error: str | None = None):
        with self.connect() as connection:
            connection.execute('''UPDATE updates SET status='failed', last_error=?
                WHERE id=?''', (str(error) if error else None, update_id))

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
            owner_filter = '' if include_observed else " AND (author_is_owner=1 OR source_type IN ('csv', 'freshdesk', 'freshchat'))"
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
            if author_is_owner is True:
                where_clauses.append("(author_is_owner=1 OR source_type IN ('csv', 'freshdesk', 'freshchat'))")
            elif author_is_owner is False:
                where_clauses.append("author_is_owner=0 AND source_type NOT IN ('csv', 'freshdesk', 'freshchat')")
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
            event_id = None
            if shift_id or detail:
                event_id = connection.execute('''INSERT INTO case_events
                    (case_id,shift_id,event_type,detail,actor_role,outcome,occurred_at,created_at)
                    VALUES (?,?,?,?,?,?,?,?)''',
                    (case_id, shift_id, event_type, detail or title, 'owner', status, stamp, stamp)).lastrowid
            self._index_source_in_connection(connection, 'case', case_id)
            if event_id:
                self._index_source_in_connection(connection, 'case_event', event_id)
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
            case = connection.execute('SELECT c.name as client_name, w.* FROM work_cases w LEFT JOIN clients c ON w.client_id = c.id WHERE w.id=?', (case_id,)).fetchone()
            if not case:
                raise ValueError('Case not found.')
            stamp = now_iso()
            event_id = connection.execute('''INSERT INTO case_events
                (case_id,shift_id,event_type,detail,actor_role,outcome,source_message_id,
                 occurred_at,created_at) VALUES (?,?,?,?,?,?,?,?,?)''',
                (case_id, shift_id, event_type, detail, actor_role, outcome,
                 source_message_id, occurred_at or stamp, stamp)).lastrowid
            connection.execute('UPDATE work_cases SET updated_at=? WHERE id=?', (stamp, case_id))
            self._index_source_in_connection(connection, 'case', case_id)
            self._index_source_in_connection(connection, 'case_event', event_id)
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
            self._index_source_in_connection(connection, 'case', case_id)
            self._index_source_in_connection(connection, 'case_event', event_id)
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
            self._index_source_in_connection(connection, 'test_session', session_id)
            if event_id:
                self._index_source_in_connection(connection, 'case_event', event_id)
            if case_id:
                self._index_source_in_connection(connection, 'case', case_id)
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
            self._index_source_in_connection(connection, 'test_session', session_id)
            if event_id:
                self._index_source_in_connection(connection, 'case_event', event_id)

    def evidence_item(self, evidence_id):
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM evidence WHERE id=?', (evidence_id,)).fetchone()
            return dict(row) if row else None

    def add_evidence(self, kind, shift_id=None, case_id=None, test_session_id=None, path=None,
                     telegram_file_id=None, caption=None, sha256=None, mime_type=None):
        if path and not sha256:
            p = Path(path)
            if p.is_file():
                import hashlib
                h = hashlib.sha256()
                with p.open('rb') as fh:
                    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                        h.update(chunk)
                sha256 = h.hexdigest()

        with self.connect() as connection:
            if test_session_id is not None:
                ts = connection.execute('SELECT id, case_id FROM test_sessions WHERE id=?', (test_session_id,)).fetchone()
                if not ts:
                    raise ValueError(f'Test session {test_session_id} not found.')
                if case_id is not None and ts['case_id'] != case_id:
                    raise ValueError(f'Test session {test_session_id} does not belong to case {case_id}.')
            if case_id is not None:
                c = connection.execute('SELECT 1 FROM work_cases WHERE id=?', (case_id,)).fetchone()
                if not c:
                    raise ValueError(f'Case {case_id} not found.')

            if sha256:
                existing = connection.execute('''SELECT id FROM evidence WHERE sha256=?
                    AND case_id IS ? AND test_session_id IS ?''',
                    (sha256, case_id, test_session_id)).fetchone()
                if existing:
                    return existing[0]

            evidence_id = connection.execute('''INSERT OR IGNORE INTO evidence
                (case_id,test_session_id,shift_id,kind,path,telegram_file_id,caption,sha256,mime_type,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (case_id, test_session_id, shift_id, kind, str(path) if path else None, telegram_file_id, caption,
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

    def snooze_followup(self, followup_id, due_at=None, *, days=None, correlation_id=None, actor='system'):
        """Set or advance a pending follow-up due date through one audited path."""
        with self.connect() as connection:
            before = connection.execute('SELECT * FROM followups WHERE id=?', (followup_id,)).fetchone()
            if not before:
                return False
            if days is not None:
                try:
                    base = datetime.fromisoformat((before['due_at'] or now_iso()).replace('Z', '+00:00'))
                except ValueError:
                    base = datetime.now(timezone.utc)
                due_at = (base + timedelta(days=int(days))).isoformat()
            if not due_at:
                raise ValueError('A due_at value or days offset is required.')
            if connection.execute('''UPDATE followups SET due_at=?,reminded_at=NULL
                WHERE id=? AND status='pending' ''', (due_at, followup_id)).rowcount != 1:
                raise ValueError('Follow-up is unavailable or already completed.')
            if correlation_id:
                after = connection.execute('SELECT * FROM followups WHERE id=?', (followup_id,)).fetchone()
                fields = ('due_at', 'reminded_at')
                self._record_audit_in_connection(
                    connection, correlation_id, 'snooze_followup', actor, 'followups', followup_id,
                    {k: before[k] for k in fields}, {k: after[k] for k in fields})
        return True

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

    def record_agent_run_audit(self, run_id: str, source_update_id: int | None,
                               metadata: dict) -> int:
        """Persist operational agent metadata without prompts, reasoning, or credentials."""
        safe = {key: metadata.get(key) for key in (
            'route', 'authorization_scope', 'tool_ids', 'tool_count', 'final_status',
            'duration_ms', 'error_reference')}
        with self.connect() as connection:
            return self._record_audit_in_connection(
                connection, run_id, 'external_agent_run', 'agent', 'agent_runs',
                int(source_update_id or 0), None, safe, 'irreversible')

    def record_external_write_audit(self, proposal_id: str, owner_id: int | None,
                                    metadata: dict) -> int:
        """Persist a secret-free record of a confirmed external mutation attempt."""
        safe = {key: metadata.get(key) for key in (
            'server', 'canonical_tool_id', 'arguments_hash', 'risk',
            'authorization_family', 'required_capabilities', 'execution_timestamp', 'status')}
        safe['proposal_id'] = proposal_id
        safe['confirmation_owner'] = owner_id
        with self.connect() as connection:
            return self._record_audit_in_connection(
                connection, proposal_id, 'confirmed_external_write', 'owner',
                'external_writes', int(owner_id or 0), None, safe, 'irreversible')

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
            'source_messages', 'shift_templates', 'shifts',
            'record_links', 'plan_snapshots', 'nl_corrections'
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

    def get_relevant_corrections(self, query_text: str, limit: int = 5,
                                  min_score: float = 0.25, intent: str = None) -> list[dict]:
        """
        Search saved corrections by similarity to the incoming message instead of simply
        selecting the newest. Considers wording, intent, and entities.
        """
        def _score(q: str, c: str) -> float:
            if not q or not c:
                return 0.0
            qn = re.sub(r'\s+', ' ', q.strip().casefold())
            cn = re.sub(r'\s+', ' ', c.strip().casefold())
            if qn == cn:
                return 1.0
            seq = difflib.SequenceMatcher(None, qn, cn).ratio()
            q_tokens = set(re.findall(r'\b[a-z0-9_#-]+\b', qn))
            c_tokens = set(re.findall(r'\b[a-z0-9_#-]+\b', cn))
            if not q_tokens or not c_tokens:
                return seq
            jaccard = len(q_tokens & c_tokens) / len(q_tokens | c_tokens)
            containment = len(q_tokens & c_tokens) / min(len(q_tokens), len(c_tokens))
            kw_groups = (
                {'shift', 'working', 'hours', 'extended', 'lunch', 'day off'},
                {'carried', 'carry', 'yesterday', 'unfinished', 'pending'},
                {'testing', 'tested', 'retest', 'reproduced', 'verified'},
                {'addressed', 'resolved', 'assisted', 'query', 'concern', 'issue', 'teams', 'whatsapp'},
                {'reported', 'shared', 'developer', 'dev', 'confirmed'},
                {'tod', 'pl', 'eod', 'lunch update', 'start of day', 'end of day'},
            )
            bonus = 0.0
            for g in kw_groups:
                if (q_tokens & g) and (c_tokens & g):
                    bonus += 0.08
                    break
            return max(0.0, min(1.0, 0.35 * seq + 0.35 * jaccard + 0.20 * containment + bonus))

        with self.connect() as connection:
            if intent:
                rows = connection.execute('''
                    SELECT c.*, i.raw_text
                    FROM nl_corrections c
                    LEFT JOIN nl_interactions i ON c.interaction_id = i.id
                    WHERE c.corrected_intent = ? AND COALESCE(c.is_active, 1) = 1
                    ORDER BY c.id DESC LIMIT 200
                ''', (intent,)).fetchall()
            else:
                rows = connection.execute('''
                    SELECT c.*, i.raw_text
                    FROM nl_corrections c
                    LEFT JOIN nl_interactions i ON c.interaction_id = i.id
                    WHERE COALESCE(c.is_active, 1) = 1
                    ORDER BY c.id DESC LIMIT 200
                ''').fetchall()
            scored = []
            for r in rows:
                d = dict(r)
                if d.get('corrected_entities_json'):
                    try:
                        d['corrected_entities'] = json.loads(d['corrected_entities_json'])
                    except Exception:
                        d['corrected_entities'] = {}
                else:
                    d['corrected_entities'] = {}
                raw_text = d.get('raw_text') or ''
                similarity = _score(query_text, raw_text) if query_text else 0.0
                d['similarity_score'] = round(similarity, 4)
                d['score'] = round(similarity, 4)
                if not query_text or similarity >= min_score:
                    scored.append(d)
            if query_text:
                scored.sort(key=lambda item: (item['similarity_score'], item['id']), reverse=True)
            return scored[:limit]

    def get_approved_corrections(self, limit: int = 5, intent: str = None, query_text: str = None) -> list[dict]:
        if query_text:
            return self.get_relevant_corrections(query_text, limit=limit, intent=intent)
        with self.connect() as connection:
            if intent:
                rows = connection.execute('''
                    SELECT c.*, i.raw_text
                    FROM nl_corrections c
                    LEFT JOIN nl_interactions i ON c.interaction_id = i.id
                    WHERE c.corrected_intent = ? AND COALESCE(c.is_active, 1) = 1
                    ORDER BY c.id DESC LIMIT ?
                ''', (intent, limit)).fetchall()
            else:
                rows = connection.execute('''
                    SELECT c.*, i.raw_text
                    FROM nl_corrections c
                    LEFT JOIN nl_interactions i ON c.interaction_id = i.id
                    WHERE COALESCE(c.is_active, 1) = 1
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

    def list_nl_corrections(self, limit: int = 50, include_inactive: bool = True) -> list[dict]:
        with self.connect() as conn:
            query = '''
                SELECT c.*, i.raw_text
                FROM nl_corrections c
                LEFT JOIN nl_interactions i ON c.interaction_id = i.id
            '''
            if not include_inactive:
                query += ' WHERE COALESCE(c.is_active, 1) = 1'
            query += ' ORDER BY c.id DESC LIMIT ?'
            rows = conn.execute(query, (limit,)).fetchall()
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

    def set_nl_correction_active(self, correction_id: int, is_active: bool) -> bool:
        with self.connect() as conn:
            cur = conn.execute('UPDATE nl_corrections SET is_active=? WHERE id=?', (1 if is_active else 0, correction_id))
            return cur.rowcount > 0

    def delete_nl_correction(self, correction_id: int) -> bool:
        with self.connect() as conn:
            cur = conn.execute('DELETE FROM nl_corrections WHERE id=?', (correction_id,))
            return cur.rowcount > 0

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
        """Return the current conversation context.

        Canonical return shape::

            {
                "active_case_id": int | None,
                "active_task_id": int | None,
                "active_test_session_id": int | None,
                "active_client": str | None,
                "last_intent": str | None,
                "expires_at": str,
                "data": {          # ← nested custom data (was flat in older versions)
                    "expecting_blocker_reason_for": int | None,
                    "pending_clarification": str | None,
                    ...
                }
            }

        The "data" key always exists (defaulting to {}) so callers can safely
        do ``ctx.get("data", {})`` or ``ctx["data"]`` without a KeyError.
        """
        now = now_iso()
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM conversation_context WHERE context_key=?', (context_key,)).fetchone()
            if not row:
                return {'data': {}}
            if row['expires_at'] < now:
                return {'data': {}}
            raw_data = json.loads(row['context_data_json']) if row['context_data_json'] else {}
            return {
                'active_case_id': row['active_case_id'],
                'active_task_id': row['active_task_id'],
                'active_test_session_id': row['active_test_session_id'],
                'active_client': row['active_client'],
                'last_intent': row['last_intent'],
                'expires_at': row['expires_at'],
                'data': raw_data,  # canonical nested key — no longer merged flat
            }

    def update_conversation_context(self, context_key='owner', active_case_id=None, active_task_id=None,
                                    active_test_session_id=None, active_client=None, last_intent=None,
                                    context_data=None, ttl_minutes=60):
        """Update (or create) the conversation context for context_key.

        ``context_data`` is a dict of custom fields (e.g. clarification state).
        It is MERGED with the existing custom data in the "data" nested key.
        """
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(minutes=ttl_minutes)).isoformat()
        current = self.get_conversation_context(context_key)

        # Correctly extract existing custom data from the canonical nested key.
        # Previously this was current.get('data', {}) which always returned {}
        # because old get_conversation_context() returned a flat dict without
        # a 'data' key.  Now it is always present as a nested dict.
        merged_data = dict(current.get('data') or {})
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

    # --- Durable Conversation Memory (conversation_turns) ---

    def record_conversation_turn(self, owner_id: int, role: str, text: str,
                                 shift_id: int | None = None,
                                 intent: str | None = None,
                                 entities_json: str | None = None,
                                 case_id: int | None = None,
                                 task_id: int | None = None,
                                 test_session_id: int | None = None,
                                 source_update_id: int | None = None,
                                 correlation_id: str | None = None,
                                 thread_id: str = 'primary', source_channel: str = 'system',
                                 source_message_id: str | None = None, client_message_id: str | None = None,
                                 created_at: str | None = None,
                                 metadata: dict | str | None = None,
                                 **kwargs) -> int:
        """Record a single conversational turn (user, assistant, or system)."""
        if role not in ('user', 'assistant', 'system'):
            raise ValueError(f"Invalid role: {role!r}. Must be 'user', 'assistant', or 'system'.")
        if not entities_json and metadata:
            entities_json = json.dumps(metadata) if isinstance(metadata, dict) else metadata
        stamp = created_at or now_iso()
        with self.connect() as connection:
            if source_update_id is not None:
                existing = connection.execute(
                    'SELECT id FROM conversation_turns WHERE owner_id=? AND source_update_id=? AND role=?',
                    (owner_id, source_update_id, role)
                ).fetchone()
                if existing:
                    return existing['id']
            if client_message_id is not None:
                existing = connection.execute(
                    'SELECT id FROM conversation_turns WHERE owner_id=? AND client_message_id=? AND role=?',
                    (owner_id, client_message_id, role)).fetchone()
                if existing:
                    return existing['id']
            return connection.execute('''INSERT INTO conversation_turns
                (owner_id, shift_id, role, text, intent, entities_json,
                 case_id, task_id, test_session_id, source_update_id, thread_id,
                 source_channel, source_message_id, client_message_id, correlation_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (owner_id, shift_id, role, text, intent, entities_json,
                 case_id, task_id, test_session_id, source_update_id, thread_id,
                 source_channel, source_message_id, client_message_id, correlation_id, stamp)).lastrowid

    def get_active_conversation_thread(self, owner_id: int) -> str:
        key = f'conversation_thread:{owner_id}'
        thread_id = self.get_setting(key) or 'primary'
        stamp = now_iso()
        with self.connect() as connection:
            connection.execute('''INSERT OR IGNORE INTO conversation_threads
                (id, owner_id, title, status, created_at, updated_at) VALUES (?,?,?, 'active', ?, ?)''',
                (thread_id, owner_id, 'Primary conversation', stamp, stamp))
        return thread_id

    def start_new_conversation_thread(self, owner_id: int) -> str:
        previous = self.get_active_conversation_thread(owner_id)
        thread_id = f'thread:{uuid.uuid4().hex}'
        stamp = now_iso()
        with self.connect() as connection:
            connection.execute("UPDATE conversation_threads SET status='archived', archived_at=?, updated_at=? WHERE id=? AND owner_id=?",
                               (stamp, stamp, previous, owner_id))
            connection.execute('''INSERT INTO conversation_threads
                (id, owner_id, title, status, created_at, updated_at) VALUES (?,?,?, 'active', ?, ?)''',
                (thread_id, owner_id, 'New conversation', stamp, stamp))
        self.set_setting(f'conversation_thread:{owner_id}', thread_id)
        return thread_id

    def get_recent_turns(self, owner_id: int, limit: int = 20,
                         shift_id: int | None = None, thread_id: str | None = None) -> list[dict]:
        """Retrieve recent conversation turns in chronological order (oldest to newest)."""
        query = 'SELECT * FROM conversation_turns WHERE owner_id=?'
        params: list = [owner_id]
        if shift_id is not None:
            query += ' AND shift_id=?'
            params.append(shift_id)
        if thread_id is not None:
            query += ' AND thread_id=?'
            params.append(thread_id)
        query += ' ORDER BY id DESC LIMIT ?'
        params.append(limit)
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
            return [dict(r) for r in reversed(rows)]

    def count_conversation_turns(self, owner_id: int | None = None) -> int:
        """Return the total number of recorded conversation turns."""
        with self.connect() as connection:
            if owner_id is not None:
                row = connection.execute(
                    'SELECT COUNT(*) FROM conversation_turns WHERE owner_id=?', (owner_id,)).fetchone()
            else:
                row = connection.execute('SELECT COUNT(*) FROM conversation_turns').fetchone()
            return row[0] if row else 0

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

    def create_proposal(self, prop_id: str, owner_id: int, action_type: str, payload_json: str | dict = None, source_update_id: int = None, expires_at: str = None) -> str:
        now = datetime.now(timezone.utc)
        exp = expires_at or (now + timedelta(minutes=15)).isoformat()
        oid = int(owner_id) if owner_id is not None else (config.OWNER_ID or 1)
        json_str = payload_json if isinstance(payload_json, str) else json.dumps(payload_json)
        with self.connect() as connection:
            connection.execute('''INSERT OR REPLACE INTO nl_proposals
                (id, owner_id, source_update_id, intent, proposal_json, status, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)''',
                (prop_id, oid, source_update_id, action_type, json_str, exp, now.isoformat()))
        return prop_id

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
            d['action_type'] = d.get('intent')
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
            d['action_type'] = d.get('intent')
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
            result['action_type'] = result.get('intent')
            return result

    def finish_nl_proposal(self, proposal_id: str, status: str):
        if status not in {'accepted', 'executed', 'failed', 'cancelled'}:
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

    # --- Phase 5: Plan Snapshots, Record Links & Planning Conversations ---

    def save_plan_snapshot(self, shift_id: int, tasks: list) -> int:
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            latest = conn.execute(
                'SELECT MAX(version) FROM plan_snapshots WHERE shift_id=?',
                (shift_id,)
            ).fetchone()[0]
            version = (latest or 0) + 1
            items = []
            for t in tasks:
                if isinstance(t, Task):
                    items.append({
                        'id': t.id,
                        'title': t.title,
                        'status': t.status.value,
                        'priority': t.priority,
                        'client': t.client,
                        'ticket': t.ticket,
                        'blocked_reason': t.blocked_reason,
                        'next_action': t.next_action
                    })
                elif isinstance(t, dict):
                    items.append(t)
            cur = conn.execute('''
                INSERT INTO plan_snapshots (shift_id, version, snapshot_json, created_at)
                VALUES (?, ?, ?, ?)
            ''', (shift_id, version, json.dumps(items), now_iso()))
            return cur.lastrowid

    def get_latest_plan_snapshot(self, shift_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute('''
                SELECT * FROM plan_snapshots
                WHERE shift_id=?
                ORDER BY version DESC LIMIT 1
            ''', (shift_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d['tasks'] = json.loads(d['snapshot_json'])
            return d

    def get_baseline_plan_snapshot(self, shift_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute('''
                SELECT * FROM plan_snapshots
                WHERE shift_id=?
                ORDER BY version ASC LIMIT 1
            ''', (shift_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d['tasks'] = json.loads(d['snapshot_json'])
            return d

    def get_plan_snapshots(self, shift_id: int) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute('''
                SELECT * FROM plan_snapshots
                WHERE shift_id=?
                ORDER BY version ASC
            ''', (shift_id,)).fetchall()
            results = []
            for r in rows:
                d = dict(r)
                d['tasks'] = json.loads(d['snapshot_json'])
                results.append(d)
            return results

    def link_records(self, source_type: str, source_id: int, target_type: str, target_id: int, link_type: str = 'related') -> int:
        with self.connect() as conn:
            row = conn.execute('''
                SELECT id FROM record_links
                WHERE (source_type=? AND source_id=? AND target_type=? AND target_id=?)
                   OR (source_type=? AND source_id=? AND target_type=? AND target_id=?)
            ''', (source_type, source_id, target_type, target_id, target_type, target_id, source_type, source_id)).fetchone()
            if row:
                return row['id']
            cur = conn.execute('''
                INSERT INTO record_links (source_type, source_id, target_type, target_id, link_type, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (source_type, source_id, target_type, target_id, link_type, now_iso()))
            return cur.lastrowid

    def unlink_records(self, source_type: str, source_id: int, target_type: str, target_id: int) -> bool:
        with self.connect() as conn:
            res = conn.execute('''
                DELETE FROM record_links
                WHERE (source_type=? AND source_id=? AND target_type=? AND target_id=?)
                   OR (source_type=? AND source_id=? AND target_type=? AND target_id=?)
            ''', (source_type, source_id, target_type, target_id, target_type, target_id, source_type, source_id))
            return res.rowcount > 0

    def get_linked_records(self, record_type: str, record_id: int) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute('''
                SELECT * FROM record_links
                WHERE (source_type=? AND source_id=?) OR (target_type=? AND target_id=?)
                ORDER BY id
            ''', (record_type, record_id, record_type, record_id)).fetchall()
            results = []
            for r in rows:
                d = dict(r)
                if d['source_type'] == record_type and d['source_id'] == record_id:
                    d['other_type'] = d['target_type']
                    d['other_id'] = d['target_id']
                else:
                    d['other_type'] = d['source_type']
                    d['other_id'] = d['source_id']
                results.append(d)
            return results

    def save_planning_conversation(self, owner_id: int, purpose: str, step: str, proposed_values: dict,
                                   shift_id: int | None = None, selected_record_type: str | None = None,
                                   selected_record_id: int | None = None, source_update_id: int | None = None,
                                   source_message_id: int | None = None, ttl_seconds: int = 3600) -> int:
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()
        now_str = now.isoformat()
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            active = conn.execute(
                'SELECT id FROM planning_conversations WHERE owner_id=? AND purpose=? AND status="active"',
                (owner_id, purpose)
            ).fetchone()
            if active:
                conn.execute('''
                    UPDATE planning_conversations
                    SET step=?, proposed_values_json=?, shift_id=COALESCE(?, shift_id),
                        selected_record_type=COALESCE(?, selected_record_type),
                        selected_record_id=COALESCE(?, selected_record_id),
                        source_update_id=COALESCE(?, source_update_id),
                        source_message_id=COALESCE(?, source_message_id),
                        expires_at=?, updated_at=?
                    WHERE id=?
                ''', (step, json.dumps(proposed_values), shift_id, selected_record_type,
                      selected_record_id, source_update_id, source_message_id,
                      expires_at, now_str, active['id']))
                return active['id']
            else:
                cur = conn.execute('''
                    INSERT INTO planning_conversations
                    (owner_id, purpose, step, proposed_values_json, shift_id,
                     selected_record_type, selected_record_id, source_update_id,
                     source_message_id, status, expires_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                ''', (owner_id, purpose, step, json.dumps(proposed_values), shift_id,
                      selected_record_type, selected_record_id, source_update_id,
                      source_message_id, expires_at, now_str, now_str))
                return cur.lastrowid

    def get_active_planning_conversation(self, owner_id: int, purpose: str = 'daily_planning') -> dict | None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            row = conn.execute('''
                SELECT * FROM planning_conversations
                WHERE owner_id=? AND purpose=? AND status='active' AND expires_at > ?
                ORDER BY id DESC LIMIT 1
            ''', (owner_id, purpose, now)).fetchone()
            if not row:
                return None
            d = dict(row)
            d['proposed_values'] = json.loads(d['proposed_values_json'])
            return d

    def complete_planning_conversation(self, owner_id: int, purpose: str = 'daily_planning'):
        with self.connect() as conn:
            conn.execute('''
                UPDATE planning_conversations
                SET status='completed', updated_at=?
                WHERE owner_id=? AND purpose=? AND status='active'
            ''', (now_iso(), owner_id, purpose))

    def cancel_planning_conversation(self, owner_id: int, purpose: str = 'daily_planning'):
        with self.connect() as conn:
            conn.execute('''
                UPDATE planning_conversations
                SET status='cancelled', updated_at=?
                WHERE owner_id=? AND purpose=? AND status='active'
            ''', (now_iso(), owner_id, purpose))

    def create_report_revision(self, source_report_id: int, text: str, style: str = 'standard', provider: str = None, model: str = None, facts_hash: str = None, facts_snapshot = None) -> int:
        stamp = now_iso()
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            src = conn.execute('SELECT * FROM reports WHERE id=?', (source_report_id,)).fetchone()
            if not src:
                raise ValueError('Source report not found.')
            latest_rev = conn.execute(
                'SELECT MAX(revision) FROM reports WHERE id=? OR source_report_id=?',
                (source_report_id, source_report_id)
            ).fetchone()[0] or 1
            new_rev = latest_rev + 1
            src_hash = src['facts_hash'] if 'facts_hash' in src.keys() else None
            effective_hash = facts_hash if facts_hash is not None else src_hash
            src_snap = src['facts_snapshot_json'] if 'facts_snapshot_json' in src.keys() else None
            effective_snap = json.dumps(facts_snapshot) if isinstance(facts_snapshot, dict) else (facts_snapshot if facts_snapshot is not None else src_snap)
            cur = conn.execute('''
                INSERT INTO reports (shift_id, kind, text, created_at, finalized, style, provider, model, source_report_id, revision, facts_hash, is_stale, facts_snapshot_json)
                VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, 0, ?)
            ''', (src['shift_id'], src['kind'], text, stamp, style, provider, model, source_report_id, new_rev, effective_hash, effective_snap))
            return cur.lastrowid

    def mark_report_stale(self, report_id: int = None, shift_id: int = None):
        with self.connect() as conn:
            if report_id is not None:
                conn.execute('UPDATE reports SET is_stale=1 WHERE id=?', (report_id,))
            elif shift_id is not None:
                conn.execute('UPDATE reports SET is_stale=1 WHERE shift_id=?', (shift_id,))

    def list_shifts(self, limit: int = 10) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute('SELECT * FROM shifts ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
            return [dict(r) for r in rows]

    def list_reports(self, shift_id: int = None) -> list[dict]:
        with self.connect() as conn:
            if shift_id:
                rows = conn.execute('SELECT * FROM reports WHERE shift_id=? ORDER BY id ASC', (shift_id,)).fetchall()
            else:
                rows = conn.execute('SELECT * FROM reports ORDER BY id ASC').fetchall()
            return [dict(r) for r in rows]

    def get_report_revisions(self, source_report_id: int) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute('SELECT * FROM reports WHERE source_report_id=? ORDER BY id ASC', (source_report_id,)).fetchall()
            return [dict(r) for r in rows]

    def save_conversation_context(self, context_key='owner', **kwargs):
        return self.update_conversation_context(context_key=context_key, **kwargs)

    def get_active_nl_proposal(self, owner_id=None):
        with self.connect() as conn:
            if owner_id:
                row = conn.execute(
                    "SELECT * FROM nl_proposals WHERE owner_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
                    (owner_id,)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM nl_proposals WHERE status='pending' ORDER BY id DESC LIMIT 1"
                ).fetchone()
            if not row:
                return None
            res = dict(row)
            res['proposal'] = json.loads(res['proposal_json'])
            return res

    def confirm_daily_plan_atomic(self, owner_id: int, conv_id: int, start_iso: str, end_iso: str,
                                  lunch_iso: str | None, task_plan: list[dict]) -> tuple[int, list[Task]]:
        """Atomically starts/revises shift, creates/associates tasks, saves baseline snapshot, and completes conversation."""
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conv_row = conn.execute('SELECT * FROM planning_conversations WHERE id=?', (conv_id,)).fetchone()
            if not conv_row:
                raise ValueError('Planning session expired or not found.')
            if conv_row['status'] != 'active':
                raise ValueError(f"Planning session is already {conv_row['status']}.")

            # 1. Start or revise shift
            active_shift = conn.execute(
                'SELECT * FROM shifts WHERE closed_at IS NULL ORDER BY id DESC LIMIT 1'
            ).fetchone()
            if active_shift:
                sid = active_shift['id']
                conn.execute(
                    'UPDATE shifts SET start=?, end=?, lunch=? WHERE id=?',
                    (start_iso, end_iso, lunch_iso, sid)
                )
            else:
                cur = conn.execute(
                    'INSERT INTO shifts (start, end, lunch) VALUES (?, ?, ?)',
                    (start_iso, end_iso, lunch_iso)
                )
                sid = cur.lastrowid

            # 2. Process tasks
            for item in task_plan:
                if item.get('is_new'):
                    title = item['title']
                    cur_t = conn.execute('''INSERT INTO tasks
                        (title,status,created_at,priority,planned_shift_id)
                        VALUES (?,?,?,?,?)''',
                        (title, 'pending', now_iso(), 0, sid))
                    tid = cur_t.lastrowid
                    self._activity(conn, sid, 'plan', title, task_id=tid)
                else:
                    tid = item['id']
                    # Associate existing task with this shift
                    conn.execute('UPDATE tasks SET planned_shift_id=? WHERE id=?', (sid, tid))
                    ex_act = conn.execute(
                        "SELECT id FROM activities WHERE shift_id=? AND task_id=? AND category='plan'",
                        (sid, tid)
                    ).fetchone()
                    if not ex_act:
                        t_row = conn.execute('SELECT title FROM tasks WHERE id=?', (tid,)).fetchone()
                        title = t_row['title'] if t_row else item.get('title', '')
                        self._activity(conn, sid, 'plan', title, task_id=tid)

            # Gather all tasks for this shift
            all_t_rows = conn.execute(
                'SELECT * FROM tasks WHERE planned_shift_id=? ORDER BY priority DESC, id ASC',
                (sid,)
            ).fetchall()
            confirmed_tasks = [Task.from_row(r) for r in all_t_rows]

            # 3. Save baseline plan snapshot
            snap_payload = [
                {
                    'id': t.id,
                    'title': t.title,
                    'status': getattr(t.status, 'value', str(t.status)),
                    'priority': t.priority,
                    'blocked_reason': t.blocked_reason,
                    'next_action': t.next_action,
                    'client': t.client,
                    'ticket': t.ticket
                }
                for t in confirmed_tasks
            ]
            latest_v = conn.execute(
                'SELECT MAX(version) FROM plan_snapshots WHERE shift_id=?',
                (sid,)
            ).fetchone()[0]
            next_version = (latest_v or 0) + 1
            conn.execute('''
                INSERT INTO plan_snapshots (shift_id, version, snapshot_json, created_at)
                VALUES (?, ?, ?, ?)
            ''', (sid, next_version, json.dumps(snap_payload), now_iso()))

            # 4. Save daily plan setting
            plan_ids_json = json.dumps([t.id for t in confirmed_tasks])
            conn.execute(
                'INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                (f'daily_plan:{sid}', plan_ids_json)
            )

            # 5. Complete planning conversation
            conn.execute(
                "UPDATE planning_conversations SET status='completed', updated_at=? WHERE id=?",
                (now_iso(), conv_id)
            )

            return sid, confirmed_tasks

    def save_memory_summary(self, owner_id: int, memory_type: str, summary_text: str,
                            shift_id: int | None = None, source_turn_start_id: int | None = None,
                            source_turn_end_id: int | None = None) -> int:
        stamp = now_iso()
        with self.connect() as connection:
            summary_id = connection.execute('''INSERT INTO assistant_memory_summaries
                (owner_id, shift_id, memory_type, summary_text, source_turn_start_id, source_turn_end_id, version, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)''',
                (owner_id, shift_id, memory_type, summary_text, source_turn_start_id, source_turn_end_id, stamp, stamp)).lastrowid
            self._index_source_in_connection(connection, 'conversation_summary', summary_id)
            return summary_id

    def get_latest_memory_summary(self, owner_id: int, shift_id: int | None = None) -> dict | None:
        with self.connect() as connection:
            if shift_id:
                row = connection.execute('''SELECT * FROM assistant_memory_summaries
                    WHERE owner_id=? AND shift_id=? ORDER BY id DESC LIMIT 1''', (owner_id, shift_id)).fetchone()
            else:
                row = connection.execute('''SELECT * FROM assistant_memory_summaries
                    WHERE owner_id=? ORDER BY id DESC LIMIT 1''', (owner_id,)).fetchone()
            return dict(row) if row else None

    def update_memory_summary(self, summary_id: int, summary_text: str) -> bool:
        """Update a summary and its single canonical FTS row in one transaction."""
        with self.connect() as connection:
            changed = connection.execute('''UPDATE assistant_memory_summaries
                SET summary_text=?, version=version+1, updated_at=? WHERE id=?''',
                (summary_text, now_iso(), summary_id)).rowcount
            if changed:
                self._index_source_in_connection(connection, 'conversation_summary', summary_id)
            return bool(changed)

    def index_fts_record(self, source_type: str, source_id: str | int, title: str, content: str,
                         client: str | None = None, product: str | None = None, created_at: str | None = None):
        stamp = created_at or now_iso()
        with self.connect() as connection:
            try:
                self._fts_replace(connection, str(source_type), source_id, title, content,
                                  client, product, stamp)
            except sqlite3.Error:
                pass

    def search_historical_memory(self, query: str, limit: int = 10) -> list[dict]:
        if not query or not query.strip():
            return []
        cleaned = re.sub(r'[^\w\s]', ' ', query).strip()
        if not cleaned:
            return []
        with self.connect() as connection:
            try:
                rows = connection.execute('''SELECT * FROM fts_work_memory WHERE fts_work_memory MATCH ?
                    ORDER BY rowid DESC LIMIT ?''', (cleaned, limit)).fetchall()
            except sqlite3.OperationalError:
                words = cleaned.split()
                where_clause = ' OR '.join(['title LIKE ? OR content LIKE ?' for _ in words])
                params = []
                for w in words:
                    params.extend([f'%{w}%', f'%{w}%'])
                params.append(limit)
                rows = connection.execute(f'''SELECT * FROM fts_work_memory WHERE {where_clause}
                    ORDER BY rowid DESC LIMIT ?''', params).fetchall()
            return [dict(row) for row in rows]

    # ==========================================================================
    # Workflows, Members, Roles & Work Item Metadata Operations (Schema v16)
    # ==========================================================================

    def get_workflow_templates(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM workflow_templates ORDER BY is_default DESC, id ASC").fetchall()
            templates = [dict(r) for r in rows]
            for tmpl in templates:
                stg_rows = conn.execute(
                    "SELECT * FROM workflow_stages WHERE template_id=? ORDER BY stage_order ASC",
                    (tmpl['id'],)
                ).fetchall()
                tmpl['stages'] = [dict(s) for s in stg_rows]
            return templates

    def get_workflow_template(self, template_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM workflow_templates WHERE id=?", (template_id,)).fetchone()
            if not row:
                return None
            tmpl = dict(row)
            stg_rows = conn.execute(
                "SELECT * FROM workflow_stages WHERE template_id=? ORDER BY stage_order ASC",
                (template_id,)
            ).fetchall()
            tmpl['stages'] = [dict(s) for s in stg_rows]
            return tmpl

    def create_workflow_template(self, name: str, description: str | None = None,
                                 work_type: str = 'requirement', is_default: bool = False,
                                 stages: list[dict] | None = None) -> int:
        now = now_iso()
        with self.connect() as conn:
            if is_default:
                conn.execute("UPDATE workflow_templates SET is_default=0 WHERE work_type=?", (work_type,))
            cur = conn.execute(
                "INSERT INTO workflow_templates (name, description, work_type, is_default, created_at) VALUES (?, ?, ?, ?, ?)",
                (name, description, work_type, 1 if is_default else 0, now)
            )
            template_id = cur.lastrowid
            if stages:
                for idx, stage in enumerate(stages, 1):
                    conn.execute('''INSERT INTO workflow_stages
                        (template_id, name, stage_order, description, expected_role, expected_duration_hours, is_waiting, is_active, required_artifacts_json, checklist_items_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)''',
                        (template_id, stage['name'], stage.get('stage_order', idx),
                         stage.get('description'), stage.get('expected_role'),
                         stage.get('expected_duration_hours'), 1 if stage.get('is_waiting') else 0,
                         json.dumps(stage.get('required_artifacts', [])),
                         json.dumps(stage.get('checklist_items', []))))
            return template_id

    def get_roles(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM roles ORDER BY name ASC").fetchall()
            return [dict(r) for r in rows]

    def add_role(self, name: str, description: str | None = None) -> int:
        with self.connect() as conn:
            cur = conn.execute("INSERT OR IGNORE INTO roles (name, description, created_at) VALUES (?, ?, ?)",
                               (name, description, now_iso()))
            if cur.lastrowid:
                return cur.lastrowid
            existing = conn.execute("SELECT id FROM roles WHERE name=?", (name,)).fetchone()
            return existing['id']

    def get_members(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM members ORDER BY name ASC").fetchall()
            members = [dict(r) for r in rows]
            for m in members:
                r_rows = conn.execute('''SELECT r.name FROM roles r
                    JOIN member_roles mr ON mr.role_id=r.id WHERE mr.member_id=?''', (m['id'],)).fetchall()
                m['roles'] = [row['name'] for row in r_rows]
            return members

    def get_member(self, member_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM members WHERE id=?", (member_id,)).fetchone()
            if not row:
                return None
            m = dict(row)
            r_rows = conn.execute('''SELECT r.name FROM roles r
                JOIN member_roles mr ON mr.role_id=r.id WHERE mr.member_id=?''', (member_id,)).fetchall()
            m['roles'] = [row['name'] for row in r_rows]
            return m

    def add_member(self, name: str, email: str | None = None,
                   telegram_handle: str | None = None, notes: str | None = None,
                   roles: list[str] | None = None) -> int:
        now = now_iso()
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO members (name, email, telegram_handle, notes, created_at) VALUES (?, ?, ?, ?, ?)",
                (name, email, telegram_handle, notes, now)
            )
            member_id = cur.lastrowid
            if roles:
                for role_name in roles:
                    r_row = conn.execute("SELECT id FROM roles WHERE name=?", (role_name,)).fetchone()
                    if r_row:
                        conn.execute("INSERT OR IGNORE INTO member_roles (member_id, role_id) VALUES (?, ?)",
                                     (member_id, r_row['id']))
            return member_id

    def get_work_item_meta(self, entity_type: str, entity_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute('''SELECT m.*, t.name AS template_name, s.name AS stage_name, s.is_waiting,
                mem.name AS owner_name
                FROM work_item_meta m
                LEFT JOIN workflow_templates t ON t.id=m.workflow_template_id
                LEFT JOIN workflow_stages s ON s.id=m.current_stage_id
                LEFT JOIN members mem ON mem.id=m.owner_member_id
                WHERE m.entity_type=? AND m.entity_id=?''', (entity_type, entity_id)).fetchone()
            return dict(row) if row else None

    def upsert_work_item_meta(self, entity_type: str, entity_id: int, **kwargs) -> dict:
        now = now_iso()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT * FROM work_item_meta WHERE entity_type=? AND entity_id=?",
                (entity_type, entity_id)
            ).fetchone()
            if existing:
                updates = []
                params = []
                for k, v in kwargs.items():
                    if k in ('workflow_template_id', 'current_stage_id', 'operational_status',
                             'owner_member_id', 'target_role', 'estimated_hours', 'actual_hours',
                             'due_date', 'next_action', 'completion_note'):
                        updates.append(f"{k}=?")
                        params.append(v)
                updates.append("updated_at=?")
                params.append(now)
                params.extend([entity_type, entity_id])
                conn.execute(f"UPDATE work_item_meta SET {', '.join(updates)} WHERE entity_type=? AND entity_id=?", params)
            else:
                conn.execute('''INSERT INTO work_item_meta
                    (entity_type, entity_id, workflow_template_id, current_stage_id, operational_status,
                     owner_member_id, target_role, estimated_hours, actual_hours, due_date, next_action,
                     completion_note, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (entity_type, entity_id, kwargs.get('workflow_template_id'),
                     kwargs.get('current_stage_id'), kwargs.get('operational_status', 'active'),
                     kwargs.get('owner_member_id'), kwargs.get('target_role'),
                     kwargs.get('estimated_hours'), kwargs.get('actual_hours'),
                     kwargs.get('due_date'), kwargs.get('next_action'),
                     kwargs.get('completion_note'), now, now))
        return self.get_work_item_meta(entity_type, entity_id)

    def transition_stage(self, entity_type: str, entity_id: int, new_stage_id: int,
                         actor_member_id: int | None = None, note: str | None = None) -> bool:
        now = now_iso()
        with self.connect() as conn:
            # Check current stage
            meta = conn.execute(
                "SELECT current_stage_id, workflow_template_id FROM work_item_meta WHERE entity_type=? AND entity_id=?",
                (entity_type, entity_id)
            ).fetchone()
            if meta and meta['current_stage_id']:
                conn.execute('''UPDATE stage_history SET exited_at=?
                    WHERE entity_type=? AND entity_id=? AND stage_id=? AND exited_at IS NULL''',
                    (now, entity_type, entity_id, meta['current_stage_id']))

            # Insert new stage history
            conn.execute('''INSERT INTO stage_history
                (entity_type, entity_id, stage_id, entered_at, actor_member_id, note)
                VALUES (?, ?, ?, ?, ?, ?)''',
                (entity_type, entity_id, new_stage_id, now, actor_member_id, note))

            # Fetch stage info to determine waiting
            stg = conn.execute("SELECT is_waiting, name FROM workflow_stages WHERE id=?", (new_stage_id,)).fetchone()
            is_waiting = bool(stg['is_waiting']) if stg else False

            # Update meta
            if meta:
                conn.execute('''UPDATE work_item_meta SET current_stage_id=?, updated_at=?,
                    operational_status = CASE WHEN operational_status IN ('completed', 'cancelled') THEN operational_status
                                              WHEN ? = 1 THEN 'waiting'
                                              ELSE 'active' END
                    WHERE entity_type=? AND entity_id=?''',
                    (new_stage_id, now, 1 if is_waiting else 0, entity_type, entity_id))
            else:
                conn.execute('''INSERT INTO work_item_meta
                    (entity_type, entity_id, current_stage_id, operational_status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)''',
                    (entity_type, entity_id, new_stage_id, 'waiting' if is_waiting else 'active', now, now))
            return True

    def get_stage_history(self, entity_type: str, entity_id: int) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute('''SELECT h.*, s.name AS stage_name, m.name AS actor_name
                FROM stage_history h
                JOIN workflow_stages s ON s.id=h.stage_id
                LEFT JOIN members m ON m.id=h.actor_member_id
                WHERE h.entity_type=? AND h.entity_id=?
                ORDER BY h.id ASC''', (entity_type, entity_id)).fetchall()
            return [dict(r) for r in rows]

    # Blockers and Dependencies
    def add_blocker_dependency(self, entity_type: str, entity_id: int, dependency_type: str,
                               description: str, waiting_on_member_id: int | None = None,
                               waiting_on_role: str | None = None, target_entity_type: str | None = None,
                               target_entity_id: int | None = None,
                               expected_resolution_date: str | None = None) -> int:
        now = now_iso()
        with self.connect() as conn:
            cur = conn.execute('''INSERT INTO blockers_dependencies
                (entity_type, entity_id, dependency_type, description, waiting_on_member_id,
                 waiting_on_role, target_entity_type, target_entity_id, status, started_at,
                 expected_resolution_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)''',
                (entity_type, entity_id, dependency_type, description, waiting_on_member_id,
                 waiting_on_role, target_entity_type, target_entity_id, now, expected_resolution_date))
            # Automatically update operational_status to blocked if active
            conn.execute('''UPDATE work_item_meta SET operational_status='blocked', updated_at=?
                WHERE entity_type=? AND entity_id=? AND operational_status NOT IN ('completed','cancelled')''',
                (now, entity_type, entity_id))
            return cur.lastrowid

    def resolve_blocker(self, blocker_id: int, resolution_notes: str | None = None) -> bool:
        now = now_iso()
        with self.connect() as conn:
            b = conn.execute("SELECT * FROM blockers_dependencies WHERE id=?", (blocker_id,)).fetchone()
            if not b:
                return False
            conn.execute('''UPDATE blockers_dependencies
                SET status='resolved', resolved_at=?, resolution_notes=? WHERE id=?''',
                (now, resolution_notes, blocker_id))
            # Check if any other active blockers remain for this entity
            active = conn.execute(
                "SELECT COUNT(*) AS c FROM blockers_dependencies WHERE entity_type=? AND entity_id=? AND status='active'",
                (b['entity_type'], b['entity_id'])
            ).fetchone()['c']
            if active == 0:
                conn.execute('''UPDATE work_item_meta SET operational_status='active', updated_at=?
                    WHERE entity_type=? AND entity_id=? AND operational_status='blocked' ''',
                    (now, b['entity_type'], b['entity_id']))
            return True

    def get_blockers(self, entity_type: str | None = None, entity_id: int | None = None,
                     status: str | None = 'active') -> list[dict]:
        with self.connect() as conn:
            query = '''SELECT b.*, m.name AS waiting_on_name
                FROM blockers_dependencies b
                LEFT JOIN members m ON m.id=b.waiting_on_member_id WHERE 1=1'''
            params = []
            if entity_type and entity_id is not None:
                query += " AND b.entity_type=? AND b.entity_id=?"
                params.extend([entity_type, entity_id])
            if status:
                query += " AND b.status=?"
                params.append(status)
            query += " ORDER BY b.id DESC"
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    # Requirements
    def create_requirement(self, title: str, description: str | None = None,
                           requirement_text: str | None = None, user_story: str | None = None,
                           acceptance_criteria: str | None = None, client: str | None = None,
                           product: str | None = None, ticket: str | None = None,
                           priority: int = 1, due_date: str | None = None,
                           owner_member_id: int | None = None, workflow_template_id: int | None = None) -> int:
        now = now_iso()
        with self.connect() as conn:
            cur = conn.execute('''INSERT INTO requirements
                (title, description, requirement_text, user_story, acceptance_criteria, client, product,
                 ticket, priority, due_date, owner_member_id, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)''',
                (title, description, requirement_text, user_story, acceptance_criteria, client, product,
                 ticket, priority, due_date, owner_member_id, now, now))
            req_id = cur.lastrowid

            # Attach default or specified workflow template
            tmpl_id = workflow_template_id
            first_stage_id = None
            if not tmpl_id:
                default_tmpl = conn.execute(
                    "SELECT id FROM workflow_templates WHERE work_type='requirement' AND is_default=1"
                ).fetchone()
                if default_tmpl:
                    tmpl_id = default_tmpl['id']
            if tmpl_id:
                fstg = conn.execute(
                    "SELECT id FROM workflow_stages WHERE template_id=? ORDER BY stage_order ASC LIMIT 1",
                    (tmpl_id,)
                ).fetchone()
                if fstg:
                    first_stage_id = fstg['id']

            conn.execute('''INSERT INTO work_item_meta
                (entity_type, entity_id, workflow_template_id, current_stage_id, operational_status,
                 owner_member_id, due_date, created_at, updated_at)
                VALUES ('requirement', ?, ?, ?, 'active', ?, ?, ?, ?)''',
                (req_id, tmpl_id, first_stage_id, owner_member_id, due_date, now, now))

            if first_stage_id:
                conn.execute('''INSERT INTO stage_history
                    (entity_type, entity_id, stage_id, entered_at, note)
                    VALUES ('requirement', ?, ?, ?, 'Initial requirement creation')''',
                    (req_id, first_stage_id, now))

            self._fts_replace(conn, 'requirement', req_id, title,
                              f"Requirement #{req_id}: {title} {description or ''} {acceptance_criteria or ''}",
                              client=client, product=product, created_at=now)
            return req_id

    def get_requirement(self, req_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute('''SELECT r.*, m.name AS owner_name, meta.workflow_template_id,
                meta.current_stage_id, meta.operational_status, stg.name AS current_stage_name,
                stg.is_waiting AS current_stage_is_waiting, tmpl.name AS workflow_template_name
                FROM requirements r
                LEFT JOIN members m ON m.id=r.owner_member_id
                LEFT JOIN work_item_meta meta ON meta.entity_type='requirement' AND meta.entity_id=r.id
                LEFT JOIN workflow_stages stg ON stg.id=meta.current_stage_id
                LEFT JOIN workflow_templates tmpl ON tmpl.id=meta.workflow_template_id
                WHERE r.id=?''', (req_id,)).fetchone()
            if not row:
                return None
            req = dict(row)
            # Test conditions
            tc_rows = conn.execute("SELECT * FROM test_conditions WHERE requirement_id=? ORDER BY id ASC", (req_id,)).fetchall()
            req['test_conditions'] = [dict(tc) for tc in tc_rows]
            # Test cases
            tcase_rows = conn.execute("SELECT * FROM test_cases WHERE requirement_id=? ORDER BY id ASC", (req_id,)).fetchall()
            req['test_cases'] = [dict(tc) for tc in tcase_rows]
            # Active blockers
            req['blockers'] = self.get_blockers('requirement', req_id, status='active')
            return req

    def list_requirements(self, status: str | None = None) -> list[dict]:
        with self.connect() as conn:
            query = '''SELECT r.*, m.name AS owner_name, meta.workflow_template_id,
                meta.current_stage_id, meta.operational_status, stg.name AS current_stage_name,
                tmpl.name AS workflow_template_name
                FROM requirements r
                LEFT JOIN members m ON m.id=r.owner_member_id
                LEFT JOIN work_item_meta meta ON meta.entity_type='requirement' AND meta.entity_id=r.id
                LEFT JOIN workflow_stages stg ON stg.id=meta.current_stage_id
                LEFT JOIN workflow_templates tmpl ON tmpl.id=meta.workflow_template_id'''
            params = []
            if status:
                query += " WHERE r.status=?"
                params.append(status)
            query += " ORDER BY r.priority DESC, r.id DESC"
            rows = conn.execute(query, params).fetchall()
            res = []
            for r in rows:
                item = dict(r)
                item['conditions_count'] = conn.execute(
                    "SELECT COUNT(*) AS c FROM test_conditions WHERE requirement_id=?", (item['id'],)
                ).fetchone()['c']
                item['test_cases_count'] = conn.execute(
                    "SELECT COUNT(*) AS c FROM test_cases WHERE requirement_id=?", (item['id'],)
                ).fetchone()['c']
                res.append(item)
            return res

    # Test Conditions & Cases
    def add_test_condition(self, requirement_id: int, title: str, description: str | None = None,
                           category: str = 'functional', risk_level: str = 'medium',
                           status: str = 'draft', notes: str | None = None) -> int:
        now = now_iso()
        with self.connect() as conn:
            cur = conn.execute('''INSERT INTO test_conditions
                (requirement_id, title, description, category, risk_level, status, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (requirement_id, title, description, category, risk_level, status, notes, now))
            return cur.lastrowid

    def get_test_condition(self, condition_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute('''SELECT tc.*, r.title AS requirement_title
                FROM test_conditions tc
                LEFT JOIN requirements r ON r.id=tc.requirement_id
                WHERE tc.id=?''', (condition_id,)).fetchone()
            return dict(row) if row else None

    def list_test_conditions(self, requirement_id: int | None = None, status: str | None = None) -> list[dict]:
        with self.connect() as conn:
            query = '''SELECT tc.*, r.title AS requirement_title,
                (SELECT COUNT(*) FROM test_cases WHERE test_condition_id=tc.id) AS test_cases_count
                FROM test_conditions tc
                LEFT JOIN requirements r ON r.id=tc.requirement_id'''
            params = []
            clauses = []
            if requirement_id:
                clauses.append("tc.requirement_id=?")
                params.append(requirement_id)
            if status:
                clauses.append("tc.status=?")
                params.append(status)
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY tc.id ASC"
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def update_test_condition(self, condition_id: int, **kwargs) -> bool:
        allowed = ('title', 'description', 'category', 'risk_level', 'status', 'notes')
        updates = []
        params = []
        for k, v in kwargs.items():
            if k in allowed:
                updates.append(f"{k}=?")
                params.append(v)
        if not updates:
            return False
        params.append(condition_id)
        with self.connect() as conn:
            res = conn.execute(f"UPDATE test_conditions SET {', '.join(updates)} WHERE id=?", params)
            return res.rowcount > 0

    def update_test_condition_status(self, condition_id: int, status: str) -> bool:
        return self.update_test_condition(condition_id, status=status)

    def delete_test_condition(self, condition_id: int) -> bool:
        with self.connect() as conn:
            conn.execute("UPDATE test_cases SET test_condition_id=NULL WHERE test_condition_id=?", (condition_id,))
            res = conn.execute("DELETE FROM test_conditions WHERE id=?", (condition_id,))
            return res.rowcount > 0

    def add_test_case(self, title: str, requirement_id: int | None = None,
                      test_condition_id: int | None = None, objective: str | None = None,
                      preconditions: str | None = None, steps: str | None = None,
                      test_data: str | None = None, expected_result: str | None = None,
                      priority: int = 2, automation_status: str = 'manual') -> int:
        now = now_iso()
        with self.connect() as conn:
            cur = conn.execute('''INSERT INTO test_cases
                (requirement_id, test_condition_id, title, objective, preconditions, steps,
                 test_data, expected_result, priority, automation_status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (requirement_id, test_condition_id, title, objective, preconditions, steps,
                 test_data, expected_result, priority, automation_status, now))
            return cur.lastrowid

    def get_test_case(self, test_case_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute('''SELECT tc.*, r.title AS requirement_title, cond.title AS condition_title
                FROM test_cases tc
                LEFT JOIN requirements r ON r.id=tc.requirement_id
                LEFT JOIN test_conditions cond ON cond.id=tc.test_condition_id
                WHERE tc.id=?''', (test_case_id,)).fetchone()
            if not row:
                return None
            res = dict(row)
            latest = conn.execute(
                "SELECT * FROM test_executions WHERE test_case_id=? ORDER BY id DESC LIMIT 1",
                (test_case_id,)
            ).fetchone()
            if latest:
                res['latest_result'] = latest['result']
                res['latest_build'] = latest['build']
                res['latest_executed_at'] = latest['executed_at']
                res['latest_actual_result'] = latest['actual_result']
            else:
                res['latest_result'] = 'not_run'
            return res

    def update_test_case(self, test_case_id: int, **kwargs) -> bool:
        allowed = ('title', 'objective', 'preconditions', 'steps', 'test_data',
                   'expected_result', 'priority', 'automation_status', 'requirement_id', 'test_condition_id')
        updates = []
        params = []
        for k, v in kwargs.items():
            if k in allowed:
                updates.append(f"{k}=?")
                params.append(v)
        if not updates:
            return False
        params.append(test_case_id)
        with self.connect() as conn:
            res = conn.execute(f"UPDATE test_cases SET {', '.join(updates)} WHERE id=?", params)
            return res.rowcount > 0

    def delete_test_case(self, test_case_id: int) -> bool:
        with self.connect() as conn:
            conn.execute("DELETE FROM test_executions WHERE test_case_id=?", (test_case_id,))
            conn.execute("UPDATE defects SET test_case_id=NULL WHERE test_case_id=?", (test_case_id,))
            res = conn.execute("DELETE FROM test_cases WHERE id=?", (test_case_id,))
            return res.rowcount > 0

    def list_test_cases(self, requirement_id: int | None = None) -> list[dict]:
        with self.connect() as conn:
            query = '''SELECT tc.*, r.title AS requirement_title, cond.title AS condition_title
                FROM test_cases tc
                LEFT JOIN requirements r ON r.id=tc.requirement_id
                LEFT JOIN test_conditions cond ON cond.id=tc.test_condition_id'''
            params = []
            if requirement_id:
                query += " WHERE tc.requirement_id=?"
                params.append(requirement_id)
            query += " ORDER BY tc.id DESC"
            rows = conn.execute(query, params).fetchall()
            cases = []
            for r in rows:
                item = dict(r)
                latest = conn.execute(
                    "SELECT result, build, executed_at, actual_result FROM test_executions WHERE test_case_id=? ORDER BY id DESC LIMIT 1",
                    (item['id'],)
                ).fetchone()
                if latest:
                    item['latest_result'] = latest['result']
                    item['latest_build'] = latest['build']
                    item['latest_executed_at'] = latest['executed_at']
                    item['latest_actual_result'] = latest['actual_result']
                    item['last_execution_result'] = latest['result']
                else:
                    item['latest_result'] = 'not_run'
                    item['last_execution_result'] = 'not_run'
                cases.append(item)
            return cases

    def record_test_execution(self, test_case_id: int, result: str, build: str | None = None,
                              environment: str | None = None, actual_result: str | None = None,
                              defect_id: int | None = None, executed_by_member_id: int | None = None,
                              notes: str | None = None, session_id: int | None = None) -> int:
        now = now_iso()
        with self.connect() as conn:
            cur = conn.execute('''INSERT INTO test_executions
                (test_case_id, session_id, build, environment, result, actual_result, defect_id,
                 executed_by_member_id, notes, executed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (test_case_id, session_id, build, environment, result, actual_result, defect_id,
                 executed_by_member_id, notes, now))
            return cur.lastrowid

    def list_test_executions(self, test_case_id: int | None = None, limit: int = 50) -> list[dict]:
        with self.connect() as conn:
            query = '''SELECT te.*, tc.title AS test_case_title, m.name AS executed_by_name,
                d.title AS defect_title
                FROM test_executions te
                LEFT JOIN test_cases tc ON tc.id = te.test_case_id
                LEFT JOIN members m ON m.id = te.executed_by_member_id
                LEFT JOIN defects d ON d.id = te.defect_id'''
            params = []
            if test_case_id:
                query += " WHERE te.test_case_id=?"
                params.append(test_case_id)
            query += " ORDER BY te.id DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    # ==========================================================================
    # Defects Management
    # ==========================================================================

    def create_defect(self, title: str, description: str | None = None,
                      severity: str = 'major', priority: int = 2, status: str = 'new',
                      requirement_id: int | None = None, test_condition_id: int | None = None,
                      test_case_id: int | None = None, execution_id: int | None = None,
                      assigned_member_id: int | None = None, client: str | None = None,
                      product: str | None = None, ticket: str | None = None,
                      steps_to_reproduce: str | None = None, expected_result: str | None = None,
                      actual_result: str | None = None, build_found: str | None = None,
                      environment: str | None = None) -> int:
        now = now_iso()
        with self.connect() as conn:
            cur = conn.execute('''INSERT INTO defects
                (title, description, severity, priority, status, requirement_id,
                 test_condition_id, test_case_id, execution_id, assigned_member_id,
                 client, product, ticket, steps_to_reproduce, expected_result,
                 actual_result, build_found, environment, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (title, description, severity, priority, status, requirement_id,
                 test_condition_id, test_case_id, execution_id, assigned_member_id,
                 client, product, ticket, steps_to_reproduce, expected_result,
                 actual_result, build_found, environment, now, now))
            defect_id = cur.lastrowid
            return defect_id

    def get_defect(self, defect_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute('''SELECT d.*, r.title AS requirement_title,
                tc.title AS test_case_title, cond.title AS condition_title,
                m.name AS assigned_member_name
                FROM defects d
                LEFT JOIN requirements r ON r.id = d.requirement_id
                LEFT JOIN test_cases tc ON tc.id = d.test_case_id
                LEFT JOIN test_conditions cond ON cond.id = d.test_condition_id
                LEFT JOIN members m ON m.id = d.assigned_member_id
                WHERE d.id=?''', (defect_id,)).fetchone()
            if not row:
                return None
            res = dict(row)
            res['display_id'] = f"DEF-{defect_id}"
            return res

    def list_defects(self, requirement_id: int | None = None, status: str | None = None,
                     assigned_member_id: int | None = None, limit: int = 100) -> list[dict]:
        with self.connect() as conn:
            query = '''SELECT d.*, r.title AS requirement_title,
                tc.title AS test_case_title, m.name AS assigned_member_name
                FROM defects d
                LEFT JOIN requirements r ON r.id = d.requirement_id
                LEFT JOIN test_cases tc ON tc.id = d.test_case_id
                LEFT JOIN members m ON m.id = d.assigned_member_id'''
            params = []
            clauses = []
            if requirement_id:
                clauses.append("d.requirement_id=?")
                params.append(requirement_id)
            if status:
                if status == 'open':
                    clauses.append("d.status NOT IN ('verified', 'closed')")
                elif status == 'retesting':
                    clauses.append("d.status IN ('fix_ready', 'retest_required', 'retesting')")
                else:
                    clauses.append("d.status=?")
                    params.append(status)
            if assigned_member_id:
                clauses.append("d.assigned_member_id=?")
                params.append(assigned_member_id)
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY d.priority ASC, d.id DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, params).fetchall()
            items = []
            for r in rows:
                d = dict(r)
                d['display_id'] = f"DEF-{d['id']}"
                items.append(d)
            return items

    def update_defect(self, defect_id: int, **kwargs) -> bool:
        allowed = ('title', 'description', 'severity', 'priority', 'status',
                   'requirement_id', 'test_condition_id', 'test_case_id', 'execution_id',
                   'assigned_member_id', 'client', 'product', 'ticket', 'steps_to_reproduce',
                   'expected_result', 'actual_result', 'build_found', 'build_fixed',
                   'environment', 'retest_notes', 'resolution', 'closed_at')
        updates = []
        params = []
        now = now_iso()
        for k, v in kwargs.items():
            if k in allowed:
                updates.append(f"{k}=?")
                params.append(v)
        if not updates:
            return False
        updates.append("updated_at=?")
        params.append(now)
        params.append(defect_id)
        with self.connect() as conn:
            res = conn.execute(f"UPDATE defects SET {', '.join(updates)} WHERE id=?", params)
            return res.rowcount > 0

    def retest_defect(self, defect_id: int, result: str, build_fixed: str | None = None,
                      retest_notes: str | None = None, tester_member_id: int | None = None) -> bool:
        now = now_iso()
        status = 'verified' if result.lower() in ('pass', 'verified') else 'reopened'
        with self.connect() as conn:
            conn.execute('''UPDATE defects
                SET status=?, build_fixed=COALESCE(?, build_fixed),
                    retest_notes=?, updated_at=?,
                    closed_at=CASE WHEN ?='verified' THEN ? ELSE closed_at END
                WHERE id=?''',
                (status, build_fixed, retest_notes, now, status, now, defect_id))
            return True

    def reopen_defect(self, defect_id: int, reason: str | None = None) -> bool:
        now = now_iso()
        with self.connect() as conn:
            conn.execute('''UPDATE defects
                SET status='reopened', closed_at=NULL, updated_at=?
                WHERE id=?''', (now, defect_id))
            return True

    # ==========================================================================
    # Artifacts Management
    # ==========================================================================

    def add_artifact(self, entity_type: str, entity_id: int, artifact_type: str,
                     name: str, path: str | None = None, url: str | None = None,
                     details: dict | None = None) -> int:
        now = now_iso()
        details_str = json.dumps(details or {})
        with self.connect() as conn:
            cur = conn.execute('''INSERT INTO artifacts
                (entity_type, entity_id, artifact_type, name, path, url, details_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (entity_type, entity_id, artifact_type, name, path, url, details_str, now))
            return cur.lastrowid

    def list_artifacts(self, entity_type: str | None = None, entity_id: int | None = None) -> list[dict]:
        with self.connect() as conn:
            query = "SELECT * FROM artifacts"
            params = []
            clauses = []
            if entity_type:
                clauses.append("entity_type=?")
                params.append(entity_type)
            if entity_id:
                clauses.append("entity_id=?")
                params.append(entity_id)
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY id DESC"
            rows = conn.execute(query, params).fetchall()
            res = []
            for r in rows:
                item = dict(r)
                try:
                    item['details'] = json.loads(item.get('details_json') or '{}')
                except Exception:
                    item['details'] = {}
                res.append(item)
            return res

    def delete_artifact(self, artifact_id: int) -> bool:
        with self.connect() as conn:
            res = conn.execute("DELETE FROM artifacts WHERE id=?", (artifact_id,))
            return res.rowcount > 0

    # ==========================================================================
    # Human-Readable Timeline & Testing Posture & Completion Reports
    # ==========================================================================

    def get_timeline(self, entity_type: str, entity_id: int) -> list[dict]:
        """
        Builds a comprehensive, unified, human-readable timeline for any work item or requirement.
        """
        events = []
        with self.connect() as conn:
            # 1. Base Item Creation
            if entity_type == 'requirement':
                req = conn.execute("SELECT * FROM requirements WHERE id=?", (entity_id,)).fetchone()
                if req:
                    events.append({
                        'timestamp': req['created_at'],
                        'event_type': 'created',
                        'title': 'Requirement Created',
                        'actor': 'User / System',
                        'description': f"Requirement REQ-{entity_id} '{req['title']}' was logged into intake.",
                        'badge_class': 'tag-info'
                    })
            elif entity_type == 'defect':
                def_row = conn.execute("SELECT * FROM defects WHERE id=?", (entity_id,)).fetchone()
                if def_row:
                    events.append({
                        'timestamp': def_row['created_at'],
                        'event_type': 'created',
                        'title': 'Defect Logged',
                        'actor': 'QA / Reporter',
                        'description': f"Defect DEF-{entity_id} '{def_row['title']}' logged with severity {def_row['severity']}.",
                        'badge_class': 'tag-err'
                    })

            # 2. Stage Transitions
            stg_rows = conn.execute('''SELECT sh.*, s.name AS stage_name,
                m.name AS actor_name
                FROM stage_history sh
                LEFT JOIN workflow_stages s ON s.id=sh.stage_id
                LEFT JOIN members m ON m.id=sh.actor_member_id
                WHERE sh.entity_type=? AND sh.entity_id=?
                ORDER BY sh.entered_at ASC''', (entity_type, entity_id)).fetchall()
            for s in stg_rows:
                s_dict = dict(s)
                stg_name = s_dict.get('stage_name') or 'Stage Progress'
                desc = f"Entered stage '{stg_name}'"
                if s_dict.get('note'):
                    desc += f" — {s_dict['note']}"
                events.append({
                    'timestamp': s_dict.get('entered_at'),
                    'event_type': 'stage_transition',
                    'title': f"Stage: {stg_name}",
                    'actor': s_dict.get('actor_name') or 'Team Member',
                    'description': desc,
                    'badge_class': 'tag-ok'
                })

            # 3. Blockers
            b_rows = conn.execute('''SELECT b.*, m.name AS waiting_name
                FROM blockers_dependencies b
                LEFT JOIN members m ON m.id=b.waiting_on_member_id
                WHERE b.entity_type=? AND b.entity_id=?
                ORDER BY b.started_at ASC''', (entity_type, entity_id)).fetchall()
            for b in b_rows:
                b_dict = dict(b)
                events.append({
                    'timestamp': b_dict.get('started_at'),
                    'event_type': 'blocker_logged',
                    'title': f"Blocker: {(b_dict.get('dependency_type') or 'dependency').replace('_', ' ').title()}",
                    'actor': 'Operations',
                    'description': f"{b_dict.get('description') or ''}" + (f" (Waiting on {b_dict['waiting_name']})" if b_dict.get('waiting_name') else ""),
                    'badge_class': 'tag-err'
                })
                if b_dict.get('resolved_at'):
                    events.append({
                        'timestamp': b_dict['resolved_at'],
                        'event_type': 'blocker_resolved',
                        'title': 'Blocker Resolved',
                        'actor': 'Operations',
                        'description': f"Resolved: {b_dict.get('description') or ''}" + (f" — {b_dict['resolution_notes']}" if b_dict.get('resolution_notes') else ""),
                        'badge_class': 'tag-ok'
                    })

            # 4. Test Executions (for requirements)
            if entity_type == 'requirement':
                tcs = conn.execute("SELECT id, title FROM test_cases WHERE requirement_id=?", (entity_id,)).fetchall()
                tc_map = {tc['id']: tc['title'] for tc in tcs}
                if tc_map:
                    placeholders = ','.join('?' for _ in tc_map)
                    execs = conn.execute(f'''SELECT te.*, m.name AS tester_name
                        FROM test_executions te
                        LEFT JOIN members m ON m.id=te.executed_by_member_id
                        WHERE te.test_case_id IN ({placeholders})
                        ORDER BY te.executed_at ASC''', list(tc_map.keys())).fetchall()
                    for ex in execs:
                        ex_dict = dict(ex)
                        tc_title = tc_map.get(ex_dict['test_case_id'], 'Test Case')
                        res = (ex_dict.get('result') or 'pass').upper()
                        b_cls = 'tag-ok' if res == 'PASS' else ('tag-err' if res == 'FAIL' else 'tag')
                        events.append({
                            'timestamp': ex_dict.get('executed_at'),
                            'event_type': 'test_execution',
                            'title': f"Execution: TC-{ex_dict['test_case_id']} ({res})",
                            'actor': ex_dict.get('tester_name') or 'QA Tester',
                            'description': f"{tc_title} evaluated as {res}" + (f" on build {ex_dict['build']}" if ex_dict.get('build') else "") + (f" — {ex_dict['actual_result']}" if ex_dict.get('actual_result') else ""),
                            'badge_class': b_cls
                        })

            # 5. Defects Logged (for requirements)
            if entity_type == 'requirement':
                defs = conn.execute("SELECT * FROM defects WHERE requirement_id=? ORDER BY created_at ASC", (entity_id,)).fetchall()
                for d in defs:
                    d_dict = dict(d)
                    events.append({
                        'timestamp': d_dict.get('created_at'),
                        'event_type': 'defect_created',
                        'title': f"Defect DEF-{d_dict['id']}: {d_dict['title']}",
                        'actor': 'QA Reporter',
                        'description': f"Defect logged ({d_dict.get('severity', 'major')}) — {d_dict.get('description') or d_dict.get('actual_result') or ''}",
                        'badge_class': 'tag-err'
                    })
                    if d_dict.get('status') == 'verified':
                        events.append({
                            'timestamp': d_dict.get('updated_at'),
                            'event_type': 'defect_verified',
                            'title': f"Defect DEF-{d_dict['id']} Verified",
                            'actor': 'QA Tester',
                            'description': f"Fix verified on target build: {d_dict.get('retest_notes') or 'Retest passed.'}",
                            'badge_class': 'tag-ok'
                        })

            # 6. Artifacts Linked
            arts = conn.execute("SELECT * FROM artifacts WHERE entity_type=? AND entity_id=? ORDER BY created_at ASC",
                                (entity_type, entity_id)).fetchall()
            for a in arts:
                a_dict = dict(a)
                events.append({
                    'timestamp': a_dict.get('created_at'),
                    'event_type': 'artifact_linked',
                    'title': f"Artifact: {a_dict['name']}",
                    'actor': 'System / User',
                    'description': f"Linked {(a_dict.get('artifact_type') or 'doc').replace('_', ' ').title()}: {a_dict.get('url') or a_dict.get('path') or a_dict['name']}",
                    'badge_class': 'tag-info'
                })

        events.sort(key=lambda x: x.get('timestamp') or '', reverse=True)
        return events

    def get_testing_posture(self, requirement_id: int) -> dict:
        """
        Calculates actionable testing posture for a requirement.
        """
        with self.connect() as conn:
            req = conn.execute("SELECT * FROM requirements WHERE id=?", (requirement_id,)).fetchone()
            if not req:
                return {}
            
            tcs = conn.execute("SELECT id FROM test_cases WHERE requirement_id=?", (requirement_id,)).fetchall()
            total_tc = len(tcs)
            passed = 0
            failed = 0
            blocked = 0
            not_run = 0

            for tc in tcs:
                latest = conn.execute(
                    "SELECT result FROM test_executions WHERE test_case_id=? ORDER BY id DESC LIMIT 1",
                    (tc['id'],)
                ).fetchone()
                if not latest:
                    not_run += 1
                else:
                    res = latest['result'].lower()
                    if res == 'pass':
                        passed += 1
                    elif res == 'fail':
                        failed += 1
                    elif res == 'blocked':
                        blocked += 1
                    else:
                        not_run += 1

            # Defects count
            open_defects = conn.execute(
                "SELECT COUNT(*) AS c FROM defects WHERE requirement_id=? AND status NOT IN ('verified', 'closed')",
                (requirement_id,)
            ).fetchone()['c']
            critical_defects = conn.execute(
                "SELECT COUNT(*) AS c FROM defects WHERE requirement_id=? AND status NOT IN ('verified', 'closed') AND severity IN ('critical', 'blocker')",
                (requirement_id,)
            ).fetchone()['c']

            # Active Blockers
            active_blockers = conn.execute(
                "SELECT COUNT(*) AS c FROM blockers_dependencies WHERE entity_type='requirement' AND entity_id=? AND status='active'",
                (requirement_id,)
            ).fetchone()['c']

            # Test conditions
            total_conditions = conn.execute(
                "SELECT COUNT(*) AS c FROM test_conditions WHERE requirement_id=?", (requirement_id,)
            ).fetchone()['c']
            approved_conditions = conn.execute(
                "SELECT COUNT(*) AS c FROM test_conditions WHERE requirement_id=? AND status='approved'", (requirement_id,)
            ).fetchone()['c']

            verified_pct = int((passed / total_tc) * 100) if total_tc > 0 else 0

            # Overall Status Assessment
            if active_blockers > 0 or critical_defects > 0:
                readiness = 'Blocked'
                readiness_badge = 'tag-err'
            elif failed > 0 or open_defects > 0:
                readiness = 'Defects Pending'
                readiness_badge = 'tag-err'
            elif total_tc == 0 or not_run > 0 or total_conditions == 0:
                readiness = 'In Progress'
                readiness_badge = 'tag'
            elif passed == total_tc and total_tc > 0 and open_defects == 0 and active_blockers == 0:
                readiness = 'Ready for Release'
                readiness_badge = 'tag-ok'
            else:
                readiness = 'In Progress'
                readiness_badge = 'tag'

            return {
                'requirement_id': requirement_id,
                'total_test_cases': total_tc,
                'passed': passed,
                'failed': failed,
                'blocked': blocked,
                'not_run': not_run,
                'verified_percentage': verified_pct,
                'open_defects': open_defects,
                'critical_defects': critical_defects,
                'active_blockers': active_blockers,
                'total_conditions': total_conditions,
                'approved_conditions': approved_conditions,
                'readiness': readiness,
                'readiness_badge': readiness_badge
            }

    def generate_completion_report(self, requirement_id: int) -> dict:
        """
        Generates a comprehensive testing completion report for release sign-off.
        """
        with self.connect() as conn:
            req = self.get_requirement(requirement_id)
            if not req:
                return {}
            posture = self.get_testing_posture(requirement_id)
            conditions = self.list_test_conditions(requirement_id=requirement_id)
            cases = self.list_test_cases(requirement_id=requirement_id)
            defects = self.list_defects(requirement_id=requirement_id)
            blockers = self.get_blockers('requirement', requirement_id, status=None)
            artifacts = self.list_artifacts('requirement', requirement_id)
            timeline = self.get_timeline('requirement', requirement_id)

            report = {
                'requirement_id': requirement_id,
                'requirement': req,
                'title': req.get('title'),
                'client': req.get('client'),
                'product': req.get('product'),
                'ticket': req.get('ticket'),
                'owner_name': req.get('owner_name'),
                'current_stage_name': req.get('current_stage_name'),
                'operational_status': req.get('operational_status'),
                'user_story': req.get('user_story'),
                'acceptance_criteria': req.get('acceptance_criteria'),
                'generated_at': now_iso(),
                'posture': posture,
                'conditions': conditions,
                'test_cases': cases,
                'defects': defects,
                'blockers': blockers,
                'artifacts': artifacts,
                'timeline': timeline[:10], # recent 10 events
                'is_ready_for_release': posture.get('readiness') == 'Ready for Release'
            }
            return report

    # Unified Work Items Aggregator
    def get_all_work_items(self) -> list[dict]:
        """
        Unified aggregator combining Tasks, Cases, and Requirements into cohesive Work Item records.
        """
        with self.connect() as conn:
            items = []

            # 1. Requirements
            reqs = conn.execute('''SELECT r.id, r.title, r.client, r.product, r.ticket, r.priority,
                r.due_date, r.created_at, r.updated_at, m.name AS owner_name, m.id AS owner_member_id,
                meta.workflow_template_id, meta.current_stage_id, meta.operational_status,
                meta.next_action, stg.name AS stage_name, tmpl.name AS template_name
                FROM requirements r
                LEFT JOIN work_item_meta meta ON meta.entity_type='requirement' AND meta.entity_id=r.id
                LEFT JOIN workflow_stages stg ON stg.id=meta.current_stage_id
                LEFT JOIN workflow_templates tmpl ON tmpl.id=meta.workflow_template_id
                LEFT JOIN members m ON m.id=meta.owner_member_id
                ORDER BY r.id DESC''').fetchall()
            for r in reqs:
                items.append({
                    'entity_type': 'requirement',
                    'entity_id': r['id'],
                    'display_id': f"REQ-{r['id']}",
                    'title': r['title'],
                    'operational_status': r['operational_status'] or 'active',
                    'workflow_template_id': r['workflow_template_id'],
                    'workflow_template_name': r['template_name'],
                    'current_stage_id': r['current_stage_id'],
                    'current_stage_name': r['stage_name'],
                    'priority': r['priority'],
                    'client': r['client'],
                    'product': r['product'],
                    'ticket': r['ticket'],
                    'owner_member_id': r['owner_member_id'],
                    'owner_name': r['owner_name'],
                    'due_date': r['due_date'],
                    'next_action': r['next_action'],
                    'created_at': r['created_at'],
                    'updated_at': r['updated_at'],
                })

            # 2. Cases
            cases = conn.execute('''SELECT c.id, c.title, cl.name AS client, c.product, c.platform, c.ticket,
                c.priority, c.status, c.participation, c.next_action, c.waiting_on, c.created_at, c.updated_at,
                meta.workflow_template_id, meta.current_stage_id, meta.operational_status,
                stg.name AS stage_name, tmpl.name AS template_name, m.name AS owner_name, m.id AS owner_member_id
                FROM work_cases c
                LEFT JOIN clients cl ON cl.id=c.client_id
                LEFT JOIN work_item_meta meta ON meta.entity_type='case' AND meta.entity_id=c.id
                LEFT JOIN workflow_stages stg ON stg.id=meta.current_stage_id
                LEFT JOIN workflow_templates tmpl ON tmpl.id=meta.workflow_template_id
                LEFT JOIN members m ON m.id=meta.owner_member_id
                WHERE c.review_state='approved'
                ORDER BY c.id DESC''').fetchall()
            for c in cases:
                op_status = c['operational_status']
                if not op_status:
                    if c['status'] in ('resolved', 'closed'):
                        op_status = 'completed'
                    elif c['status'] in ('waiting_client', 'waiting_internal'):
                        op_status = 'waiting'
                    else:
                        op_status = 'active'
                items.append({
                    'entity_type': 'case',
                    'entity_id': c['id'],
                    'display_id': f"CASE-{c['id']}",
                    'title': c['title'],
                    'operational_status': op_status,
                    'workflow_template_id': c['workflow_template_id'],
                    'workflow_template_name': c['template_name'],
                    'current_stage_id': c['current_stage_id'],
                    'current_stage_name': c['stage_name'] or c['status'],
                    'priority': c['priority'],
                    'client': c['client'],
                    'product': c['product'],
                    'ticket': c['ticket'],
                    'owner_member_id': c['owner_member_id'],
                    'owner_name': c['owner_name'],
                    'waiting_on': c['waiting_on'],
                    'due_date': None,
                    'next_action': c['next_action'],
                    'created_at': c['created_at'],
                    'updated_at': c['updated_at'],
                })

            # 3. Tasks
            tasks = conn.execute('''SELECT t.id, t.title, t.status, t.priority, t.due_date, t.project,
                t.client, t.ticket, t.next_action, t.created_at, t.completed_at, t.blocked_reason,
                meta.workflow_template_id, meta.current_stage_id, meta.operational_status,
                stg.name AS stage_name, tmpl.name AS template_name, m.name AS owner_name, m.id AS owner_member_id
                FROM tasks t
                LEFT JOIN work_item_meta meta ON meta.entity_type='task' AND meta.entity_id=t.id
                LEFT JOIN workflow_stages stg ON stg.id=meta.current_stage_id
                LEFT JOIN workflow_templates tmpl ON tmpl.id=meta.workflow_template_id
                LEFT JOIN members m ON m.id=meta.owner_member_id
                ORDER BY t.id DESC''').fetchall()
            for t in tasks:
                op_status = t['operational_status']
                if not op_status:
                    if t['status'] == 'completed':
                        op_status = 'completed'
                    elif t['status'] == 'blocked':
                        op_status = 'blocked'
                    elif t['status'] == 'cancelled':
                        op_status = 'cancelled'
                    elif t['status'] == 'in_progress':
                        op_status = 'active'
                    else:
                        op_status = 'pending'
                items.append({
                    'entity_type': 'task',
                    'entity_id': t['id'],
                    'display_id': f"TASK-{t['id']}",
                    'title': t['title'],
                    'operational_status': op_status,
                    'workflow_template_id': t['workflow_template_id'],
                    'workflow_template_name': t['template_name'],
                    'current_stage_id': t['current_stage_id'],
                    'current_stage_name': t['stage_name'] or t['status'],
                    'priority': t['priority'],
                    'client': t['client'],
                    'product': t['project'],
                    'ticket': t['ticket'],
                    'owner_member_id': t['owner_member_id'],
                    'owner_name': t['owner_name'],
                    'due_date': t['due_date'],
                    'next_action': t['next_action'],
                    'created_at': t['created_at'],
                    'updated_at': t['completed_at'] or t['created_at'],
                })

            # Check active blockers count for each item
            for it in items:
                b_count = conn.execute(
                    "SELECT COUNT(*) AS c FROM blockers_dependencies WHERE entity_type=? AND entity_id=? AND status='active'",
                    (it['entity_type'], it['entity_id'])
                ).fetchone()['c']
                it['active_blockers_count'] = b_count
                if b_count > 0:
                    it['is_blocked'] = True

            return items

    def get_work_item_detail(self, entity_type: str, entity_id: int) -> dict | None:
        """Retrieves full, enriched work item details including base data, workflow pipeline, stage history, blockers, and related items."""
        with self.connect() as conn:
            base_data = None
            if entity_type == 'requirement':
                row = conn.execute('''SELECT r.*, m.name AS owner_name, m.id AS owner_member_id
                    FROM requirements r
                    LEFT JOIN members m ON m.id=r.owner_member_id
                    WHERE r.id=?''', (entity_id,)).fetchone()
                if row:
                    base_data = dict(row)
                    base_data['display_id'] = f"REQ-{entity_id}"
                    base_data['type_name'] = 'Requirement'
            elif entity_type == 'case':
                row = conn.execute('''SELECT c.*, cl.name AS client_name, m.name AS owner_name, m.id AS owner_member_id
                    FROM work_cases c
                    LEFT JOIN clients cl ON cl.id=c.client_id
                    LEFT JOIN members m ON m.id=c.owner_user_id
                    WHERE c.id=?''', (entity_id,)).fetchone()
                if row:
                    base_data = dict(row)
                    base_data['display_id'] = f"CASE-{entity_id}"
                    base_data['type_name'] = 'Support Case'
                    base_data['client'] = base_data.get('client_name') or base_data.get('client')
            elif entity_type == 'task':
                row = conn.execute('''SELECT t.*, m.name AS owner_name, m.id AS owner_member_id
                    FROM tasks t
                    LEFT JOIN members m ON m.id=t.assigned_member_id
                    WHERE t.id=?''', (entity_id,)).fetchone()
                if row:
                    base_data = dict(row)
                    base_data['display_id'] = f"TASK-{entity_id}"
                    base_data['type_name'] = 'Task'
                    base_data['product'] = base_data.get('project')

            if not base_data:
                return None

            # Get metadata
            meta = self.get_work_item_meta(entity_type, entity_id)
            base_data['meta'] = meta or {}
            base_data['operational_status'] = (meta and meta.get('operational_status')) or base_data.get('status') or 'active'
            base_data['workflow_template_id'] = meta and meta.get('workflow_template_id')
            base_data['current_stage_id'] = meta and meta.get('current_stage_id')

            # Workflow template and stages
            template = None
            stages = []
            if base_data['workflow_template_id']:
                template = self.get_workflow_template(base_data['workflow_template_id'])
                if template:
                    stages = template.get('stages', [])
            elif entity_type == 'requirement':
                # Try default template for requirement
                def_tmpl = conn.execute("SELECT id FROM workflow_templates WHERE work_type='requirement' AND is_default=1 LIMIT 1").fetchone()
                if not def_tmpl:
                    def_tmpl = conn.execute("SELECT id FROM workflow_templates WHERE work_type='requirement' ORDER BY id ASC LIMIT 1").fetchone()
                if def_tmpl:
                    template = self.get_workflow_template(def_tmpl['id'])
                    if template:
                        stages = template.get('stages', [])

            base_data['workflow_template'] = template
            base_data['stages'] = stages

            # Determine current stage object
            current_stage_obj = None
            if base_data['current_stage_id']:
                for s in stages:
                    if s['id'] == base_data['current_stage_id']:
                        current_stage_obj = s
                        break
            if not current_stage_obj and stages:
                current_stage_obj = stages[0]
            base_data['current_stage'] = current_stage_obj

            # Stage history
            base_data['stage_history'] = self.get_stage_history(entity_type, entity_id)

            # Blockers
            base_data['blockers'] = self.get_blockers(entity_type=entity_type, entity_id=entity_id, status=None)
            base_data['active_blockers'] = [b for b in base_data['blockers'] if b.get('status') == 'active']

            # Related items
            related_test_cases = []
            test_conditions = []
            defects = []
            posture = {}
            if entity_type == 'requirement':
                related_test_cases = self.list_test_cases(requirement_id=entity_id)
                test_conditions = self.list_test_conditions(requirement_id=entity_id)
                defects = self.list_defects(requirement_id=entity_id)
                posture = self.get_testing_posture(requirement_id=entity_id)

            base_data['related_test_cases'] = related_test_cases
            base_data['test_conditions'] = test_conditions
            base_data['defects'] = defects
            base_data['posture'] = posture
            base_data['artifacts'] = self.list_artifacts(entity_type=entity_type, entity_id=entity_id)
            base_data['timeline'] = self.get_timeline(entity_type=entity_type, entity_id=entity_id)

            related_tasks = []
            ticket = base_data.get('ticket')
            client = base_data.get('client')
            if ticket:
                t_rows = conn.execute("SELECT id, title, status, priority, due_date FROM tasks WHERE ticket=? AND id!=?",
                                      (ticket, entity_id if entity_type == 'task' else 0)).fetchall()
                related_tasks = [dict(r) for r in t_rows]
            base_data['related_tasks'] = related_tasks

            # Followups
            followups = []
            if entity_type == 'case':
                f_rows = conn.execute("SELECT * FROM followups WHERE case_id=? ORDER BY id DESC", (entity_id,)).fetchall()
                followups = [dict(r) for r in f_rows]
            base_data['followups'] = followups

            return base_data

    def update_work_item(self, entity_type: str, entity_id: int, **kwargs) -> bool:
        """Updates work item fields on base table and meta table."""
        now = now_iso()
        with self.connect() as conn:
            if entity_type == 'requirement':
                req_fields = ['title', 'description', 'requirement_text', 'user_story',
                              'acceptance_criteria', 'client', 'product', 'ticket', 'priority',
                              'due_date', 'status', 'owner_member_id']
                updates = []
                params = []
                for k, v in kwargs.items():
                    if k in req_fields:
                        updates.append(f"{k}=?")
                        params.append(v)
                if updates:
                    updates.append("updated_at=?")
                    params.append(now)
                    params.append(entity_id)
                    conn.execute(f"UPDATE requirements SET {', '.join(updates)} WHERE id=?", params)

            elif entity_type == 'case':
                case_fields = ['title', 'product', 'platform', 'ticket', 'priority', 'status',
                               'participation', 'next_action', 'waiting_on']
                updates = []
                params = []
                for k, v in kwargs.items():
                    if k in case_fields:
                        updates.append(f"{k}=?")
                        params.append(v)
                if updates:
                    updates.append("updated_at=?")
                    params.append(now)
                    params.append(entity_id)
                    conn.execute(f"UPDATE work_cases SET {', '.join(updates)} WHERE id=?", params)

            elif entity_type == 'task':
                task_fields = ['title', 'status', 'priority', 'due_date', 'project', 'client',
                               'ticket', 'next_action', 'blocked_reason']
                updates = []
                params = []
                for k, v in kwargs.items():
                    if k in task_fields:
                        updates.append(f"{k}=?")
                        params.append(v)
                if updates:
                    params.append(entity_id)
                    conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id=?", params)

            # Also update metadata
            meta_fields = {}
            for k in ('workflow_template_id', 'current_stage_id', 'operational_status',
                      'owner_member_id', 'target_role', 'estimated_hours', 'actual_hours',
                      'due_date', 'next_action', 'completion_note'):
                if k in kwargs:
                    meta_fields[k] = kwargs[k]
            if meta_fields:
                self.upsert_work_item_meta(entity_type, entity_id, **meta_fields)

        return True

    def delete_work_item(self, entity_type: str, entity_id: int) -> bool:
        """Safely removes a work item and its associated metadata in a single transaction."""
        with self.connect() as conn:
            if entity_type == 'requirement':
                conn.execute("DELETE FROM requirements WHERE id=?", (entity_id,))
                conn.execute("DELETE FROM test_conditions WHERE requirement_id=?", (entity_id,))
                conn.execute("UPDATE test_cases SET requirement_id=NULL WHERE requirement_id=?", (entity_id,))
            elif entity_type == 'case':
                conn.execute("DELETE FROM work_cases WHERE id=?", (entity_id,))
                conn.execute("DELETE FROM followups WHERE case_id=?", (entity_id,))
            elif entity_type == 'task':
                conn.execute("DELETE FROM tasks WHERE id=?", (entity_id,))

            conn.execute("DELETE FROM work_item_meta WHERE entity_type=? AND entity_id=?", (entity_type, entity_id))
            conn.execute("DELETE FROM blockers_dependencies WHERE entity_type=? AND entity_id=?", (entity_type, entity_id))
            conn.execute("DELETE FROM stage_history WHERE entity_type=? AND entity_id=?", (entity_type, entity_id))
        return True

    def add_workflow_stage(self, template_id: int, name: str, stage_order: int,
                           description: str = '', expected_role: str = '',
                           expected_duration_hours: float = 0.0, is_waiting: int = 0) -> int:
        """Adds a new stage to an existing workflow template."""
        with self.connect() as conn:
            cur = conn.execute('''INSERT INTO workflow_stages
                (template_id, name, stage_order, description, expected_role, expected_duration_hours, is_waiting, is_active, required_artifacts_json, checklist_items_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, '[]', '[]')''',
                (template_id, name, stage_order, description, expected_role, expected_duration_hours, 1 if is_waiting else 0))
            return cur.lastrowid



