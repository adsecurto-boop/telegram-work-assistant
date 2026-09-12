"""
Google Workspace External Action Service.
Executes server-side Google Workspace mutations via the authorized action pipeline,
enforcing risk classification, proposal hashing, replay protection, and audit logging.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from mcp_registry import RiskLevel

logger = logging.getLogger("workspace_service")

PROPOSAL_TTL_SECONDS = 600  # 10 minutes


class WorkspaceActionError(Exception):
    pass


class WorkspaceActionService:
    def __init__(self, db, mcp_manager=None):
        self.db = db
        self.mcp_manager = mcp_manager
        self._proposals: Dict[str, Dict[str, Any]] = {}

    def _cleanup_expired_proposals(self):
        now = time.time()
        expired = [pid for pid, p in self._proposals.items() if now - p['timestamp'] > PROPOSAL_TTL_SECONDS]
        for pid in expired:
            self._proposals.pop(pid, None)

    def propose_action(self, actor: str, action: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validate, classify risk, compute argument hash, and return an actionable proposal.
        """
        self._cleanup_expired_proposals()

        allowed_actions = {
            'sheets_export',
            'tasks_create',
            'tasks_patch',
            'drive_create_folder',
            'drive_delete_file',
            'docs_create_handover',
            'keep_bridge_tasks',
            'keep_bridge_docs',
        }

        if action not in allowed_actions:
            raise WorkspaceActionError(f"Unsupported workspace action: '{action}'.")

        # Sanitize and hash exact arguments
        args_json = json.dumps(args, sort_keys=True)
        args_hash = hashlib.sha256(args_json.encode('utf-8')).hexdigest()

        # Determine risk level and required capabilities
        if action == 'drive_delete_file':
            risk = RiskLevel.DESTRUCTIVE
            req_caps = ["drive.file.delete", "external.delete"]
            title = "Delete File from Google Drive"
            description = f"Permanently delete file '{args.get('name', 'unnamed')}' (ID: {args.get('file_id')}) from Google Drive."
        elif action == 'sheets_export':
            risk = RiskLevel.EXTERNAL_WRITE
            req_caps = ["google_sheets.spreadsheet.create", "google_sheets.values.append"]
            row_count = len(args.get('rows', []))
            title = "Export to Google Sheets"
            description = f"Create new Google Spreadsheet '{args.get('title', 'Export')}' with {row_count} rows of operational data."
        elif action == 'tasks_create':
            risk = RiskLevel.EXTERNAL_WRITE
            req_caps = ["google_tasks.task.create"]
            title = "Create Google Task"
            description = f"Add new task '{args.get('title', '')}' to Google Tasks list '{args.get('list_id', '@default')}'."
        elif action == 'tasks_patch':
            risk = RiskLevel.EXTERNAL_WRITE
            req_caps = ["google_tasks.task.patch"]
            title = "Update Task Status"
            description = f"Update task status for '{args.get('title', 'Task')}' to '{args.get('status')}'."
        elif action == 'drive_create_folder':
            risk = RiskLevel.EXTERNAL_WRITE
            req_caps = ["drive.folder.create", "drive.file.write"]
            title = "Create Google Drive Folder"
            description = f"Create a new folder titled '{args.get('name', 'New Folder')}' in Google Drive root."
        elif action == 'docs_create_handover':
            risk = RiskLevel.EXTERNAL_WRITE
            req_caps = ["google_docs.document.create", "google_docs.document.write"]
            title = "Export Handover to Google Doc"
            description = f"Create new Google Document titled '{args.get('title', 'Daily Handover')}' with operational report."
        elif action == 'keep_bridge_tasks':
            risk = RiskLevel.EXTERNAL_WRITE
            req_caps = ["google_tasks.task.create"]
            title = "Convert Note to Google Task"
            description = f"Create a new task in Google Tasks from note '{args.get('title', '')}'."
        elif action == 'keep_bridge_docs':
            risk = RiskLevel.EXTERNAL_WRITE
            req_caps = ["google_docs.document.create", "google_docs.document.write"]
            title = "Export Note to Google Doc"
            description = f"Create new Google Document from note '{args.get('title', '')}'."
        else:
            risk = RiskLevel.EXTERNAL_WRITE
            req_caps = ["google_workspace.write"]
            title = action.replace('_', ' ').title()
            description = f"Execute {action} with {len(args)} parameters."

        proposal_id = str(uuid.uuid4())
        self._proposals[proposal_id] = {
            'proposal_id': proposal_id,
            'actor': actor,
            'action': action,
            'args': args,
            'args_hash': args_hash,
            'risk_level': risk,
            'required_capabilities': req_caps,
            'title': title,
            'description': description,
            'timestamp': time.time(),
            'consumed': False,
        }

        return {
            'proposal_id': proposal_id,
            'action': action,
            'risk': risk.name,
            'is_destructive': risk == RiskLevel.DESTRUCTIVE,
            'required_capabilities': req_caps,
            'title': title,
            'description': description,
            'arguments_hash': args_hash,
        }

    def execute_action(
        self,
        proposal_id: str,
        google_access_token: str,
        actor: str = 'owner',
        owner_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Execute an approved proposal via server-side Google REST APIs,
        protecting against replay and argument tampering, and logging audit without tokens.
        """
        self._cleanup_expired_proposals()

        proposal = self._proposals.get(proposal_id)
        if not proposal:
            raise WorkspaceActionError("Proposal not found or expired. Please initiate the action again.")

        if proposal['consumed']:
            raise WorkspaceActionError("Proposal has already been executed. Replay rejected.")

        if not google_access_token or not isinstance(google_access_token, str) or len(google_access_token) < 10:
            raise WorkspaceActionError("Valid Google OAuth access token is required for external execution.")

        # Mark consumed immediately to prevent replay
        proposal['consumed'] = True

        action = proposal['action']
        args = proposal['args']
        args_hash = proposal['args_hash']
        risk_level = proposal['risk_level']
        req_caps = proposal['required_capabilities']

        audit_meta = {
            'server': 'google_workspace',
            'canonical_tool_id': f'google_workspace.{action}',
            'arguments_hash': args_hash,
            'risk': risk_level.name,
            'authorization_family': 'google_workspace',
            'required_capabilities': req_caps,
            'execution_timestamp': datetime.now(timezone.utc).isoformat(),
        }

        try:
            result_data = self._dispatch_google_api(action, args, google_access_token)
            audit_meta['status'] = 'success'
            self.db.record_external_write_audit(proposal_id, owner_id or 0, audit_meta)
            return {
                'success': True,
                'action': action,
                'data': result_data,
            }
        except Exception as exc:
            logger.error("Workspace action %s failed: %s", action, exc)
            audit_meta['status'] = 'failed'
            self.db.record_external_write_audit(proposal_id, owner_id or 0, audit_meta)
            raise WorkspaceActionError(f"Google Workspace API execution failed: {exc}") from exc

    def _dispatch_google_api(self, action: str, args: Dict[str, Any], token: str) -> Dict[str, Any]:
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }

        def _http_req(url: str, method: str = 'GET', data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
            req_body = json.dumps(data).encode('utf-8') if data is not None else None
            req = urllib.request.Request(url, data=req_body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    resp_bytes = resp.read()
                    if not resp_bytes:
                        return {'status': resp.status}
                    return json.loads(resp_bytes.decode('utf-8'))
            except urllib.error.HTTPError as http_err:
                err_body = http_err.read().decode('utf-8', errors='replace')
                try:
                    err_json = json.loads(err_body)
                    msg = err_json.get('error', {}).get('message', err_body)
                except Exception:
                    msg = err_body
                raise WorkspaceActionError(f"HTTP {http_err.code}: {msg}")
            except Exception as e:
                raise WorkspaceActionError(str(e))

        if action == 'sheets_export':
            title = args.get('title') or 'Personal Work Assistant Export'
            rows = args.get('rows') or []
            # 1. Create spreadsheet
            create_res = _http_req(
                'https://sheets.googleapis.com/v4/spreadsheets',
                method='POST',
                data={'properties': {'title': title}}
            )
            sheet_id = create_res.get('spreadsheetId')
            if not sheet_id:
                raise WorkspaceActionError("Failed to create Google Spreadsheet.")

            # 2. Append values
            if rows:
                _http_req(
                    f'https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/A1:append?valueInputOption=USER_ENTERED',
                    method='POST',
                    data={'values': rows}
                )

            return {
                'spreadsheetId': sheet_id,
                'title': title,
                'editUrl': f'https://docs.google.com/spreadsheets/d/{sheet_id}/edit',
                'rowsAppended': len(rows),
            }

        elif action == 'tasks_create':
            list_id = args.get('list_id') or '@default'
            title = args.get('title') or 'Untitled Task'
            notes = args.get('notes')
            due = args.get('due')
            payload: Dict[str, Any] = {'title': title}
            if notes:
                payload['notes'] = notes
            if due:
                payload['due'] = due
            res = _http_req(
                f'https://tasks.googleapis.com/tasks/v1/lists/{list_id}/tasks',
                method='POST',
                data=payload
            )
            return {'taskId': res.get('id'), 'title': title, 'listId': list_id}

        elif action == 'tasks_patch':
            list_id = args.get('list_id') or '@default'
            task_id = args.get('task_id')
            status = args.get('status') or 'needsAction'
            if not task_id:
                raise WorkspaceActionError("Missing task_id for tasks_patch.")
            res = _http_req(
                f'https://tasks.googleapis.com/tasks/v1/lists/{list_id}/tasks/{task_id}',
                method='PATCH',
                data={'status': status}
            )
            return {'taskId': task_id, 'status': status}

        elif action == 'drive_create_folder':
            name = args.get('name') or 'New Folder'
            res = _http_req(
                'https://www.googleapis.com/drive/v3/files',
                method='POST',
                data={'name': name, 'mimeType': 'application/vnd.google-apps.folder'}
            )
            return {'folderId': res.get('id'), 'name': name}

        elif action == 'drive_delete_file':
            file_id = args.get('file_id')
            if not file_id:
                raise WorkspaceActionError("Missing file_id for drive_delete_file.")
            _http_req(
                f'https://www.googleapis.com/drive/v3/files/{file_id}',
                method='DELETE'
            )
            return {'fileId': file_id, 'deleted': True}

        elif action == 'docs_create_handover':
            title = args.get('title') or 'Daily Operations Handover'
            body_text = args.get('body') or ''
            # 1. Create document
            doc_res = _http_req(
                'https://docs.googleapis.com/v1/documents',
                method='POST',
                data={'title': title}
            )
            doc_id = doc_res.get('documentId')
            if not doc_id:
                raise WorkspaceActionError("Failed to create Google Document.")

            # 2. Insert text
            if body_text:
                _http_req(
                    f'https://docs.googleapis.com/v1/documents/{doc_id}:batchUpdate',
                    method='POST',
                    data={
                        'requests': [
                            {
                                'insertText': {
                                    'location': {'index': 1},
                                    'text': body_text,
                                }
                            }
                        ]
                    }
                )

            return {
                'documentId': doc_id,
                'title': title,
                'editUrl': f'https://docs.google.com/document/d/{doc_id}/edit',
            }

        elif action == 'keep_bridge_tasks':
            list_id = args.get('list_id') or '@default'
            title = args.get('title') or 'Note Task'
            notes = args.get('body') or ''
            res = _http_req(
                f'https://tasks.googleapis.com/tasks/v1/lists/{list_id}/tasks',
                method='POST',
                data={'title': title, 'notes': notes}
            )
            return {'taskId': res.get('id'), 'title': title}

        elif action == 'keep_bridge_docs':
            title = args.get('title') or 'Work Note'
            body_text = args.get('body') or ''
            doc_res = _http_req(
                'https://docs.googleapis.com/v1/documents',
                method='POST',
                data={'title': title}
            )
            doc_id = doc_res.get('documentId')
            if not doc_id:
                raise WorkspaceActionError("Failed to create Google Document.")

            if body_text:
                _http_req(
                    f'https://docs.googleapis.com/v1/documents/{doc_id}:batchUpdate',
                    method='POST',
                    data={
                        'requests': [
                            {
                                'insertText': {
                                    'location': {'index': 1},
                                    'text': body_text,
                                }
                            }
                        ]
                    }
                )

            return {
                'documentId': doc_id,
                'title': title,
                'editUrl': f'https://docs.google.com/document/d/{doc_id}/edit',
            }

        else:
            raise WorkspaceActionError(f"Action '{action}' handler not implemented.")
