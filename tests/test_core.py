import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from database import Database
from models import TaskStatus
from reports import generate_report
from shifts import new_shift, clock_on_shift, validate_schedule
from datetime import datetime
from zoneinfo import ZoneInfo

class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.db=Database(self.root/'work.sqlite3')
        self.sid=self.db.start_shift('2026-09-08T12:00:00+05:30','2026-09-08T21:00:00+05:30')

    def test_concurrent_writes_and_reopen(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            tasks=list(pool.map(self.db.add_task,[f'Task {i}' for i in range(40)]))
        self.assertEqual(len(self.db.list_tasks()),40)
        self.assertEqual(len({t.id for t in tasks}),40)
        self.db.mark_status(tasks[0].id,TaskStatus.COMPLETED,shift_id=self.sid)
        self.db.mark_status(tasks[0].id,TaskStatus.PENDING,shift_id=self.sid)
        self.assertIsNone(self.db.get_task(tasks[0].id).completed_at)

    def test_migration_backup_idempotence(self):
        source=self.root/'tasks.json'
        data={'tasks':[{'id':8,'title':'Legacy','status':'pending','created_at':'2025-01-01'}]}
        source.write_text(json.dumps(data))
        self.db.migrate_json(source); self.db.migrate_json(source)
        self.assertEqual(len(self.db.list_tasks()),1)
        self.assertEqual(json.loads(source.read_text()),data)
        self.assertEqual(Path(self.db.get_setting('json_migrated')).read_text(),source.read_text())
        self.assertGreater(self.db.add_task('New').id,8)

    def test_bad_migration_preserves_source(self):
        source=self.root/'bad.json'; source.write_text('{broken')
        with self.assertRaises(json.JSONDecodeError): self.db.migrate_json(source)
        self.assertEqual(source.read_text(),'{broken')
        self.assertEqual(self.db.list_tasks(),[])

    def test_backup_restore(self):
        self.db.add_task('Persist me',shift_id=self.sid)
        path=self.db.backup(self.root/'backup.sqlite3')
        restored=Database(path)
        self.assertEqual(restored.list_tasks()[0].title,'Persist me')
        self.assertEqual(restored.active_shift()['id'],self.sid)
        self.assertEqual(len(restored.activities(self.sid)),1)

    def test_only_one_shift_and_persistent_dedup(self):
        with self.assertRaises(ValueError):
            self.db.start_shift('2026-09-09T12:00:00+05:30','2026-09-09T21:00:00+05:30')
        self.assertTrue(self.db.claim_update(42))
        self.assertFalse(Database(self.db.path).claim_update(42))
        self.db.close_shift(self.sid)
        self.assertIsNone(self.db.active_shift())

    def test_report_scope_counts_and_versions(self):
        old=self.db.add_task('Old completion')
        self.db.mark_status(old.id,TaskStatus.COMPLETED)
        task=self.db.add_task('Current testing',shift_id=self.sid)
        self.db.mark_status(task.id,TaskStatus.IN_PROGRESS,shift_id=self.sid)
        self.db.add_activity(self.sid,'support','Investigated installation','Pace','Teams','investigated')
        self.db.add_activity(self.sid,'support','Resolved query','pace','WhatsApp','resolved')
        self.db.add_activity(self.sid,'support','Assisted with settings',None,'Teams','assisted')
        result=generate_report('eod',self.db.active_shift(),self.db.activities(self.sid),self.db.list_tasks())
        self.assertNotIn('Old completion',result)
        self.assertIn('Current testing',result)
        self.assertIn('Named clients: 1; logged interactions: 3; resolved interactions: 1.',result)
        self.assertIn('Client total incomplete',result)
        rid=self.db.save_report(self.sid,'eod',result); self.db.finalize(rid)
        self.db.mark_status(task.id,TaskStatus.COMPLETED,shift_id=self.sid)
        self.assertEqual(self.db.report(rid)['text'],result)
        self.assertEqual(self.db.report(rid)['finalized'],1)

    def test_correction_audit_and_quota(self):
        aid=self.db.add_activity(self.sid,'note','Original')
        self.db.correct_activity(self.sid,aid,'Corrected')
        self.assertEqual(self.db.activities(self.sid)[0]['detail'],'Corrected')
        self.db.correct_activity(self.sid,aid,None)
        self.assertEqual(self.db.activities(self.sid),[])
        with self.db.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM activity_audit').fetchone()[0],2)
        self.assertTrue(self.db.reserve_ai('2026-09-08',1))
        self.assertFalse(self.db.reserve_ai('2026-09-08',1))

    def test_carry_forward_does_not_duplicate_tasks(self):
        self.db.add_task('Tomorrow',shift_id=self.sid)
        self.db.close_shift(self.sid)
        self.db.start_shift('2026-09-09T12:00:00+05:30','2026-09-09T21:00:00+05:30')
        self.assertEqual(len(self.db.list_pending()),1)
        self.assertEqual(self.db.activities(self.db.active_shift()['id']),[])

class ShiftTests(unittest.TestCase):
    def test_overnight_and_lunch(self):
        start,end=new_shift('Asia/Kolkata','2026-09-08T23:00','08:00')
        self.assertEqual(end.day,9)
        lunch=clock_on_shift('03:00',start)
        validate_schedule(start,end,lunch)
        self.assertEqual(lunch.day,9)
        with self.assertRaises(ValueError): validate_schedule(start,end,clock_on_shift('10:00',start))

    def test_start_now_duration(self):
        start,end=new_shift('Asia/Kolkata')
        self.assertEqual((end-start).total_seconds(),9*3600)
        self.assertIsNotNone(start.tzinfo)

class RuntimeTests(unittest.TestCase):
    def test_v1_migration_is_backup_first_and_preserves_records(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'work.sqlite3'
            connection=sqlite3.connect(path)
            try:
                connection.executescript('''
                    CREATE TABLE tasks (id INTEGER PRIMARY KEY,title TEXT,status TEXT,
                        blocked_reason TEXT,created_at TEXT,completed_at TEXT,priority INTEGER);
                    CREATE TABLE settings (key TEXT PRIMARY KEY,value TEXT);
                    CREATE TABLE shifts (id INTEGER PRIMARY KEY,start TEXT,end TEXT,lunch TEXT,closed_at TEXT);
                    CREATE TABLE activities (id INTEGER PRIMARY KEY,shift_id INTEGER,category TEXT,
                        detail TEXT,client TEXT,channel TEXT,outcome TEXT,task_id INTEGER,created_at TEXT);
                    CREATE TABLE reports (id INTEGER PRIMARY KEY,shift_id INTEGER,kind TEXT,text TEXT,
                        created_at TEXT,finalized INTEGER DEFAULT 0);
                    CREATE TABLE deliveries (shift_id INTEGER,kind TEXT,PRIMARY KEY(shift_id,kind));
                    CREATE TABLE updates (id INTEGER PRIMARY KEY);
                    CREATE TABLE ai_usage (day TEXT PRIMARY KEY,requests INTEGER);
                    CREATE TABLE activity_audit (id INTEGER PRIMARY KEY,activity_id INTEGER,previous TEXT,changed_at TEXT);
                    CREATE TABLE proposals (id INTEGER PRIMARY KEY,activity_id INTEGER,original TEXT,payload TEXT,applied INTEGER DEFAULT 0);
                    INSERT INTO tasks VALUES (1,'Legacy task','pending',NULL,'2026-01-01',NULL,0);
                    PRAGMA user_version=1;
                ''')
                connection.commit()
            finally:
                connection.close()
            database=Database(path)
            self.assertEqual(database.get_task(1).title,'Legacy task')
            with database.connect() as connection:
                self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0],3)
                columns={row['name'] for row in connection.execute('PRAGMA table_info(tasks)')}
                self.assertIn('due_date',columns)
            backups=list((path.parent/'backups').glob('pre-migration-v1-*.sqlite3'))
            self.assertEqual(len(backups),1)
            connection=sqlite3.connect(backups[0])
            try:
                self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0],1)
            finally:
                connection.close()

    def test_bot_token_validation_rejects_hidden_paste_character(self):
        from config import validate_bot_token
        validate_bot_token('123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi')
        with self.assertRaisesRegex(ValueError, 'hidden'):
            validate_bot_token('123456:ABC\x16DEFghijklmnopqrstuvwxyz')

    def test_instance_lock_releases(self):
        from runtime import instance_lock
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'work.sqlite3'
            with instance_lock(path):
                with self.assertRaises(RuntimeError):
                    with instance_lock(path): pass
            with instance_lock(path): pass

    def test_stale_eod_close_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            db=Database(Path(folder)/'work.sqlite3')
            sid=db.start_shift('2026-09-08T12:00:00+05:30','2026-09-08T21:00:00+05:30')
            rid=db.save_report(sid,'eod','Draft')
            db.add_activity(sid,'testing','New result')
            with self.assertRaises(ValueError): db.finalize_and_close(rid,sid)
            self.assertIsNotNone(db.active_shift())
            fresh=db.save_report(sid,'eod','Fresh')
            db.finalize_and_close(fresh,sid)
            self.assertIsNone(db.active_shift())
            self.assertEqual(db.report(fresh)['finalized'],1)

class PhaseTwoTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.db=Database(self.root/'work.sqlite3')
        self.sid=self.db.start_shift('2026-09-09T12:00:00+05:30','2026-09-09T21:00:00+05:30')

    def test_rich_records_reporting_search_and_metadata(self):
        task=self.db.add_task('Test login',priority=3,shift_id=self.sid,due_date='2026-09-09',
            project='EmpMonitor',client='Pace',ticket='REQ-7',next_action='Retest',tags='linux,login')
        self.db.mark_status(task.id,TaskStatus.COMPLETED,shift_id=self.sid,completion_note='Passed on Rocky Linux')
        self.db.add_support(self.sid,'Resolved install query','Pace','Teams','resolved','EmpMonitor',
            'installation','Send guide','SUP-1',2,'install-pace')
        self.db.add_support(self.sid,'Investigated absence','pace','Freshchat','investigated','EmpMonitor',
            'attendance','Await logs','SUP-2',1,'absence-pace')
        self.db.add_testing(self.sid,'Linux web blocking','failed','EmpMonitor','Rocky Linux','4.2','Two defects','BUG-4','Required')
        self.db.add_learning(self.sid,'Agent deployment','KT','EmpMonitor','Use silent flags','Practice tomorrow')
        activities=self.db.activities(self.sid)
        report=generate_report('eod',self.db.active_shift(),activities,self.db.list_tasks(),style='detailed')
        self.assertIn('Explicit resolved queries: 2.',report)
        self.assertIn('Named clients: 1; logged interactions: 2; resolved interactions: 1.',report)
        self.assertIn('Rocky Linux',report)
        self.assertIn('Agent deployment',report)
        self.assertIn('Passed on Rocky Linux',report)
        self.assertTrue(self.db.search('BUG-4',category='testing'))
        stored=self.db.get_task(task.id)
        self.assertEqual(stored.priority,3)
        self.assertEqual(stored.ticket,'REQ-7')

    def test_client_mask_and_unplanned_completion(self):
        task=self.db.add_task('Carry forward')
        self.db.mark_status(task.id,TaskStatus.COMPLETED,shift_id=self.sid)
        self.db.add_support(self.sid,'Resolved query','Secret Client','Teams','resolved')
        report=generate_report('eod',self.db.active_shift(),self.db.activities(self.sid),
            self.db.list_tasks(),mask_clients=True)
        self.assertIn('Additional unplanned work',report)
        self.assertIn('Carry forward',report)
        self.assertIn('Client 1',report)
        self.assertNotIn('Secret Client',report)

    def test_proposal_edit_drop_and_dismiss(self):
        aid=self.db.add_activity(self.sid,'note','mixed note')
        payload=[{'category':'plan','detail':'Task A'},{'category':'note','detail':'Note B'}]
        pid=self.db.propose(aid,'mixed note',payload,'test-model','v2')
        self.db.update_proposal_entry(pid,1,'Edited task')
        self.db.update_proposal_entry(pid,2,remove=True)
        self.assertEqual(self.db.proposal(pid)['entries'][0]['detail'],'Edited task')
        self.db.apply_proposal(pid,self.sid)
        self.assertEqual(self.db.list_tasks()[0].title,'Edited task')
        aid=self.db.add_activity(self.sid,'note','keep me')
        pid=self.db.propose(aid,'keep me',[{'category':'note','detail':'Changed'}])
        self.db.dismiss_proposal(pid)
        with self.assertRaises(ValueError): self.db.apply_proposal(pid,self.sid)
        self.assertIn('keep me',[item['detail'] for item in self.db.activities(self.sid)])

    def test_export_formula_safety_and_backup_retention(self):
        from exporter import create_export
        from maintenance import create_rotating_backup
        self.db.add_task('=DANGEROUS',shift_id=self.sid)
        csv_path=create_export(self.db,'csv',self.root/'exports')
        self.assertIn("'=DANGEROUS",csv_path.read_text(encoding='utf-8-sig'))
        for number in range(5):
            create_rotating_backup(self.db,self.root/'backups',2,prefix=f'test{number}')
        self.assertEqual(len(list((self.root/'backups').glob('*.sqlite3'))),2)
