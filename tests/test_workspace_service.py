"""Tests for server-side Google Workspace Actions (WorkspaceActionService).

Verifies the proposal-execution pattern, argument validation, cryptographic hashing,
destructive operation confirmation enforcement, and audit trail generation.
"""

import unittest
import tempfile
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

from database import Database
from application.workspace_service import WorkspaceActionService, WorkspaceActionError


class WorkspaceActionServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / 'work.sqlite3'
        self.db = Database(self.db_path)
        self.service = WorkspaceActionService(self.db)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_propose_sheets_export(self):
        proposal = self.service.propose_action('owner', 'sheets_export', {
            'title': 'Sprint 42 Operations Export',
            'rows': [['ID', 'Title'], ['1', 'Task A']]
        })
        self.assertIsNotNone(proposal)
        self.assertTrue(len(proposal['proposal_id']) > 10)
        self.assertEqual(proposal['action'], 'sheets_export')
        self.assertFalse(proposal['is_destructive'])
        self.assertIn('Sprint 42 Operations Export', proposal['description'])
        self.assertIn('2 rows', proposal['description'])

    def test_propose_drive_delete_file_is_destructive(self):
        proposal = self.service.propose_action('owner', 'drive_delete_file', {
            'file_id': 'file_xyz_123',
            'name': 'old_report.pdf'
        })
        self.assertTrue(proposal['is_destructive'])
        self.assertIn('Delete File from Google Drive', proposal['title'])
        self.assertIn('old_report.pdf', proposal['description'])

    def test_propose_unknown_action_raises_workspace_error(self):
        with self.assertRaises(WorkspaceActionError):
            self.service.propose_action('owner', 'invalid_action', {})

    def test_execution_requires_connector_managed_credential(self):
        proposal = self.service.propose_action('owner', 'tasks_create', {'title': 'No token'})
        with self.assertRaises(WorkspaceActionError):
            self.service.execute_action(proposal['proposal_id'])

    def test_propose_tasks_create(self):
        proposal = self.service.propose_action('owner', 'tasks_create', {
            'list_id': '@default',
            'title': 'Review test results'
        })
        self.assertEqual(proposal['action'], 'tasks_create')
        self.assertFalse(proposal['is_destructive'])

    @patch('urllib.request.urlopen')
    def test_execute_sheets_export_audited(self, mock_urlopen):
        # Mock Google Sheets API creation and append responses
        mock_create_resp = MagicMock()
        mock_create_resp.read.return_value = json.dumps({
            'spreadsheetId': 'sheet_12345',
            'spreadsheetUrl': 'https://docs.google.com/spreadsheets/d/sheet_12345/edit'
        }).encode('utf-8')
        mock_create_resp.status = 200

        mock_append_resp = MagicMock()
        mock_append_resp.read.return_value = json.dumps({'updatedRows': 2}).encode('utf-8')
        mock_append_resp.status = 200

        # Context manager __enter__ mocking
        cm1 = MagicMock()
        cm1.__enter__.return_value = mock_create_resp
        cm2 = MagicMock()
        cm2.__enter__.return_value = mock_append_resp

        mock_urlopen.side_effect = [cm1, cm2]

        proposal = self.service.propose_action('owner', 'sheets_export', {
            'title': 'Test Export',
            'rows': [['Col1', 'Col2'], ['Val1', 'Val2']]
        })

        result = self.service.execute_action(proposal['proposal_id'], 'mock_access_token_abc')
        self.assertTrue(result['success'])
        self.assertEqual(result['data']['spreadsheetId'], 'sheet_12345')
        self.assertIn('https://docs.google.com/spreadsheets/d/sheet_12345/edit', result['data']['editUrl'])

        # Proposal cannot be re-executed (one-time use / replay protection)
        with self.assertRaises(WorkspaceActionError):
            self.service.execute_action(proposal['proposal_id'], 'mock_access_token_abc')

        # Verify audit log was recorded in SQLite
        with self.db.connect() as conn:
            recent_audit = [dict(r) for r in conn.execute('SELECT * FROM audit_log ORDER BY id DESC LIMIT 10').fetchall()]
        self.assertTrue(any(e['operation_type'] == 'confirmed_external_write' and 'sheets_export' in (e['after_state_json'] or '') for e in recent_audit))

    @patch('urllib.request.urlopen')
    def test_execute_drive_delete_file_audited(self, mock_urlopen):
        mock_del_resp = MagicMock()
        mock_del_resp.read.return_value = b''
        mock_del_resp.status = 204
        
        cm = MagicMock()
        cm.__enter__.return_value = mock_del_resp
        mock_urlopen.return_value = cm

        proposal = self.service.propose_action('owner', 'drive_delete_file', {
            'file_id': 'file_delete_999',
            'name': 'obsolete.docx'
        })

        result = self.service.execute_action(proposal['proposal_id'], 'mock_access_token_abc')
        self.assertTrue(result['success'])
        self.assertEqual(result['data']['fileId'], 'file_delete_999')

        # Verify audit log
        with self.db.connect() as conn:
            recent_audit = [dict(r) for r in conn.execute('SELECT * FROM audit_log ORDER BY id DESC LIMIT 10').fetchall()]
        self.assertTrue(any(e['operation_type'] == 'confirmed_external_write' and 'drive_delete_file' in (e['after_state_json'] or '') for e in recent_audit))

    @patch('urllib.request.urlopen')
    def test_proposal_survives_service_restart(self, mock_urlopen):
        response = MagicMock()
        response.read.return_value = b''
        response.status = 204
        cm = MagicMock(); cm.__enter__.return_value = response
        mock_urlopen.return_value = cm
        proposal = self.service.propose_action('owner', 'drive_delete_file', {'file_id': 'restart-file'})
        restarted = WorkspaceActionService(self.db)
        self.assertTrue(restarted.execute_action(proposal['proposal_id'], 'mock_access_token_abc')['success'])

    def test_execution_rejects_tampered_arguments_hash(self):
        proposal = self.service.propose_action('owner', 'drive_delete_file', {'file_id': 'file_123', 'name': 'target.txt'})
        # Tamper with the persisted arguments directly in SQLite
        with self.db.connect() as conn:
            import json
            row = conn.execute('SELECT proposal_json FROM nl_proposals WHERE id=?', (proposal['proposal_id'],)).fetchone()
            payload = json.loads(row['proposal_json'])
            payload['args']['file_id'] = 'file_tampered_999'  # arguments changed without updating args_hash
            conn.execute('UPDATE nl_proposals SET proposal_json=? WHERE id=?', (json.dumps(payload), proposal['proposal_id']))

        with self.assertRaises(WorkspaceActionError) as ctx:
            self.service.execute_action(proposal['proposal_id'], 'mock_access_token_abc')
        self.assertIn('Proposal arguments changed or hash mismatch', str(ctx.exception))

    def test_execution_rejects_tampered_capabilities(self):
        proposal = self.service.propose_action('owner', 'drive_delete_file', {'file_id': 'file_456', 'name': 'target.txt'})
        # Tamper with the required capabilities
        with self.db.connect() as conn:
            import json
            row = conn.execute('SELECT proposal_json FROM nl_proposals WHERE id=?', (proposal['proposal_id'],)).fetchone()
            payload = json.loads(row['proposal_json'])
            payload['required_capabilities'] = ['drive.tampered']
            conn.execute('UPDATE nl_proposals SET proposal_json=? WHERE id=?', (json.dumps(payload), proposal['proposal_id']))

        with self.assertRaises(WorkspaceActionError) as ctx:
            self.service.execute_action(proposal['proposal_id'], 'mock_access_token_abc')
        self.assertIn('Proposal capability semantics changed', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
