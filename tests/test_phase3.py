import csv
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from connectors import CSVConnector, sync_connector
from database import Database
from dashboard import DashboardService
from reports import generate_report
from telegram_import import extract_metadata, import_export, parse_file, redact


HTML = '''<!doctype html><html><body>
<div class="page_header"><div class="text bold">Test logs</div></div>
<div class="message default clearfix" id="message100"><div class="body">
<div class="date details" title="08.09.2026 12:00:00 UTC+05:30">12:00</div>
<div class="from_name">Owner Alias</div><div class="text">Testing login failed for client user@example.com</div>
</div></div>
<div class="message default joined" id="message101"><div class="body">
<div class="date details" title="08.09.2026 12:05:00 UTC+05:30">12:05</div>
<div class="reply_to details">In reply to <a href="#go_to_message100">this</a></div>
<div class="text">Retest passed</div></div></div>
</body></html>'''


class PhaseThreeDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / 'work.sqlite3')
        self.sid = self.db.start_shift('2026-09-09T12:00:00+05:30',
                                       '2026-09-09T21:00:00+05:30')

    def test_case_lifecycle_metrics_and_report(self):
        case_id = self.db.create_case('Login failure', client='Acme', channel='Teams',
            status='investigating', participation='handled', shift_id=self.sid, detail='Investigated login')
        self.db.update_case(case_id, 'status', 'resolved', self.sid, 'Fix verified')
        self.db.update_case(case_id, 'client_updated', True, self.sid, 'Client informed')
        session = self.db.add_test_session('Login retest', self.sid, case_id, 'Windows 11',
                                           '4.3', expected='Login succeeds', actual='Login succeeds',
                                           result='passed')
        metrics = self.db.case_metrics(self.sid)
        self.assertEqual(metrics['clients_handled'], 1)
        self.assertEqual(metrics['queries_worked'], 1)
        self.assertEqual(metrics['queries_resolved'], 1)
        report = generate_report('eod', self.db.active_shift(), self.db.activities(self.sid),
            self.db.list_tasks(), cases=self.db.cases_for_shift(self.sid),
            test_sessions=self.db.test_sessions(self.sid))
        self.assertIn('Clients handled: 1', report)
        self.assertIn(f'TEST-{session}', report)

    def test_inbox_accept_merge_followup_and_duplicate_protection(self):
        values = dict(source_type='telegram_forward', source_key='stable-key', text='Client login issue',
                      redacted_text='Client login issue', author_is_owner=True,
                      classification='client_query', confidence=.8, shift_id=self.sid)
        message, inserted = self.db.add_source_message(**values)
        _, duplicate = self.db.add_source_message(**values)
        self.assertTrue(inserted); self.assertFalse(duplicate)
        case_id = self.db.accept_source_message(message['id'], self.sid)
        second = self.db.create_case('Second', shift_id=self.sid)
        self.db.merge_cases(second, case_id)
        due = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        followup = self.db.add_followup(case_id, due, 'Reply', 'client', self.sid)
        self.assertEqual(self.db.due_followups(datetime.now(timezone.utc).isoformat())[0]['id'], followup)
        self.db.complete_followup(followup)
        self.assertEqual(self.db.list_followups(), [])

    def test_test_session_and_evidence_deduplicate(self):
        case_id = self.db.create_case('Deployment', shift_id=self.sid)
        test_id = self.db.add_test_session('Install', self.sid, case_id, result='failed',
                                           retest_required=True)
        first = self.db.add_evidence('photo', self.sid, case_id, test_id,
                                     str(self.root/'x.jpg'), sha256='abc')
        second = self.db.add_evidence('photo', self.sid, case_id, test_id,
                                      str(self.root/'x.jpg'), sha256='abc')
        self.assertEqual(first, second)
        self.assertEqual(len(self.db.test_sessions(case_id=case_id)), 1)


class TelegramImportTests(unittest.TestCase):
    def test_parser_redaction_import_and_reimport(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            export = root / 'export'; export.mkdir()
            (export / 'messages.html').write_text(HTML, encoding='utf-8')
            title, messages, sender = parse_file(export / 'messages.html')
            self.assertEqual(title, 'Test logs')
            self.assertEqual(sender, 'Owner Alias')
            self.assertEqual(messages[1].reply_to_id, '100')
            self.assertNotIn('user@example.com', redact(messages[0].text))
            metadata = extract_metadata('Client name: Acme | Teams | Windows | ticket: SUP-42')
            self.assertEqual(metadata['client'], 'Acme')
            self.assertEqual(metadata['channel'], 'Teams')
            self.assertEqual(metadata['platform'], 'Windows')
            db = Database(root / 'work.sqlite3')
            result = import_export(db, export, ['Owner Alias'], apply=True)
            self.assertEqual(result['inserted'], 2)
            self.assertEqual(len(db.inbox()), 2)
            again = import_export(db, export, ['Owner Alias'], apply=True)
            self.assertEqual(again['status'], 'already_imported')
            removed = db.rollback_import(result['import_id'])
            self.assertEqual(removed, 2)
            reapplied = import_export(db, export, ['Owner Alias'], apply=True)
            self.assertEqual(reapplied['inserted'], 2)

    def test_csv_connector_enters_review_inbox(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); path = root / 'items.csv'
            with path.open('w', newline='', encoding='utf-8') as handle:
                writer = csv.DictWriter(handle, fieldnames=('id','text','updated_at'))
                writer.writeheader(); writer.writerow({'id':'T-1','text':'Client query needs testing',
                    'updated_at':'2026-09-09T12:00:00+05:30'})
            db = Database(root / 'work.sqlite3')
            result = sync_connector(db, CSVConnector(path))
            self.assertEqual(result['inserted'], 1)
            self.assertEqual(len(db.inbox()), 1)


class MigrationTests(unittest.TestCase):
    def test_v2_migration_is_backup_first(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'work.sqlite3'
            connection = sqlite3.connect(path)
            try:
                connection.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
                connection.execute('PRAGMA user_version=2')
                connection.commit()
            finally:
                connection.close()
            db = Database(path)
            with db.connect() as connection:
                from database import SCHEMA_VERSION
                self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
                self.assertIsNotNone(connection.execute(
                    "SELECT name FROM sqlite_master WHERE name='work_cases'").fetchone())
            self.assertEqual(len(list((path.parent/'backups').glob('pre-migration-v2-*.sqlite3'))), 1)


class DashboardTests(unittest.TestCase):
    def test_dashboard_is_loopback_and_token_protected(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / 'work.sqlite3')
            service = DashboardService(db, '127.0.0.1', 0).start()
            self.addCleanup(service.stop)
            port = service.server.server_address[1]
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=3)
            self.assertEqual(denied.exception.code, 403)
            response = urllib.request.urlopen(
                f'http://127.0.0.1:{port}/?token={service.token}', timeout=3)
            self.assertIn('Telegram Work Assistant', response.read().decode())


if __name__ == '__main__':
    unittest.main()
